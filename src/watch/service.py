"""Web Monitoring engine: fetch → snapshot → classify → alert.

The loop is small on purpose, and every side effect is injected:

    fetch (or replay)  →  diff  →  typed events  →  provenance record
                                     │
                                     ├── webhook / email / API
                                     └── adaptive schedule for the next run

Two properties matter more than features here.

**Silence is correct.** A monitor that pages you about a cookie banner gets
muted within a week, so events below ``min_significance`` are stored but never
notified, and identical events inside the dedupe window are collapsed.

**Every change is provable.** Each run writes a snapshot (content-addressed),
a provenance record, and the event carries both ids — so "the price changed on
Tuesday" can be traced to the exact bytes that said so, months later.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any, Callable, Iterable, Sequence

from src.provenance.ledger import ProvenanceLedger, record_for_page
from src.provenance.snapshots import SnapshotStore
from src.watch.diff import EVENT_KINDS, ChangeEvent, classify, template_noise
from src.watch.models import Watch, WatchTarget, human_schedule, parse_schedule

logger = logging.getLogger("wcp.watch")

DEFAULT_KINDS = ["price_change", "new_product", "removed_product",
                 "policy_change", "announcement", "jobs_change", "content_update"]
ALERT_KINDS = ("price_change", "new_product", "removed_product", "policy_change",
               "announcement")

#: dedupe window (seconds) for identical alerts
DEDUPE_WINDOW = 3600


class FetchError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# default fetcher (offline-safe, injectable)
# --------------------------------------------------------------------------- #
def default_fetcher(url: str, *, timeout: float = 20.0) -> tuple[int, str, dict[str, str]]:
    """Fetch a page with plain HTTP.

    Returns ``(status, body, headers)``. Imported lazily and guarded so that a
    deployment without the networking stack (and the whole test suite) never
    reaches for the network by accident: offline mode returns a synthetic 503
    *error*, never fake content.
    """
    import os

    if os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes"):
        return 503, "", {"x-wcp-offline": "1"}
    try:
        import requests

        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "WebIntelligenceOS/1.0 (+monitoring; polite)",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        })
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "json" not in ctype and "text" not in ctype:
            raise FetchError(f"unsupported content type {ctype!r}")
        return resp.status_code, resp.text, dict(resp.headers)
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - network stack is optional
        raise FetchError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
class WatchService:
    """Tenant-scoped monitor store + runner."""

    def __init__(self, redis_client, *, ledger: ProvenanceLedger | None = None,
                 snapshots: SnapshotStore | None = None,
                 fetcher: Callable[..., tuple[int, str, dict[str, str]]] | None = None,
                 emit: Callable[[str, str, dict], Any] | None = None,
                 now: Callable[[], float] | None = None) -> None:
        self.r = redis_client
        self.ledger = ledger or ProvenanceLedger(redis_client)
        self.snapshots = snapshots or SnapshotStore()
        self.fetcher = fetcher or default_fetcher
        # emit(tenant, event_type, payload) — wired to the webhook service by
        # the API container; None means "store events, notify nobody".
        self.emit = emit
        self._now = now or time.time

    # -- keys ---------------------------------------------------------------- #
    @staticmethod
    def _k(tenant: str, *parts: str) -> str:
        return ":".join(["wcp:watch", tenant or "public", *parts])

    @staticmethod
    def _dec(raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- CRUD ---------------------------------------------------------------- #
    def create(self, tenant: str, name: str, urls: Sequence[str] | Sequence[dict],
               *, goal: str = "", kinds: Sequence[str] | None = None,
               schedule: str | int = "1h", min_significance: float = 0.35,
               notify_webhook: bool = True, adaptive: bool = True) -> Watch:
        targets = [u if isinstance(u, WatchTarget) else WatchTarget.from_dict(u)
                   for u in ({"url": u} if isinstance(u, str) else u for u in urls)]
        if not targets:
            raise ValueError("a watch needs at least one URL")
        kinds_clean = [k for k in (kinds or DEFAULT_KINDS) if k in EVENT_KINDS]
        watch = Watch(
            name=name.strip()[:160] or "Untitled watch",
            tenant=tenant,
            goal=goal.strip(),
            targets=targets,
            kinds=kinds_clean or DEFAULT_KINDS,
            schedule_seconds=parse_schedule(schedule),
            min_significance=max(0.0, min(1.0, float(min_significance))),
            notify_webhook=notify_webhook,
            adaptive=adaptive,
            next_run_at=self._now(),          # first run immediately
        )
        self._save(watch)
        logger.info("watch %s created (%s targets, every %s)",
                    watch.watch_id, len(watch.targets),
                    human_schedule(watch.schedule_seconds))
        return watch

    def _save(self, watch: Watch) -> None:
        pipe = self.r.pipeline()
        pipe.set(self._k(watch.tenant, "w", watch.watch_id),
                 json.dumps(watch.to_dict(), separators=(",", ":"), default=str))
        pipe.zadd(self._k(watch.tenant, "index"),
                  {watch.watch_id: float(watch.created_at)})
        pipe.zadd(self._k("_all", "due"),
                  {f"{watch.tenant}:{watch.watch_id}": float(watch.next_run_at)})
        pipe.execute()

    def get(self, tenant: str, watch_id: str) -> Watch | None:
        raw = self.r.get(self._k(tenant, "w", watch_id))
        if not raw:
            return None
        return Watch.from_dict(json.loads(raw))

    def list(self, tenant: str, *, state: str | None = None,
             limit: int = 100) -> list[Watch]:
        ids = [self._dec(i) for i in self.r.zrevrange(self._k(tenant, "index"),
                                                      0, max(0, limit - 1))]
        out = [w for w in (self.get(tenant, i) for i in ids) if w is not None]
        if state:
            out = [w for w in out if w.state == state]
        return out

    def update(self, tenant: str, watch_id: str, **fields) -> Watch | None:
        watch = self.get(tenant, watch_id)
        if watch is None:
            return None
        for key, value in fields.items():
            if key == "schedule":
                watch.schedule_seconds = parse_schedule(value)
            elif key == "urls" and value:
                watch.targets = [WatchTarget(url=u) if isinstance(u, str) else
                                 WatchTarget.from_dict(u) for u in value]
            elif hasattr(watch, key) and value is not None:
                setattr(watch, key, value)
        watch.__post_init__()
        self._save(watch)
        return watch

    def delete(self, tenant: str, watch_id: str) -> bool:
        pipe = self.r.pipeline()
        pipe.delete(self._k(tenant, "w", watch_id))
        pipe.zrem(self._k(tenant, "index"), watch_id)
        pipe.zrem(self._k("_all", "due"), f"{tenant}:{watch_id}")
        return bool(pipe.execute())

    # -- running ------------------------------------------------------------- #
    def due(self, limit: int = 20, now: float | None = None) -> list[Watch]:
        """Watches whose next run has arrived (the Celery beat entry point)."""
        now = now or self._now()
        members = self.r.zrangebyscore(self._k("_all", "due"), 0, now,
                                       start=0, num=max(1, limit))
        out: list[Watch] = []
        for raw in members:
            tenant, _, watch_id = self._dec(raw).partition(":")
            watch = self.get(tenant, watch_id)
            if watch is None or watch.state != "active":
                self.r.zrem(self._k("_all", "due"), self._dec(raw))
                continue
            out.append(watch)
        return out

    def run(self, tenant: str, watch_id: str, *, force: bool = False) -> dict[str, Any]:
        """Run one watch now, returning the events it produced."""
        watch = self.get(tenant, watch_id)
        if watch is None:
            raise KeyError(watch_id)
        if watch.state != "active" and not force:
            return {"watch_id": watch_id, "skipped": "paused", "events": []}
        return self._run(watch)

    def _run(self, watch: Watch) -> dict[str, Any]:
        started = self._now()
        events: list[ChangeEvent] = []
        checked = 0
        failures: list[str] = []

        for target in watch.targets:
            try:
                status, body, headers = self.fetcher(target.url)
            except Exception as exc:  # noqa: BLE001 - a dead target is not a crash
                failures.append(f"{target.url}: {exc}")
                continue
            if status >= 400 or not body:
                failures.append(f"{target.url}: HTTP {status}")
                continue
            checked += 1
            events.extend(self._process_target(watch, target, body, status, headers))

        significant = [e for e in events if e.significance >= watch.min_significance]
        after = self._schedule_next(watch, significant, checked)

        watch.runs += 1
        watch.failures += len(failures)
        watch.events += len(significant)
        watch.last_run_at = started
        watch.last_error = failures[0] if failures and not checked else ""
        watch.next_run_at = after
        self._save(watch)
        self._store_events(watch, events)
        notified = self._notify(watch, significant)

        return {
            "watch_id": watch.watch_id,
            "name": watch.name,
            "checked": checked,
            "targets": len(watch.targets),
            "events": [e.to_dict() for e in events],
            "notified": notified,
            "failures": failures,
            "next_run_at": after,
            "next_run_in": human_schedule(int(max(60, after - started))),
            "duration_ms": round((self._now() - started) * 1000, 1),
        }

    def _process_target(self, watch: Watch, target: WatchTarget, body: str,
                        status: int, headers: dict[str, str]) -> list[ChangeEvent]:
        snap = self.snapshots.put(body, url=target.url,
                                  content_type=headers.get("content-type", "text/html"),
                                  status=status)
        prev_id = self._last_snapshot(watch, target.url)
        self._set_last_snapshot(watch, target.url, snap.snapshot_id, snap.hex)

        record = record_for_page(
            tenant=watch.tenant, url=target.url, body=body, status=status,
            method="http", snapshot_id=snap.snapshot_id,
            extractor="watch-diff", extractor_version="1",
            confidence=0.9, dataset=f"watch:{watch.watch_id}",
            extra={"watch_id": watch.watch_id, "template_noise": template_noise(body)},
        )
        record = self.ledger.record(record)

        if not prev_id:
            return []          # first sighting: a baseline, not a change
        previous = self.snapshots.get(prev_id) or b""
        before = previous.decode("utf-8", "replace")
        events = classify(before, body, url=target.url, watch_id=watch.watch_id,
                          tenant=watch.tenant, kinds=watch.kinds)
        for e in events:
            e.snapshot_id = snap.snapshot_id
            e.prev_snapshot_id = prev_id
            e.provenance_id = record.record_id
        if not events:
            return []
        return events

    def _notify(self, watch: Watch, events: list[ChangeEvent]) -> int:
        if not events or not self.emit or not watch.notify_webhook:
            return 0
        sent = 0
        for e in events:
            if self._is_duplicate(watch, e):
                continue
            try:
                self.emit(watch.tenant, "watch.changed",
                          {"watch_id": watch.watch_id, "watch": watch.name,
                           **e.to_dict()})
                e.notified = True
                sent += 1
            except Exception as exc:  # noqa: BLE001 - never fail a run on notify
                logger.warning("watch %s notify failed: %s", watch.watch_id, exc)
        if sent:
            self._store_events(watch, [e for e in events if e.notified])
        return sent

    def _is_duplicate(self, watch: Watch, event: ChangeEvent) -> bool:
        key = self._k(watch.tenant, "sent",
                      f"{watch.watch_id}:{event.kind}:"
                      f"{abs(hash((event.url, event.summary[:60]))) % 10**10}")
        if self.r.get(key):
            return True
        self.r.setex(key, DEDUPE_WINDOW, "1")
        return False

    def _schedule_next(self, watch: Watch, events: Sequence[ChangeEvent],
                       checked: int) -> float:
        """Adaptive interval: quiet pages get checked less, noisy ones more.

        A monitor that changed something since the last run keeps its cadence;
        one that has been silent for several runs backs off — down to a floor
        of the user's own schedule, because "never came back" is a bug.
        """
        base = watch.schedule_seconds
        if not watch.adaptive:
            return self._now() + base
        if events:
            return self._now() + max(300, base // 2)
        # silent run: stretch by the number of consecutive silent runs (capped)
        silent_runs = max(0, watch.runs - watch.events)
        factor = min(4.0, 1.0 + 0.5 * (silent_runs % 8))
        if checked == 0:                     # everything errored: retry sooner
            return self._now() + max(300, base // 2)
        return self._now() + min(base * factor, 86400.0)

    # -- event storage ------------------------------------------------------- #
    def _store_events(self, watch: Watch, events: Sequence[ChangeEvent]) -> None:
        if not events:
            return
        pipe = self.r.pipeline()
        for e in events:
            payload = json.dumps(e.to_dict(), separators=(",", ":"), default=str)
            pipe.set(self._k(watch.tenant, "e", e.event_id), payload)
            pipe.zadd(self._k(watch.tenant, "events"), {e.event_id: e.detected_at})
            pipe.zadd(self._k(watch.tenant, "w", watch.watch_id, "events"),
                      {e.event_id: e.detected_at})
            pipe.zremrangebyrank(self._k(watch.tenant, "events"), 0, -5001)
        pipe.execute()

    def events(self, tenant: str, *, watch_id: str = "", limit: int = 50,
               min_significance: float = 0.0,
               kinds: Sequence[str] | None = None) -> list[ChangeEvent]:
        key = (self._k(tenant, "w", watch_id, "events") if watch_id
               else self._k(tenant, "events"))
        ids = [self._dec(i) for i in self.r.zrevrange(key, 0, max(0, limit * 3 - 1))]
        out: list[ChangeEvent] = []
        for eid in ids:
            raw = self.r.get(self._k(tenant, "e", eid))
            if not raw:
                continue
            event = ChangeEvent.from_dict(json.loads(raw))
            if event.significance < min_significance:
                continue
            if kinds and event.kind not in kinds:
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def stats(self, tenant: str) -> dict[str, Any]:
        watches = self.list(tenant, limit=1000)
        events = self.events(tenant, limit=500)
        by_kind: dict[str, int] = {}
        for e in events:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {
            "watches": len(watches),
            "active": sum(1 for w in watches if w.state == "active"),
            "targets": sum(len(w.targets) for w in watches),
            "runs": sum(w.runs for w in watches),
            "failures": sum(w.failures for w in watches),
            "events_recent": len(events),
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
            "next_due": min((w.next_run_at for w in watches if w.state == "active"),
                            default=None),
        }

    # -- snapshot bookkeeping ------------------------------------------------ #
    def _last_snapshot(self, watch: Watch, url: str) -> str:
        raw = self.r.hget(self._k(watch.tenant, "w", watch.watch_id, "snap"), url)
        return self._dec(raw) if raw else ""

    def _set_last_snapshot(self, watch: Watch, url: str, snapshot_id: str,
                           hex_digest: str) -> None:
        pipe = self.r.pipeline()
        pipe.hset(self._k(watch.tenant, "w", watch.watch_id, "snap"), url, snapshot_id)
        pipe.hset(self._k(watch.tenant, "w", watch.watch_id, "snaphex"),
                  url, hex_digest)
        pipe.execute()

    def snapshot_history(self, watch: Watch, url: str) -> str:
        return self._last_snapshot(watch, url)


# --------------------------------------------------------------------------- #
# convenience for tests and one-off runs
# --------------------------------------------------------------------------- #
def classify_pair(before: str, after: str, **kwargs) -> list[dict[str, Any]]:
    return [e.to_dict() for e in classify(before, after, **kwargs)]


def event_kinds() -> list[str]:
    return list(EVENT_KINDS)


def to_asdict(obj: Any) -> dict[str, Any]:
    return asdict(obj)
