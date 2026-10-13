"""Language detection (fastText) + PII redaction (Presidio).

Both integrations are optional-dependency guarded: if fastText or
Presidio is not installed, we fall back to heuristic language detection
and regex-based PII scrubbing so the pipeline still runs anywhere.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("wcp.pipeline.language_pii")

# NOTE: resolved lazily (per call) instead of at import time so that tests or
# deployment code which sets/changes ``FASTTEXT_MODEL`` after this module has
# already been imported still see the correct value.  Reading it at import
# time was a source of cross-test pollution when another test (or fixture)
# mutated the environment after import.
_DEFAULT_MODEL_PATH = "lid.176.bin"
_model_cache = None
_model_cache_path = None


def _model_path() -> str:
    return os.environ.get("FASTTEXT_MODEL", _DEFAULT_MODEL_PATH)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)")
_CC_RE = re.compile(r"(?<!\d)(?:\d[ -]*?){13,16}(?!\d)")

# Presidio's default replace-mask format: <EMAIL_ADDRESS>, <PERSON>, ...
_PRESIDIO_TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>")

# Canonical [REDACTED_*] tokens for the well-known Presidio entity types.
_ENTITY_TOKEN_MAP = {
    "EMAIL_ADDRESS": "[REDACTED_EMAIL]",
    "PHONE_NUMBER": "[REDACTED_PHONE]",
    "CREDIT_CARD": "[REDACTED_CARD]",
    "US_SSN": "[REDACTED_SSN]",
    "PERSON": "[REDACTED_PERSON]",
}


def _normalize_pii_tokens(text: str) -> str:
    """Map Presidio ``<ENTITY_TYPE>`` masks to canonical ``[REDACTED_*]``."""

    def repl(m: re.Match) -> str:
        ent = m.group(1)
        if ent in _ENTITY_TOKEN_MAP:
            return _ENTITY_TOKEN_MAP[ent]
        return f"[REDACTED_{ent.lower()}]"

    return _PRESIDIO_TOKEN_RE.sub(repl, text)


def detect_language(text: str) -> tuple[str, float]:
    """Return (iso639-1 code, confidence). Uses fastText when available."""
    global _model_cache, _model_cache_path
    path = _model_path()
    if os.path.exists(path):
        try:
            import fasttext  # lazy

            if _model_cache is None or _model_cache_path != path:
                _model_cache = fasttext.load_model(path)
                _model_cache_path = path
            labels, probs = _model_cache.predict(text.replace("\n", " ")[:512], k=1)
            return labels[0].replace("__label__", ""), float(probs[0])
        except Exception:  # noqa: BLE001
            pass
    # Heuristic fallback: crude script/stopword sniffing
    if re.search(r"[\u0980-\u09FF]", text):
        return "bn", 0.60
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar", 0.60
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh", 0.60
    stopwords = {"the", "and", "is", "of", "to"}
    words = set(w.lower() for w in re.findall(r"\w+", text))
    score = len(words & stopwords) / max(len(words), 1)
    return ("en", min(0.5 + score, 0.99)) if score else ("unknown", 0.0)


class PIIRedactor:
    """Presidio-backed PII redaction with a regex fallback."""

    def __init__(self, entities: tuple[str, ...] = (
        "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "US_SSN", "PERSON",
    )):
        self.entities = entities
        self._nlp = None
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine

            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
        except ImportError:
            self._analyzer = self._anonymizer = None

    def redact(self, text: str) -> str:
        if self._analyzer is not None:
            try:
                results = self._analyzer.analyze(text=text, language="en",
                                                 entities=list(self.entities))
                if results:
                    text = self._anonymizer.anonymize(
                        text=text, analyzer_results=results).text
            except Exception as exc:  # noqa: BLE001 - presidio runtime failure
                logger.warning("presidio redaction failed (%s); regex fallback",
                               exc)
        # Always sweep with the regex fallback too: guarantees canonical
        # [REDACTED_*] tokens for email/phone/card even when Presidio already
        # masked spans as <EMAIL_ADDRESS>/<PHONE_NUMBER> etc.
        text = _normalize_pii_tokens(text)
        text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
        text = _CC_RE.sub("[REDACTED_CARD]", text)
        return text
