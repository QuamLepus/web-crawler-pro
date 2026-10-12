"""Synthetic training-data generation pipeline.

Turns real crawled seeds into instruction-tuning / QA datasets, with or
without an LLM:

Modes
-----
* ``self_instruct``  — seed -> new instruction variants (LLM when available,
  template mutation offline).
* ``paraphrase``     — semantic-preserving rewrites (back-translation hook /
  LLM; offline: rule-based synonym + structure transforms).
* ``qa_from_text``   — extractive/abstractive QA pairs from documents
  (offline: entity/sentence templates; LLM: richer and deduplicated).
* ``augment``        — mix above with configurable ratios, emit JSONL.

Every generated example carries provenance (``source``, ``method``,
``seed_hash``) so downstream curation (:mod:`src.ai.agents.data_curator`)
can filter machine noise before it reaches a tokenizer.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

SELF_INSTRUCT_PROMPT = """Here is a task description written by a human:
{seed}

Write {n} NEW but similar tasks a different person might ask an AI assistant.
Vary topic/domain slightly but keep the same *type* of work.
Return ONLY a JSON array of strings."""

QA_PROMPT = """From the CONTEXT below produce up to {n} question-answer pairs
that are answerable strictly from the text. Return ONLY a JSON array of
objects: {{"question": "...", "answer": "..."}}

CONTEXT:
{context}"""


@dataclass
class SyntheticExample:
    instruction: str
    output: str
    source: str            # seed document / url / dataset name
    method: str            # self_instruct | paraphrase | qa | template
    meta: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps({"instruction": self.instruction,
                           "output": self.output, "source": self.source,
                           "method": self.method, "meta": self.meta,
                           "seed_hash": _h(self.instruction + self.output)},
                          ensure_ascii=False)


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Offline building blocks
# --------------------------------------------------------------------------- #
_SYNONYMS = {
    "quickly": "rapidly", "fast": "swift", "buy": "purchase",
    "help": "assist", "show": "display", "make": "create",
    "big": "large", "small": "tiny", "important": "critical",
    "about": "regarding", "start": "begin", "end": "finish",
    "use": "utilize", "need": "require", "get": "obtain",
}

_QUESTION_TEMPLATES = [
    ("What is {entity}?", "{sentence}"),
    ("Summarize the passage about {entity}.", "{summary}"),
    ("According to the text, what happened with {entity}?", "{sentence}"),
    ("List key facts about {entity}.", "{facts}"),
]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if 25 <= len(p.strip()) <= 400]


def _entities(sentence: str) -> list[str]:
    """Crude NER: capitalized runs & quoted strings."""
    ents = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", sentence)
    ents += re.findall(r"[\"“']([^\"”']{3,40})[\"”']", sentence)
    seen, out = set(), []
    for e in ents:
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def paraphrase_rule(text: str, rng: random.Random) -> str:
    """Deterministic-ish offline paraphrase: synonyms + voice tweaks."""
    out = text
    for old, new in _SYNONYMS.items():
        out = re.sub(rf"\b{old}\b", lambda m: new.capitalize()
                     if m.group(0)[0].isupper() else new,
                     out, flags=re.I)
    sentences = _sentences(out) or [out]
    rng.shuffle(sentences)
    return " ".join(sentences)


def qa_from_text_offline(doc_id: str, text: str, n: int = 3,
                         rng: random.Random | None = None) -> list[dict]:
    """Template QA pairs grounded in sentences with named entities."""
    rng = rng or random.Random(_h(text))
    pairs: list[dict] = []
    sents = _sentences(text)
    rng.shuffle(sents)
    for sent in sents:
        ents = _entities(sent)
        if not ents:
            continue
        ent = ents[0]
        qtpl, atpl = rng.choice(_QUESTION_TEMPLATES)
        summary = sent[:160]
        facts = "; ".join(s[:80] for s in sents[:2])
        pairs.append({"question": qtpl.format(entity=ent),
                      "answer": atpl.format(sentence=sent, summary=summary,
                                            facts=facts)})
        if len(pairs) >= n:
            break
    return pairs


# --------------------------------------------------------------------------- #
# The generator
# --------------------------------------------------------------------------- #
class SyntheticDataGenerator:
    """Config-driven synthetic dataset builder.

    Parameters
    ----------
    llm:
        Optional ``callable(prompt:str)->str`` (production: GPT-4o). When
        *None*, offline template methods are used throughout.
    seed:
        RNG seed for reproducible datasets.
    """

    def __init__(self, llm: Callable[[str], str] | None = None,
                 seed: int = 7, model: str | None = None):
        self.llm = llm if llm is not None else self._default_llm(model)
        self.rng = random.Random(seed)

    @staticmethod
    def _default_llm(model: str | None):
        if not os.getenv("OPENAI_API_KEY"):
            return None
        try:
            from openai import OpenAI

            client = OpenAI()
            model = model or os.getenv("SYNTH_MODEL", "gpt-4o-mini")

            def _call(prompt: str) -> str:
                r = client.chat.completions.create(
                    model=model, temperature=0.8, max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}])
                return r.choices[0].message.content

            return _call
        except Exception:  # pragma: no cover
            return None

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _json_array(raw: str) -> list:
        raw = re.sub(r"```(json)?", "", raw)
        start = min((i for i in (raw.find("["),) if i != -1), default=-1)
        if start == -1:
            raise ValueError("no JSON array")
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "[":
                depth += 1
            elif raw[i] == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(raw[start:i + 1])
        raise ValueError("unbalanced JSON array")

    # -- modes ---------------------------------------------------------------- #
    def self_instruct(self, seeds: Iterable[str], n_per_seed: int = 3,
                      ) -> Iterator[SyntheticExample]:
        for seed in seeds:
            if self.llm:
                try:
                    items = self._json_array(self.llm(
                        SELF_INSTRUCT_PROMPT.format(seed=seed, n=n_per_seed)))
                    for it in items[:n_per_seed]:
                        yield SyntheticExample(
                            instruction=str(it),
                            output="",  # outputs filled by `solve` pass
                            source=_h(seed), method="self_instruct")
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("self_instruct LLM failed (%s)", exc)
            # offline: mutate the seed instruction via templates/synonyms
            verbs = ["Explain", "Summarize", "Compare", "Draft a plan for"]
            base = seed.rstrip(".!")
            for v in verbs[:n_per_seed]:
                instr = f"{v} {base.lower()} in three bullet points."
                yield SyntheticExample(instruction=instr, output=base,
                                       source=_h(seed), method="template")

    def paraphrases(self, texts: Iterable[tuple[str, str]], n: int = 1,
                    ) -> Iterator[SyntheticExample]:
        """texts: iterable of (doc_id, text)."""
        for doc_id, text in texts:
            produced = 0
            if self.llm:
                try:
                    raw = self.llm(
                        f"Rewrite the following preserving meaning. Return "
                        f"ONLY a JSON array of {n} variants:\n\n{text[:4000]}")
                    for v in self._json_array(raw)[:n]:
                        yield SyntheticExample(instruction="Paraphrase:",
                                               output=str(v), source=doc_id,
                                               method="paraphrase")
                        produced += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("paraphrase LLM failed (%s)", exc)
            while produced < n:
                yield SyntheticExample(instruction="Paraphrase:",
                                       output=paraphrase_rule(text, self.rng),
                                       source=doc_id, method="paraphrase_rule")
                produced += 1

    def qa_pairs(self, docs: Iterable[tuple[str, str]], n: int = 3,
                 ) -> Iterator[SyntheticExample]:
        for doc_id, text in docs:
            pairs: list[dict] = []
            if self.llm:
                try:
                    pairs = self._json_array(self.llm(
                        QA_PROMPT.format(n=n, context=text[:4000])))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("qa LLM failed (%s); offline templates", exc)
            if not pairs:
                pairs = qa_from_text_offline(doc_id, text, n=n, rng=self.rng)
            for p in pairs:
                if not p.get("question") or not p.get("answer"):
                    continue
                yield SyntheticExample(instruction=str(p["question"]),
                                       output=str(p["answer"]),
                                       source=doc_id, method="qa")

    # -- orchestration ---------------------------------------------------------- #
    def generate(self, seeds: list[str], docs: list[tuple[str, str]],
                 mix: dict | None = None) -> list[SyntheticExample]:
        """Run all configured modes over the corpus.

        mix: {"self_instruct": 3, "paraphrase": 2, "qa": 4} per-seed counts.
        """
        mix = mix or {"self_instruct": 2, "paraphrase": 1, "qa": 3}
        out: list[SyntheticExample] = []
        if mix.get("self_instruct"):
            out += list(self.self_instruct(seeds,
                                           n_per_seed=mix["self_instruct"]))
        if mix.get("paraphrase"):
            out += list(self.paraphrases(docs, n=mix["paraphrase"]))
        if mix.get("qa"):
            out += list(self.qa_pairs(docs, n=mix["qa"]))
        # de-dup on content hash
        seen, uniq = set(), []
        for ex in out:
            k = _h(ex.instruction + ex.output)
            if k not in seen:
                seen.add(k)
                uniq.append(ex)
        return uniq

    def write_jsonl(self, examples: Iterable[SyntheticExample], path: str,
                    ) -> int:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ex in examples:
                fh.write(ex.to_jsonl() + "\n")
                n += 1
        logger.info("wrote %d synthetic examples -> %s", n, path)
        return n


def generate_qa_pairs(docs: list[tuple[str, str]], n: int = 3, **kw) -> list[dict]:
    g = SyntheticDataGenerator(**kw)
    return [{"question": e.instruction, "answer": e.output,
             "source": e.source} for e in g.qa_pairs(docs, n=n)]


def generate_paraphrases(texts: list[tuple[str, str]], n: int = 1,
                         **kw) -> list[str]:
    g = SyntheticDataGenerator(**kw)
    return [e.output for e in g.paraphrases(texts, n=n)]
