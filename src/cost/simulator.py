"""Cost Simulator — "this crawl costs $4.20 and takes 12 minutes".

Finance teams approve budgets they can see. Engineers pick the configuration
that fits. Both questions have the same shape: given a size, a fetch strategy
and a proxy tier, what will it cost in money and in wall-clock time?

The model is a *price book plus arithmetic*, deliberately not a mystery: every
number comes from :class:`PriceBook`, every assumption is printed with the
estimate, and the inverse questions ("how many pages fit in $50?", "what does
a 100k-page crawl cost per 1000 usable pages?") are answered from the same
book. Rates are the platform's own defaults and are overridable by env
(``WCP_PRICE_*``) or by the caller, so a self-hoster on cheap egress gets
honest numbers instead of our cloud assumptions.

Nothing here is a promise about a third party's pricing — it is our own
cost-to-serve model, which is what a product needs in order to price work.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RenderMode = Literal["http", "browser", "auto"]
ProxyTier = Literal["none", "datacenter", "residential", "mobile"]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PriceBook:
    """Unit economics of one crawl. All money in USD, all sizes in bytes.

    Defaults are conservative mid-2026 cloud figures on a 3-year committed
    instance with on-demand egress; they exist so an estimate is *possible*
    out of the box, not so it is exact.
    """

    # egress + proxy
    egress_usd_per_gb: float = 0.09
    datacenter_proxy_usd_per_gb: float = 0.60
    residential_proxy_usd_per_gb: float = 3.50
    mobile_proxy_usd_per_gb: float = 9.00

    # compute
    worker_usd_per_hour: float = 0.036          # 2 vCPU spot-ish worker
    browser_worker_usd_per_hour: float = 0.115  # 4 vCPU + memory for Chromium

    # anti-bot
    captcha_usd_each: float = 0.0025

    # AI
    llm_usd_per_mtok_in: float = 0.35
    llm_usd_per_mtok_out: float = 1.40
    embedding_usd_per_mtok: float = 0.02

    # storage
    storage_usd_per_gb_month: float = 0.023
    snapshot_usd_per_gb_month: float = 0.023

    @classmethod
    def from_env(cls) -> PriceBook:
        return cls(
            egress_usd_per_gb=_env_float("WCP_PRICE_EGRESS_GB", 0.09),
            datacenter_proxy_usd_per_gb=_env_float("WCP_PRICE_PROXY_DC_GB", 0.60),
            residential_proxy_usd_per_gb=_env_float("WCP_PRICE_PROXY_RES_GB", 3.50),
            mobile_proxy_usd_per_gb=_env_float("WCP_PRICE_PROXY_MOBILE_GB", 9.00),
            worker_usd_per_hour=_env_float("WCP_PRICE_WORKER_HOUR", 0.036),
            browser_worker_usd_per_hour=_env_float("WCP_PRICE_BROWSER_HOUR", 0.115),
            captcha_usd_each=_env_float("WCP_PRICE_CAPTCHA", 0.0025),
            llm_usd_per_mtok_in=_env_float("WCP_PRICE_LLM_IN", 0.35),
            llm_usd_per_mtok_out=_env_float("WCP_PRICE_LLM_OUT", 1.40),
            embedding_usd_per_mtok=_env_float("WCP_PRICE_EMBED", 0.02),
            storage_usd_per_gb_month=_env_float("WCP_PRICE_STORAGE_GB", 0.023),
            snapshot_usd_per_gb_month=_env_float("WCP_PRICE_SNAPSHOT_GB", 0.023),
        )

    def proxy_rate(self, tier: ProxyTier) -> float:
        return {
            "none": 0.0,
            "datacenter": self.datacenter_proxy_usd_per_gb,
            "residential": self.residential_proxy_usd_per_gb,
            "mobile": self.mobile_proxy_usd_per_gb,
        }[tier]


@dataclass
class CrawlSpec:
    """What you are about to crawl."""

    pages: int = 10_000
    avg_page_kb: float = 180.0             # transferred per page, post-compression
    render_share: float = 0.15             # fraction needing a real browser
    proxy_tier: ProxyTier = "none"
    captcha_rate: float = 0.0              # fraction of pages that hit a captcha
    extract_llm_share: float = 0.0         # fraction extracted by an LLM
    tokens_in_per_page: int = 1_800
    tokens_out_per_page: int = 350
    embed_share: float = 0.0               # fraction embedded for semantic search
    keep_snapshots: bool = True
    retention_days: int = 90
    concurrency: int = 16                  # parallel fetches
    seconds_per_http_page: float = 0.55    # incl. parse, with concurrency
    seconds_per_browser_page: float = 3.20
    pages_per_host: float = 25.0           # politeness: per-host batch size
    host_delay_seconds: float = 1.0        # politeness delay between hits
    duplicate_rate: float = 0.12           # pages dropped as near-duplicates
    fetch_failure_rate: float = 0.05       # retries included in the average

    def normalised(self) -> CrawlSpec:
        return CrawlSpec(
            pages=max(0, int(self.pages)),
            avg_page_kb=max(1.0, float(self.avg_page_kb)),
            render_share=min(1.0, max(0.0, float(self.render_share))),
            proxy_tier=self.proxy_tier,
            captcha_rate=min(1.0, max(0.0, float(self.captcha_rate))),
            extract_llm_share=min(1.0, max(0.0, float(self.extract_llm_share))),
            tokens_in_per_page=max(0, int(self.tokens_in_per_page)),
            tokens_out_per_page=max(0, int(self.tokens_out_per_page)),
            embed_share=min(1.0, max(0.0, float(self.embed_share))),
            keep_snapshots=bool(self.keep_snapshots),
            retention_days=max(1, int(self.retention_days)),
            concurrency=max(1, int(self.concurrency)),
            seconds_per_http_page=max(0.05, float(self.seconds_per_http_page)),
            seconds_per_browser_page=max(0.2, float(self.seconds_per_browser_page)),
            pages_per_host=max(1.0, float(self.pages_per_host)),
            host_delay_seconds=max(0.0, float(self.host_delay_seconds)),
            duplicate_rate=min(0.99, max(0.0, float(self.duplicate_rate))),
            fetch_failure_rate=min(0.99, max(0.0, float(self.fetch_failure_rate))),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> CrawlSpec:
        base = cls()
        if not row:
            return base.normalised()
        allowed = {k: v for k, v in row.items() if hasattr(base, k)}
        return cls(**allowed).normalised()  # type: ignore[arg-type]


@dataclass
class CostEstimate:
    pages: int = 0
    usable_pages: int = 0
    est_seconds: float = 0.0
    est_usd: float = 0.0
    usd_per_1000_pages: float = 0.0
    usd_per_1000_usable: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    time_breakdown: dict[str, float] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def est_minutes(self) -> float:
        return self.est_seconds / 60.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "usable_pages": self.usable_pages,
            "est_seconds": round(self.est_seconds, 2),
            "est_minutes": round(self.est_minutes, 2),
            "est_hours": round(self.est_seconds / 3600.0, 3),
            "est_usd": round(self.est_usd, 4),
            "usd_per_1000_pages": round(self.usd_per_1000_pages, 4),
            "usd_per_1000_usable": round(self.usd_per_1000_usable, 4),
            "breakdown": {k: round(v, 6) for k, v in
                          sorted(self.breakdown.items(), key=lambda kv: -kv[1])},
            "time_breakdown": {k: round(v, 2) for k, v in
                               sorted(self.time_breakdown.items(), key=lambda kv: -kv[1])},
            "assumptions": list(self.assumptions),
            "spec": self.spec,
        }


def estimate(spec: CrawlSpec | dict[str, Any] | None = None, *,
             price_book: PriceBook | None = None,
             host_count: int | None = None) -> CostEstimate:
    """Estimate cost and wall-clock time for a crawl.

    ``host_count`` defaults to ``ceil(pages / pages_per_host)``: politeness
    means a fixed number of hosts caps throughput no matter how wide the
    concurrency is, which is the single most-missed factor in crawl estimates.
    """
    s = (spec if isinstance(spec, CrawlSpec) else CrawlSpec.from_dict(spec)).normalised()
    pb = price_book or PriceBook.from_env()

    pages = s.pages
    browser_pages = pages * s.render_share
    http_pages = pages - browser_pages
    fetched_pages = pages / max(0.01, 1.0 - s.fetch_failure_rate)
    bytes_total = fetched_pages * s.avg_page_kb * 1024
    gb = bytes_total / 1024**3

    # -- money --------------------------------------------------------------- #
    breakdown = {
        "egress": gb * pb.egress_usd_per_gb,
        "proxy": gb * pb.proxy_rate(s.proxy_tier),
        "compute_http": (http_pages * s.seconds_per_http_page / 3600.0)
                        * pb.worker_usd_per_hour,
        "compute_browser": (browser_pages * s.seconds_per_browser_page / 3600.0)
                           * pb.browser_worker_usd_per_hour,
        "captcha": pages * s.captcha_rate * pb.captcha_usd_each,
        "llm_extraction": (
            pages * s.extract_llm_share
            * (s.tokens_in_per_page / 1e6 * pb.llm_usd_per_mtok_in
               + s.tokens_out_per_page / 1e6 * pb.llm_usd_per_mtok_out)
        ),
        "embeddings": (pages * s.embed_share
                       * (s.tokens_in_per_page / 1e6) * pb.embedding_usd_per_mtok),
    }
    months = s.retention_days / 30.0
    stored_gb = (gb * 0.25)  # extracted JSON is far smaller than raw HTML
    breakdown["storage"] = stored_gb * pb.storage_usd_per_gb_month * months
    if s.keep_snapshots:
        snap_gb = gb * 0.3   # gzipped raw HTML, roughly a third of transferred bytes
        breakdown["snapshots"] = snap_gb * pb.snapshot_usd_per_gb_month * months

    total = sum(breakdown.values())

    # -- time ---------------------------------------------------------------- #
    hosts = int(host_count) if host_count else max(1, math.ceil(pages / s.pages_per_host))
    concurrency = min(s.concurrency, max(1, hosts))
    http_seconds = (http_pages * s.seconds_per_http_page) / concurrency
    browser_seconds = (browser_pages * s.seconds_per_browser_page) / max(1, concurrency // 2)
    # politeness floor: each host can only be hit every host_delay seconds
    per_host_pages = pages / hosts
    politeness_floor = per_host_pages * s.host_delay_seconds / max(1.0, s.concurrency / hosts)
    seconds = max(http_seconds + browser_seconds, politeness_floor)
    time_breakdown = {
        "http_fetch": http_seconds,
        "browser_render": browser_seconds,
        "politeness_floor": politeness_floor,
        "planning_overhead": seconds * 0.04,
    }
    seconds += time_breakdown["planning_overhead"]

    usable = int(round(pages * (1.0 - s.duplicate_rate)))

    return CostEstimate(
        pages=pages,
        usable_pages=usable,
        est_seconds=seconds,
        est_usd=total,
        usd_per_1000_pages=(total / pages * 1000.0) if pages else 0.0,
        usd_per_1000_usable=(total / usable * 1000.0) if usable else 0.0,
        breakdown=breakdown,
        time_breakdown=time_breakdown,
        assumptions=[
            f"{s.avg_page_kb:.0f} KB transferred per page (post-compression average)",
            f"{s.render_share * 100:.0f}% of pages need a real browser",
            f"proxy tier: {s.proxy_tier}"
            + (f" @ ${pb.proxy_rate(s.proxy_tier):.2f}/GB" if s.proxy_tier != "none" else ""),
            f"{s.fetch_failure_rate * 100:.0f}% fetch failures (retries included in the average)",
            f"{s.duplicate_rate * 100:.0f}% of fetched pages are duplicates and dropped",
            f"politeness: {s.host_delay_seconds:.1f}s between hits on one host, "
            f"{s.pages_per_host:.0f} pages per host, so ≈{hosts} hosts",
            f"storage retained for {s.retention_days} days"
            + (", raw snapshots kept" if s.keep_snapshots else ", snapshots off"),
            "prices are the platform's own cost-to-serve defaults "
            "(WCP_PRICE_* overrides them)",
        ],
        spec=s.to_dict(),
    )


def pages_for_budget(budget_usd: float, spec: CrawlSpec | dict[str, Any] | None = None,
                     *, price_book: PriceBook | None = None) -> dict[str, Any]:
    """Invert the model: how many pages fit in a budget, and what they cost.

    Solves by bisection because the model is monotonic in pages and the
    per-page mix (render share, proxy tier) is constant, so a closed form
    would only be less obvious.
    """
    s = spec if isinstance(spec, CrawlSpec) else CrawlSpec.from_dict(spec)
    lo, hi = 0, 10_000_000
    for _ in range(60):
        mid = (lo + hi) // 2
        cost = estimate(CrawlSpec(**{**s.to_dict(), "pages": mid}),
                        price_book=price_book).est_usd
        if cost <= budget_usd:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1:
            break
    best = estimate(CrawlSpec(**{**s.to_dict(), "pages": lo}), price_book=price_book)
    return {
        "budget_usd": budget_usd,
        "pages": lo,
        "est_usd": round(best.est_usd, 4),
        "est_minutes": round(best.est_minutes, 2),
        "usd_per_1000_pages": round(best.usd_per_1000_pages, 4),
    }


def compare(specs: list[CrawlSpec], *, price_book: PriceBook | None = None) -> dict[str, Any]:
    """Side-by-side options — the shape a "should we use a browser?" chat takes."""
    rows = []
    for s in specs:
        e = estimate(s, price_book=price_book)
        rows.append({"label": _label(s), **e.to_dict()})
    rows.sort(key=lambda r: r["est_usd"])
    cheapest = rows[0] if rows else None
    return {"options": rows, "cheapest": cheapest["label"] if cheapest else None,
            "saving_vs_most_expensive": round(
                (rows[-1]["est_usd"] - rows[0]["est_usd"]), 4) if len(rows) > 1 else 0.0}


def _label(s: CrawlSpec) -> str:
    bits = [f"{s.pages:,} pages"]
    if s.render_share:
        bits.append(f"{s.render_share * 100:.0f}% browser")
    if s.proxy_tier != "none":
        bits.append(s.proxy_tier)
    if s.extract_llm_share:
        bits.append(f"{s.extract_llm_share * 100:.0f}% LLM")
    return " · ".join(bits)


# --------------------------------------------------------------------------- #
# crawl-strategy presets — the three answers everyone actually wants
# --------------------------------------------------------------------------- #
PRESETS: dict[str, dict[str, Any]] = {
    "docs": {
        "label": "Documentation / static sites",
        "spec": CrawlSpec(pages=10_000, avg_page_kb=95, render_share=0.0,
                          proxy_tier="none", concurrency=24,
                          seconds_per_http_page=0.35).to_dict(),
    },
    "news": {
        "label": "News / article monitoring",
        "spec": CrawlSpec(pages=25_000, avg_page_kb=220, render_share=0.05,
                          proxy_tier="none", extract_llm_share=0.1,
                          concurrency=32, pages_per_host=60,
                          host_delay_seconds=0.5).to_dict(),
    },
    "commerce": {
        "label": "Commerce / price monitoring",
        "spec": CrawlSpec(pages=50_000, avg_page_kb=320, render_share=0.45,
                          proxy_tier="residential", captcha_rate=0.02,
                          extract_llm_share=0.05, concurrency=24,
                          pages_per_host=40).to_dict(),
    },
    "research": {
        "label": "Research report (small, deep)",
        "spec": CrawlSpec(pages=800, avg_page_kb=260, render_share=0.2,
                          extract_llm_share=0.85, embed_share=0.6,
                          concurrency=8, pages_per_host=15).to_dict(),
    },
    "archive": {
        "label": "Bulk archive (semantic layer on)",
        "spec": CrawlSpec(pages=500_000, avg_page_kb=180, render_share=0.03,
                          extract_llm_share=0.0, embed_share=0.25,
                          concurrency=64, pages_per_host=120,
                          host_delay_seconds=0.25, retention_days=180).to_dict(),
    },
}


def preset_list() -> list[dict[str, Any]]:
    out = []
    for key, payload in PRESETS.items():
        est = estimate(CrawlSpec.from_dict(payload["spec"]))
        out.append({"id": key, "label": payload["label"], **est.to_dict()})
    return out
