"""AI Research Agent — a question becomes a report with citations.

    "Build a research report on the 2026 AI chip market."

The agent decomposes the request into sub-questions, gathers evidence from
the platform's own index and (optionally) fresh pages, extracts claims, marks
which claims are corroborated across independent hosts, records provenance for
every page it touched, and writes a report where **every number has a source**
and every gap is named.

What it deliberately does *not* do: invent. There is no "summarise from memory"
path — the report is assembled from cited claims, and if the corpus cannot
answer a sub-question the report says so under *Gaps*. That constraint is the
product: a research report you can check in five minutes is worth more than a
fluent one you cannot check at all.

The LLM is optional at two seams (``planner`` and ``writer``); without one the
deterministic planner and template writer are used, and the report is still
complete, cheaper, and reproducible.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

from src.ask.service import Answer, AskBudget, AskService, Citation, Claim, terms
from src.provenance.manifest import build_manifest, signed_manifest

logger = logging.getLogger("wcp.research")

SUBQUESTION_TEMPLATES = (
    "what is {topic}",
    "who are the main players in {topic}",
    "how much does {topic} cost",
    "what changed recently in {topic}",
    "what are the risks of {topic}",
)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class ResearchPlan:
    question: str
    topic: str
    subquestions: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    sources_target: int = 25
    budget: dict[str, Any] = field(default_factory=dict)
    made_by: str = "heuristic"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    subquestion: str
    claim: str
    citations: list[int] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    confidence: float = 0.0
    verified: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "confidence": round(self.confidence, 3)}


@dataclass
class ResearchReport:
    question: str
    report_id: str
    plan: ResearchPlan
    findings: list[Finding] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    answer: str = ""
    markdown: str = ""
    confidence: float = 0.0
    cost_usd: float = 0.0
    pages: int = 0
    sources: int = 0
    evidence_count: int = 0
    used_llm: bool = False
    provenance: list[str] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    tenant: str = "public"

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "question": self.question,
            "plan": self.plan.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "gaps": list(self.gaps),
            "citations": [c.to_dict() for c in self.citations],
            "answer": self.answer,
            "markdown": self.markdown,
            "confidence": round(self.confidence, 4),
            "cost_usd": round(self.cost_usd, 6),
            "pages": self.pages,
            "sources": self.sources,
            "evidence_count": self.evidence_count,
            "used_llm": self.used_llm,
            "provenance": list(self.provenance),
            "manifest": self.manifest,
            "steps": list(self.steps),
            "created_at": round(self.created_at, 3),
            "tenant": self.tenant,
        }

    def to_markdown(self) -> str:
        return self.markdown or render_markdown(self)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def derive_topic(question: str) -> str:
    """A short topic phrase from the raw question (used in template queries)."""
    t = terms(question)
    return " ".join(t[:4]) or (question or "").strip()[:60]


def plan_research(question: str, *, budget: dict[str, Any] | None = None,
                  planner: Callable[[str], Sequence[str]] | None = None) -> ResearchPlan:
    """Decompose the question. Deterministic by default, LLM-optional."""
    topic = derive_topic(question)
    if planner is not None:
        try:
            subquestions = [str(s).strip() for s in planner(question) if str(s).strip()]
            made_by = "llm"
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the run
            logger.warning("LLM planner failed (%s) — using the heuristic plan", exc)
            subquestions, made_by = [], "heuristic"
    else:
        subquestions, made_by = [], "heuristic"

    if not subquestions:
        parts = [p.strip() for p in re.split(r"\band\b|;|,\s*(?=(?:how|what|who|which))",
                                            question or "") if p.strip()]
        subquestions = [parts[0]] if parts else [question]
        for template in SUBQUESTION_TEMPLATES[:4]:
            subquestions.append(template.format(topic=topic))
        made_by = "heuristic"

    queries: list[str] = []
    for sub in subquestions:
        query = " ".join(terms(sub)[:6])
        if query and query not in queries:
            queries.append(query)

    return ResearchPlan(
        question=question, topic=topic,
        subquestions=subquestions[:6], queries=queries[:8],
        sources_target=int((budget or {}).get("max_pages", 25)),
        budget=dict(budget or {}), made_by=made_by,
    )


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #
class ResearchAgent:
    """Plan → gather → extract → verify → report.

    ``ask`` is a configured :class:`~src.ask.service.AskService`; everything
    else is optional, and each optional piece degrades to a documented
    no-op (no graph → no entity enrichment; no ledger → no provenance ids).
    """

    def __init__(self, ask: AskService, *, graph: Any = None, ledger: Any = None,
                 writer: Callable[[str, Sequence[Finding]], str] | None = None,
                 planner: Callable[[str], Sequence[str]] | None = None,
                 manifest_secret: str = "") -> None:
        self.ask = ask
        self.graph = graph
        self.ledger = ledger
        self.writer = writer
        self.planner = planner
        self.manifest_secret = manifest_secret

    def run(self, question: str, *, budget: AskBudget | dict | None = None,
            tenant: str = "public", urls: Sequence[str] | None = None,
            subquestions: Sequence[str] | None = None,
            mode: str = "auto") -> ResearchReport:
        started = time.time()
        b = budget if isinstance(budget, AskBudget) else AskBudget.from_dict(budget)
        plan = plan_research(question, budget=b.to_dict(), planner=self.planner)
        if subquestions:
            plan.subquestions = [str(s) for s in subquestions][:8]
            plan.queries = [" ".join(terms(s)[:6]) for s in plan.subquestions][:8]
            plan.made_by = "caller"
        report = ResearchReport(question=question,
                                report_id="r_" + hex(int(time.time() * 1000))[2:],
                                plan=plan, tenant=tenant)
        report.steps.append({"name": "plan", "subquestions": len(plan.subquestions),
                             "made_by": plan.made_by})

        answers: list[Answer] = []
        for sub in plan.subquestions:
            sub_budget = AskBudget(
                max_pages=max(3, b.max_pages // max(1, len(plan.subquestions))),
                max_seconds=max(5.0, b.max_seconds / max(1, len(plan.subquestions))),
                max_usd=b.max_usd / max(1, len(plan.subquestions)),
                llm=False,
            )
            try:
                answer = self.ask.ask(
                    sub, budget=sub_budget, mode=mode,
                    tenant=tenant, urls=urls if mode in ("web", "auto") else None)
            except Exception as exc:  # noqa: BLE001 - one bad sub-question is not fatal
                logger.warning("sub-question failed (%s): %s", sub, exc)
                report.gaps.append(f"{sub}: retrieval failed ({exc})")
                continue
            answers.append(answer)
            report.cost_usd += answer.cost_usd
            report.pages += int(answer.budget_used.get("pages", 0) or 0)
            report.evidence_count += answer.evidence_count
            report.steps.append({"name": "gather", "subquestion": sub,
                                 "claims": len(answer.claims),
                                 "evidence": answer.evidence_count,
                                 "stop_reason": answer.stop_reason})

        report.findings, citations = self._findings(plan, answers, report)
        report.citations = citations
        report.sources = len({c.host for c in citations if c.host})
        report.confidence = _report_confidence(report.findings, report.sources)

        for answer in answers:
            for cite in answer.citations:
                if cite.provenance_id:
                    report.provenance.append(cite.provenance_id)
        report.provenance = sorted(set(report.provenance))

        if self.graph is not None and report.findings:
            report.steps.append(self._enrich_graph(tenant, report))

        if self.manifest_secret and self.ledger is not None:
            try:
                records = [self.ledger.get(tenant, pid) for pid in report.provenance]
                records = [r for r in records if r is not None]
                if records:
                    manifest = build_manifest(
                        dataset=f"research:{report.report_id}", version="1",
                        records=records, created_by="research-agent",
                        tenant=tenant, extra={"question": question})
                    report.manifest = signed_manifest(manifest,
                                                      self.manifest_secret)
                    report.steps.append({"name": "provenance",
                                         "records": len(records),
                                         "signature": report.manifest["signature"]["key_id"]})
            except Exception as exc:  # noqa: BLE001
                report.steps.append({"name": "provenance", "error": str(exc)})

        if self.writer is not None:
            try:
                report.answer = self.writer(question, report.findings) or ""
                report.used_llm = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM writer failed (%s) — using the template", exc)
        if not report.answer:
            report.answer = _template_summary(question, report.findings)

        report.markdown = render_markdown(report)
        report.steps.append({"name": "report", "findings": len(report.findings),
                             "gaps": len(report.gaps),
                             "seconds": round(time.time() - started, 2)})
        return report

    # -- internals ----------------------------------------------------------- #
    def _findings(self, plan: ResearchPlan, answers: Sequence[Answer],
                  report: ResearchReport) -> tuple[list[Finding], list[Citation]]:
        """Bind claims to sub-questions and to a single, deduplicated citation list."""
        citations: list[Citation] = []
        by_url: dict[str, Citation] = {}
        findings: list[Finding] = []

        for sub, answer in zip(plan.subquestions, answers):
            if not answer.claims:
                report.gaps.append(
                    f"{sub}: no claim survived extraction "
                    f"({answer.stop_reason}, {answer.evidence_count} source(s))")
                continue
            for claim in _dedupe_claims(answer.claims):
                remapped: list[int] = []
                for idx in claim.citations:
                    if not (1 <= idx <= len(answer.citations)):
                        continue
                    cite = answer.citations[idx - 1]
                    if cite.url not in by_url:
                        fresh = Citation(
                            index=len(citations) + 1, url=cite.url, title=cite.title,
                            quote=cite.quote, host=cite.host,
                            fetched_at=cite.fetched_at,
                            content_sha256=cite.content_sha256,
                            provenance_id=cite.provenance_id,
                            corroborated_by=claim.support,
                        )
                        by_url[cite.url] = fresh
                        citations.append(fresh)
                    remapped.append(by_url[cite.url].index)
                findings.append(Finding(
                    subquestion=sub, claim=claim.text, citations=remapped,
                    hosts=list(claim.hosts), confidence=claim.confidence,
                    verified=claim.support >= 2,
                    note="" if claim.support >= 2 else "single source",
                ))
        findings.sort(key=lambda f: (-f.confidence, f.subquestion))
        return findings[:40], citations

    def _enrich_graph(self, tenant: str, report: ResearchReport) -> dict[str, Any]:
        added = 0
        try:
            for finding in report.findings[:20]:
                result = self.graph.ingest_text(
                    tenant, finding.claim, url=(report.citations[0].url
                                                if report.citations else ""),
                    record_id=(report.citations[0].provenance_id
                               if report.citations else ""))
                added += len(result.get("entities", []))
        except Exception as exc:  # noqa: BLE001
            return {"name": "graph", "error": str(exc)}
        return {"name": "graph", "entities_touched": added}


def _dedupe_claims(claims: Sequence[Claim]) -> list[Claim]:
    out: list[Claim] = []
    seen: list[set[str]] = []
    for claim in claims:
        key = set(terms(claim.text))
        if any(key and prev and len(key & prev) / max(1, len(key | prev)) > 0.6
               for prev in seen):
            continue
        seen.append(key)
        out.append(claim)
    return out


def _report_confidence(findings: Sequence[Finding], sources: int) -> float:
    if not findings:
        return 0.0
    verified = sum(1 for f in findings if f.verified) / len(findings)
    mean = sum(f.confidence for f in findings) / len(findings)
    breadth = min(1.0, sources / 5.0)
    return round(min(0.94, 0.45 * mean + 0.35 * verified + 0.2 * breadth), 4)


def _template_summary(question: str, findings: Sequence[Finding]) -> str:
    if not findings:
        return (f"No verified findings for: {question}. The corpus and the fetched "
                "pages did not contain a claim that answered the sub-questions — "
                "see Gaps for what was missing.")
    top = sorted(findings, key=lambda f: (-f.confidence, -len(f.citations)))[:3]
    return " ".join(f.claim.strip() for f in top)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_markdown(report: ResearchReport) -> str:
    """The deliverable: a report whose every claim points at a URL."""
    lines = [
        f"# {report.question}",
        "",
        f"*Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(report.created_at))} · "
        f"report `{report.report_id}` · confidence "
        f"{report.confidence:.0%} · {len(report.sources)} source(s) · "
        f"${report.cost_usd:.4f} · {report.pages} page(s)*",
        "",
        "## Summary",
        "",
        report.answer or "_no summary_",
        "",
        "## Findings",
        "",
    ]
    if not report.findings:
        lines.append("_No findings: the evidence did not support any claim. "
                     "See Gaps._")
    grouped: dict[str, list[Finding]] = {}
    for finding in report.findings:
        grouped.setdefault(finding.subquestion, []).append(finding)
    for subquestion, items in grouped.items():
        lines.append(f"### {subquestion}")
        lines.append("")
        for finding in items:
            marks = "".join(f"[{i}]" for i in finding.citations)
            badge = "" if finding.verified else " `single-source`"
            lines.append(f"- {finding.claim.strip()} {marks}{badge}")
        lines.append("")

    lines += ["## Gaps & caveats", ""]
    if report.gaps:
        lines += [f"- {gap}" for gap in report.gaps]
    single = [f for f in report.findings if not f.verified]
    if single:
        lines.append(f"- {len(single)} finding(s) rest on a single source and are "
                     "marked as such")
    if not report.gaps and not single:
        lines.append("- none identified in this run")

    lines += ["", "## Sources", ""]
    for cite in report.citations:
        when = (time.strftime('%Y-%m-%d', time.gmtime(cite.fetched_at))
                if cite.fetched_at else "unknown date")
        lines.append(f"{cite.index}. [{cite.title or cite.url}]({cite.url}) — fetched "
                     f"{when}" + (f", **prov** `{cite.provenance_id[:12]}`"
                                  if cite.provenance_id else ""))
    if not report.citations:
        lines.append("_no sources_")

    lines += ["", "## Method", ""]
    lines.append(f"- Plan: `{report.plan.made_by}` with "
                 f"{len(report.plan.subquestions)} sub-question(s)")
    for query in report.plan.queries:
        lines.append(f"  - query: `{query}`")
    for step in report.steps:
        if step.get("name") in ("gather", "provenance", "graph"):
            detail = ", ".join(f"{k}={v}" for k, v in step.items() if k != "name")
            lines.append(f"- {step['name']}: {detail}")
    lines.append(f"- Evidence: {report.evidence_count} item(s) from "
                 f"{len(report.citations)} page(s) across {report.sources} host(s)")
    lines.append(f"- Cost: ${report.cost_usd:.4f} for {report.pages} freshly fetched "
                 "page(s)")

    if report.manifest:
        sig = report.manifest.get("signature", {})
        lines += ["", "## Provenance", "",
                  f"- Manifest: `{report.manifest.get('merkle_root')}`",
                  f"- Signature: `{sig.get('value', '')[:32]}…` "
                  f"({sig.get('key_id', '-')}, {sig.get('algorithm', '-')})",
                  f"- Records: {report.manifest.get('record_count')} "
                  f"(licences: {report.manifest.get('licenses', {}).get('counts')})"]
    return "\n".join(lines) + "\n"
