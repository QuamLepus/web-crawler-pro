"""Tenant-scoped web watch state, diffing, and event history."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

WATCH = "wcp:watch:{watch_id}"
TENANT_WATCHES = "wcp:watch:tenant:{tenant}"
DUE = "wcp:watch:due"
EVENTS = "wcp:watch:events:{watch_id}"
SNAPSHOTS = "wcp:watch:snapshots:{watch_id}"
MAX_SNAPSHOTS = 20
MAX_SNAPSHOT_TEXT = 5000
SNAPSHOT_RETENTION_SECONDS = 90 * 24 * 60 * 60


def _text(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def create_watch(redis, *, tenant: str, watch_id: str, url: str, interval_minutes: int) -> dict:
    now = time.time()
    watch = {"watch_id": watch_id, "tenant": tenant, "url": url,
             "interval_minutes": interval_minutes, "active": True,
             "created_at": now, "last_checked_at": None,
             "last_hash": None, "last_title": None, "last_text": None}
    pipe = redis.pipeline()
    pipe.set(WATCH.format(watch_id=watch_id), json.dumps(watch, separators=(",", ":")))
    pipe.sadd(TENANT_WATCHES.format(tenant=tenant), watch_id)
    pipe.zadd(DUE, {watch_id: now + interval_minutes * 60})
    pipe.execute()
    return watch


def get_watch(redis, watch_id: str, tenant: str) -> dict | None:
    raw = redis.get(WATCH.format(watch_id=watch_id))
    if not raw:
        return None
    record = json.loads(_text(raw))
    return record if record.get("tenant") == tenant else None


def _text_excerpt(value: str, *, change_at: int, limit: int = 500) -> str:
    """Return a bounded excerpt around the first changed character."""
    if len(value) <= limit:
        return value
    start = max(0, change_at - limit // 2)
    end = min(len(value), start + limit)
    if end - start < limit:
        start = max(0, end - limit)
    excerpt = value[start:end]
    return ("…" if start else "") + excerpt + ("…" if end < len(value) else "")


def _first_difference(left: str, right: str) -> int:
    """Find the common-prefix boundary without a quadratic diff algorithm."""
    boundary = min(len(left), len(right))
    index = 0
    while index < boundary and left[index] == right[index]:
        index += 1
    return index


def _save_snapshot(redis, *, watch_id: str, title: str, text: str,
                   content_hash: str, captured_at: float, baseline: bool) -> None:
    """Store a 5k-character snapshot, capped and timestamp-retained per watch."""
    key = SNAPSHOTS.format(watch_id=watch_id)
    cutoff = captured_at - SNAPSHOT_RETENTION_SECONDS
    retained = []
    for raw in redis.lrange(key, 0, -1):
        snapshot = json.loads(_text(raw))
        if float(snapshot.get("captured_at", 0)) >= cutoff:
            retained.append(snapshot)
    snapshot = {
        "snapshot_id": f"{int(captured_at * 1000)}-{content_hash[:12]}",
        "captured_at": captured_at,
        "content_sha256": content_hash,
        "title": title,
        "text": text[:MAX_SNAPSHOT_TEXT],
        "truncated": len(text) > MAX_SNAPSHOT_TEXT,
        "baseline": baseline,
    }
    retained.append(snapshot)
    retained = retained[-MAX_SNAPSHOTS:]
    pipe = redis.pipeline()
    pipe.delete(key)
    pipe.rpush(key, *(json.dumps(item, separators=(",", ":"), ensure_ascii=False)
                      for item in retained))
    pipe.expire(key, SNAPSHOT_RETENTION_SECONDS)
    pipe.execute()


def observe(redis, *, tenant: str, watch_id: str, item: dict) -> dict | None:
    """Store one crawl observation and return a change event, if any."""
    key = WATCH.format(watch_id=watch_id)
    watch = get_watch(redis, watch_id, tenant)
    if not watch or not watch.get("active") or item.get("url") != watch.get("url"):
        return None
    text = str(item.get("text") or "")[:20000]
    title = str(item.get("title") or "")[:512]
    content_hash = hashlib.sha256((title + "\n" + text).encode("utf-8")).hexdigest()
    captured_at = time.time()
    previous_hash = watch.get("last_hash")
    event = None
    if previous_hash and previous_hash != content_hash:
        old_text = str(watch.get("last_text") or "")
        prices_old = sorted(set(re.findall(r"(?:\$|৳|BDT\s*)\s?\d[\d,.]*", old_text, re.I)))
        prices_new = sorted(set(re.findall(r"(?:\$|৳|BDT\s*)\s?\d[\d,.]*", text, re.I)))
        text_changed = old_text != text
        difference_at = _first_difference(old_text, text) if text_changed else 0
        event = {"watch_id": watch_id, "url": watch["url"],
                 "event_type": "page.changed", "changed_at": captured_at,
                 "previous_hash": previous_hash, "content_sha256": content_hash,
                 "title_changed": watch.get("last_title") != title,
                 "text_changed": text_changed,
                 "old_text_excerpt": (_text_excerpt(old_text, change_at=difference_at)
                                      if text_changed else ""),
                 "new_text_excerpt": (_text_excerpt(text, change_at=difference_at)
                                      if text_changed else ""),
                 "price_mentions_before": prices_old[:30],
                 "price_mentions_after": prices_new[:30],
                 "summary": "Page content changed; price mentions are text matches, not verified prices."}
        serialized = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        pipe = redis.pipeline()
        pipe.rpush(EVENTS.format(watch_id=watch_id), serialized)
        pipe.ltrim(EVENTS.format(watch_id=watch_id), -200, -1)
        pipe.execute()
    if previous_hash is None or previous_hash != content_hash:
        _save_snapshot(redis, watch_id=watch_id, title=title, text=text,
                       content_hash=content_hash, captured_at=captured_at,
                       baseline=previous_hash is None)
    watch.update({"last_hash": content_hash, "last_title": title, "last_text": text,
                  "last_checked_at": time.time()})
    redis.set(key, json.dumps(watch, separators=(",", ":"), ensure_ascii=False))
    return event


def list_events(redis, watch_id: str, *, limit: int = 50) -> list[dict]:
    return [json.loads(_text(raw)) for raw in redis.lrange(EVENTS.format(watch_id=watch_id), -limit, -1)]


def list_snapshots(redis, watch_id: str, *, limit: int = MAX_SNAPSHOTS) -> list[dict]:
    """Return newest retained snapshots first, omitting entries older than 90d."""
    cutoff = time.time() - SNAPSHOT_RETENTION_SECONDS
    raw_items = redis.lrange(SNAPSHOTS.format(watch_id=watch_id), 0, -1)
    snapshots = [json.loads(_text(raw)) for raw in raw_items]
    retained = [item for item in snapshots
                if float(item.get("captured_at", 0)) >= cutoff]
    return list(reversed(retained[-max(1, min(limit, MAX_SNAPSHOTS)):]))
