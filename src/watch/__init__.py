"""Real-Time Web Change Intelligence.

Point it at pages you care about; it answers *what changed* in business terms
— a price, a new product, a policy rewrite, an announcement — not "the HTML
differs". See :mod:`src.watch.diff` for the classifiers and
:mod:`src.watch.service` for the run/alert loop.
"""

from src.watch.diff import (
    EVENT_KINDS,
    SIGNIFICANCE,
    ChangeEvent,
    classify,
    content_lines,
    count_jobs,
    extract_prices,
    extract_products,
    highest,
    html_to_lines,
    policy_fingerprint,
    template_noise,
)
from src.watch.models import (
    PRESET_SCHEDULES,
    Watch,
    WatchTarget,
    human_schedule,
    parse_schedule,
    summarise_events,
)
from src.watch.service import ALERT_KINDS, DEFAULT_KINDS, WatchService, classify_pair

__all__ = [
    "ALERT_KINDS",
    "DEFAULT_KINDS",
    "EVENT_KINDS",
    "PRESET_SCHEDULES",
    "SIGNIFICANCE",
    "ChangeEvent",
    "Watch",
    "WatchService",
    "WatchTarget",
    "classify",
    "classify_pair",
    "content_lines",
    "count_jobs",
    "extract_prices",
    "extract_products",
    "highest",
    "html_to_lines",
    "human_schedule",
    "parse_schedule",
    "policy_fingerprint",
    "summarise_events",
    "template_noise",
]
