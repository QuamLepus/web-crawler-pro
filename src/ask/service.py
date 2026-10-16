"""``POST /ask`` — one question in, a cited answer with a real budget out.

    {"question": "best budget laptops 2026",
     "budget": {"max_pages": 50, "max_seconds": 60}}

    → {answer, citations[], confidence, cost_usd, steps[]}

This is the smallest useful unit of the whole platform: a question, a spend
limit, an answer that says where it came from. It is implemented as a
*planner plus an evidence pipeline*, not as a model call, because that is what
makes it honest:

* **Budget is enforced, not suggested.** ``max_pages``, ``max_seconds`` and
  ``max_usd`` all stop the run early and the response says what stopped it.
* **Two evidence paths.** ``corpus``/``index`` answers from records the
  platform already holds (works offline, costs nothing, cites stored
  provenance); ``web`` fetches fresh pages within budget; ``auto`` uses both.
* **Confidence is derived, not asserted.** A claim corroborated by two
  independent hosts outranks one that appears once, and the response lists
  single-source claims as caveats rather than hiding them.
* **No evidence produces no claim.** If nothing matches, the answer says so.
  A confident-sounding answer with empty citations is the failure mode this
  module exists to prevent.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Sequence

from src.cost.simulator import CrawlSpec, PriceBook, estimate
from src.provenance.ledger import ProvenanceLedger, record_for_page
from src.provenance.snapshots import SnapshotStore
from src.watch.diff import content_lines

logger = logging.getLogger("wcp.ask")

STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "is", "are",
    "was", "were", "be", "with", "what", "which", "who", "how", "why", "when",
    "where", "best", "top", "good", "list", "show", "me", "give", "find",
    "compare", "vs", "versus", "between", "please", "than", "that", "this",
    "from", "by", "at", "as", "it", "its", "their", "our", "your", "most",
    "more", "least", "does", "do", "can", "should", "would", "will", "about",
}

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u0980-\u09FF])|\n{2,}")
_NUM_RE = re.compile(
    r"\d[\d,.]*\s?(?:%|USD|EUR|BDT|\u09f3|\$|\u20ac|\u00a3|kg|gb|mb|tb|ms|s|x)?",
    re.IGNORECASE,
)


def terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w\u0980-\u09FF]+", (text or "").lower())
            if len(w) > 2 and w not in STOPWORDS]


# --------------------------------------------------------------------------- #
# request / response models
# --------------------------------------------------------------------------- #
@dataclass
class AskBudget:
    max_pages: int = 25
    max_seconds: float = 30.0
    max_usd: float = 0.50
    render_share: float = 0.0
    llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> AskBudget:
        base = cls()
        if not row:
            return base
        allowed = {k: v for k, v in row.items() if hasattr(base, k)}
        return cls(**allowed)  # type: ignore[arg-type]


@dataclass
class Evidence:
    url: str
    title: str = ""
    snippet: str = ""
    text: str = ""
    host: str = ""
    source: str = "corpus"          # corpus | web | index
    fetched_at: float = 0.0
    confidence: float = 0.5
    provenance_id: str = ""
    content_sha256: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("text", None)          # the full text is not part of the contract
        return d


@dataclass
class Citation:
    index: int
    url: str
    title: str = ""
    quote: str = ""
    host: str = ""
    fetched_at: float | None = None
    content_sha256: str = ""
    provenance_id: str = ""
    corroborated_by: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Claim:
    text: str
    citations: list[int] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    support: int = 0
    confidence: float = 0.0
    numeric: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": list(self.citations),
            "hosts": list(self.hosts),
            "support": self.support,
            "confidence": round(self.confidence, 3),
            "numeric": self.numeric,
        }


@dataclass
class Step:
    name: str
    ok: bool = True
    detail: str = ""
    ms: float = 0.0
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "ms": round(self.ms, 1), "count": self.count}


@dataclass
class Answer:
    question: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    confidence: float = 0.0
    caveats: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    budget_used: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    steps: list[Step] = field(default_factory=list)
    mode: str = "auto"
    evidence_count: int = 0
    used_llm: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "claims": [c.to_dict() for c in self.claims],
            "confidence": round(self.confidence, 4),
            "caveats": list(self.caveats),
            "cost_usd": round(self.cost_usd, 6),
            "cost_breakdown": {k: round(v, 6) for k, v in
                               sorted(self.cost_breakdown.items(),
                                      key=lambda kv: -kv[1])},
            "budget": dict(self.budget),
            "budget_used": dict(self.budget_used),
            "stop_reason": self.stop_reason,
            "steps": [s.to_dict() for s in self.steps],
            "mode": self.mode,
            "evidence_count": self.evidence_count,
            "used_llm": self.used_llm,
            "created_at": round(self.created_at, 3),
        }


# --------------------------------------------------------------------------- #
# claim extraction and scoring (pure functions)
# --------------------------------------------------------------------------- #
def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    return [s.strip() for s in SENTENCE_RE.split(text)
            if 40 <= len(s.strip()) <= 400]


def score_sentence(sentence: str, query_terms: Sequence[str]) -> float:
    """How much does this sentence answer the question?"""
    low = sentence.lower()
    if not query_terms:
        return 0.0
    hits = sum(1 for t in set(query_terms) if t in low)
    coverage = hits / max(1, len(set(query_terms)))
    numbers = len(_NUM_RE.findall(sentence))
    length_penalty = 1.0 if 60 <= len(sentence) <= 260 else 0.75
    return round((0.62 * coverage + 0.38 * min(1.0, numbers / 2.0)) * length_penalty, 4)


def extract_claims(evidence: Sequence[Evidence], question: str,
                   *, max_claims: int = 8) -> list[Claim]:
    """Pull the best supporting sentences, merging near-identical ones.

    Claim-level (not document-level) citation is what makes an answer
    checkable: the unit a reviewer verifies is one sentence from one URL, not
    "somewhere in these five pages".
    """
    qt = terms(question)
    candidates: list[tuple[float, str, Evidence]] = []
    for e in evidence:
        haystack = e.text or e.snippet or ""
        for sentence in split_sentences(haystack):
            s = score_sentence(sentence, qt)
            if s >= 0.35:
                candidates.append((s, sentence, e))
    candidates.sort(key=lambda c: -c[0])

    claims: list[Claim] = []
    seen_keys: list[set[str]] = []
    for score, sentence, e in candidates:
        key = set(terms(sentence))
        merged = False
        for i, prev in enumerate(seen_keys):
            if not key or not prev:
                continue
            overlap = len(key & prev) / max(1, len(key | prev))
            if overlap > 0.6:
                claim = claims[i] if i < len(claims) else None
                if claim is not None and e.host and e.host not in claim.hosts:
                    claim.hosts.append(e.host)
                    claim.support = len(claim.hosts)
                    claim.confidence = _confidence(claim.support, score)
                merged = True
                break
        if merged:
            continue
        seen_keys.append(key)
        claims.append(Claim(
            text=sentence,
            citations=[i + 1 for i, ev in enumerate(evidence) if ev is e],
            hosts=[e.host] if e.host else [],
            support=1,
            confidence=_confidence(1, score),
            numeric=bool(_NUM_RE.search(sentence)),
        ))
        if len(claims) >= max_claims:
            break
    return claims


def _confidence(support: int, sentence_score: float) -> float:
    """Independent corroboration dominates; sentence match modulates it."""
    base = {1: 0.45, 2: 0.72, 3: 0.84}.get(support, 0.9)
    return round(min(0.97, base * (0.6 + 0.4 * sentence_score)), 4)


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
Fetcher = Callable[..., tuple[int, str, dict[str, str]]]


class AskService:
    """Question → cited answer, over the corpus, the web, or both.

    Collaborators are injected so the same code runs in the API, in a Celery
    worker, in the MCP server and in tests:

    ``search``     ``(query, limit) -> list[Evidence]`` — usually the own index
    ``fetch``      ``(url) -> (status, body, headers)`` — the polite fetcher
    ``ledger``     provenance sink; every page the run touches is recorded
    ``snapshots``  Time Machine store for replay
    """

    def __init__(self, redis_client=None, *, search: Callable | None = None,
                 fetch: Fetcher | None = None,
                 ledger: ProvenanceLedger | None = None,
                 snapshots: SnapshotStore | None = None,
                 price_book: PriceBook | None = None,
                 llm: Callable | None = None,
                 now: Callable[[], float] | None = None) -> None:
        self.r = redis_client
        self.ledger = ledger or (ProvenanceLedger(redis_client) if redis_client else None)
        self.snapshots = snapshots or SnapshotStore()
        self.search = search
        self.fetch = fetch
        self.price_book = price_book or PriceBook.from_env()
        self.llm = llm
        self._now = now or time.time

    # -- planning ------------------------------------------------------------ #
    def plan_question(self, question: str) -> dict[str, Any]:
        """Turn a sentence into queries.

        Deliberately transparent: the queries are returned to the caller, so a
        user can see (and correct) what the platform understood.
        """
        q = (question or "").strip()
        t = terms(q)
        queries = [" ".join(t[:6])] if t else []
        if len(t) > 6:
            queries.append(" ".join(t[6:12]))
        low = q.lower()
        if any(w in low for w in ("price", "cost", "cheap", "budget", "$")):
            queries.append(" ".join(t[:3] + ["price"]))
        if any(w in low for w in ("compare", "versus", "vs", "better")):
            queries.append(" ".join(t[:3] + ["comparison"]))
        seen: list[str] = []
        for query in queries:
            if query and query not in seen:
                seen.append(query)
        return {"question": q, "queries": seen[:3], "terms": t}

    # -- the pipeline -------------------------------------------------------- #
    def ask(self, question: str, *, budget: AskBudget | dict | None = None,
            mode: str = "auto", tenant: str = "public",
            urls: Sequence[str] | None = None) -> Answer:
        started = self._now()
        b = budget if isinstance(budget, AskBudget) else AskBudget.from_dict(budget)
        plan = self.plan_question(question)
        answer = Answer(question=question, answer="", mode=mode, budget=b.to_dict())

        evidence: list[Evidence] = []
        pages_fetched = 0
        timed_out = False

        # 1) plan ------------------------------------------------------------ #
        answer.steps.append(Step("plan", detail=" \u00b7 ".join(plan["queries"]),
                                 count=len(plan["queries"])))

        # 2) corpus / index retrieval ---------------------------------------- #
        t0 = self._now()
        if mode in ("auto", "corpus", "index") and self.search:
            try:
                query = plan["queries"][0] if plan["queries"] else question
                hits = list(self.search(query, limit=max(5, b.max_pages)) or [])
                evidence.extend(_coerce_evidence(h, source="index") for h in hits)
                answer.steps.append(Step("retrieve", ok=True,
                                         detail=f"{len(hits)} index/corpus hits",
                                         ms=(self._now() - t0) * 1000,
                                         count=len(hits)))
            except Exception as exc:  # noqa: BLE001 - retrieval must not kill an ask
                answer.steps.append(Step("retrieve", ok=False, detail=str(exc),
                                         ms=(self._now() - t0) * 1000))
        else:
            answer.steps.append(Step("retrieve", ok=True,
                                     detail="no retriever configured",
                                     ms=(self._now() - t0) * 1000))

        # 3) fetch fresh pages (budgeted) ------------------------------------ #
        if mode in ("web", "auto") and (urls or self.fetch):
            t0 = self._now()
            for url in list(urls or [])[: b.max_pages]:
                if pages_fetched >= b.max_pages:
                    answer.stop_reason = "page budget reached"
                    break
                if (self._now() - started) > b.max_seconds:
                    timed_out = True
                    answer.stop_reason = "time budget reached"
                    break
                if self._spend(pages_fetched, evidence) >= b.max_usd:
                    answer.stop_reason = "dollar budget reached"
                    break
                try:
                    status, body, headers = (self.fetch or _null_fetch)(url)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ask fetch failed %s: %s", url, exc)
                    continue
                if status >= 400 or not body:
                    continue
                pages_fetched += 1
                ev, prov = self._ingest(url, body, status, tenant)
                evidence.append(ev)
                if prov:
                    ev.provenance_id = prov.record_id
            answer.steps.append(Step("fetch", ok=pages_fetched > 0,
                                     detail=f"{pages_fetched} page(s) fetched",
                                     ms=(self._now() - t0) * 1000,
                                     count=pages_fetched))

        # 4) rank + claims ---------------------------------------------------- #
        t0 = self._now()
        evidence = _rank(evidence, question)
        claims = extract_claims(evidence, question)
        answer.steps.append(Step("extract", ok=bool(claims) or not evidence,
                                 detail=f"{len(claims)} claim(s) from "
                                        f"{len(evidence)} source(s)",
                                 ms=(self._now() - t0) * 1000, count=len(claims)))

        # 5) citations -------------------------------------------------------- #
        cited: dict[str, Citation] = {}
        for claim in claims:
            for idx in claim.citations:
                if not (1 <= idx <= len(evidence)):
                    continue
                ev = evidence[idx - 1]
                if ev.url not in cited:
                    cited[ev.url] = Citation(
                        index=len(cited) + 1, url=ev.url, title=ev.title,
                        quote=claim.text[:300], host=ev.host,
                        fetched_at=ev.fetched_at or None,
                        content_sha256=ev.content_sha256,
                        provenance_id=ev.provenance_id,
                        corroborated_by=len(claim.hosts) or 1,
                    )
                else:
                    cited[ev.url].corroborated_by = max(
                        cited[ev.url].corroborated_by, len(claim.hosts) or 1)
        answer.citations = list(cited.values())

        # 6) compose ---------------------------------------------------------- #
        answer.answer = self._compose(question, claims, answer.citations)
        answer.claims = claims
        answer.evidence_count = len(evidence)
        answer.used_llm = bool(b.llm and self.llm)
        answer.confidence = _overall_confidence(claims, answer.citations, evidence)
        answer.caveats = _caveats(claims, answer.citations, evidence, pages_fetched,
                                  timed_out)
        answer.cost_usd, answer.cost_breakdown = self._cost(pages_fetched, evidence, b)
        answer.budget_used = {
            "pages": pages_fetched,
            "seconds": round(self._now() - started, 2),
            "usd": round(answer.cost_usd, 6),
            "evidence": len(evidence),
        }
        answer.stop_reason = answer.stop_reason or "completed"
        return answer

    # -- helpers ------------------------------------------------------------- #
    def _ingest(self, url: str, body: str, status: int, tenant: str
                ) -> tuple[Evidence, Any]:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        snap = self.snapshots.put(body, url=url, status=status,
                                  content_type="text/html")
        text = "\n".join(content_lines(body))[:20_000]
        prov = None
        if self.ledger is not None:
            prov = self.ledger.record(record_for_page(
                tenant=tenant, url=url, body=body, status=status, method="http",
                extractor="ask", extractor_version="1", confidence=0.7,
                snapshot_id=snap.snapshot_id, dataset="ask",
                extra={"question_mode": True},
            ))
        return Evidence(
            url=url, title=_title_of(body), snippet=text[:240], text=text,
            host=host, source="web", fetched_at=self._now(),
            # Fetched pages start at "usable but unverified": corroboration by
            # a second host is what raises a claim's confidence.
            confidence=0.6,
            provenance_id=getattr(prov, "record_id", ""),
            content_sha256=snap.snapshot_id,
        ), prov

    def _cost(self, pages: int, evidence: Sequence[Evidence],
              b: AskBudget) -> tuple[float, dict[str, float]]:
        est = estimate(CrawlSpec(pages=max(1, pages), render_share=b.render_share,
                                 keep_snapshots=True, concurrency=8),
                       price_book=self.price_book)
        breakdown = dict(est.breakdown) if pages else {}
        if b.llm and self.llm:
            tokens_in = sum(len((e.text or "")[:6000]) // 4 for e in evidence)
            breakdown["llm"] = (
                tokens_in / 1e6 * self.price_book.llm_usd_per_mtok_in
                + 400 / 1e6 * self.price_book.llm_usd_per_mtok_out)
        return round(sum(breakdown.values()), 6), breakdown

    def _spend(self, pages: int, evidence: Sequence[Evidence]) -> float:
        return self._cost(pages, evidence, AskBudget())[0]

    def _compose(self, question: str, claims: Sequence[Claim],
                 citations: Sequence[Citation]) -> str:
        if not claims:
            return (
                f"**No verified evidence for:** {question}\n\n"
                "Nothing in the corpus or the fetched pages supported an answer, so "
                "none was produced. Options: widen the budget, add seed URLs, or "
                "build/extend the own search index."
            )
        strongest = sorted(claims, key=lambda c: (-c.confidence, -c.support))
        lines = [f"**{question.strip()}**", ""]
        for claim in strongest[:5]:
            marker = "".join(f"[{i}]" for i in claim.citations)
            flag = " \u2691" if claim.support == 1 else ""
            lines.append(f"- {claim.text.strip()} {marker}{flag}".strip())
        if self.llm:
            try:
                narrative = self.llm(question, [c.text for c in strongest[:5]])
                if narrative:
                    lines += ["", "**Synthesis**", str(narrative).strip()]
            except Exception as exc:  # noqa: BLE001
                lines += ["", f"_(LLM synthesis unavailable: {exc})_"]
        if len(citations) >= 2:
            lines += ["", "Sources are listed below; \u2691 marks a claim that only "
                          "one source supports."]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# module-level helpers (pure, testable)
# --------------------------------------------------------------------------- #
def _coerce_evidence(hit: Any, *, source: str) -> Evidence:
    if isinstance(hit, Evidence):
        return hit
    if isinstance(hit, dict):
        def get(k, default=None):
            return hit.get(k, default)
    else:
        def get(k, default=None):
            return getattr(hit, k, default)

    from urllib.parse import urlparse

    url = str(get("url", "") or "")
    return Evidence(
        url=url,
        title=str(get("title", "") or ""),
        snippet=str(get("snippet", "") or ""),
        text=str(get("text", "") or get("snippet", "") or ""),
        host=str(get("host", "") or (urlparse(url).hostname or "")),
        source=source,
        fetched_at=float(get("fetched_at", 0) or 0),
        confidence=float(get("confidence", 0.5) or 0.5),
        provenance_id=str(get("provenance_id", "") or ""),
        content_sha256=str(get("content_sha256", "") or ""),
        score=float(get("score", 0.0) or 0.0),
    )


def _rank(evidence: Iterable[Evidence], question: str) -> list[Evidence]:
    qt = terms(question)
    rows = list(evidence)
    for e in rows:
        haystack = f"{e.title} {e.snippet} {e.text[:2000]}".lower()
        hits = sum(1 for t in set(qt) if t in haystack) / max(1, len(set(qt)))
        e.score = round(0.5 * hits + 0.3 * e.confidence + 0.2 * min(1.0, e.score), 4)
    rows.sort(key=lambda e: (-e.score, e.url))
    return rows


def _overall_confidence(claims: Sequence[Claim], citations: Sequence[Citation],
                        evidence: Sequence[Evidence]) -> float:
    if not claims:
        return 0.0
    claim_conf = sum(c.confidence for c in claims) / len(claims)
    corroboration = min(1.0, len({c.host for c in citations if c.host}) / 3.0)
    volume = min(1.0, len(evidence) / 5.0)
    return round(min(0.95, 0.55 * claim_conf + 0.3 * corroboration + 0.15 * volume), 4)


def _caveats(claims: Sequence[Claim], citations: Sequence[Citation],
             evidence: Sequence[Evidence], pages: int, timed_out: bool) -> list[str]:
    out: list[str] = []
    single = [c for c in claims if c.support == 1]
    if single:
        out.append(f"{len(single)} of {len(claims)} claims rest on a single source")
    if citations and len({c.host for c in citations if c.host}) < 2:
        out.append("all citations come from one host — treat as unconfirmed")
    if timed_out:
        out.append("the run hit its time budget before the evidence was exhausted")
    if not evidence:
        out.append("no evidence was retrieved (empty corpus and no pages fetched)")
    if evidence and not claims:
        out.append("pages were retrieved but none contained a sentence that answers "
                   "the question")
    if pages == 0 and evidence:
        out.append("answer is drawn from previously collected records only")
    return out


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", flags=re.I | re.S)
    if m:
        return re.sub(r"<[^>]+>", " ", m.group(1)).strip()[:200]
    return ""


def _null_fetch(url: str) -> tuple[int, str, dict[str, str]]:
    from src.watch.service import default_fetcher

    return default_fetcher(url)
