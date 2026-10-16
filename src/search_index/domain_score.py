"""Public Domain Score — one honest number per host, computable offline.

Every crawler operator ends up asking the same questions about a domain:
how well do we know it, how authoritative is it, is it fresh, what may we do
with its content? This module turns the platform's own index plus the
provenance ledger into a 0–100 score with named components, a letter grade and
the evidence behind each part.

It is deliberately *public* and linkable: ``GET /domain/{host}`` is a page
anyone can check, which is both useful and the cheapest SEO surface a data
platform has. It is also deliberately **refusable**: when there is not enough
data, the score is ``None`` with ``coverage: "insufficient"`` rather than a
confident guess.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

GRADES = ((85, "A"), (70, "B"), (55, "C"), (40, "D"), (25, "E"), (0, "F"))

WEIGHTS = {
    "authority": 0.30,
    "coverage": 0.25,
    "freshness": 0.20,
    "licence": 0.15,
    "robots": 0.10,
}


@dataclass
class DomainScore:
    host: str
    score: float | None
    grade: str
    components: dict[str, float] = field(default_factory=dict)
    coverage: str = "insufficient"     # insufficient | thin | partial | good | rich
    docs: int = 0
    sources: list[str] = field(default_factory=list)
    last_indexed_at: float | None = None
    notes: list[str] = field(default_factory=list)
    computed_at: float = field(default_factory=time.time)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["score"] = None if self.score is None else round(self.score, 2)
        d["components"] = {k: round(v, 4) for k, v in self.components.items()}
        return d


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def coverage_band(docs: int) -> str:
    if docs <= 0:
        return "insufficient"
    if docs < 3:
        return "thin"
    if docs < 20:
        return "partial"
    if docs < 200:
        return "good"
    return "rich"


def score_domain(host: str, *, docs: int = 0, authority: float | None = None,
                 last_indexed_at: float | None = None, sources: Sequence[str] = (),
                 robots_verdicts: Iterable[str] = (), licences: Iterable[str] = (),
                 now: float | None = None) -> DomainScore:
    """Compute the score from whatever signals are actually available."""
    now = now or time.time()
    host = (host or "").lower().lstrip(".")
    notes: list[str] = []
    band = coverage_band(docs)

    if docs <= 0:
        return DomainScore(
            host=host, score=None, grade="—", coverage="insufficient", docs=0,
            sources=list(sources),
            notes=["this domain is not in the index — no score is published "
                   "rather than a guessed one"],
            evidence={"authority": authority},
        )

    coverage_component = _clamp(docs / 100.0) if docs < 100 else 1.0
    authority_component = _clamp(float(authority)) if authority is not None else None
    if authority_component is None:
        # No authority signal: fall back to index depth alone and say so.
        authority_component = coverage_component * 0.7
        notes.append("no authority signal in the index — using an index-depth proxy")

    if last_indexed_at:
        age_days = max(0.0, (now - float(last_indexed_at)) / 86400.0)
        freshness_component = _clamp(1.0 - age_days / 180.0)
        if age_days > 180:
            notes.append(f"not re-indexed in {int(age_days)} days")
    else:
        freshness_component = 0.3
        notes.append("no crawl timestamp recorded for this host")

    verdicts = [v for v in robots_verdicts if v]
    robots_component = 1.0
    if verdicts:
        allowed = sum(1 for v in verdicts if v == "allowed")
        robots_component = allowed / len(verdicts)
        if robots_component < 1.0:
            notes.append(f"robots.txt disallowed {len(verdicts) - allowed} of "
                         f"{len(verdicts)} recorded fetches")

    licence_values = [l for l in licences if l]
    if licence_values:
        redistributable = sum(1 for l in licence_values
                              if l in ("cc-by", "cc-by-sa", "cc0", "public-domain",
                                       "mit", "apache-2.0"))
        licence_component = redistributable / len(licence_values)
        if licence_component < 0.5:
            notes.append("licence clarity is low for this host — check before "
                         "redistributing")
    else:
        licence_component = 0.4
        notes.append("no licence evidence recorded for this host")

    components = {
        "authority": authority_component,
        "coverage": coverage_component,
        "freshness": freshness_component,
        "licence": licence_component,
        "robots": robots_component,
    }
    score = 100.0 * sum(WEIGHTS[k] * v for k, v in components.items())
    return DomainScore(
        host=host, score=score, grade=grade_for(score), components=components,
        coverage=band, docs=docs, sources=list(sources),
        last_indexed_at=last_indexed_at,
        notes=notes or ["all components present"],
        evidence={"authority": authority, "docs": docs,
                  "robots": dict(_counts(verdicts)),
                  "licences": dict(_counts(licence_values))},
    )


def _counts(values: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def grade_for(score: float) -> str:
    for threshold, grade in GRADES:
        if score >= threshold:
            return grade
    return "F"


def score_from_index(index: Any, host: str, *, ledger: Any = None,
                     tenant: str = "public", now: float | None = None
                     ) -> DomainScore:
    """Read the own index (and ledger, when present) for one host.

    Everything is defensive: an index that is not built, a shard that fails to
    load, or a missing ledger degrades to a lower-confidence score with a
    stated reason — never to an exception on a public page.
    """
    host = (host or "").lower()
    docs = 0
    authority = None
    last_indexed_at = None
    sources: set[str] = set()

    if index is not None and getattr(index, "available", False):
        try:
            results = index.search(host, limit=50, collapse_host=False) or []
            rows = [r for r in results if getattr(r, "host", "") == host
                    or str(getattr(r, "host", "")).endswith("." + host)]
            docs = len(rows)
            if rows:
                authority = max(float(getattr(r, "authority", 0.0) or 0.0)
                                for r in rows)
                sources = {str(getattr(r, "source", "") or "") for r in rows}
            try:
                stats = index.stats()
                for shard in getattr(stats, "shards", []) or []:
                    stamp = getattr(shard, "built_at", None) or \
                        getattr(shard, "updated_at", None)
                    if stamp and (last_indexed_at is None or stamp > last_indexed_at):
                        last_indexed_at = float(stamp)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    robots: list[str] = []
    licences: list[str] = []
    if ledger is not None:
        try:
            for record in ledger.recent(tenant, limit=500):
                if host and host in (record.source_url or "").lower():
                    robots.append(record.robots_verdict)
                    licences.append(record.license)
                    docs = max(docs, 1)
        except Exception:  # noqa: BLE001
            pass

    return score_domain(host, docs=docs, authority=authority,
                        last_indexed_at=last_indexed_at, sources=sorted(sources - {""}),
                        robots_verdicts=robots, licences=licences, now=now)
