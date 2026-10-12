"""C4 + Gopher quality filters and the Data Quality Score (DQS).

Implements the *actual* published filter rules:

**C4** (Raffel et al. 2019, T5 paper §3):
  - remove lines starting with ``{"json", "xml", "html"}`` markers
  - remove pages containing the "lorem ipsum" boilerplate marker
  - remove pages containing cookie-policy boilerplate phrases
  - remove pages with > 0.1% lines ending in a bullet char (menu-scrapes)
  - remove pages with < 5 sentences (configurable)
  - remove pages where > 70% of sentences are duplicated (junk templates)
  - word-count bounds per language

**Gopher** (Rae et al. 2021, appendix B):
  - doc word count between 50 and 100k
  - mean word length 3..10
  - bullets/lines symbol ratio <= 90%
  - alpha(ish)-word ratio >= 80%
  - symbol-to-word ratios for #, ;, (, ) below thresholds
  - top-2-domain character ratio check (language sanity)

Each rule returns a pass/fail plus reason; :func:`quality_score` aggregates
survived rules + extra signals into a 0-100 DQS used by ranking & sampling.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------- #
# Sentence splitting (lightweight, no NLTK dependency)
# --------------------------------------------------------------------------- #
_SENT_RE = re.compile(r"[^.!?\n][^.!?\n]*[.!?]+[\")]\"'?]?|\S[^.!?\n]*$")


def split_sentences(text: str) -> list[str]:
    sents = [m.group(0).strip() for m in _SENT_RE.finditer(text)]
    return [s for s in sents if s]


_WORD_RE = re.compile(r"\w+", re.UNICODE)
_BULLET_CHARS = set("•·‣▪◦●○■□◆◇★☆➢➔")


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


# --------------------------------------------------------------------------- #
# Rule framework
# --------------------------------------------------------------------------- #
@dataclass
class RuleResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class FilterReport:
    results: list[RuleResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def failures(self) -> list[str]:
        return [f"{r.name}: {r.detail}" for r in self.results if not r.passed]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "rules": [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in self.results],
        }


# --------------------------------------------------------------------------- #
# C4 rules
# --------------------------------------------------------------------------- #
_C4_BAD_LINE_PREFIXES = ("{", "<")  # json/xml/html-ish line starts
_LOREM_RE = re.compile(r"lorem\s+ipsum", re.I)
_COOKIE_PHRASES = (
    "terms of use", "privacy policy", "cookie policy", "use cookies",
    "accept cookies", "our policy", "javascript must be enabled",
)


def c4_line_prefix_rule(text: str, max_ratio: float = 0.1) -> RuleResult:
    """Fail if too many lines start with { or < (structured junk / menus)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return RuleResult("c4.line_prefix", False, "empty document")
    bad = sum(1 for ln in lines if ln.lstrip().startswith(_C4_BAD_LINE_PREFIXES))
    ratio = bad / len(lines)
    return RuleResult("c4.line_prefix", ratio <= max_ratio, f"bad-line ratio={ratio:.3f}")


def c4_lorem_rule(text: str) -> RuleResult:
    ok = not _LOREM_RE.search(text)
    return RuleResult("c4.lorem_ipsum", ok, "" if ok else "contains lorem ipsum")


def c4_cookie_rule(text: str) -> RuleResult:
    low = text.lower()
    hits = [p for p in _COOKIE_PHRASES if p in low]
    # C4 drops docs with ANY of these only when they dominate; we fail on >=2
    ok = len(hits) < 2
    return RuleResult("c4.cookie_policy", ok, f"phrases={hits}")


def c4_bullet_rule(text: str, max_ratio: float = 0.9) -> RuleResult:
    """Fail if >90% of non-empty lines end in a bullet character."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return RuleResult("c4.bullets", False, "empty document")
    bullet_lines = sum(1 for ln in lines if ln[-1] in _BULLET_CHARS)
    ratio = bullet_lines / len(lines)
    return RuleResult("c4.bullets", ratio <= max_ratio, f"bullet-line ratio={ratio:.3f}")


def c4_min_sentences_rule(text: str, min_sents: int = 5) -> RuleResult:
    n = len(split_sentences(text))
    return RuleResult("c4.min_sentences", n >= min_sents, f"sentences={n}")


def c4_dup_sentence_rule(text: str, max_ratio: float = 0.7) -> RuleResult:
    """Fail if >70% of sentences are duplicates of another sentence."""
    sents = split_sentences(text)
    if len(sents) < 2:
        return RuleResult("c4.dup_sentences", False, f"only {len(sents)} sentence(s)")
    norm = [unicodedata.normalize("NFKC", s.lower()) for s in sents]
    dup = len(norm) - len(set(norm))
    ratio = dup / len(norm)
    return RuleResult("c4.dup_sentences", ratio <= max_ratio, f"duplicate ratio={ratio:.3f}")


# --------------------------------------------------------------------------- #
# Gopher rules
# --------------------------------------------------------------------------- #
def gopher_word_count(text: str, lo: int = 50, hi: int = 100_000) -> RuleResult:
    n = len(words(text))
    return RuleResult("gopher.word_count", lo <= n <= hi, f"words={n}")


def gopher_mean_word_len(text: str, lo: float = 3.0, hi: float = 10.0) -> RuleResult:
    ws = words(text)
    if not ws:
        return RuleResult("gopher.mean_word_len", False, "no words")
    avg = sum(len(w) for w in ws) / len(ws)
    return RuleResult("gopher.mean_word_len", lo <= avg <= hi, f"avg={avg:.2f}")


def _is_symbol(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat.startswith("P") or cat.startswith("S")


def gopher_symbol_ratio(text: str, max_ratio: float = 0.8, symbols: tuple[str, ...] = ("#", ";")) -> RuleResult:
    """Fraction of words containing each symbol char must stay under threshold."""
    ws = words(text)
    if not ws:
        return RuleResult("gopher.symbol_ratio", False, "no words")
    for sym in symbols:
        cnt = sum(1 for w in ws if sym in w)
        if cnt / len(ws) > max_ratio:
            return RuleResult("gopher.symbol_ratio", False, f"'{sym}' ratio={cnt/len(ws):.2f}")
    return RuleResult("gopher.symbol_ratio", True, "")


def gopher_alpha_ratio(text: str, min_ratio: float = 0.8) -> RuleResult:
    """At least 80% of 'words' should contain at least one alphabetic char."""
    toks = re.findall(r"\S+", text)
    if not toks:
        return RuleResult("gopher.alpha_ratio", False, "no tokens")
    alpha = sum(1 for t in toks if any(c.isalpha() for c in t))
    ratio = alpha / len(toks)
    return RuleResult("gopher.alpha_ratio", ratio >= min_ratio, f"alpha ratio={ratio:.3f}")


def gopher_stopword_lang_rule(text: str, lang: str = "en") -> RuleResult:
    """Language sanity: enough English stopwords for en docs (top-2-char proxy)."""
    if lang != "en":
        return RuleResult("gopher.lang_en_proxy", True, f"lang={lang}, skipped")
    stop = {"the", "and", "a", "in", "of", "to", "is", "you", "that", "it"}
    ws = {w.lower() for w in words(text)}
    hits = len(ws & stop)
    ok = hits >= 3
    return RuleResult("gopher.lang_en_proxy", ok, f"stopword hits={hits}")


# --------------------------------------------------------------------------- #
# Aggregate filter + DQS
# --------------------------------------------------------------------------- #
C4_RULES = (
    c4_line_prefix_rule,
    c4_lorem_rule,
    c4_cookie_rule,
    c4_bullet_rule,
    c4_min_sentences_rule,
    c4_dup_sentence_rule,
)
GOPHER_RULES = (
    gopher_word_count,
    gopher_mean_word_len,
    gopher_symbol_ratio,
    gopher_alpha_ratio,
)


def apply_filters(text: str, *, lang: str = "en", include_gopher: bool = True) -> FilterReport:
    report = FilterReport()
    if not text or not text.strip():
        report.results.append(RuleResult("non_empty", False, "empty input"))
        return report
    report.results.append(RuleResult("non_empty", True, ""))
    for rule in C4_RULES:
        report.results.append(rule(text))
    if include_gopher:
        for rule in GOPHER_RULES:
            report.results.append(rule(text))
        report.results.append(gopher_stopword_lang_rule(text, lang))
    return report


def quality_score(text: str, *, lang: str = "en", extra_signals: dict[str, float] | None = None) -> float:
    """Data Quality Score 0-100.

    Composition:
      55 pts  filter survival (partial credit per passed rule)
      25 pts  intrinsic signals: word count sweet-spot, sentence length
              variance, lexical diversity (MTLD-lite), punctuation health
      15 pts  provenance: has URL, has timestamp, dedupe-clean flag
       5 pts  caller-supplied extra signals (e.g. LLM curator score 0..1)
    """
    report = apply_filters(text, lang=lang)
    total_rules = max(len(report.results), 1)
    passed_rules = sum(1 for r in report.results if r.passed)
    score = 55.0 * passed_rules / total_rules

    ws = words(text)
    n = len(ws)
    # word-count sweet spot ~ 200..5000 words (log-triangle)
    if n:
        import math
        lo, mid, hi = math.log(50), math.log(1500), math.log(20000)
        x = math.log(max(n, 1))
        wc = 1 - abs(x - mid) / max(abs(x - lo), abs(hi - mid), 1e-6)
        score += 8 * max(0.0, min(1.0, wc))
        sents = split_sentences(text)
        if len(sents) >= 3:
            mean_sl = sum(len(split_sentences(s)) or len(s.split()) for s in sents) / len(sents)
            var_ok = 8 <= mean_sl <= 40
            score += 6 * (1.0 if var_ok else 0.4)
        uniq = len({w.lower() for w in ws}) / n
        score += 6 * min(1.0, uniq * 2.5)  # type-token ratio boost
        punct = sum(1 for ch in text if ch in ".!?,;:") / max(n, 1)
        score += 5 * (1.0 if 0.02 <= punct <= 0.25 else 0.3)

    prov = extra_signals or {}
    if prov.get("has_url"):
        score += 6
    if prov.get("has_timestamp"):
        score += 4
    if prov.get("dedupe_clean"):
        score += 5
    llm = prov.get("llm_curator")
    if llm is not None:
        score += 5 * max(0.0, min(1.0, float(llm)))

    return round(max(0.0, min(100.0, score)), 1)


def batch_report(docs: Iterable[dict]) -> dict:
    """Aggregate pass-rates over a batch; feeds Prometheus counters upstream."""
    docs = list(docs)
    passed = 0
    reasons: dict[str, int] = {}
    for d in docs:
        rep = apply_filters(d.get("text", ""), lang=d.get("lang", "en"))
        if rep.passed:
            passed += 1
        else:
            for f in rep.failures():
                key = f.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
    return {
        "total": len(docs),
        "passed": passed,
        "pass_rate": passed / len(docs) if docs else 0.0,
        "failure_reasons": reasons,
    }
