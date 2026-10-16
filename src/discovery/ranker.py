"""Score, de-duplicate and shortlist discovered URLs.

Ranking is deliberately *explainable*: every :class:`Candidate` carries the
list of human-readable reasons behind its score, because "the best N websites
for a topic" is a judgement call and the operator is entitled to see (and
override) the reasoning behind it.

Signals combined (weights in :data:`Ranker.WEIGHTS`)
----------------------------------------------------
``relevance``
    Cosine similarity between the topic and ``title + snippet``. Uses the
    project embedding stack (:mod:`src.ai.vector_search`); with
    ``EMBEDDING_PROVIDER=hash`` (the default) this is free and offline.
``lexical``
    Keyword overlap, reusing :class:`~src.ai.agents.agent_crawler.HeuristicPlanner`
    — the same scorer the autonomous agent already trusts.
``agreement``
    How many *independent* providers surfaced the page. Two search engines
    agreeing is a much stronger signal than one engine ranking something first.
``rank``
    Positional decay within each provider's result list.
``quality``
    Penalties from the C4 rules in :mod:`src.training.filters` (lorem ipsum,
    cookie-wall text, symbol soup).
``seed``
    A caller-supplied seed gets a boost: explicit instructions outrank guesses.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlparse

from src.ai.agents.agent_crawler import HeuristicPlanner
from src.training.filters import (
    c4_cookie_rule,
    c4_lorem_rule,
    gopher_symbol_ratio,
)

logger = logging.getLogger("wcp.discovery.ranker")

_PARKING_RE = re.compile(
    r"(parking|domain-for-sale|buy-this-domain|godaddy|sedoparking|"
    r"under-construction|coming-soon)", re.IGNORECASE)


def domain_of(url: str) -> str:
    """Lower-cased hostname without a leading ``www.``."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001 - malformed URL
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host.removeprefix("www.")


@dataclass
class Candidate:
    """One proposed website, scored and explained."""

    url: str
    domain: str = ""
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    providers: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    seeded: bool = False
    rejected: bool = False
    selected: bool = False

    def __post_init__(self) -> None:
        if not self.domain:
            self.domain = domain_of(self.url)

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "url": self.url, "domain": self.domain, "title": self.title,
            "snippet": self.snippet, "score": round(self.score, 4),
            "providers": list(self.providers), "reasons": list(self.reasons),
            "seeded": self.seeded, "rejected": self.rejected,
            "selected": self.selected,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Candidate:
        return cls(
            url=data.get("url", ""), domain=data.get("domain", ""),
            title=data.get("title", ""), snippet=data.get("snippet", ""),
            score=float(data.get("score", 0.0)),
            providers=list(data.get("providers", [])),
            reasons=list(data.get("reasons", [])),
            seeded=bool(data.get("seeded", False)),
            rejected=bool(data.get("rejected", False)),
            selected=bool(data.get("selected", False)),
        )


class Ranker:
    """Turns raw :class:`~src.discovery.providers.SearchHit` lists into a
    shortlist of distinct, ranked websites."""

    WEIGHTS: ClassVar[dict[str, float]] = {
        "relevance": 0.40, "lexical": 0.20, "agreement": 0.20,
        "rank": 0.10, "quality": 0.10}
    SEED_BOOST: ClassVar[float] = 0.15

    def __init__(self, topic: str, *, language: str = "en",
                 blocklist: Sequence[str] = (), embedder=None) -> None:
        self.topic = topic or ""
        self.language = language
        self.blocklist = {d.lower().lstrip(".") for d in blocklist if d}
        self._lexical = HeuristicPlanner(topic or "")
        self._embedder = embedder
        self._topic_vec: list[float] | None = None

    # -- embedding ---------------------------------------------------------- #
    @property
    def embedder(self):
        if self._embedder is None:
            try:
                from src.ai.vector_search import SemanticSearch

                # Only `.embedder` is touched — this never opens ChromaDB.
                self._embedder = SemanticSearch().embedder
            except Exception as exc:  # noqa: BLE001
                logger.debug("embedding backend unavailable (%s); "
                             "relevance falls back to lexical only", exc)
                self._embedder = _NoEmbedder()
        return self._embedder

    def _topic_embedding(self) -> list[float] | None:
        if self._topic_vec is None:
            try:
                self._topic_vec = self.embedder.embed([self.topic])[0]
            except Exception as exc:  # noqa: BLE001
                logger.debug("topic embedding failed (%s)", exc)
                self._topic_vec = []
        return self._topic_vec or None

    def relevance(self, text: str) -> float:
        """Cosine similarity between the topic and ``text``, in ``[0, 1]``."""
        topic_vec = self._topic_embedding()
        if topic_vec is None or not text.strip():
            return 0.0
        try:
            doc_vec = self.embedder.embed([text])[0]
        except Exception:  # noqa: BLE001
            return 0.0
        dot = sum(a * b for a, b in zip(topic_vec, doc_vec))
        na = math.sqrt(sum(a * a for a in topic_vec)) or 1.0
        nb = math.sqrt(sum(b * b for b in doc_vec)) or 1.0
        return max(0.0, min(1.0, dot / (na * nb)))

    # -- quality ------------------------------------------------------------ #
    def quality_penalty(self, url: str, text: str) -> tuple[float, list[str]]:
        """Return ``(penalty 0..1, reasons)`` from the C4 rules + URL shape."""
        penalty = 0.0
        reasons: list[str] = []
        blob = f"{url} {text}"
        if _PARKING_RE.search(blob):
            penalty += 0.6
            reasons.append("looks like a parked/placeholder domain")
        if not c4_lorem_rule(text).passed:
            penalty += 0.4
            reasons.append("lorem ipsum placeholder text")
        if not c4_cookie_rule(text).passed:
            penalty += 0.15
            reasons.append("cookie/consent wall text dominates")
        if text and not gopher_symbol_ratio(text).passed:
            penalty += 0.15
            reasons.append("mostly symbols/boilerplate")
        if len(url) > 220:
            penalty += 0.1
            reasons.append("suspiciously long URL")
        return min(1.0, penalty), reasons

    def blocked(self, domain: str) -> bool:
        if domain in self.blocklist:
            return True
        return any(domain == b or domain.endswith("." + b) for b in self.blocklist)

    # -- pipeline ----------------------------------------------------------- #
    def build(self, hits: Iterable, seed_domains: Iterable[str] = ()) -> list[Candidate]:
        """Group hits by URL, keeping every provider that found it."""
        seeds = {domain_of(s) for s in seed_domains if s}
        by_url: dict[str, Candidate] = {}
        for hit in hits:
            url = getattr(hit, "url", "")
            if not url:
                continue
            cand = by_url.get(url)
            if cand is None:
                cand = Candidate(url=url, domain=domain_of(url),
                                 title=getattr(hit, "title", "") or "",
                                 snippet=getattr(hit, "snippet", "") or "")
                by_url[url] = cand
            else:
                # keep the richest metadata we have seen
                if not cand.title:
                    cand.title = getattr(hit, "title", "") or ""
                if not cand.snippet:
                    cand.snippet = getattr(hit, "snippet", "") or ""
            prov = getattr(hit, "provider", "") or ""
            if prov and prov not in cand.providers:
                cand.providers.append(prov)
            if getattr(hit, "rank", 0):
                cand.reasons.append(  # raw signal, replaced by score reasons later
                    f"rank {hit.rank} on {prov}")
            cand.seeded = cand.seeded or cand.domain in seeds
        return list(by_url.values())

    def score(self, candidates: Sequence[Candidate]) -> list[Candidate]:
        """Score in place and return the list, best first."""
        lexical_raw = [self._lexical.score(c.url, anchor=c.title)
                       for c in candidates]
        lex_max = max(lexical_raw) if lexical_raw else 0.0
        lex_min = min(lexical_raw) if lexical_raw else 0.0
        lex_span = (lex_max - lex_min) or 1.0

        for cand, raw in zip(candidates, lexical_raw):
            text = f"{cand.title} {cand.snippet}".strip()
            relevance = self.relevance(text or cand.url)
            lexical = (raw - lex_min) / lex_span if lex_span else 0.0
            lexical = max(0.0, min(1.0, lexical))
            agreement = min(1.0, (len(cand.providers) - 1) / 2.0) if cand.providers else 0.0
            rank_signal = self._rank_signal(cand)
            quality_pen, quality_reasons = self.quality_penalty(cand.url, text)

            score = (
                self.WEIGHTS["relevance"] * relevance
                + self.WEIGHTS["lexical"] * lexical
                + self.WEIGHTS["agreement"] * agreement
                + self.WEIGHTS["rank"] * rank_signal
                + self.WEIGHTS["quality"] * (1.0 - quality_pen)
            )
            if cand.seeded:
                score += self.SEED_BOOST

            cand.score = max(0.0, min(1.0, score))
            cand.reasons = self._explain(cand, relevance, agreement,
                                         rank_signal, quality_pen,
                                         quality_reasons)
            if self.blocked(cand.domain):
                cand.rejected = True
                cand.reasons.append("domain is blocklisted")
            elif quality_pen >= 1.0:
                cand.rejected = True
                cand.reasons.append("failed quality gate")

        candidates.sort(key=lambda c: (c.rejected, -c.score, c.domain, c.url))
        return candidates

    def select(self, candidates: Sequence[Candidate], count: int,
               per_domain: int = 1) -> list[Candidate]:
        """Shortlist ``count`` websites — by default one page per domain.

        ``per_domain=1`` is what "give me the best N *websites*" means: the
        shortlist is N distinct domains, and the crawler then explores each one
        from its best entry point.
        """
        chosen: list[Candidate] = []
        per_domain_count: dict[str, int] = {}
        for cand in candidates:
            if len(chosen) >= count:
                break
            if cand.rejected:
                continue
            used = per_domain_count.get(cand.domain, 0)
            if used >= per_domain:
                continue
            per_domain_count[cand.domain] = used + 1
            chosen.append(cand)
        return chosen

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _rank_signal(cand: Candidate) -> float:
        """Average positional decay across every provider that found it."""
        ranks = [int(r.split()[1]) for r in cand.reasons
                 if r.startswith("rank ") and r.split()[1].isdigit()]
        if not ranks:
            return 0.0
        best = min(ranks)
        return 1.0 / (1.0 + math.log2(max(best, 1)))

    def _explain(self, cand: Candidate, relevance: float, agreement: float,
                 rank_signal: float, quality_pen: float,
                 quality_reasons: list[str]) -> list[str]:
        reasons: list[str] = []
        if cand.seeded:
            reasons.append("seeded by user")
        if cand.providers:
            providers = ", ".join(sorted(cand.providers))
            reasons.append(
                f"found on {len(cand.providers)} provider(s): {providers}")
        reasons.append(f"relevance {relevance:.2f}")
        if agreement:
            reasons.append(f"cross-provider agreement {agreement:.2f}")
        if rank_signal:
            reasons.append(f"position signal {rank_signal:.2f}")
        if quality_reasons:
            reasons.extend(quality_reasons)
        elif not quality_pen:
            reasons.append("passed quality rules")
        return reasons


class _NoEmbedder:
    """Neutral stand-in when no embedding backend is available."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]
