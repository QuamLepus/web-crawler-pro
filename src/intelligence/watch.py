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


def observe(redis, *, tenant: str, watch_id: str, item: dict) -> dict | None:
    """Store one crawl observation and return a change event, if any."""
    key = WATCH.format(watch_id=watch_id)
    watch = get_watch(redis, watch_id, tenant)
    if not watch or not watch.get("active") or item.get("url") != watch.get("url"):
        return None
    text = str(item.get("text") or "")[:20000]
    title = str(item.get("title") or "")[:512]
    content_hash = hashlib.sha256((title + "\n" + text).encode("utf-8")).hexdigest()
    previous_hash = watch.get("last_hash")
    event = None
    if previous_hash and previous_hash != content_hash:
        old_text = str(watch.get("last_text") or "")
        prices_old = sorted(set(re.findall(r"(?:\$|৳|BDT\s*)\s?\d[\d,.]*", old_text, re.I)))
        prices_new = sorted(set(re.findall(r"(?:\$|৳|BDT\s*)\s?\d[\d,.]*", text, re.I)))
        event = {"watch_id": watch_id, "url": watch["url"],
                 "event_type": "page.changed", "changed_at": time.time(),
                 "previous_hash": previous_hash, "content_sha256": content_hash,
                 "title_changed": watch.get("last_title") != title,
                 "price_mentions_before": prices_old[:30],
                 "price_mentions_after": prices_new[:30],
                 "summary": "Page content changed; price mentions are text matches, not verified prices."}
        serialized = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        pipe = redis.pipeline()
        pipe.rpush(EVENTS.format(watch_id=watch_id), serialized)
        pipe.ltrim(EVENTS.format(watch_id=watch_id), -200, -1)
        pipe.execute()
    watch.update({"last_hash": content_hash, "last_title": title, "last_text": text,
                  "last_checked_at": time.time()})
    redis.set(key, json.dumps(watch, separators=(",", ":"), ensure_ascii=False))
    return event


def list_events(redis, watch_id: str, *, limit: int = 50) -> list[dict]:
    return [json.loads(_text(raw)) for raw in redis.lrange(EVENTS.format(watch_id=watch_id), -limit, -1)]
