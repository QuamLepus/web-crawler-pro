"""Intelligent Crawl Optimizer — more *useful* data per dollar, not more pages.

A crawl budget is the only resource that really matters: requests, bandwidth,
money and the site's patience all scale with pages fetched, while the value of
those pages is wildly uneven. A pricing table changes daily; an "about" page
has not changed since 2019; a JS-only catalogue is invisible to a plain HTTP
fetch; and a hundred near-duplicate listing pages are worth less than the one
product page they point at.

This module is the decision layer that sits in front of fetching:

* :func:`score_page` — how much is this URL worth, and why (every score carries
  its reasons, so a crawl log can be explained rather than trusted);
* :func:`fetch_strategy` — plain HTTP, a real browser, or skip entirely;
* :func:`next_check_at` — when to come back, derived from observed change
  rate and page importance instead of a fixed interval;
* :func:`plan` — allocate a fixed budget of pages or dollars across a frontier
  by marginal value per cent, with per-host fairness caps.

Everything is a pure function over plain data: no network, no Redis, no clock
side effects beyond an injectable ``now``. That is what makes the behaviour
testable, and what lets the Celery workers and the API agree on it.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from src.cost.simulator import CrawlSpec, PriceBook, estimate

# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
@dataclass
class PageSignals:
    """What the platform knows about one URL before fetching it.

    Every field is optional; missing signals degrade to neutral defaults
    rather than guessing. ``query_overlap`` and ``authority`` normally come
    from the own search index, ``change_rate`` from watch history, and the
    rendering fields from a cheap head fetch or a previous visit.
    """

    url: str
    depth: int = 0
    inlinks: int = 0
    authority: float = 0.0            # 0..1 topic-independent site authority
    query_overlap: float = 0.0        # 0..1 relevance to the current goal
    change_rate: float = 0.0          # observed changes per day
    last_fetched_at: float | None = None
    has_js_app: bool = False          # body is a shell without JS
    needs_browser: bool = False       # previously required a browser
    word_count: int = 0
    is_duplicate: bool = False
    template_noise: float = 0.0       # 0..1 share of boilerplate (nav/legal)
    http_status: int = 0
    language_match: bool = True
    content_type: str = "text/html"
    pages_per_host: int = 0           # how much we already took from this host

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> PageSignals:
        base = cls(url="")
        if not row:
            return base
        allowed = {k: v for k, v in row.items() if hasattr(base, k)}
        return cls(**allowed)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
WEIGHTS = {
    "query_overlap": 0.30,
    "authority": 0.16,
    "change": 0.18,
    "inlinks": 0.10,
    "freshness": 0.08,
    "listing": 0.04,
    "language": 0.04,
    "depth": 0.10,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _change_component(rate: float) -> float:
    """Changes/day → 0..1 with diminishing returns (a page that changes 5×/day
    is not 5× more useful than one that changes daily)."""
    return _clamp(math.log1p(max(0.0, rate)) / math.log1p(7.0))


def _freshness_component(last_fetched_at: float | None, now: float) -> float:
    """Unseen pages score highest, then decay; 30 days stale ≈ half credit."""
    if not last_fetched_at:
        return 1.0
    age_days = max(0.0, (now - last_fetched_at) / 86400.0)
    return _clamp(0.5 ** (age_days / 30.0))


@dataclass
class PageScore:
    url: str
    score: float                      # 0..100
    expected_yield: float             # 0..1 share of the goal this page serves
    value_per_dollar: float           # relative; higher = buy this first
    action: str                       # crawl | defer | skip
    fetch_with: str                   # http | browser
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "score": round(self.score, 2),
            "expected_yield": round(self.expected_yield, 4),
            "value_per_dollar": round(self.value_per_dollar, 5),
            "action": self.action,
            "fetch_with": self.fetch_with,
            "reasons": list(self.reasons),
            "components": {k: round(v, 4) for k, v in self.components.items()},
        }


def fetch_strategy(sig: PageSignals) -> tuple[str, list[str]]:
    """``http`` | ``browser`` plus the reasoning.

    The default is HTTP: a browser costs ~6× more per page and delivers
    nothing on a page whose content is already in the HTML. A browser is
    requested only on positive evidence (a known-shell page, or a previous
    visit that yielded nothing without JS).
    """
    reasons: list[str] = []
    if sig.needs_browser:
        reasons.append("previous visit yielded no content without JS")
        return "browser", reasons
    if sig.has_js_app and sig.word_count and sig.word_count < 250:
        reasons.append("HTML shell detected (<250 words) — content is client-rendered")
        return "browser", reasons
    if sig.has_js_app:
        reasons.append("page mounts a JS app but already ships content")
    return "http", reasons or ["content is present in the HTML — plain fetch is enough"]


def score_page(sig: PageSignals, *, now: float | None = None,
               cost_hint: float = 1.0) -> PageScore:
    """Score one URL 0–100 with an explicit, printable rationale."""
    now = now or time.time()
    reasons: list[str] = []

    if sig.is_duplicate:
        return PageScore(sig.url, 0.0, 0.0, 0.0, "skip", "http",
                         ["content already collected (dedupe hit)"],
                         {"duplicate": 1.0})
    if sig.http_status in (404, 410):
        return PageScore(sig.url, 0.0, 0.0, 0.0, "skip", "http",
                         [f"permanently gone (HTTP {sig.http_status})"],
                         {"status": 1.0})
    if not sig.language_match:
        return PageScore(sig.url, 0.0, 0.0, 0.0, "skip", "http",
                         ["language does not match the goal"], {"language": 0.0})

    depth_c = _clamp(1.0 - sig.depth / 8.0)
    inlink_c = _clamp(math.log1p(max(0, sig.inlinks)) / math.log1p(50))
    change_c = _change_component(sig.change_rate)
    fresh_c = _freshness_component(sig.last_fetched_at, now)
    noise_c = _clamp(1.0 - sig.template_noise)

    components = {
        "query_overlap": _clamp(sig.query_overlap),
        "authority": _clamp(sig.authority),
        "change": change_c,
        "inlinks": inlink_c,
        "freshness": fresh_c,
        "listing": 0.35 if sig.url.rstrip("/").endswith(("/tag", "/category",
                                                          "page=", "/search")) else 1.0,
        "language": 1.0 if sig.language_match else 0.0,
        "depth": depth_c,
    }
    score = sum(WEIGHTS[k] * v for k, v in components.items()) * 100.0 * (0.6 + 0.4 * noise_c)

    # -- readable reasons (the part humans actually read) -------------------- #
    if sig.query_overlap >= 0.6:
        reasons.append(f"strong topical match ({sig.query_overlap:.2f})")
    elif sig.query_overlap < 0.15:
        reasons.append(f"weak topical match ({sig.query_overlap:.2f})")
    if sig.change_rate >= 1.0:
        reasons.append(f"changes often ({sig.change_rate:.1f}×/day) — worth watching")
    elif sig.change_rate:
        reasons.append(f"changes about {sig.change_rate:.2f}×/day")
    if sig.authority >= 0.6:
        reasons.append(f"authoritative host ({sig.authority:.2f})")
    if sig.depth >= 5:
        reasons.append(f"deep in the site (depth {sig.depth})")
    if sig.template_noise >= 0.5:
        reasons.append(f"mostly boilerplate ({sig.template_noise:.0%} nav/legal text)")
    if not sig.last_fetched_at:
        reasons.append("never fetched")
    fetch_with, fetch_reasons = fetch_strategy(sig)
    reasons.extend(fetch_reasons)

    action = "crawl"
    if score < 12:
        action = "skip"
        reasons.append("below the value floor — not worth a request")
    elif score < 30:
        action = "defer"
        reasons.append("low value per request — revisit only if the budget frees up")

    cost = cost_hint * (1.0 if fetch_with == "http" else 6.0)
    expected_yield = _clamp(score / 100.0 * (0.35 + 0.65 * _clamp(sig.query_overlap or 0.5)))
    return PageScore(
        url=sig.url,
        score=score,
        expected_yield=expected_yield,
        value_per_dollar=expected_yield / max(1e-6, cost),
        action=action,
        fetch_with=fetch_with,
        reasons=reasons,
        components=components,
    )


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
def next_check_at(sig: PageSignals, *, now: float | None = None,
                  min_hours: float = 6.0, max_days: float = 45.0) -> float:
    """When to revisit, from observed change rate and importance.

    A page that changes several times a day is worth checking twice a day; a
    static page is worth checking monthly. Importance (authority + relevance)
    shortens the interval; the politeness floor keeps it humane.
    """
    now = now or time.time()
    importance = 0.5 * _clamp(sig.authority) + 0.5 * _clamp(sig.query_overlap or 0.5)
    rate = max(0.0, sig.change_rate)
    if rate <= 0:
        # No observed changes: back off towards the maximum, but keep pages we
        # care about on a real schedule.
        hours = max(min_hours, max_days * 24 * (1.0 - 0.6 * importance))
    else:
        hours = 24.0 / rate
        hours *= (1.5 - 0.5 * importance)          # important pages: shorter
        hours = max(min_hours, min(max_days * 24, hours))
    return now + hours * 3600.0


# --------------------------------------------------------------------------- #
# budget planning
# --------------------------------------------------------------------------- #
@dataclass
class CrawlPlan:
    selected: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    browser_pages: int = 0
    est_usd: float = 0.0
    est_minutes: float = 0.0
    expected_yield: float = 0.0
    value_per_dollar: float = 0.0
    host_caps: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "browser_pages": self.browser_pages,
            "est_usd": round(self.est_usd, 4),
            "est_minutes": round(self.est_minutes, 2),
            "expected_yield": round(self.expected_yield, 4),
            "value_per_dollar": round(self.value_per_dollar, 6),
            "host_caps": dict(sorted(self.host_caps.items())),
            "selected": self.selected,
            "skipped": self.skipped,
            "notes": self.notes,
        }


def plan(signals: Iterable[PageSignals | dict[str, Any]], *,
         budget_pages: int | None = None,
         budget_usd: float | None = None,
         per_host_cap: int | None = None,
         price_book: PriceBook | None = None,
         now: float | None = None,
         max_host_share: float = 0.35) -> CrawlPlan:
    """Choose which URLs to fetch, in what order, under a real budget.

    Greedy by marginal value per dollar, with a per-host cap so one large site
    cannot eat the whole budget ("fair" is also the politeness policy). The
    chosen order is the fetch order — the frontier gets this list, not a
    random sample.
    """
    now = now or time.time()
    rows = [s if isinstance(s, PageSignals) else PageSignals.from_dict(s)
            for s in signals]
    scored = [score_page(s, now=now) for s in rows]
    scored.sort(key=lambda s: (-s.value_per_dollar, -s.score, s.url))

    cap = per_host_cap
    if cap is None and budget_pages:
        cap = max(10, int(budget_pages * max_host_share))
    elif cap is None:
        cap = 10_000

    out = CrawlPlan()
    host_used: dict[str, int] = {}
    chosen_specs: list[CrawlSpec] = []
    notes: list[str] = []

    for s in scored:
        host = _host(s.url)
        if s.action == "skip":
            out.skipped.append({**s.to_dict(), "skip_reason": "below value floor"})
            continue
        if budget_pages is not None and out.pages >= budget_pages:
            out.skipped.append({**s.to_dict(), "skip_reason": "page budget reached"})
            continue
        if host_used.get(host, 0) >= cap:
            out.skipped.append({**s.to_dict(), "skip_reason": f"host cap ({cap}) reached"})
            continue

        one = CrawlSpec(pages=1, render_share=1.0 if s.fetch_with == "browser" else 0.0,
                        proxy_tier="none", keep_snapshots=False,
                        seconds_per_http_page=0.55, seconds_per_browser_page=3.2)
        cost = estimate(one, price_book=price_book).est_usd
        if budget_usd is not None and out.est_usd + cost > budget_usd:
            out.skipped.append({**s.to_dict(), "skip_reason": "dollar budget reached"})
            continue

        host_used[host] = host_used.get(host, 0) + 1
        out.pages += 1
        if s.fetch_with == "browser":
            out.browser_pages += 1
        out.est_usd += cost
        out.expected_yield += s.expected_yield
        chosen_specs.append(one)
        out.selected.append({**s.to_dict(), "cost_usd": round(cost, 6)})

    if out.pages:
        render_share = out.browser_pages / out.pages
        aggregate = estimate(
            CrawlSpec(pages=out.pages, render_share=render_share,
                      keep_snapshots=False, concurrency=min(16, out.pages)),
            price_book=price_book)
        out.est_minutes = aggregate.est_minutes
        out.est_usd = max(out.est_usd, aggregate.est_usd)
        out.value_per_dollar = out.expected_yield / max(1e-9, out.est_usd)
        out.host_caps = host_used
    else:
        notes.append("nothing cleared the value floor — widen the topic or raise the budget")
    if out.skipped:
        notes.append(f"{len(out.skipped)} URLs skipped; each row says why")
    return out


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# frontier integration
# --------------------------------------------------------------------------- #
def frontier_priority(score: PageScore, *, base: float = 0.0) -> float:
    """Map a page score onto the URL frontier's priority scale (higher first).

    The frontier pops by priority, so a score above 50 gets a positive boost
    and a deferred page sinks below fresh seeds without being dropped.
    """
    return base + (score.score - 50.0) / 10.0


def summarise(plan_result: CrawlPlan) -> str:
    """One-line, human summary for logs and the console."""
    return (
        f"{plan_result.pages} pages "
        f"({plan_result.browser_pages} via browser) · "
        f"${plan_result.est_usd:.2f} · {plan_result.est_minutes:.1f} min · "
        f"yield {plan_result.expected_yield:.2f} "
        f"(${plan_result.value_per_dollar:.2f} yield per dollar)"
    )


def top_reasons(scores: Sequence[PageScore], limit: int = 5) -> list[str]:
    """The reasons that kept repeating — what to say in the crawl log."""
    from collections import Counter

    c: Counter[str] = Counter()
    for s in scores:
        for r in s.reasons:
            c[r] += 1
    return [f"{reason} ({n}×)" for reason, n in c.most_common(limit)]
