"""Change detection over web pages — the part that makes monitoring valuable.

"Something on the page changed" is noise. "The Pro plan went from $49 to $59
on Tuesday" is a business event, and it is the thing people will pay a
subscription for. So this module classifies a diff into typed, queryable
events with a significance score:

===========================  ==========================================
kind                          what matched
===========================  ==========================================
``price_change``              currency amounts with before/after values
``new_product`` / ``removed``  product-shaped rows appearing or vanishing
``announcement``              headline/hero/intro text changed
``policy_change``             privacy/terms/refund/SLA wording changed
``jobs_change``               careers/job-posting count moved
``content_update``            substantial main-text change
``cosmetic``                  nav, footer, timestamps, whitespace only
===========================  ==========================================

Everything is heuristic on purpose: it runs inside the crawler with no model
download, it is deterministic (so the same two snapshots always classify the
same way, which makes the tests meaningful), and a language model can be
layered on top later for the cases the heuristics label ``content_update``.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

EVENT_KINDS = (
    "price_change",
    "new_product",
    "removed_product",
    "announcement",
    "policy_change",
    "jobs_change",
    "content_update",
    "cosmetic",
)

# significance bands, used by alerting thresholds and the console filter
SIGNIFICANCE = {
    "price_change": (0.35, 1.0),
    "new_product": (0.45, 0.9),
    "removed_product": (0.4, 0.85),
    "policy_change": (0.5, 0.95),
    "announcement": (0.3, 0.8),
    "jobs_change": (0.25, 0.7),
    "content_update": (0.15, 0.6),
    "cosmetic": (0.0, 0.08),
}

# --------------------------------------------------------------------------- #
# extraction primitives
# --------------------------------------------------------------------------- #
_CURRENCY = {
    "$": "USD", "us$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "₹": "INR", "inr": "INR",
    "৳": "BDT", "tk": "BDT", "bdt": "BDT",
    "¥": "JPY", "jpy": "JPY",
}

# A money-shaped number, with both grouping conventions accepted. The naive
# pattern (``\d+(?:\.\d{1,2})?``) silently turns the European "1.199" into
# 1.19, which is the kind of bug a price monitor cannot afford.
_NUM = (r"(?:\d{1,3}(?:[,\s]\d{3})+(?:[.]\d{1,2})?"
        r"|\d{1,3}(?:[.,]\d{3})+(?:,\d{1,2})?"
        r"|\d+(?:[.,]\d{1,2})?)")

_PRICE_SYMBOLS = "$€£₹৳¥"
_PRICE_WORDS = "usd|eur|gbp|inr|bdt|jpy|tk"

_PRICE_BEFORE = re.compile(
    r"(?P<cur>us\$|[$€£₹৳¥]|\b(?:usd|eur|gbp|inr|bdt|jpy|tk)\b)\s*"
    rf"(?P<num>{_NUM})(?![\d])",
    re.IGNORECASE,
)
# Number-then-currency ("12,999 ৳"). The lookahead is what stops "Pixel 9 $799"
# from yielding a phantom "$9" price.
_PRICE_AFTER = re.compile(
    rf"(?<![\w.,])(?P<num>{_NUM})\s?(?P<cur>us\$|[$€£₹৳¥]|\b(?:usd|eur|gbp|inr|bdt|jpy|tk)\b)"
    r"(?![\d])",
    re.IGNORECASE,
)

_POLICY_TERMS = (
    "privacy policy", "terms of service", "terms & conditions", "terms and conditions",
    "cookie policy", "refund policy", "data processing", "gdpr", "ccpa", "sla",
    "acceptable use", "subprocessor", "data retention",
)
_JOB_TERMS = ("we're hiring", "we are hiring", "apply now", "open positions",
              "job opening", "join our team", "vacancies", "hiring")
_ANNOUNCE_TERMS = ("announcing", "introducing", "launching", "now available",
                   "we're excited", "we are excited", "new release", "generally available")

# lines that exist on every page and mean nothing — removed before diffing
_NOISE_PATTERNS = (
    re.compile(r"^\s*(cookie|cookies)\b", re.I),
    re.compile(r"^\s*©|^\s*\(c\)\s*\d{4}", re.I),
    re.compile(r"^\s*all rights reserved", re.I),
    re.compile(r"last (updated|modified|reviewed)\s*:?\s*", re.I),
    re.compile(r"^\s*(share|follow|subscribe|newsletter|copyright)\b", re.I),
    re.compile(r"^\s*[\w\s]{0,40}(privacy|terms|cookie)\s*(policy|notice|preferences)\s*$", re.I),
    re.compile(r"^\s*(sign in|log in|login|register|menu|search|home)\s*$", re.I),
)
_NOISE_RE = re.compile("|".join(p.pattern for p in _NOISE_PATTERNS), re.I)

#: Words that only ever appear in site chrome. A line made *entirely* of these
#: is navigation, not content — this is what keeps a redesign of the menu from
#: looking like an article rewrite.
_NAV_WORDS = {
    "home", "about", "us", "contact", "products", "product", "services",
    "service", "support", "pricing", "price", "plans", "docs", "documentation",
    "blog", "news", "careers", "career", "jobs", "login", "log", "sign", "in",
    "up", "register", "signup", "account", "menu", "search", "cart", "basket",
    "checkout", "help", "faq", "resources", "company", "team", "press",
    "partners", "legal", "privacy", "terms", "cookies", "sitemap", "more",
    "next", "previous", "prev", "back", "share", "follow", "subscribe", "all",
    "language", "english", "en", "get", "started", "download", "app", "store",
    "shop", "homepage", "overview", "features", "solutions", "customers",
    "community", "events", "status", "security", "integrations", "api",
}


def is_navigation(line: str) -> bool:
    """True when a line is only chrome words (and short)."""
    words = [w.strip(".,:;|·•-–—!?()[]").lower() for w in line.split()]
    words = [w for w in words if w]
    if not words or len(words) > 12:
        return False
    return all(w in _NAV_WORDS or w.isdigit() for w in words)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)
_WS_RE = re.compile(r"[ \t\u00a0]+")


def html_to_lines(html: str) -> list[str]:
    """Cheap HTML → cleaned text lines (no parser needed, works on fragments).

    Block-level tags become newlines so list items, table rows and headings
    stay separable, which is what price and product detection need.
    """
    if not html:
        return []
    text = _SCRIPT_RE.sub(" ", html)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(
        r"</?(?:p|div|li|tr|h[1-6]|br|section|article|header|footer|nav|ul|ol|"
        r"aside|form|select|label|td|th|dt|dd|blockquote|pre)\b[^>]*>",
        "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    # entities: the handful that matter for money and quotes
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"'),
                         ("&pound;", "£"), ("&euro;", "€")):
        text = text.replace(entity, char)
    out: list[str] = []
    for raw in text.splitlines():
        line = _WS_RE.sub(" ", raw).strip()
        if len(line) < 2:
            continue
        out.append(line)
    return out


def content_lines(html_or_text: str) -> list[str]:
    """Lines worth diffing: noise, nav and legal boilerplate removed."""
    lines = html_to_lines(html_or_text)
    kept = [ln for ln in lines if not _NOISE_RE.search(ln) and not is_navigation(ln)]
    # Deduplicate *consecutive* repeats (shared headers/footers repeat wholesale)
    deduped: list[str] = []
    for ln in kept:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)
    return deduped


def template_noise(html: str) -> float:
    """0..1 — the share of text lines that look like chrome rather than content."""
    lines = html_to_lines(html)
    if not lines:
        return 0.0
    noisy = sum(1 for ln in lines
                if _NOISE_RE.search(ln) or is_navigation(ln) or len(ln.split()) < 3)
    return round(noisy / len(lines), 3)


def _parse_amount(raw: str) -> float | None:
    """Parse "1,299.00", "12 999", "1.199" and "1.199,50" correctly.

    Separator conventions differ by locale, and guessing wrong by a factor of a
    thousand is a catastrophic bug in a price monitor. The rules used here are
    the conservative ones: if both separators appear, the *last* one is the
    decimal separator; if only dots appear in strict groups of three, they are
    thousands separators.
    """
    text = (raw or "").strip().replace(" ", "").replace("\u00a0", "")
    if not text:
        return None
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma and re.fullmatch(r"\d{1,3}(,\d{3})+", text):
        text = text.replace(",", "")
    elif has_dot and re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    else:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def extract_prices(html_or_text: str) -> dict[str, float]:
    """``{'USD:1299.0': ...}``-style map of the amounts on a page.

    Keyed by currency + normalised amount so the *same* price found twice
    (once in a schema block, once in the rendered table) collapses to one
    entry instead of looking like a change.
    """
    found: dict[str, float] = {}
    text = html_or_text or ""
    for pattern in (_PRICE_BEFORE, _PRICE_AFTER):
        for m in pattern.finditer(text):
            cur = _CURRENCY.get(m.group("cur").strip().lower().replace(" ", ""))
            if not cur:
                continue
            value = _parse_amount(m.group("num"))
            if value is None:
                continue
            if value <= 0 or value > 100_000_000:
                continue
            found[f"{cur}:{value:g}"] = value
    return found


def price_tuples(html_or_text: str) -> list[tuple[str, float]]:
    out = []
    for key in extract_prices(html_or_text):
        cur, _, val = key.partition(":")
        out.append((cur, float(val)))
    return sorted(out)


def extract_products(html: str) -> dict[str, str]:
    """Product-ish rows: ``name -> price or ''``.

    Looks at JSON-LD ``Product`` blocks first (most commerce sites emit them),
    then falls back to label/price line pairs.
    """
    products: dict[str, str] = {}
    for chunk in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>",
                            html or "", flags=re.I | re.S):
        try:
            import json

            data = json.loads(chunk.strip())
        except Exception:  # noqa: BLE001 - malformed JSON-LD is extremely common
            continue
        for node in _walk_json(data):
            if not isinstance(node, dict):
                continue
            if str(node.get("@type", "")).lower() != "product":
                continue
            name = str(node.get("name", "")).strip()
            if not name:
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = ""
            if isinstance(offers, dict):
                price = str(offers.get("price") or offers.get("lowPrice") or "")
            products[name[:160]] = price

    lines = content_lines(html)
    for i, line in enumerate(lines):
        if len(line) < 4:
            continue
        value = _first_price_on_line(line)
        if not value:
            continue
        cur, _, amount = value.partition(":")
        label = _name_before_price(line)
        if not label and i:
            previous = _WS_RE.sub(" ", lines[i - 1]).strip()
            if 3 <= len(previous) <= 120 and not extract_prices(previous):
                label = previous
        if 3 <= len(label) <= 120 and not _NAME_ONLY_RE.fullmatch(label):
            products.setdefault(label, f"{cur} {amount}")
    return products


_NAME_ONLY_RE = re.compile(r"^[\d\s.,]+$")


def _first_price_on_line(line: str) -> str:
    """The first currency amount on a line, in ``CUR:amount`` form."""
    best: tuple[int, str] | None = None
    for pattern in (_PRICE_BEFORE, _PRICE_AFTER):
        match = pattern.search(line)
        if not match:
            continue
        cur = _CURRENCY.get(match.group("cur").strip().lower().replace(" ", ""))
        if not cur:
            continue
        amount = _parse_amount(match.group("num"))
        if amount is None or amount <= 0:
            continue
        if best is None or match.start() < best[0]:
            best = (match.start(), f"{cur}:{amount:g}")
    return best[1] if best else ""


def _name_before_price(line: str) -> str:
    """Product-ish text preceding the first amount on a line ("Pixel 10   $1099")."""
    starts = [m.start() for pattern in (_PRICE_BEFORE, _PRICE_AFTER)
              for m in [pattern.search(line)] if m]
    if not starts:
        return ""
    prefix = line[: min(starts)].strip(" -:|·•\t")
    prefix = _WS_RE.sub(" ", prefix).strip()
    if 3 <= len(prefix) <= 120 and any(c.isalpha() for c in prefix):
        return prefix
    return ""


def _walk_json(node: Any) -> Iterable[Any]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_json(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json(v)


def count_jobs(html: str) -> int:
    text = (html or "").lower()
    n = sum(text.count(term) for term in _JOB_TERMS)
    n += len(re.findall(r"href=[\"'][^\"']*/(?:careers?|jobs?)/[^\"']*[\"']", html or "",
                        flags=re.I))
    return n


def policy_fingerprint(html_or_text: str) -> str:
    """A stable hash of the policy-ish sentences on a page."""
    text = (html_or_text or "").lower()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
                 if any(t in s for t in _POLICY_TERMS)]
    blob = "\n".join(sorted(set(sentences)))[:20_000]
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def announcements(html_or_text: str) -> list[str]:
    """Lines that read like an announcement (hero/headline/intro)."""
    out = []
    for line in content_lines(html_or_text):
        low = line.lower()
        if any(term in low for term in _ANNOUNCE_TERMS) and 12 <= len(line) <= 220:
            out.append(line)
    return out[:10]


# --------------------------------------------------------------------------- #
# the event
# --------------------------------------------------------------------------- #
@dataclass
class ChangeEvent:
    kind: str
    summary: str
    significance: float
    url: str = ""
    watch_id: str = ""
    tenant: str = ""
    event_id: str = ""
    detected_at: float = field(default_factory=time.time)
    before: Any = None
    after: Any = None
    delta: Any = None
    details: dict[str, Any] = field(default_factory=dict)
    snapshot_id: str = ""
    prev_snapshot_id: str = ""
    provenance_id: str = ""
    notified: bool = False

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = "ev_" + uuid.uuid4().hex[:18]
        if self.kind not in EVENT_KINDS:
            self.kind = "content_update"
        self.significance = round(max(0.0, min(1.0, float(self.significance))), 4)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["detected_at"] = round(float(self.detected_at), 3)
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ChangeEvent:
        base = cls(kind=row.get("kind", "content_update"),
                   summary=row.get("summary", ""),
                   significance=float(row.get("significance", 0.0)))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        base.__post_init__()
        return base


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def classify(before: str, after: str, *, url: str = "", watch_id: str = "",
             tenant: str = "", kinds: Iterable[str] | None = None,
             now: float | None = None) -> list[ChangeEvent]:
    """Diff two page snapshots into typed, scored events.

    Returns an empty list when nothing meaningful changed — silence is a
    feature: the alert that fires on every whitespace change gets muted, and a
    muted alert is worse than no alert.
    """
    wanted = set(kinds) if kinds else set(EVENT_KINDS)
    events: list[ChangeEvent] = []
    now = now or time.time()

    if (before or "") == (after or ""):
        return events

    before_lines = content_lines(before or "")
    after_lines = content_lines(after or "")
    before_text = "\n".join(before_lines)
    after_text = "\n".join(after_lines)

    if before_text == after_text:
        return _maybe_cosmetic(before, after, url, watch_id, tenant, wanted, now)

    # -------- prices -------------------------------------------------------- #
    if "price_change" in wanted:
        events.extend(_price_events(before_text, after_text, url, watch_id,
                                    tenant, now))

    # -------- products ------------------------------------------------------ #
    if wanted & {"new_product", "removed_product"}:
        events.extend(_product_events(before, after, url, watch_id, tenant, now))

    # -------- policy -------------------------------------------------------- #
    if "policy_change" in wanted:
        fp_before, fp_after = policy_fingerprint(before_text), policy_fingerprint(after_text)
        if fp_before != fp_after:
            hits = sorted({t for t in _POLICY_TERMS
                           if t in before_text.lower() or t in after_text.lower()})
            events.append(ChangeEvent(
                kind="policy_change", url=url, watch_id=watch_id, tenant=tenant,
                summary="Policy wording changed"
                        + (f" ({', '.join(hits[:3])})" if hits else ""),
                significance=0.62, detected_at=now,
                details={"terms": hits, "fingerprint": [fp_before, fp_after]}))

    # -------- jobs ---------------------------------------------------------- #
    if "jobs_change" in wanted:
        jb, ja = count_jobs(before), count_jobs(after)
        if jb != ja and abs(jb - ja) >= 1:
            direction = "more" if ja > jb else "fewer"
            events.append(ChangeEvent(
                kind="jobs_change", url=url, watch_id=watch_id, tenant=tenant,
                summary=f"{abs(ja - jb)} {direction} hiring mentions on the page",
                significance=0.34 + min(0.3, abs(ja - jb) / 20.0), detected_at=now,
                before=jb, after=ja, delta=ja - jb))

    # -------- announcements ------------------------------------------------- #
    if "announcement" in wanted:
        ann_before, ann_after = set(announcements(before_text)), set(announcements(after_text))
        fresh = [a for a in ann_after - ann_before if len(a) > 20]
        if fresh:
            events.append(ChangeEvent(
                kind="announcement", url=url, watch_id=watch_id, tenant=tenant,
                summary=fresh[0][:200],
                significance=0.5, detected_at=now,
                after=fresh[:3], details={"count": len(fresh)}))

    # -------- substantial content ------------------------------------------ #
    if "content_update" in wanted:
        add, removed = _line_diff(before_lines, after_lines)
        if len(add) >= 3 or len(removed) >= 3:
            ratio = difflib.SequenceMatcher(None, before_text, after_text).ratio()
            existing_kinds = {e.kind for e in events}
            # If we already found a specific kind, do not also cry "content"
            events.append(ChangeEvent(
                kind="content_update", url=url, watch_id=watch_id, tenant=tenant,
                summary=f"{len(add)} lines added, {len(removed)} removed "
                        f"({ratio:.0%} similar)",
                significance=round(max(0.15, 0.55 * (1.0 - ratio)), 4),
                detected_at=now,
                after=add[:5], before=removed[:5],
                details={"added": len(add), "removed": len(removed),
                         "similarity": round(ratio, 4),
                         "also": sorted(existing_kinds)}))

    if not events:
        events = _maybe_cosmetic(before, after, url, watch_id, tenant, wanted, now)
    return events


def _price_events(before: str, after: str, url: str, watch_id: str, tenant: str,
                  now: float) -> list[ChangeEvent]:
    out: list[ChangeEvent] = []
    pb, pa = extract_prices(before), extract_prices(after)
    for currency in sorted({k.split(":")[0] for k in set(pb) | set(pa)}):
        old = sorted(v for k, v in pb.items() if k.startswith(currency + ":"))
        new = sorted(v for k, v in pa.items() if k.startswith(currency + ":"))
        if not old or not new:
            continue
        up = max(new) > max(old)
        down = min(new) < min(old)
        if not (up or down):
            continue
        old_lo, new_lo = min(old), min(new)
        pct = ((new_lo - old_lo) / old_lo * 100.0) if old_lo else 0.0
        direction = "up" if pct > 0 else ("down" if pct < 0 else "changed")
        # A 1% wobble is rounding on a marketplace; 40% is a business event.
        magnitude = min(1.0, abs(pct) / 40.0)
        out.append(ChangeEvent(
            kind="price_change", url=url, watch_id=watch_id, tenant=tenant,
            detected_at=now,
            summary=(f"{currency} price {direction}: "
                     f"{old_lo:,.2f} → {new_lo:,.2f} ({pct:+.1f}%)"),
            significance=round(0.3 + 0.6 * magnitude, 4),
            before={"currency": currency, "values": old, "low": old_lo, "high": max(old)},
            after={"currency": currency, "values": new, "low": new_lo, "high": max(new)},
            delta={"abs": round(new_lo - old_lo, 4), "pct": round(pct, 3)}))
    return out


def _product_events(before: str, after: str, url: str, watch_id: str, tenant: str,
                    now: float) -> list[ChangeEvent]:
    out: list[ChangeEvent] = []
    pb, pa = extract_products(before), extract_products(after)
    if not pa and not pb:
        return out
    added = [k for k in pa if k not in pb]
    removed = [k for k in pb if k not in pa]
    if added:
        out.append(ChangeEvent(
            kind="new_product", url=url, watch_id=watch_id, tenant=tenant,
            summary=f"{len(added)} new product(s): " + ", ".join(added[:3]),
            significance=min(0.9, 0.5 + 0.08 * len(added)), detected_at=now,
            after=[{"name": a, "price": pa[a]} for a in added[:20]],
            details={"count": len(added)}))
    if removed:
        out.append(ChangeEvent(
            kind="removed_product", url=url, watch_id=watch_id, tenant=tenant,
            summary=f"{len(removed)} product(s) disappeared: " + ", ".join(removed[:3]),
            significance=min(0.85, 0.45 + 0.08 * len(removed)), detected_at=now,
            before=[{"name": r, "price": pb[r]} for r in removed[:20]],
            details={"count": len(removed)}))
    return out


def _line_diff(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(after[j1:j2])
        if tag in ("replace", "delete"):
            removed.extend(before[i1:i2])
    return [a for a in added if len(a) > 3], [r for r in removed if len(r) > 3]


def _maybe_cosmetic(before: str, after: str, url: str, watch_id: str, tenant: str,
                    wanted: set[str], now: float) -> list[ChangeEvent]:
    """Classify as cosmetic only when the *content* is provably unchanged."""
    if "cosmetic" not in wanted:
        return []
    bl = content_lines(before or "")
    al = content_lines(after or "")
    if bl == al:
        return []
    ratio = difflib.SequenceMatcher(None, "\n".join(bl), "\n".join(al)).ratio()
    if ratio > 0.97:
        add, removed = _line_diff(bl, al)
        return [ChangeEvent(
            kind="cosmetic", url=url, watch_id=watch_id, tenant=tenant,
            summary="only boilerplate/whitespace changed",
            significance=0.03, detected_at=now,
            after=add[:3], before=removed[:3],
            details={"similarity": round(ratio, 4)})]
    return []


def highest(events: Iterable[ChangeEvent]) -> ChangeEvent | None:
    best = None
    for e in events:
        if best is None or e.significance > best.significance:
            best = e
    return best
