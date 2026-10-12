"""GPT-4 Data Curator — quality scoring for scraped/training records.

Implements the "LLM-as-judge" curation step of the training-data pipeline:
every record receives multi-dimensional scores (relevance, coherence,
information density, safety) plus an aggregate 0..1 quality score and a
keep/drop/review decision.

Design
------
* ``score_fn`` injection point: pass any ``callable(prompt:str)->str``.
  Production uses OpenAI GPT-4 with a strict JSON rubric; tests use canned
  responses; the offline default is a deterministic **heuristic scorer** so
  the module never requires network access.
* Batch API-friendly: :meth:`DataCurator.curate` processes iterables and
  returns per-record verdicts + aggregate stats usable by downstream filters
  (see :mod:`src.training.filters`).
"""

from __future__ import annotations

import json
import os
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

CURATOR_PROMPT = """You are a rigorous data curator for LLM training corpora.
Rate the following record on four dimensions, each integer 1-5:
- relevance: on-topic value for general web knowledge
- coherence: grammatical, self-contained, not truncated/garbled
- density: informative tokens vs boilerplate/marketing/noise
- safety: free of PII, hate, sexual content, dangerous instructions (5 = clean)

Return ONLY this JSON object:
{{"relevance": int, "coherence": int, "density": int, "safety": int,
 "reason": "<one short sentence>"}}

RECORD:
{record}
"""

WEIGHTS = {"relevance": 0.35, "coherence": 0.25, "density": 0.25,
           "safety": 0.15}


@dataclass
class QualityScore:
    relevance: int
    coherence: int
    density: int
    safety: int
    reason: str = ""
    overall: float = 0.0
    decision: str = "review"   # keep | drop | review

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CurationReport:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    reviewed: int = 0
    mean_quality: float = 0.0
    results: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
def heuristic_score(record_text: str) -> dict:
    """Deterministic offline stand-in for the GPT-4 judge."""
    text = record_text.strip()
    n = len(text)
    words = re.findall(r"[A-Za-z']+", text.lower())
    uniq_ratio = len(set(words)) / max(1, len(words))

    # length sweet spot ~200-4000 chars
    if n < 60:
        coherence = 2
    elif n <= 4000:
        coherence = 5
    else:
        coherence = 4

    density = 5 if uniq_ratio > 0.55 else 4 if uniq_ratio > 0.4 else 2
    # boilerplate markers lower relevance/density
    boiler = len(re.findall(
        r"\b(cookie|subscribe now|advertis|sign up free|copyright)\b",
        text, re.I))
    density = max(1, density - boiler)
    relevance = 3 + min(2, len(words) // 40)

    pii = bool(re.search(
        r"[\w.+-]+@[\w-]+\.[\w.-]+|\b\d{3}-\d{2}-\d{4}\b|\b\d{16}\b", text))
    toxic = bool(re.search(r"\b(hate speech|kill yourself|porn)\b",
                           text, re.I))
    safety = 1 if (pii or toxic) else 5

    return {"relevance": relevance, "coherence": coherence,
            "density": density, "safety": safety,
            "reason": "heuristic offline score"}


def openai_score_fn(model: str | None = None) -> Callable[[str], str]:
    """Build a real GPT-4 judge callable (requires OPENAI_API_KEY)."""
    from openai import OpenAI

    client = OpenAI()
    model = model or os.getenv("CURATOR_MODEL", "gpt-4o")

    def _call(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=model, temperature=0.0, max_tokens=200,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}])
        return resp.choices[0].message.content

    return _call


_first_json = None  # populated below


def _extract_json(text: str) -> dict:
    """Parse the first JSON object in `text` (tolerates markdown fences)."""
    text = re.sub(r"```(json)?", "", text)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON")


# --------------------------------------------------------------------------- #
class DataCurator:
    """Score & filter records via LLM-as-judge (or heuristic fallback)."""

    def __init__(self, score_fn: Callable[[str], str] | None = None,
                 keep_threshold: float = 0.62,
                 drop_threshold: float = 0.38,
                 batch_size: int = 8):
        if score_fn is None:
            score_fn = self._default_score_fn()
        self.score_fn = score_fn
        self.keep_threshold = keep_threshold
        self.drop_threshold = drop_threshold
        self.batch_size = batch_size

    @staticmethod
    def _default_score_fn():
        if os.getenv("OPENAI_API_KEY"):
            try:
                fn = openai_score_fn()
                logger.info("DataCurator: GPT-4 judge enabled")
                return fn
            except Exception as exc:  # pragma: no cover
                logger.warning("curator LLM init failed (%s); heuristic", exc)
        logger.info("DataCurator: heuristic offline judge")
        return None  # sentinel -> curate() uses heuristic_score directly

    # ------------------------------------------------------------------ #
    def score_record(self, record: dict | str) -> QualityScore:
        text = (json.dumps(record, ensure_ascii=False)
                if isinstance(record, dict) else str(record))
        if self.score_fn is None:
            raw = heuristic_score(text)
        else:
            prompt = CURATOR_PROMPT.format(record=text[:6000])
            try:
                raw = _extract_json(self.score_fn(prompt))
            except Exception as exc:  # noqa: BLE001
                logger.warning("judge output unparsable (%s); heuristic", exc)
                raw = heuristic_score(text)
        dims = {}
        for k in WEIGHTS:
            v = raw.get(k, 3)
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = 3
            dims[k] = max(1, min(5, v))
        overall = round(sum(dims[k] * w for k, w in WEIGHTS.items()) / 5.0, 4)
        if dims["safety"] <= 1:
            decision = "drop"          # hard safety veto
        elif overall >= self.keep_threshold:
            decision = "keep"
        elif overall < self.drop_threshold:
            decision = "drop"
        else:
            decision = "review"
        return QualityScore(**dims, reason=str(raw.get("reason", ""))[:300],
                            overall=overall, decision=decision)

    # ------------------------------------------------------------------ #
    def curate(self, records: Iterable[dict | str]) -> CurationReport:
        report = CurationReport()
        overalls: list[float] = []
        for rec in records:
            qs = self.score_record(rec)
            report.total += 1
            overalls.append(qs.overall)
            if qs.decision == "keep":
                report.kept += 1
            elif qs.decision == "drop":
                report.dropped += 1
            else:
                report.reviewed += 1
            entry = {"record": rec if isinstance(rec, str) else
                     json.dumps(rec)[:300], **qs.to_dict()}
            report.results.append(entry)
        report.mean_quality = round(statistics.fmean(overalls), 4) \
            if overalls else 0.0
        return report

    def filter_records(self, records: list[dict]) -> list[dict]:
        """Drop low-quality entries, return only 'keep' records (pipeline use)."""
        out = []
        for rec in records:
            if self.score_record(rec).decision == "keep":
                out.append(rec)
        return out


def curate_records(records: Iterable[dict | str], **kwargs) -> CurationReport:
    """Convenience wrapper used by Celery tasks / CLI."""
    return DataCurator(**kwargs).curate(records)
