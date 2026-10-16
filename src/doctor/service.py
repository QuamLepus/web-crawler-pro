"""Crawl Doctor — per-phase timing and a diagnosis when things go wrong.

A crawl that suddenly drops from 95% to 40% success is a fire, but "success
rate fell" is not a diagnosis. The difference between a blocked site, a slow
origin and a broken extractor is visible only if the phases are timed
separately: DNS, TLS, TTFB, body transfer, render, extract. So every fetch is
recorded as a span set, and the doctor turns a window of spans into a ranked
list of *causes* rather than a chart.

Deliberately storage-light: spans are aggregated into rolling counters in
Redis (and optionally emitted to OpenTelemetry by the caller), because storing
every span for every page costs more than the crawl.

Phase names follow the OpenTelemetry HTTP semantic conventions where they
exist, so exporting them to Jaeger/Tempo later needs no translation.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

logger = logging.getLogger("wcp.doctor")

PHASES = ("dns", "connect", "tls", "ttfb", "transfer", "render", "parse",
          "extract", "queue_delay")

FAILURE_CAUSES = (
    "dns_failure", "tls_failure", "timeout", "connection_reset",
    "http_4xx", "http_5xx", "robots_disallowed", "empty_body", "extract_empty",
    "captcha", "blocked", "unsupported_content", "budget_exhausted", "unknown",
)


@dataclass
class Span:
    """One fetch, phase-timed."""

    url: str = ""
    host: str = ""
    status: int = 0
    bytes: int = 0
    phases: dict[str, float] = field(default_factory=dict)   # seconds
    render: bool = False
    proxy_tier: str = "none"
    cause: str = ""                 # set when the fetch failed
    ok: bool = True
    at: float = field(default_factory=time.time)
    job_id: str = ""
    worker: str = ""

    @property
    def total_ms(self) -> float:
        return round(sum(self.phases.values()) * 1000, 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_ms"] = self.total_ms
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Span:
        base = cls(url=row.get("url", ""))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base


def classify_failure(status: int, *, error: str = "", body: bytes | str = "",
                     captcha: bool = False, robots: str = "unknown",
                     extracted: int | None = None) -> str:
    """Turn an outcome into one of :data:`FAILURE_CAUSES`.

    One label per failure keeps the dashboard readable ("timeouts: 412")
    instead of a long tail of exception strings nobody can act on.
    """
    if robots == "disallowed":
        return "robots_disallowed"
    if captcha:
        return "captcha"
    low = (error or "").lower()
    if "dns" in low or "name resolution" in low or "nodename" in low:
        return "dns_failure"
    if "ssl" in low or "tls" in low or "certificate" in low:
        return "tls_failure"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "reset" in low or "aborted" in low or "broken pipe" in low:
        return "connection_reset"
    if "403" in low or "cloudflare" in low or "just a moment" in low:
        return "blocked"
    if status == 0 and error:
        return "unknown"
    if 400 <= status < 500:
        return "http_4xx"
    if status >= 500:
        return "http_5xx"
    if status and not body:
        return "empty_body"
    if extracted == 0:
        return "extract_empty"
    return "" if status else "unknown"


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
@dataclass
class HealthReport:
    window_seconds: float
    samples: int
    success_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    phase_means_ms: dict[str, float]
    slowest_phase: str
    causes: dict[str, int]
    hosts: dict[str, int]
    render_share: float
    verdict: str
    advice: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_seconds": self.window_seconds,
            "samples": self.samples,
            "success_rate": round(self.success_rate, 4),
            "p50_ms": round(self.p50_ms, 1),
            "p95_ms": round(self.p95_ms, 1),
            "p99_ms": round(self.p99_ms, 1),
            "phase_means_ms": {k: round(v, 1) for k, v in
                               sorted(self.phase_means_ms.items(), key=lambda kv: -kv[1])},
            "slowest_phase": self.slowest_phase,
            "causes": dict(sorted(self.causes.items(), key=lambda kv: -kv[1])),
            "hosts": dict(sorted(self.hosts.items(), key=lambda kv: -kv[1])[:10]),
            "render_share": round(self.render_share, 3),
            "verdict": self.verdict,
            "advice": list(self.advice),
        }


def diagnose(spans: Sequence[Span | dict[str, Any]], *,
             window_seconds: float = 900.0) -> HealthReport:
    """Aggregate spans into a report with a verdict and concrete advice."""
    rows = [s if isinstance(s, Span) else Span.from_dict(s) for s in spans]
    n = len(rows)
    if not n:
        return HealthReport(window_seconds, 0, 1.0, 0, 0, 0, {}, "", {}, {}, 0.0,
                            "no data",
                            ["enable span recording (WCP_DOCTOR=1) to see a diagnosis"])

    ok = [s for s in rows if s.ok and 200 <= (s.status or 200) < 400]
    totals = sorted(s.total_ms for s in rows)
    phases: dict[str, list[float]] = {}
    for s in rows:
        for name, seconds in (s.phases or {}).items():
            phases.setdefault(name, []).append(seconds * 1000.0)
    means = {k: statistics.fmean(v) for k, v in phases.items() if v}
    causes = Counter(s.cause for s in rows if s.cause)
    hosts = Counter(s.host for s in rows if s.host)
    render_share = sum(1 for s in rows if s.render) / n

    report = HealthReport(
        window_seconds=window_seconds,
        samples=n,
        success_rate=len(ok) / n,
        p50_ms=_percentile(totals, 50),
        p95_ms=_percentile(totals, 95),
        p99_ms=_percentile(totals, 99),
        phase_means_ms=means,
        slowest_phase=max(means, key=means.get) if means else "",
        causes=dict(causes),
        hosts=dict(hosts),
        render_share=render_share,
        verdict="healthy",
    )
    report.advice = _advice(report)
    report.verdict = _verdict(report)
    return report


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _verdict(r: HealthReport) -> str:
    if r.success_rate >= 0.97 and r.p95_ms < 3000:
        return "healthy"
    if r.success_rate >= 0.9:
        return "degraded"
    if r.success_rate >= 0.7:
        return "unhealthy"
    return "failing"


def _advice(r: HealthReport) -> list[str]:
    out: list[str] = []
    if r.samples < 20:
        out.append("small sample — treat this as indicative until ~100 fetches")
    top_cause = next(iter(r.causes), "")
    if r.causes.get("timeout", 0) / max(1, r.samples) > 0.05:
        out.append("many timeouts: raise REQUEST_TIMEOUT or reduce concurrency per host")
    if r.causes.get("http_4xx", 0) / max(1, r.samples) > 0.10:
        out.append("lots of 4xx: check URL canonicalisation — most 404s are link noise")
    if r.causes.get("blocked", 0) or r.causes.get("captcha", 0):
        out.append("blocking detected: enable the stealth fetch path / rotate egress "
                   "tier for the affected hosts")
    if r.causes.get("extract_empty", 0) / max(1, r.samples) > 0.05:
        out.append("pages fetched but nothing extracted: the recipe selectors are "
                   "probably stale — run `recipe test` for the affected hosts")
    if r.causes.get("dns_failure", 0):
        out.append("DNS failures: retry with a resolver fallback; drop dead hosts from "
                   "the frontier after two failures")
    if r.render_share > 0.5:
        out.append(f"{r.render_share:.0%} of fetches used a browser — most pages do not "
                   "need one; enable the crawl optimizer's HTTP-first strategy")
    if r.p95_ms > 8000:
        out.append("p95 above 8s: the origin or the proxy tier is the bottleneck, not "
                   "the extractor")
    if r.phase_means_ms:
        slowest = r.slowest_phase
        if slowest == "ttfb":
            out.append("most time is server think-time (TTFB): add host-level caching "
                       "and lower per-host concurrency")
        elif slowest in ("parse", "extract"):
            out.append("most time is in parsing/extraction: prefer the built-in "
                       "extractors over regex chains, and index in batches")
        elif slowest == "render":
            out.append("most time is browser rendering: try the HTTP-first strategy "
                       "and only escalate on empty bodies")
    if not out:
        out.append("nothing anomalous in this window")
    if top_cause:
        out.append(f"most common failure cause: {top_cause}")
    return out


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
class DoctorRecorder:
    """Rolling span buffer in Redis (bounded, sampled, never blocking).

    Keeps the last ``capacity`` spans as a capped list plus hourly counters,
    so the console can show a window without a time-series database.
    """

    def __init__(self, redis_client, *, capacity: int = 2000,
                 prefix: str = "wcp:doctor") -> None:
        self.r = redis_client
        self.capacity = capacity
        self.prefix = prefix

    def _k(self, tenant: str, *parts: str) -> str:
        return ":".join([self.prefix, tenant or "public", *parts])

    def record(self, tenant: str, span: Span) -> None:
        try:
            key = self._k(tenant, "spans")
            pipe = self.r.pipeline()
            pipe.lpush(key, json.dumps(span.to_dict(), separators=(",", ":")))
            pipe.ltrim(key, 0, self.capacity - 1)
            pipe.incr(self._k(tenant, "total"))
            if not span.ok:
                pipe.incr(self._k(tenant, "failures"))
            if span.cause:
                pipe.hincrby(self._k(tenant, "causes"), span.cause, 1)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a fetch
            logger.debug("span record failed: %s", exc)

    def recent(self, tenant: str, limit: int = 500) -> list[Span]:
        raw = self.r.lrange(self._k(tenant, "spans"), 0, max(0, limit - 1))
        out = []
        for row in raw:
            try:
                out.append(Span.from_dict(json.loads(row)))
            except Exception:  # noqa: BLE001
                continue
        return out

    def report(self, tenant: str, *, limit: int = 500) -> HealthReport:
        return diagnose(self.recent(tenant, limit=limit))

    def counters(self, tenant: str) -> dict[str, Any]:
        total = self.r.get(self._k(tenant, "total"))
        failures = self.r.get(self._k(tenant, "failures"))
        causes = self.r.hgetall(self._k(tenant, "causes")) or {}
        decode = lambda v: v.decode() if isinstance(v, bytes) else v  # noqa: E731
        return {
            "total": int(decode(total) or 0),
            "failures": int(decode(failures) or 0),
            "causes": {decode(k): int(decode(v)) for k, v in causes.items()},
        }


def timed_phases(phases: dict[str, float], **overrides) -> Span:
    """Build a span from a phase dict (helper for crawler integrations)."""
    merged = dict(phases)
    merged.update(overrides.pop("phases", {}) or {})
    return Span(phases=merged, **overrides)
