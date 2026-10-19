"""Tenant-scoped Redis indexes for workflow run history filtering."""
from __future__ import annotations

import json
import time
import unicodedata
import uuid
from datetime import UTC, datetime

RUN_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
RUN_TTL_SECONDS = 30 * 86400
RUN_INDEX_LOCK_SECONDS = 300
TENANT_RUNS = "wcp:v1:workflow_runs:{tenant}"
TENANT_RUN_STATUS = "wcp:v1:workflow_runs:{tenant}:status:{status}"
TENANT_RUN_WORKFLOW = "wcp:v1:workflow_runs:{tenant}:workflow:{workflow_id}"
TENANT_RUN_TERM = "wcp:v1:workflow_runs:{tenant}:term:{term}"
RUN_INDEX_READY = "wcp:v1:workflow_runs:{tenant}:indexes-ready"
RUN_INDEX_LOCK = "wcp:v1:workflow_runs:{tenant}:indexes-lock"


def _tokenize(text: str) -> set[str]:
    tokens = set()
    current = []
    for char in text.casefold():
        category = unicodedata.category(char)[0]
        if category in {"L", "N"} or (category == "M" and current):
            current.append(char)
        elif current:
            tokens.add("".join(current))
            current = []
    if current:
        tokens.add("".join(current))
    return tokens


def run_search_tokens(record: dict) -> set[str]:
    return _tokenize(str(record.get("name", "")))


def query_search_tokens(query: str) -> list[str]:
    return sorted(_tokenize(query))


def _status_key(tenant: str, status: str) -> str:
    return TENANT_RUN_STATUS.format(tenant=tenant, status=status)


def _term_key(tenant: str, term: str) -> str:
    return TENANT_RUN_TERM.format(tenant=tenant, term=term)


def _created_score(record: dict, fallback: float) -> float:
    """Use the persisted creation instant as the primary/index score."""
    try:
        created_at = datetime.fromisoformat(str(record["created_at"]))
    except (KeyError, TypeError, ValueError):
        return fallback
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at.timestamp()


def queue_new_run_indexes(pipe, record: dict, score: float) -> None:
    """Queue primary, status, and token indexes alongside a new run record."""
    score = _created_score(record, score)
    tenant = record["tenant"]
    run_id = record["run_id"]
    cutoff = score - RUN_TTL_SECONDS
    tenant_runs_key = TENANT_RUNS.format(tenant=tenant)
    pipe.zremrangebyscore(tenant_runs_key, "-inf", cutoff)
    pipe.zadd(tenant_runs_key, {run_id: score})
    for status in RUN_STATUSES:
        key = _status_key(tenant, status)
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.zrem(key, run_id)
        pipe.expire(key, RUN_TTL_SECONDS)
    if record.get("status") in RUN_STATUSES:
        pipe.zadd(_status_key(tenant, record["status"]), {run_id: score})
    workflow_key = TENANT_RUN_WORKFLOW.format(
        tenant=tenant, workflow_id=record["workflow_id"])
    pipe.zremrangebyscore(workflow_key, "-inf", cutoff)
    pipe.zadd(workflow_key, {run_id: score})
    pipe.expire(workflow_key, RUN_TTL_SECONDS)
    for term in run_search_tokens(record):
        key = _term_key(tenant, term)
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.zadd(key, {run_id: score})
        pipe.expire(key, RUN_TTL_SECONDS)


def refresh_ready_marker_ttl(redis, pipe, tenant: str) -> None:
    ready_key = RUN_INDEX_READY.format(tenant=tenant)
    if redis.exists(ready_key):
        pipe.expire(ready_key, RUN_TTL_SECONDS)


def prune_expired_run_indexes(redis, tenant: str) -> None:
    """Remove expired run IDs from primary and fixed status indexes on reads."""
    cutoff = time.time() - RUN_TTL_SECONDS
    pipe = redis.pipeline()
    pipe.zremrangebyscore(TENANT_RUNS.format(tenant=tenant), "-inf", cutoff)
    for status in RUN_STATUSES:
        pipe.zremrangebyscore(_status_key(tenant, status), "-inf", cutoff)
    pipe.execute()


def _queue_current_status(pipe, record: dict, score: float) -> None:
    tenant = record.get("tenant")
    run_id = record.get("run_id")
    if not tenant or not run_id:
        return
    for status in RUN_STATUSES:
        key = _status_key(tenant, status)
        pipe.zrem(key, run_id)
        pipe.expire(key, RUN_TTL_SECONDS)
    if record.get("status") in RUN_STATUSES:
        pipe.zadd(_status_key(tenant, record["status"]), {run_id: score})
    workflow_key = TENANT_RUN_WORKFLOW.format(
        tenant=tenant, workflow_id=record["workflow_id"])
    pipe.zadd(workflow_key, {run_id: score})
    pipe.expire(workflow_key, RUN_TTL_SECONDS)
    for term in run_search_tokens(record):
        key = _term_key(tenant, term)
        pipe.zadd(key, {run_id: score})
        pipe.expire(key, RUN_TTL_SECONDS)


def refresh_run_status_index(redis, record: dict) -> None:
    """Move a run between status indexes when its persisted status changes."""
    tenant = record.get("tenant")
    run_id = record.get("run_id")
    if not tenant or not run_id:
        return
    lock_key = RUN_INDEX_LOCK.format(tenant=tenant)
    deadline = time.monotonic() + RUN_INDEX_LOCK_SECONDS
    while redis.exists(lock_key) and time.monotonic() < deadline:
        time.sleep(0.025)
    score = redis.zscore(TENANT_RUNS.format(tenant=tenant), run_id)
    if score is None:
        return
    pipe = redis.pipeline()
    _queue_current_status(pipe, record, score)
    ready_key = RUN_INDEX_READY.format(tenant=tenant)
    if redis.exists(ready_key):
        pipe.expire(ready_key, RUN_TTL_SECONDS)
    pipe.execute()


def ensure_run_filter_indexes(redis, tenant: str) -> bool:
    """Lazily backfill indexes once per tenant; return False if another worker owns it."""
    ready_key = RUN_INDEX_READY.format(tenant=tenant)
    if redis.exists(ready_key):
        return True
    lock_key = RUN_INDEX_LOCK.format(tenant=tenant)
    token = uuid.uuid4().hex
    if not redis.set(lock_key, token, nx=True, ex=RUN_INDEX_LOCK_SECONDS):
        return bool(redis.exists(ready_key))
    try:
        tenant_runs_key = TENANT_RUNS.format(tenant=tenant)
        # Status indexes are small and fixed in number; clear them before a
        # rebuild, then repopulate only live, tenant-owned records.
        status_keys = [_status_key(tenant, status) for status in RUN_STATUSES]
        redis.delete(*status_keys)
        touched_index_keys = set(status_keys)
        cursor = 0
        while True:
            cursor, members = redis.zscan(tenant_runs_key, cursor=cursor, count=1000)
            batch = list(members.items()) if isinstance(members, dict) else list(members)
            if batch:
                reads = redis.pipeline()
                normalized_batch = []
                for raw_id, score in batch:
                    run_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                    normalized_batch.append((run_id, score))
                    reads.get(f"wcp:v1:workflow_run:{run_id}")
                records = reads.execute()
                writes = redis.pipeline()
                for (run_id, score), raw in zip(normalized_batch, records):
                    if not raw:
                        writes.zrem(tenant_runs_key, run_id)
                        continue
                    record = json.loads(raw)
                    if record.get("tenant") != tenant:
                        continue
                    status = record.get("status")
                    if status in RUN_STATUSES:
                        key = _status_key(tenant, status)
                        writes.zadd(key, {run_id: score})
                    workflow_key = TENANT_RUN_WORKFLOW.format(
                        tenant=tenant, workflow_id=record["workflow_id"])
                    writes.zadd(workflow_key, {run_id: score})
                    touched_index_keys.add(workflow_key)
                    for term in run_search_tokens(record):
                        key = _term_key(tenant, term)
                        writes.zadd(key, {run_id: score})
                        touched_index_keys.add(key)
                writes.execute()
            if cursor == 0:
                break
        expiry = redis.pipeline()
        for index_key in touched_index_keys:
            expiry.expire(index_key, RUN_TTL_SECONDS)
        expiry.execute()
        redis.set(ready_key, "1", ex=RUN_TTL_SECONDS)
        return True
    finally:
        owner = redis.get(lock_key)
        if isinstance(owner, bytes):
            owner = owner.decode()
        if owner == token:
            redis.delete(lock_key)


def linear_filtered_run_page(redis, tenant: str, *, page: int, page_size: int,
                             tokens: list[str], status: str | None,
                             date_from, date_to,
                             workflow_id: str | None = None) -> tuple[list[dict], int]:
    """Correctness fallback while another request is building tenant indexes."""
    matches = []
    for raw_id in redis.zrevrange(TENANT_RUNS.format(tenant=tenant), 0, -1):
        run_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        raw = redis.get(f"wcp:v1:workflow_run:{run_id}")
        if not raw:
            continue
        record = json.loads(raw)
        if record.get("tenant") != tenant or (status and record.get("status") != status):
            continue
        if workflow_id and record.get("workflow_id") != workflow_id:
            continue
        if tokens and not set(tokens).issubset(run_search_tokens(record)):
            continue
        if date_from or date_to:
            try:
                created = datetime.fromisoformat(str(record.get("created_at", ""))).date()
            except ValueError:
                continue
            if date_from and created < date_from:
                continue
            if date_to and created > date_to:
                continue
        matches.append(record)
    offset = (page - 1) * page_size
    return matches[offset:offset + page_size], len(matches)


def filtered_run_page(redis, tenant: str, *, page: int, page_size: int,
                      tokens: list[str], status: str | None,
                      date_from, date_to, workflow_id: str | None = None) -> tuple[list[str], int]:
    """Return indexed run IDs and exact total in newest-first order."""
    tenant_runs_key = TENANT_RUNS.format(tenant=tenant)
    offset = (page - 1) * page_size
    lower = (datetime.combine(date_from, datetime.min.time(), UTC).timestamp()
             if date_from else None)
    upper = (datetime.combine(date_to, datetime.min.time(), UTC).timestamp()
             + 86400 if date_to else None)

    # A date-only filter can use the main sorted set directly without building
    # a temporary intersection or reading every run record into the API.
    if not tokens and not status and not workflow_id and (lower is not None or upper is not None):
        minimum = lower if lower is not None else "-inf"
        maximum = f"({upper}" if upper is not None else "+inf"
        total = redis.zcount(tenant_runs_key, minimum, maximum)
        ids = redis.zrevrangebyscore(tenant_runs_key, maximum, minimum,
                                     start=offset, num=page_size)
        ids = [run_id.decode() if isinstance(run_id, bytes) else run_id for run_id in ids]
        return ids, int(total)

    keys = [tenant_runs_key]
    if status:
        keys.append(_status_key(tenant, status))
    if workflow_id:
        keys.append(TENANT_RUN_WORKFLOW.format(tenant=tenant, workflow_id=workflow_id))
    keys.extend(_term_key(tenant, term) for term in tokens)
    temp_key = f"wcp:v1:workflow_run_filter_tmp:{tenant}:{uuid.uuid4().hex}"
    redis.zinterstore(temp_key, keys, aggregate="MIN")
    redis.expire(temp_key, 30)
    try:
        if lower is not None:
            redis.zremrangebyscore(temp_key, "-inf", f"({lower}")
        if upper is not None:
            redis.zremrangebyscore(temp_key, upper, "+inf")
        total = int(redis.zcard(temp_key) or 0)
        ids = redis.zrevrange(temp_key, offset, offset + page_size - 1)
        ids = [run_id.decode() if isinstance(run_id, bytes) else run_id for run_id in ids]
        return ids, total
    finally:
        redis.delete(temp_key)
