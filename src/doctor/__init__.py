"""Crawl Doctor (see :mod:`src.doctor.service`)."""

from src.doctor.service import (
    FAILURE_CAUSES,
    PHASES,
    DoctorRecorder,
    HealthReport,
    Span,
    classify_failure,
    diagnose,
    timed_phases,
)

__all__ = [
    "FAILURE_CAUSES",
    "PHASES",
    "DoctorRecorder",
    "HealthReport",
    "Span",
    "classify_failure",
    "diagnose",
    "timed_phases",
]
