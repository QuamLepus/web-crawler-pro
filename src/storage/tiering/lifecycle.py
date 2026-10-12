"""Tiered storage lifecycle: Hot (NVMe) -> Warm (HDD) -> Cold (Glacier/LTO).

Rules engine evaluated nightly by a Celery beat task. Each object key gets a
tier tag plus an "last access" timestamp in Redis; transitions copy the
object to the destination connector, retag it, and delete the source copy.
Retrieval requests against cold data trigger an async ``restore`` job
(Glacier restores take hours; LTO tape recall days - callers get notified via
webhook when ready).

Policy defaults (override per-prefix via :meth:`LifecycleEngine.set_policy`):
  hot->warm   after 14 days untouched
  warm->cold  after 90 days untouched
  restore     cold->hot with a 7-day temporary window before re-cooling
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

TIER_HOT, TIER_WARM, TIER_COLD = "hot", "warm", "cold"
DAY = 86_400


@dataclass
class TierPolicy:
    prefix: str
    hot_to_warm_days: int = 14
    warm_to_cold_days: int = 90
    restore_days: int = 7
    storage_class_cold: str = "GLACIER_IR"


@dataclass
class LifecycleEngine:
    """Moves objects between tier connectors according to policies.

    ``tiers`` maps tier name -> StorageConnector (duck-typed: needs
    ``get/put/delete/exists/walk``). ``redis_client`` stores:
      tier:{key}          -> json {"tier":..., "ts":...}
      tier:policy         -> hash prefix -> json(TierPolicy)
    """

    redis_client: object
    tiers: dict[str, object]
    _policies: dict[str, TierPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.default_policy = TierPolicy(prefix="")
        # load persisted custom policies
        raw = self.redis_client.hgetall("tier:policy") or {}
        for k, v in raw.items():
            p = json.loads(v)
            self._policies[p["prefix"]] = TierPolicy(**p)

    # -- policy management ---------------------------------------------------- #
    def set_policy(self, policy: TierPolicy) -> None:
        self._policies[policy.prefix] = policy
        self.redis_client.hset(
            "tier:policy", policy.prefix, json.dumps(policy.__dict__)
        )

    def policy_for(self, key: str) -> TierPolicy:
        """Longest-matching prefix wins; falls back to default."""
        best: TierPolicy | None = None
        for pfx, pol in self._policies.items():
            if key.startswith(pfx) and (best is None or len(pfx) > len(best.prefix)):
                best = pol
        return best or self.default_policy

    # -- tagging ------------------------------------------------------------------ #
    def touch(self, key: str, tier: str = TIER_HOT) -> None:
        """Record creation/access; called by upload & download paths."""
        self.redis_client.set(
            f"tier:{key}", json.dumps({"tier": tier, "ts": time.time()})
        )

    def current_tier(self, key: str) -> str | None:
        raw = self.redis_client.get(f"tier:{key}")
        return json.loads(raw)["tier"] if raw else None

    # -- transitions ----------------------------------------------------------------- #
    def move(self, key: str, dest_tier: str) -> bool:
        src_name = self.current_tier(key)
        if src_name is None or src_name == dest_tier:
            return False
        src, dst = self.tiers[src_name], self.tiers[dest_tier]
        try:
            data = src.get(key)  # type: ignore[attr-defined]
            dst.put(key, data)  # type: ignore[attr-defined]
            src.delete(key)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - never lose data on partial failure
            logger.error("tier move %s %s->%s failed: %s", key, src_name, dest_tier, exc)
            return False
        self.touch(key, dest_tier)
        logger.info("moved %s: %s -> %s (%d bytes)", key, src_name, dest_tier, len(data))
        return True

    def evaluate_key(self, key: str, now: float | None = None) -> str | None:
        """Return new tier if this key should transition, else None."""
        raw = self.redis_client.get(f"tier:{key}")
        if not raw:
            return None
        state = json.loads(raw)
        age_days = ((now or time.time()) - state["ts"]) / DAY
        pol = self.policy_for(key)
        tier = state["tier"]
        if tier == TIER_HOT and age_days >= pol.hot_to_warm_days:
            return TIER_WARM
        if tier == TIER_WARM and age_days >= pol.warm_to_cold_days:
            return TIER_COLD
        return None

    def run_cycle(self, *, scan_prefix: str = "", limit: int = 100_000) -> dict:
        """Nightly sweep: find candidates and execute moves. Returns summary."""
        moved = {f"{a}_to_{b}": 0 for a, b in [
            (TIER_HOT, TIER_WARM), (TIER_WARM, TIER_COLD)
        ]}
        scanned = 0
        hot = self.tiers.get(TIER_HOT)
        if hot is None or not hasattr(hot, "walk"):
            logger.warning("no hot tier connector with walk(); skipping cycle")
            return {"scanned": 0, "moved": moved}
        for key, _size in hot.walk(scan_prefix):  # type: ignore[attr-defined]
            scanned += 1
            if scanned > limit:
                break
            dest = self.evaluate_key(key)
            if dest and self.move(key, dest):
                moved[f"{self.current_tier(key) or dest}"] = moved.get(dest, 0) + 1
                moved[f"{'hot' if dest == TIER_WARM else 'warm'}_to_{dest}"] += 1
        return {"scanned": scanned, "moved": moved}

    # -- cold restore ------------------------------------------------------------------- #
    def request_restore(self, key: str) -> dict:
        """Queue a cold->hot restore; returns ETA info for the API layer."""
        tier = self.current_tier(key)
        if tier != TIER_COLD:
            return {"status": "not-needed", "tier": tier}
        pol = self.policy_for(key)
        payload = {"key": key, "expires_in_days": pol.restore_days, "queued_at": time.time()}
        self.redis_client.lpush("tier:restores", json.dumps(payload))
        return {"status": "queued", "eta_hours": 5 if "GLACIER" in pol.storage_class_cold else 72}

    def process_restores(self, max_jobs: int = 100) -> int:
        """Worker-side: drain restore queue moving cold->hot."""
        done = 0
        for _ in range(max_jobs):
            raw = self.redis_client.rpop("tier:restores")
            if not raw:
                break
            job = json.loads(raw)
            if self.move(job["key"], TIER_HOT):
                done += 1
        return done
