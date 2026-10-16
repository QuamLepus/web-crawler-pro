"""Web Monitoring: watches, their schedules and their change history."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
PRESET_SCHEDULES = {
    "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800,
    "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800,
}


def parse_schedule(schedule: str | int | None, *, default: int = 3600) -> int:
    """``"30m"`` / ``"every 6 hours"`` / ``900`` → seconds.

    Deliberately small: a monitor's cadence is a number of seconds, and every
    cron-shaped API eventually becomes a support ticket about timezones.
    """
    if schedule is None or schedule == "":
        return default
    if isinstance(schedule, (int, float)):
        return max(60, int(schedule))
    text = str(schedule).strip().lower()
    if text in PRESET_SCHEDULES:
        return PRESET_SCHEDULES[text]
    text = re.sub(r"^every\s+", "", text).strip()
    m = re.match(r"^(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|"
                 r"days?|d|weeks?|w)$", text)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        if unit.startswith(("week", "w")):
            factor = 604800
        elif unit.startswith(("day", "d")):
            factor = 86400
        elif unit.startswith(("hour", "hr", "h")):
            factor = 3600
        elif unit.startswith(("minute", "min", "m")):
            factor = 60
        else:
            factor = 1
        return max(60, amount * factor)
    return default


def human_schedule(seconds: int) -> str:
    for label, secs in sorted(PRESET_SCHEDULES.items(), key=lambda kv: kv[1]):
        if seconds == secs:
            return label
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class WatchTarget:
    """One monitored URL, optionally scoped to a region of the page."""

    url: str
    label: str = ""
    # CSS-free scoping: a substring that must appear in the content we diff,
    # used to ignore the parts of a page nobody cares about.
    focus: str = ""
    selector: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> WatchTarget:
        return cls(url=row.get("url", ""), label=row.get("label", ""),
                   focus=row.get("focus", ""), selector=row.get("selector", ""))


@dataclass
class Watch:
    """A standing question: "tell me when this changes"."""

    name: str
    tenant: str
    watch_id: str = ""
    goal: str = ""
    targets: list[WatchTarget] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    schedule_seconds: int = 3600
    min_significance: float = 0.35
    state: str = "active"                 # active | paused
    created_at: float = field(default_factory=time.time)
    last_run_at: float | None = None
    next_run_at: float = 0.0
    runs: int = 0
    failures: int = 0
    events: int = 0
    last_error: str = ""
    notify_webhook: bool = True
    adaptive: bool = True                 # let the optimizer stretch the interval

    def __post_init__(self) -> None:
        import uuid

        if not self.watch_id:
            self.watch_id = "w_" + uuid.uuid4().hex[:16]
        self.schedule_seconds = max(60, int(self.schedule_seconds))
        if not self.next_run_at:
            self.next_run_at = time.time()
        if self.state not in ("active", "paused"):
            self.state = "active"
        self.targets = [t if isinstance(t, WatchTarget) else WatchTarget.from_dict(t)
                        for t in self.targets]

    @property
    def urls(self) -> list[str]:
        return [t.url for t in self.targets]

    @property
    def due(self) -> bool:
        return self.state == "active" and float(self.next_run_at) <= time.time()

    @property
    def observed_change_rate(self) -> float:
        """Changes per day seen so far — feeds the crawl optimizer."""
        if not self.runs:
            return 0.0
        age_days = max(1.0 / 24.0, (time.time() - float(self.created_at)) / 86400.0)
        return round(self.events / age_days, 4)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["targets"] = [t.to_dict() for t in self.targets]
        d["due"] = self.due
        d["schedule"] = human_schedule(self.schedule_seconds)
        d["observed_change_rate"] = self.observed_change_rate
        d["urls"] = self.urls
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Watch:
        row = dict(row)
        row.pop("due", None)
        row.pop("schedule", None)
        row.pop("observed_change_rate", None)
        row.pop("urls", None)
        allowed = {k: v for k, v in row.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #
def summarise_events(events: list[dict[str, Any]] | list[Any]) -> dict[str, Any]:
    """Counts by kind/significance for the console and the health page."""
    rows = [e.to_dict() if hasattr(e, "to_dict") else dict(e) for e in events]
    by_kind: dict[str, int] = {}
    for e in rows:
        by_kind[e.get("kind", "?")] = by_kind.get(e.get("kind", "?"), 0) + 1
    significant = [e for e in rows if float(e.get("significance", 0)) >= 0.5]
    return {
        "events": len(rows),
        "significant": len(significant),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "top": sorted(rows, key=lambda e: -float(e.get("significance", 0)))[:10],
    }
