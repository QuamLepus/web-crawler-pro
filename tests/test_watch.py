"""Tests for Real-Time Web Change Intelligence (``src/watch``).

A monitoring product lives or dies on one behaviour: it must tell you about
*the price change* and stay quiet about the cookie banner. So these tests pin
the classification (price, product, policy, jobs, announcement, cosmetic),
the significance ordering, the silence rules, and the alert dedupe.
"""

from __future__ import annotations

import time

import fakeredis
import pytest

from src.watch import (
    ChangeEvent,
    Watch,
    WatchService,
    classify,
    content_lines,
    count_jobs,
    extract_prices,
    extract_products,
    human_schedule,
    parse_schedule,
    summarise_events,
    template_noise,
)

BEFORE = """
<html><head><title>Shop</title></head><body>
<nav>Home Products Support</nav>
<h1>Our products</h1>
<div class="product"><span>Pixel 9</span><span class="price">$799</span></div>
<div class="product"><span>Pixel 9 Pro</span><span class="price">$999</span></div>
<p>We are hiring: 3 open positions. Apply now.</p>
<footer>© 2026 Shop. All rights reserved. Privacy policy.</footer>
</body></html>
"""

AFTER = """
<html><head><title>Shop</title></head><body>
<nav>Home Products Support</nav>
<h1>Our products</h1>
<div class="product"><span>Pixel 9</span><span class="price">$699</span></div>
<div class="product"><span>Pixel 9 Pro</span><span class="price">$999</span></div>
<div class="product"><span>Pixel 10</span><span class="price">$1099</span></div>
<p>Announcing Pixel 10 — now available with a new camera system.</p>
<p>We are hiring: 7 open positions. Apply now. Join our team — new
vacancies in Dhaka and remote.</p>
<footer>© 2026 Shop. All rights reserved. Privacy policy.</footer>
</body></html>
"""


# --------------------------------------------------------------------------- #
# extraction primitives
# --------------------------------------------------------------------------- #
def test_price_extraction_handles_currencies_and_formats():
    prices = extract_prices("Was $1,299.00 now ৳ 12,999 or EUR 1.199 or ₹45,000")
    keys = set(prices)
    assert "USD:1299" in keys
    assert "BDT:12999" in keys
    assert "EUR:1199" in keys   # 1.199 is European grouping, not 1.19
    assert "INR:45000" in keys


def test_content_lines_drop_navigation_and_legal_noise():
    lines = content_lines(BEFORE)
    joined = " ".join(lines)
    assert "Pixel 9" in joined
    assert "Home Products Support" not in joined
    assert "All rights reserved" not in joined


def test_template_noise_measures_boilerplate_share():
    noisy = "<nav>Home About Contact</nav>" * 10 + "<p>Real content here today.</p>"
    assert template_noise(noisy) > 0.5
    assert template_noise("<p>One two three four five six seven eight.</p>") == 0.0


def test_job_counting_and_product_extraction():
    assert count_jobs(BEFORE) >= 2
    products = extract_products(AFTER)
    assert any("Pixel 10" in name for name in products)


def test_products_read_schema_org_json_ld():
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Pixel 10 Pro","offers":{"price":"1099","priceCurrency":"USD"}}
    </script>"""
    products = extract_products(html)
    assert products.get("Pixel 10 Pro") == "1099"


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_price_change_is_classified_with_magnitude():
    events = classify(BEFORE, AFTER, url="https://shop.test")
    kinds = {e.kind for e in events}
    assert "price_change" in kinds
    price = next(e for e in events if e.kind == "price_change")
    assert price.before["low"] == 799.0
    assert price.after["low"] == 699.0
    assert price.delta["pct"] == pytest.approx(-12.5, abs=0.1)
    assert price.significance > 0.3


def test_new_product_and_announcement_and_jobs_are_detected():
    events = classify(BEFORE, AFTER)
    kinds = {e.kind for e in events}
    assert "new_product" in kinds
    assert "announcement" in kinds
    assert "jobs_change" in kinds


def test_policy_change_is_detected_separately():
    before = "<p>Our privacy policy says we keep data for 30 days.</p>"
    after = "<p>Our privacy policy says we keep data for 365 days and share it.</p>"
    events = classify(before, after, kinds=["policy_change"])
    assert [e.kind for e in events] == ["policy_change"]
    assert events[0].significance >= 0.5


def test_whitespace_only_change_is_cosmetic_and_never_significant():
    events = classify(BEFORE, BEFORE.replace("</h1>", "  </h1>\n\n"))
    assert events == []          # identical content lines: complete silence
    events = classify(BEFORE, BEFORE.replace("Pixel 9</span>", "Pixel 9 </span>"))
    assert all(e.kind == "cosmetic" and e.significance < 0.1 for e in events)


def test_no_change_returns_nothing():
    assert classify(BEFORE, BEFORE) == []
    assert classify("", "") == []


def test_event_kind_filter_narrows_output():
    only_prices = classify(BEFORE, AFTER, kinds=["price_change"])
    assert {e.kind for e in only_prices} == {"price_change"}


def test_event_round_trips_through_dict():
    event = classify(BEFORE, AFTER)[0]
    restored = ChangeEvent.from_dict(event.to_dict())
    assert restored.kind == event.kind
    assert restored.significance == event.significance


# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("30m", 1800), ("1h", 3600), ("6h", 21600), ("1d", 86400), ("15m", 900),
    ("every 2 hours", 7200), ("45 minutes", 2700), (600, 600), (None, 3600),
    ("nonsense", 3600), ("1s", 60),
])
def test_parse_schedule(text, expected):
    assert parse_schedule(text) == expected


def test_human_schedule_round_trips():
    assert human_schedule(3600) == "1h"
    assert human_schedule(900) == "15m"
    assert human_schedule(172800) == "2d"


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
class FakeFetcher:
    """Serves a scripted sequence of bodies per URL (no network, ever)."""

    def __init__(self) -> None:
        self.responses: dict[str, list[str]] = {}
        self.calls: list[str] = []

    def add(self, url: str, *bodies: str) -> None:
        self.responses.setdefault(url, []).extend(bodies)

    def __call__(self, url: str, **kwargs):
        self.calls.append(url)
        queue = self.responses.get(url, [])
        body = queue.pop(0) if queue else (queue[-1] if queue else "")
        return (200, body, {"content-type": "text/html"})


@pytest.fixture()
def service(tmp_path):
    fetcher = FakeFetcher()
    emitted: list[tuple] = []
    service = WatchService(
        fakeredis.FakeStrictRedis(decode_responses=True),
        snapshots=__import__("src.provenance.snapshots", fromlist=["SnapshotStore"])
        .SnapshotStore(tmp_path / "snap"),
        fetcher=fetcher,
        emit=lambda tenant, event, payload: emitted.append((tenant, event, payload)),
    )
    service.fetcher_script = fetcher       # convenience for tests
    service.emitted = emitted              # type: ignore[attr-defined]
    return service


def test_first_run_is_a_baseline_not_a_change(service):
    service.fetcher_script.add("https://shop.test/p", BEFORE, AFTER)
    watch = service.create("t1", "Shop prices", ["https://shop.test/p"],
                           schedule="15m", adaptive=False)
    first = service.run("t1", watch.watch_id)
    assert first["events"] == []
    assert first["checked"] == 1

    second = service.run("t1", watch.watch_id)
    kinds = {e["kind"] for e in second["events"]}
    assert "price_change" in kinds
    assert second["notified"] >= 1
    assert service.emitted                     # the webhook fired


def test_events_are_stored_and_queryable(service):
    service.fetcher_script.add("https://shop.test/p", BEFORE, AFTER)
    watch = service.create("t1", "Shop", ["https://shop.test/p"], adaptive=False)
    service.run("t1", watch.watch_id)
    service.run("t1", watch.watch_id)

    events = service.events("t1", watch_id=watch.watch_id, limit=20)
    assert events
    prices = service.events("t1", kinds=["price_change"], limit=10)
    assert prices and all(e.kind == "price_change" for e in prices)
    stats = service.stats("t1")
    assert stats["watches"] == 1 and stats["runs"] == 2
    assert stats["by_kind"].get("price_change", 0) >= 1


def test_alerts_are_deduplicated_within_the_window(service):
    service.fetcher_script.add("https://shop.test/p", BEFORE, AFTER, AFTER)
    watch = service.create("t1", "Shop", ["https://shop.test/p"], adaptive=False)
    service.run("t1", watch.watch_id)          # baseline
    service.run("t1", watch.watch_id)          # first detection -> notified
    first_notified = len(service.emitted)
    service.run("t1", watch.watch_id)          # same content again -> quiet
    assert len(service.emitted) == first_notified


def test_low_significance_changes_are_stored_but_not_notified(service):
    original = ("<p>" + "One sentence of body text. " * 20 + "</p>"
                "<p>Second paragraph, unchanged.</p>")
    rewritten = ("<p>" + "One sentence of body text. " * 20 + "</p>"
                 "<p>Second paragraph, unchanged.</p>"
                 "<p>A third paragraph appeared here.</p>"
                 "<p>Another line of new prose nobody had before.</p>"
                 "<p>A fourth sentence with new details.</p>")
    service.fetcher_script.add("https://blog.test/", original, rewritten)
    watch = service.create("t1", "Blog", ["https://blog.test/"],
                           min_significance=0.9, adaptive=False)
    service.run("t1", watch.watch_id)
    result = service.run("t1", watch.watch_id)
    assert result["events"]            # stored
    assert result["notified"] == 0     # but silent
    assert service.emitted == []


def test_adaptive_schedule_stretches_when_nothing_changes(service):
    service.fetcher_script.add("https://static.test/", "<p>same</p>", "<p>same</p>",
                               "<p>same</p>")
    watch = service.create("t1", "Static", ["https://static.test/"], schedule="1h")
    service.run("t1", watch.watch_id)
    second = service.run("t1", watch.watch_id)
    third = service.run("t1", watch.watch_id)
    assert third["next_run_at"] >= second["next_run_at"]


def test_paused_watch_is_skipped_unless_forced(service):
    service.fetcher_script.add("https://a.test/", "<p>a</p>")
    watch = service.create("t1", "A", ["https://a.test/"])
    service.update("t1", watch.watch_id, state="paused")
    result = service.run("t1", watch.watch_id)
    assert result["skipped"] == "paused"
    forced = service.run("t1", watch.watch_id, force=True)
    assert "skipped" not in forced


def test_due_lists_only_active_watches_that_are_due(service):
    service.fetcher_script.add("https://a.test/", "<p>a</p>")
    watch = service.create("t1", "A", ["https://a.test/"], adaptive=False)
    due = service.due(limit=10)
    assert [w.watch_id for w in due] == [watch.watch_id]
    service.run("t1", watch.watch_id)
    assert service.due(limit=10) == [] or all(
        w.watch_id != watch.watch_id for w in service.due(limit=10))


def test_fetch_failures_are_reported_not_raised(service):
    def broken(url, **kwargs):
        raise RuntimeError("no route to host")

    service.fetcher = broken
    watch = service.create("t1", "Broken", ["https://broken.test/"], adaptive=False)
    result = service.run("t1", watch.watch_id)
    assert result["checked"] == 0
    assert result["failures"] and "no route to host" in result["failures"][0]
    stored = service.get("t1", watch.watch_id)
    assert stored.failures == 1


def test_watch_requires_targets(service):
    with pytest.raises(ValueError):
        service.create("t1", "empty", [])


def test_run_writes_provenance_for_every_fetched_page(service):
    service.fetcher_script.add("https://shop.test/p", BEFORE)
    watch = service.create("t1", "Shop", ["https://shop.test/p"], adaptive=False)
    service.run("t1", watch.watch_id)
    records = service.ledger.for_dataset("t1", f"watch:{watch.watch_id}")
    assert len(records) == 1
    assert records[0].source_url == "https://shop.test/p"
    assert records[0].snapshot_id.startswith("sha256:")


def test_watch_dict_and_summary_helpers(service):
    watch = Watch(name="w", tenant="t1", targets=[], schedule_seconds=900)
    payload = watch.to_dict()
    assert payload["schedule"] == "15m"
    events = [ChangeEvent(kind="price_change", summary="x", significance=0.8),
              ChangeEvent(kind="cosmetic", summary="y", significance=0.02)]
    summary = summarise_events(events)
    assert summary["events"] == 2 and summary["significant"] == 1
