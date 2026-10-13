"""Success-Rate Monitor — per-strategy health tracking with auto-switching.

The Stealth Engine escalates fetch strategies from cheap to expensive:

    http_plain  →  curl_cffi(tls)  →  playwright_stealth  →  proxy+stealth

Each strategy's outcome (success / block / error) is recorded in a sliding
window. When a strategy's success rate drops below ``min_success_rate``
(with at least ``min_samples`` observations), it is *degraded* and the
monitor automatically promotes the next strategy in the ladder. A degraded
strategy re-enters probation after ``cooldown_seconds`` (half-open circuit
breaker).

The monitor also exposes :meth:`StrategyMonitor.snapshot` for dashboards and
Prometheus export (see :mod:`src.observability.metrics`).

Pure-python, zero network access — safe for CI.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class Outcome(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"        # 403/429/captcha/challenge page
    ERROR = "error"            # network/proxy failure
    SOLVED = "solved"          # blocked but captcha solved -> still counts


# Heuristic classification of HTTP statuses / exceptions so callers can just
# report what happened and let the monitor decide.
BLOCK_STATUSES = {401, 403, 407, 429, 503}


def classify(status: int | None = None, exception: Exception | None = None,
             html_snippet: str = "") -> Outcome:
    if exception is not None:
        return Outcome.ERROR
    if status is not None and status in BLOCK_STATUSES:
        return Outcome.BLOCKED
    if status is not None and 200 <= status < 400:
        low = (html_snippet or "").lower()
        if any(m in low for m in ("captcha", "just a moment", "are you a robot",
                                  "access denied", "unusual traffic")):
            return Outcome.BLOCKED
        return Outcome.SUCCESS
    if status is not None and 400 <= status < 500:
        return Outcome.BLOCKED
    return Outcome.ERROR


@dataclass
class StrategyStats:
    name: str
    window: int = 50
    outcomes: deque = field(default_factory=deque)

    def __post_init__(self):
        self.outcomes = deque(maxlen=self.window)

    @property
    def samples(self) -> int:
        return len(self.outcomes)

    @property
    def success_rate(self) -> float:
        if not self.outcomes:
            return 1.0
        good = sum(1 for o in self.outcomes
                   if o in (Outcome.SUCCESS, Outcome.SOLVED))
        return good / len(self.outcomes)

    @property
    def block_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o == Outcome.BLOCKED) / len(self.outcomes)

    def record(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)


@dataclass
class StrategyState:
    stats: StrategyStats
    degraded_until: float = 0.0     # epoch; > now => on cooldown
    total_calls: int = 0
    total_failures: int = 0

    @property
    def available(self) -> bool:
        return time.time() >= self.degraded_until


class StrategyMonitor:
    """Tracks every fetch strategy and picks the best one automatically."""

    DEFAULT_LADDER = ["http_plain", "curl_cffi", "playwright_stealth",
                      "proxy_stealth"]

    def __init__(self, strategies: list[str] | None = None,
                 min_success_rate: float = 0.6, min_samples: int = 8,
                 cooldown_seconds: float = 120.0, window: int = 50):
        names = strategies or [
            s.strip() for s in os.getenv("STEALTH_STRATEGIES", "").split(",")
            if s.strip()
        ] or self.DEFAULT_LADDER
        self.order: list[str] = list(names)
        self.min_success_rate = min_success_rate
        self.min_samples = min_samples
        self.cooldown_seconds = cooldown_seconds
        self.states: dict[str, StrategyState] = {
            n: StrategyState(stats=StrategyStats(name=n, window=window))
            for n in self.order
        }
        self._switches = 0

    # ------------------------------------------------------------------ #
    def record(self, strategy: str, outcome: Outcome) -> None:
        st = self.states.setdefault(strategy,
                                    StrategyState(stats=StrategyStats(
                                        name=strategy, window=self.states[
                                            self.order[0]].stats.window)))
        if strategy not in self.order:
            self.order.append(strategy)
        st.stats.record(outcome)
        st.total_calls += 1
        if outcome in (Outcome.BLOCKED, Outcome.ERROR):
            st.total_failures += 1
        if (st.stats.samples >= self.min_samples
                and st.stats.success_rate < self.min_success_rate
                and st.available):
            st.degraded_until = time.time() + self.cooldown_seconds
            logger.warning(
                "strategy %r degraded: success=%.2f over %d samples "
                "(cooldown %.0fs)", strategy, st.stats.success_rate,
                st.stats.samples, self.cooldown_seconds)

    # ------------------------------------------------------------------ #
    def best_strategy(self) -> str | None:
        """Highest success-rate among available strategies (ladder order as
        tie-breaker). All degraded → first entry re-enters probation."""
        avail = [n for n in self.order if self.states[n].available]
        if not avail:
            # everything cooling: force-probe the cheapest one (half-open)
            probe = self.order[0]
            self.states[probe].degraded_until = 0.0
            logger.info("all strategies degraded; probing %r", probe)
            return probe
        return max(avail, key=lambda n: (
            self.states[n].stats.success_rate if self.states[n].stats.samples
            else -1.0,                    # unproven strategies rank lowest
            -self.order.index(n)))

    def next_after(self, failed: str, outcome: Outcome) -> str:
        """Record `outcome` for `failed` and return the strategy to try next."""
        prev = self.best_strategy()
        self.record(failed, outcome)
        chosen = self.best_strategy()
        if chosen != prev:
            self._switches += 1
            logger.info("auto-switch: %s -> %s (%s)", prev, chosen, outcome.value)
        return chosen

    # ------------------------------------------------------------------ #
    def run_with_fallback(self, fetch_fn: Callable[[str], tuple[int, str]],
                          url: str, max_attempts: int = 4) -> tuple[str, int, str]:
        """Execute `fetch_fn(strategy)->(status, body)` escalating through
        strategies until one succeeds. Returns (strategy, status, body)."""
        tried: list[str] = []
        last: tuple[str, int, str] = ("none", 0, "")
        while len(tried) < max_attempts:
            strat = self.best_strategy()
            if strat is None or strat in tried:
                # pick next untried in ladder order
                strat = next((s for s in self.order if s not in tried), None)
                if strat is None:
                    break
            tried.append(strat)
            try:
                status, body = fetch_fn(strat)
            except Exception as exc:  # noqa: BLE001
                outcome = classify(exception=exc)
                self.record(strat, outcome)
                last = (strat, 0, str(exc))
                continue
            snippet = body[:2000] if isinstance(body, str) else ""
            outcome = classify(status=status, html_snippet=snippet)
            self.record(strat, outcome)
            last = (strat, status, body)
            if outcome in (Outcome.SUCCESS,):
                return last
        return last

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        return {
            "switches": self._switches,
            "current": self.best_strategy(),
            "strategies": {
                n: {
                    "success_rate": round(s.stats.success_rate, 4),
                    "block_rate": round(s.stats.block_rate, 4),
                    "samples": s.stats.samples,
                    "calls": s.total_calls,
                    "degraded": not s.available,
                }
                for n, s in self.states.items()
            },
        }


# module-level singleton used by crawlers/workers
_default: Optional[StrategyMonitor] = None


def get_monitor(**kwargs) -> StrategyMonitor:
    global _default
    if _default is None:
        _default = StrategyMonitor(**kwargs)
    return _default


def reset_monitor() -> None:
    global _default
    _default = None
