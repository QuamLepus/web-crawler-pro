"""fastText language detection (176+ langs) + toxicity filtering.

Language: uses ``fasttext`` when the lid.176.bin model is present; otherwise a
script-range + stopword heuristic covering the top ~30 languages so pipelines
never stall on a missing model file.

Toxicity: three backends, tried in order of cost/quality:
  1. Perspective API (needs key) - public-comment-grade scores
  2. Detoxify (torch model)      - toxic/severe-toxic/insult/threat categories
  3. Lexicon fallback            - curated profanity/hate stem list + scoring
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MODEL_PATH = os.getenv("FASTTEXT_MODEL", "models/lid.176.bin")
_model_cache = None


# --------------------------------------------------------------------------- #
# Language detection
# --------------------------------------------------------------------------- #
_SCRIPT_RANGES: list[tuple[str, tuple[int, int]]] = [
    ("ar", (0x0600, 0x06FF)), ("zh", (0x4E00, 0x9FFF)), ("ja", (0x3040, 0x30FF)),
    ("ko", (0xAC00, 0xD7AF)), ("ru", (0x0400, 0x04FF)), ("he", (0x0590, 0x05FF)),
    ("hi", (0x0900, 0x097F)), ("bn", (0x0980, 0x09FF)), ("th", (0x0E00, 0x0E7F)),
    ("el", (0x0370, 0x03FF)), ("ta", (0x0B80, 0x0BFF)), ("te", (0x0C00, 0x0C7F)),
    ("kn", (0x0C80, 0x0CFF)), ("ml", (0x0D00, 0x0D7F)), ("my", (0x1000, 0x109F)),
    ("km", (0x1780, 0x17FF)), ("lo", (0x0E80, 0x0EFF)), ("am", (0x1200, 0x137F)),
]

_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "is", "of", "to", "in", "that", "it", "for"},
    "es": {"el", "la", "que", "de", "los", "las", "un", "una", "por"},
    "fr": {"le", "la", "les", "des", "une", "est", "pour", "dans", "que"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "für", "ein"},
    "pt": {"o", "a", "de", "que", "do", "da", "em", "um", "para"},
    "it": {"il", "la", "che", "di", "per", "con", "sono", "questo", "un"},
    "nl": {"de", "het", "een", "van", "en", "is", "dat", "op", "niet"},
    "tr": {"bir", "bu", "için", "ve", "ile", "değil", "çok", "daha"},
    "id": {"yang", "dan", "di", "untuk", "dengan", "ini", "itu", "adalah"},
}


def detect_language(text: str) -> tuple[str, float]:
    """Return (iso639-1, confidence). fastText preferred when available."""
    global _model_cache
    sample = re.sub(r"\s+", " ", text.strip())[:512]
    if not sample:
        return "unknown", 0.0
    if os.path.exists(_MODEL_PATH):
        try:
            import fasttext  # lazy optional dep

            if _model_cache is None:
                _model_cache = fasttext.load_model(_MODEL_PATH)
            labels, probs = _model_cache.predict(sample, k=1)
            return labels[0].replace("__label__", ""), float(probs[0])
        except Exception as exc:  # noqa: BLE001
            logger.debug("fasttext failed (%s); heuristic fallback", exc)
    return _heuristic_lang(sample)


def _heuristic_lang(text: str) -> tuple[str, float]:
    low = text.lower()
    chars = [c for c in text if c.isalpha()]
    if chars:
        for lang, (lo, hi) in _SCRIPT_RANGES:
            hits = sum(1 for c in chars if lo <= ord(c) <= hi)
            if hits / len(chars) > 0.6:
                return lang, min(0.55 + hits / len(chars) * 0.4, 0.99)
    words = set(re.findall(r"\w+", low))
    best, best_score = "unknown", 0.0
    for lang, stops in _STOPWORDS.items():
        score = len(words & stops) / max(len(words), 1)
        if score > best_score:
            best, best_score = lang, score
    if best_score >= 0.08:
        return best, min(0.5 + best_score, 0.95)
    return "unknown", 0.0


# --------------------------------------------------------------------------- #
# Toxicity
# --------------------------------------------------------------------------- #
@dataclass
class ToxicityResult:
    toxic: bool
    score: float           # max category probability 0..1
    categories: dict[str, float]
    backend: str


# Curated stems (lowercased, matched via substring on word stems)
_PROFANITY_STEMS = (
    "fuck", "shit", "bitch", "asshol", "cunt", "whore", "slut", "nigge",
    "faggot", "retard", "tranny", "kike", "spic", "chink", "gook",
    "rape", "rapist", "molest", "kill yourself", "kys", "die you",
    "hate you", "worthless", "stupid idiot",
)


class LexiconToxicity:
    """Zero-dependency toxicity scorer (recall-oriented, noisy precision)."""

    backend = "lexicon"

    def score(self, text: str) -> ToxicityResult:
        low = text.lower()
        hits = sum(low.count(s) for s in _PROFANITY_STEMS)
        density = hits / max(len(re.findall(r"\w+", low)), 1)
        cat = min(1.0, hits * 0.15 + density * 8)
        return ToxicityResult(
            toxic=cat >= 0.35, score=round(cat, 3),
            categories={"profanity": round(cat, 3)}, backend=self.backend,
        )


class DetoxifyToxicity:
    backend = "detoxify"

    def __init__(self, model_name: str = "unitary/toxic-bert") -> None:
        from detoxify import Detoxify

        self.model = Detoxify(model_name)

    def score(self, text: str) -> ToxicityResult:
        preds = self.model.predict(text[:512])
        cats = {k: float(v) for k, v in preds.items()}
        worst = max(cats.values()) if cats else 0.0
        return ToxicityResult(
            toxic=worst >= float(os.getenv("TOXIC_THRESHOLD", "0.8")),
            score=round(worst, 3), categories=cats, backend=self.backend,
        )


class PerspectiveToxicity:
    backend = "perspective"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def score(self, text: str) -> ToxicityResult:  # pragma: no cover - network
        import json
        import urllib.request

        req_body = json.dumps({
            "comment": {"text": text},
            "requestedAttributes": {
                "TOXICITY": {}, "SEVERE_TOXICITY": {}, "INSULT": {}, "THREAT": {}
            },
        }).encode()
        req = urllib.request.Request(
            "https://commentanalyzer.googleapis.com/v1/comments:analyze?key=" + self.api_key,
            data=req_body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 fixed host
            data = json.loads(resp.read())
        cats = {
            k.lower(): v["summaryScore"]["value"]
            for k, v in data.get("attributeScores", {}).items()
        }
        worst = max(cats.values()) if cats else 0.0
        return ToxicityResult(toxic=worst >= 0.8, score=round(worst, 3), categories=cats, backend=self.backend)


_toxic_backend = None


def get_toxicity_checker():
    """Pick best available backend once per process."""
    global _toxic_backend
    if _toxic_backend is not None:
        return _toxic_backend
    key = os.getenv("PERSPECTIVE_API_KEY")
    if key:
        try:
            _toxic_backend = PerspectiveToxicity(key)
            return _toxic_backend
        except Exception as exc:  # noqa: BLE001
            logger.warning("perspective init failed: %s", exc)
    try:
        _toxic_backend = DetoxifyToxicity()
        return _toxic_backend
    except Exception:  # noqa: BLE001
        pass
    _toxic_backend = LexiconToxicity()
    return _toxic_backend


def filter_toxic(docs, *, threshold: float | None = None):
    """Yield only non-toxic docs; annotate each with toxicity metadata."""
    checker = get_toxicity_checker()
    thr = threshold if threshold is not None else float(os.getenv("TOXIC_THRESHOLD", "0.8"))
    for doc in docs:
        res = checker.score(doc.get("text", ""))
        doc["toxicity"] = {"score": res.score, "categories": res.categories, "backend": res.backend}
        if res.score < thr:
            yield doc
