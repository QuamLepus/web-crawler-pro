"""Recipe runtime: apply, verify, and heal extraction recipes.

Uses BeautifulSoup when it is installed (it is, for a normal deployment) and
falls back to a small regex engine otherwise, so recipe *tests* can run in a
minimal container. Both paths implement the same field semantics.

Self-healing, step by step, for a required field that matched nothing:

1. the field's other selectors, in order;
2. a label-based search — the ``fallback_labels`` text is located in the raw
   HTML and the nearest value is taken (which is exactly how a human fixes a
   broken scraper by hand);
3. if both fail, the field is recorded as ``missing`` and the extraction's
   confidence drops — the caller decides whether to flag the recipe.

Every heal is reported in :attr:`Extraction.healed`; nothing is repaired
silently.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable, Sequence

from src.recipes.models import (
    Extraction,
    Field,
    Recipe,
    RecipeHealth,
    TestCase,
)

logger = logging.getLogger("wcp.recipes")

try:  # optional but expected
    from bs4 import BeautifulSoup  # type: ignore

    HAVE_BS4 = True
except Exception:  # pragma: no cover - minimal deployments
    BeautifulSoup = None  # type: ignore
    HAVE_BS4 = False

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_PRICE = re.compile(r"[-+]?\d[\d,\s]*(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# transforms
# --------------------------------------------------------------------------- #
def apply_transform(value: Any, transform: str) -> Any:
    if value is None:
        return None
    if transform in ("none", ""):
        return value
    if isinstance(value, list):
        return [apply_transform(v, transform if transform != "list" else "collapse")
                for v in value]
    text = str(value)
    if transform == "strip":
        return text.strip()
    if transform == "collapse":
        return _WS.sub(" ", text).strip()
    if transform == "lower":
        return _WS.sub(" ", text).strip().lower()
    if transform == "number":
        m = _PRICE.search(text.replace(",", ""))
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None
    if transform == "price":
        m = _PRICE.search(text.replace(",", ""))
        if not m:
            return None
        try:
            amount = float(m.group(0))
        except ValueError:
            return None
        currency = "USD"
        for symbol, code in (("৳", "BDT"), ("$", "USD"), ("€", "EUR"),
                             ("£", "GBP"), ("₹", "INR"), ("¥", "JPY")):
            if symbol in text:
                currency = code
                break
        return {"amount": amount, "currency": currency}
    if transform == "url":
        from urllib.parse import urljoin

        return urljoin("", text.strip())
    if transform == "date":
        text = _WS.sub(" ", text).strip()
        for fmt in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y",
                    "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return time.strftime("%Y-%m-%d", time.strptime(text[:24], fmt))
            except ValueError:
                continue
        return text[:40] or None
    if transform == "list":
        return [apply_transform(v, "collapse") for v in
                (value if isinstance(value, list) else [value])]
    return text


# --------------------------------------------------------------------------- #
# selector engines
# --------------------------------------------------------------------------- #
def _select_bs4(html: str, selector: str) -> list[Any]:
    soup = BeautifulSoup(html, "html.parser")
    try:
        return soup.select(selector)
    except Exception as exc:  # noqa: BLE001 - a bad selector is user input
        logger.debug("bad selector %r: %s", selector, exc)
        return []


def _value_from_node(node: Any, field: Field, page_url: str = "") -> Any:
    if field.kind == "html":
        value = str(node)
    elif field.kind == "attr":
        value = node.get(field.attr) if hasattr(node, "get") else None
        if value is None and field.attr == "text":
            value = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
    else:
        value = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    if field.kind == "regex" and field.pattern:
        m = re.search(field.pattern, str(value))
        value = m.group(1) if m and m.groups() else (m.group(0) if m else None)
    if field.transform == "url" and value:
        from urllib.parse import urljoin

        value = urljoin(page_url or "", str(value))
        return value
    return apply_transform(value, field.transform)


def _select_dom(html: str, selector: str) -> list[str]:
    """Dependency-free selector engine (``src.recipes.dom``).

    Used whenever BeautifulSoup is absent — and it is a real engine, not a
    regex approximation: descendant and child combinators, comma lists,
    attribute operators, ``@attr`` reads. The point is that extraction quality
    does not depend on whether a wheel happened to install on the worker.
    """
    from src.recipes.dom import parse, select_text

    try:
        return select_text(parse(html), selector)
    except Exception as exc:                      # noqa: BLE001 - user-authored selector
        logger.debug("dom selector %r failed: %s", selector, exc)
        return []


def _select_regex(html: str, selector: str) -> list[str]:
    """Last-ditch engine: ``tag.class#id[key=value]`` selectors only.

    Kept as the final fallback so a pathological page (or a selector the DOM
    parser cannot follow) still yields *something* rather than nothing.
    """
    core = selector.strip()
    attrs: list[tuple[str, str]] = []
    for m in re.finditer(r"\[([\w-]+)=['\"]?([^'\"]+)['\"]?\]", core):
        attrs.append((m.group(1), m.group(2)))
    core = re.sub(r"\[[^\]]+\]", "", core).strip()
    m = re.match(r"^([a-zA-Z0-9]+)?((?:[.#][\w-]+)*)$", core)
    if not m:
        return []
    tag = m.group(1) or "*"
    classes: list[str] = []
    ident = ""
    for token in re.findall(r"[.#][\w-]+", m.group(2) or ""):
        if token.startswith("."):
            classes.append(token[1:])
        else:
            ident = token[1:]

    out: list[str] = []
    for match in re.finditer(f"<{tag}\\b([^>]*)>(.*?)</{tag}>", html or "",
                             flags=re.I | re.S):
        attr_blob, inner = match.group(1), match.group(2)
        if ident and not re.search(rf"id=[\"']{re.escape(ident)}[\"']", attr_blob):
            continue
        if classes:
            found = re.search(r'class=["\']([^"\']*)["\']', attr_blob)
            present = set(found.group(1).split()) if found else set()
            if not set(classes) <= present:
                continue
        if any(not re.search(rf"{re.escape(k)}=[\"']{re.escape(v)}[\"']", attr_blob)
               for k, v in attrs):
            continue
        out.append(inner)
    return out


def _extract_field(html: str, field: Field, *, page_url: str = "") -> tuple[Any, str]:
    """Return ``(value, how)`` where ``how`` is ``selector`` or ``healed:*``."""
    if HAVE_BS4:
        for selector in field.selectors:
            nodes = _select_bs4(html, selector)
            if nodes:
                values = [_value_from_node(n, field, page_url) for n in nodes]
                values = [v for v in values if v not in (None, "")]
                if values:
                    return (values if len(values) > 1 else values[0], "selector")
    else:
        for selector in field.selectors:
            chunks = _select_dom(html, selector) or _select_regex(html, selector)
            if chunks:
                values = [apply_transform(_TAG.sub(" ", c), field.transform)
                          for c in chunks]
                values = [v for v in values if v]
                if values:
                    return (values if len(values) > 1 else values[0], "selector")

    # JSON-LD: most sites expose the same facts in structured data
    if field.kind == "jsonld":
        value = _from_jsonld(html, field.name)
        if value not in (None, ""):
            return apply_transform(value, field.transform), "jsonld"

    healed = _heal_by_label(html, field)
    if healed not in (None, ""):
        return healed, "healed:label"
    healed = _heal_by_shape(html, field)
    if healed not in (None, ""):
        return healed, "healed:shape"
    return None, "missing"


def _from_jsonld(html: str, name: str) -> Any:
    wanted = name.lower().replace("_", "").replace("-", "")
    for chunk in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>",
                            html or "", flags=re.I | re.S):
        try:
            data = json.loads(chunk.strip())
        except Exception:  # noqa: BLE001
            continue
        for node in _walk(data):
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if key.lower().replace("_", "").replace("-", "") == wanted:
                    if isinstance(value, dict):
                        value = value.get("name") or value.get("value") or ""
                    if isinstance(value, list):
                        value = value[0] if value else ""
                    if value not in (None, ""):
                        return value
    return None


def _walk(node: Any) -> Iterable[Any]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _heal_by_label(html: str, field: Field) -> Any:
    """Find the text after a known label ("Price", "Author", "Published")."""
    labels = list(field.fallback_labels) or [_humanise(field.name)]
    text = _TAG.sub(" ", html or "")
    text = text.replace("&nbsp;", " ")
    for label in labels:
        pattern = re.compile(re.escape(label) + r"\s*[:\-–]?\s*([^|\n]{1,120})",
                             re.IGNORECASE)
        m = pattern.search(text)
        if m:
            value = _WS.sub(" ", m.group(1)).strip(" :|-–")
            if 1 <= len(value) <= 120:
                return apply_transform(value, field.transform)
    return None



_PRICE_SHAPE = re.compile(
    r"(?:[$\u20ac\u00a3\u20b9\u09f3]\s?\d[\d.,]*(?:\s*(?:USD|EUR|GBP|INR|BDT))?"
    r"|\b\d[\d.,]*\s*(?:USD|EUR|GBP|INR|BDT)\b)", re.I)
_DATE_SHAPE = re.compile(r"\b(\d{4}-\d{2}-\d{2}T?[0-9:]*Z?)\b")
_EMAIL_SHAPE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")


def _heal_by_shape(html: str, field: Field) -> Any:
    """Recover a field from its *value shape* — only when it is unambiguous.

    Site redesigns rename classes far more often than they change what a price
    or a publication date looks like. A shape match is therefore accepted only
    when exactly one candidate exists on the page: two prices means we cannot
    know which is *the* price, so the field is reported missing instead of
    guessed. Every recovery is tagged ``healed:shape``.
    """
    text = _WS.sub(" ", _TAG.sub(" ", html or ""))
    name = field.name.lower()
    pattern: re.Pattern[str] | None = None
    if field.transform == "price" or "price" in name or "cost" in name:
        pattern = _PRICE_SHAPE
    elif field.transform == "date" or name in ("date", "published", "published_at",
                                               "updated", "published_date"):
        pattern = _DATE_SHAPE
    elif "email" in name:
        pattern = _EMAIL_SHAPE
    candidates = [m.group(0) for m in pattern.finditer(text)] if pattern else []
    distinct = {c.strip() for c in candidates if c and c.strip()}
    if len(distinct) != 1:
        return None
    return apply_transform(distinct.pop(), field.transform)

def _humanise(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().title()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def apply_recipe(recipe: Recipe, html: str, *, page_url: str = "",
                 strict: bool = False) -> Extraction:
    """Run a recipe over one page and report how it went."""
    result = Extraction(recipe=recipe.ref, checksum_ok=recipe.verify())
    if not result.checksum_ok:
        result.notes.append("recipe checksum mismatch — the installed copy was edited")
    for field in recipe.fields:
        value, how = _extract_field(html, field, page_url=page_url)
        if value in (None, ""):
            result.missing.append(field.name)
            continue
        result.values[field.name] = value
        result.found.append(field.name)
        if how.startswith("healed"):
            result.healed.append(field.name)
            result.notes.append(f"{field.name}: recovered via {how}")

    required = [f.name for f in recipe.fields if f.required]
    present_required = [n for n in required if n in result.values]
    coverage = len(result.found) / max(1, len(recipe.fields))
    required_ratio = len(present_required) / max(1, len(required))
    penalty = 0.15 * len(result.healed)
    result.confidence = max(0.0, min(1.0, 0.6 * coverage + 0.4 * required_ratio - penalty))
    if not required_ratio and required:
        result.notes.append("no required field matched — the recipe is likely stale")
    return result


def test_recipe(recipe: Recipe) -> RecipeHealth:
    """Run a recipe's own fixtures; this is what ``wcp recipe test`` calls."""
    health = RecipeHealth(recipe=recipe.ref)
    coverage: dict[str, int] = {f.name: 0 for f in recipe.fields}
    for case in recipe.tests:
        extraction = apply_recipe(recipe, case.html, strict=case.strict)
        for name in extraction.found:
            coverage[name] = coverage.get(name, 0) + 1
        problems = _check_expectations(case, extraction)
        if problems:
            health.failed += 1
            health.failures.append({"case": case.name, "problems": problems})
        else:
            health.passed += 1
    total = max(1, len(recipe.tests))
    health.field_coverage = {k: v / total for k, v in coverage.items()}
    health.verdict = ("pass" if not health.failed else
                      ("warn" if health.failed < total else "fail"))
    return health


def _check_expectations(case: TestCase, extraction: Extraction) -> list[str]:
    problems: list[str] = []
    for field_name, expected in case.expect.items():
        actual = extraction.values.get(field_name)
        if actual is None:
            problems.append(f"{field_name}: missing (expected {expected!r})")
            continue
        if isinstance(expected, str) and isinstance(actual, str):
            if expected.lower() not in actual.lower():
                problems.append(f"{field_name}: {actual!r} does not contain {expected!r}")
        elif isinstance(expected, dict):
            for key, want in expected.items():
                got = actual.get(key) if isinstance(actual, dict) else None
                if got != want:
                    problems.append(f"{field_name}.{key}: {got!r} != {want!r}")
        elif actual != expected:
            problems.append(f"{field_name}: {actual!r} != {expected!r}")
    if case.strict:
        for name in extraction.missing:
            problems.append(f"{name}: missing in a strict case")
    return problems


def draft_from_html(name: str, html: str, *, host: str = "",
                    fields: Sequence[str] | None = None) -> Recipe:
    """Bootstrap a recipe from a sample page.

    Reads JSON-LD first (best), then common class-name patterns. The result is
    a *draft*: it always ships without tests, so :meth:`Recipe.validate` tells
    the author to add fixtures before publishing.
    """
    from src.recipes.models import Field as F

    wanted = list(fields or ["title", "price", "description", "author",
                             "published_at"])
    guiess_selectors = {
        "title": ["h1", "h1.title", "meta[property='og:title']", "title"],
        "price": [".price", ".product-price", "[data-price]", ".amount"],
        "description": ["meta[name='description']", ".description", ".summary", "p"],
        "author": [".author", "[rel='author']", ".byline", "meta[name='author']"],
        "published_at": ["time", "time[datetime]", ".date", ".published"],
    }
    declared: list[F] = []
    for field_name in wanted:
        declared.append(F(
            name=field_name,
            selectors=guiess_selectors.get(field_name, [f".{field_name}"]),
            kind="jsonld" if field_name in ("title", "price", "description", "author")
            else "text",
            transform="price" if field_name == "price" else "collapse",
            required=field_name in ("title",),
            fallback_labels=[_humanise(field_name)],
        ))
    return Recipe(name=name, version="0.1.0", hosts=[host] if host else [],
                  description="Draft generated from a sample page — add tests before "
                              "publishing",
                  fields=declared,
                  tags=["draft"])


def recipe_from_dict(row: dict[str, Any]) -> Recipe:
    return Recipe.from_dict(row)
