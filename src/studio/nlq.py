"""Plain-English questions over a dataset — with no language model in the loop.

The marketing line is "ask your data a question". The honest engineering line
is narrower and more useful: there is a *grammar* of the questions people
actually ask of crawled data — how many rows, the average/highest/lowest of a
numeric field, a total, a trend over time, a distribution, the top N, filtered
by a value or a year — and every one of those is answerable deterministically,
instantly and offline.

So this module parses that grammar, and then *shows its work*: the returned
``interpretation`` names the metric, field, grouping and filters it understood,
and ``caveats`` carries the warnings (mixed currencies, three numeric values,
a filter that matched nothing). A language model, when configured, is asked
only to phrase the narrative — never to invent the number.

Unsupported phrasings get a refusal plus the list of questions that *do* work.
A wrong number presented confidently is the one failure mode worth designing
against.
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Any, Iterable, Sequence

_METRICS: list[tuple[str, re.Pattern[str]]] = [
    ("count", re.compile(r"\bhow many\b|\bcount\b|\bnumber of\b", re.I)),
    ("trend", re.compile(r"\b(trend|over time|by month|monthly|by day|growth|"
                         r"changed over|over the year)\b", re.I)),
    ("distribution", re.compile(r"\b(distribution|histogram|spread|range|quartile)\b",
                                re.I)),
    ("sum", re.compile(r"\b(total|sum|altogether|combined)\b", re.I)),
    ("mean", re.compile(r"\b(average|mean|typical)\b", re.I)),
    ("median", re.compile(r"\bmedian\b", re.I)),
    ("max", re.compile(r"\b(max|maximum|highest|most expensive|largest|peak|"
                       r"biggest)\b", re.I)),
    ("min", re.compile(r"\b(min|minimum|lowest|cheapest|smallest)\b", re.I)),
    ("list", re.compile(r"\b(show|list|give me|what are|which|rows where|"
                        r"display|find)\b", re.I)),
]

_GROUP_RE = re.compile(r"\b(?:by|per|grouped by|broken down by|for each|across)\s+"
                       r"(?:each\s+)?([a-z_][\w]*)", re.I)
_FILTER_RE = re.compile(
    r"\b(?:where|with|only|for|filter(?:ed)? by|whose)\s+([a-z_][\w]*)\s*"
    r"(>=|<=|>|<|=|is not|is|equals|contains|in)\s*"
    r"('?\"?[\w\s.,/@:-]{1,60}'?\"?)", re.I)
_LIMIT_RE = re.compile(r"\b(top|first|last|bottom|highest|lowest)\s+(\d{1,4})\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_QUOTED = re.compile(r"['\"]([^'\"]{2,60})['\"]")
_NULLS = (None, "", [], {})

CAVEAT_CURRENCY = ("{field} mixes currencies {codes} — totals across currencies are "
                   "meaningless until converted")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def numeric(value: Any) -> float | None:
    """Pull a number out of whatever the page actually said.

    ``{"amount": 899.0}`` (a price object), ``"US$1,199.00"``, ``"1.199,00"``
    (European grouping) and ``"12,999 ৳"`` all parse. ``True`` is not 1.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "price", "total"):
            if key in value:
                return numeric(value[key])
        return None
    text = str(value).strip()
    if not text:
        return None
    # A number has no words in it. "Pixel 10 Pro" is not 10, and treating it as
    # 10 is exactly the kind of silent wrong answer this module refuses to give.
    stripped_words = re.sub(r"\b(usd|bdt|eur|gbp|inr|jpy|tk|taka|rs)\b\.?", "", text,
                            flags=re.I)
    stripped_words = re.sub(r"^(us|usd|bdt|tk|rs)\$?", "", stripped_words, flags=re.I)
    if re.search(r"[A-Za-z]", stripped_words):
        return None
    cleaned = re.sub(r"[^\d.,-]", "", text)
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = (cleaned.replace(".", "").replace(",", ".")
                   if cleaned.rfind(",") > cleaned.rfind(".")
                   else cleaned.replace(",", ""))
    elif cleaned.count(",") == 1:
        head, _, tail = cleaned.rpartition(",")
        if len(tail) in (1, 2) and head:
            cleaned = f"{head}.{tail}"
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def fmt(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and not math.isclose(value, round(value), abs_tol=1e-9):
        return f"{value:,.2f}"
    return f"{int(round(float(value))):,}"


def columns(rows: Sequence[dict[str, Any]], *, sample: int = 500) -> list[str]:
    out: list[str] = []
    for row in rows[:sample]:
        for key in row:
            if key not in out:
                out.append(key)
    return out


def _match_field(question: str, fields: Iterable[str]) -> str:
    lowered = (question or "").lower()
    best, best_len = "", 0
    for name in fields:
        if not name:
            continue
        for variant in {name.lower(), name.lower().replace("_", " "),
                        name.lower().rstrip("s")}:
            if len(variant) >= 3 and variant in lowered and len(variant) > best_len:
                best, best_len = name, len(variant)
    return best


def _nearest_field(question: str, fields: Iterable[str], anchor: int = 0) -> str:
    """The column mentioned closest to the metric word.

    Distance beats length: in "average price by brand" both columns are named,
    and the one next to "average" is the one being averaged.
    """
    lowered = (question or "").lower()
    best, best_key = "", None
    for name in fields:
        if not name:
            continue
        for variant in {name.lower(), name.lower().replace("_", " "),
                        name.lower().rstrip("s")}:
            if len(variant) < 3:
                continue
            index = lowered.find(variant)
            if index < 0:
                continue
            key = (index < anchor, abs(index - anchor), -len(variant))
            if best_key is None or key < best_key:
                best, best_key = name, key
    return best


def _first_numeric(fields: Iterable[str], rows: Sequence[dict[str, Any]]) -> str:
    for name in fields:
        values = [numeric(row.get(name)) for row in rows[:200]]
        seen = [v for v in values if v is not None]
        if seen and len(seen) >= max(1, len(values) // 2):
            return name
    return ""


def parse(question: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Expose the parse itself: a black-box answer is not auditable."""
    fields = columns(rows)
    # Specificity order, not "earliest word wins": "show price over time" is a
    # trend with a cosmetic verb, and answering it as a list would be wrong.
    metric, metric_pos = "list", len(question or "")
    for name, pattern in _METRICS:
        match = pattern.search(question or "")
        if match:
            metric, metric_pos = name, match.start()
            break

    group_match = _GROUP_RE.search(question or "")
    group_by = ""
    if group_match:
        group_by = _match_field(group_match.group(1), fields) or group_match.group(1)

    # The field being measured is the one named near the metric word, never the
    # column we are grouping by — "average price by brand" measures price.
    candidates = [f for f in fields if f != group_by] or fields
    field_name = _nearest_field(question, candidates, metric_pos)

    limit_match = _LIMIT_RE.search(question or "")
    limit = int(limit_match.group(2)) if limit_match else 0
    if limit_match and limit_match.group(1).lower() in ("last", "bottom", "lowest"):
        metric = metric if metric in ("min", "max") else "min"

    filters: list[dict[str, Any]] = []
    for match in _FILTER_RE.finditer(question or ""):
        # groups: 1 = column, 2 = operator, 3 = value
        name = _match_field(match.group(1), fields)
        if name:
            filters.append({"field": name, "op": match.group(2).lower(),
                            "value": match.group(3).strip().strip("'\"")})

    # A quoted value with no explicit column: use whichever column actually
    # contains it, so "'Pixel 10'" works without naming the field.
    if not filters:
        for value in _QUOTED.findall(question or ""):
            for name in fields:
                if any(str(row.get(name) or "").strip().lower() == value.lower()
                       for row in rows[:300]):
                    filters.append({"field": name, "op": "is", "value": value})
                    break

    if not field_name and metric in ("mean", "sum", "median", "max", "min",
                                     "distribution", "trend"):
        field_name = _first_numeric(fields, rows)

    years = [int(y) for y in _YEAR_RE.findall(question or "")]
    return {"metric": metric, "field": field_name, "group_by": group_by,
            "filters": filters, "limit": limit or (10 if metric in ("list", "max", "min")
                                                   else 0),
            "years": years, "columns": fields, "question": question}


def describe(plan: dict[str, Any]) -> str:
    bits = [f"metric = {plan['metric']}"]
    if plan.get("field"):
        bits.append(f"field = {plan['field']}")
    if plan.get("group_by"):
        bits.append(f"group by {plan['group_by']}")
    for item in plan.get("filters", []):
        bits.append(f"where {item['field']} {item['op']} {item['value']}")
    if plan.get("years"):
        bits.append(f"year ∈ {plan['years']}")
    if plan.get("limit"):
        bits.append(f"limit {plan['limit']}")
    return ", ".join(bits)


def examples(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The questions this grammar can answer for *this* dataset."""
    fields = columns(rows)
    numeric_field = _first_numeric(fields, rows) or (fields[0] if fields else "price")
    categorical = next((f for f in fields if f not in (numeric_field, "url", "title",
                                                       "text", "html")), "source")
    return [
        "how many rows are there?",
        f"what is the average {numeric_field}?",
        f"what is the highest {numeric_field}?",
        f"what is the lowest {numeric_field}?",
        f"total {numeric_field} by {categorical}",
        f"top 5 {numeric_field}",
        f"distribution of {numeric_field}",
        f"show {numeric_field} over time",
        f"list the rows where {numeric_field} > 500",
    ]


# --------------------------------------------------------------------------- #
# answering
# --------------------------------------------------------------------------- #
def _apply_filter(rows: list[dict[str, Any]], field_name: str, op: str,
                  raw_value: str) -> list[dict[str, Any]]:
    value = raw_value.strip().strip("'\"")
    wanted = numeric(value)
    op = op.lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        cell = row.get(field_name)
        number = numeric(cell)
        if op in (">", ">=", "<", "<=") and wanted is not None and number is not None:
            ok = {">": number > wanted, ">=": number >= wanted,
                  "<": number < wanted, "<=": number <= wanted}[op]
        elif op in ("is", "=", "equals"):
            ok = (str(cell).strip().lower() == value.lower()
                  or (wanted is not None and number == wanted))
        elif op in ("is not", "!="):
            ok = str(cell).strip().lower() != value.lower()
        elif op == "contains":
            ok = value.lower() in str(cell or "").lower()
        elif op == "in":
            options = [v.strip().lower() for v in re.split(r"[,/]|\bor\b", value)]
            ok = str(cell).strip().lower() in options
        else:
            ok = False
        if ok:
            out.append(row)
    return out


def answer(question: str, rows: Sequence[dict[str, Any]], *, dataset: str = "",
           version: str = "", limit: int = 5000) -> dict[str, Any]:
    """Answer one question and return the working, not just the number."""
    question = (question or "").strip()
    plan = parse(question, rows)
    result: dict[str, Any] = {
        "question": question, "dataset": dataset, "version": version,
        "metric": plan["metric"], "field": plan["field"],
        "group_by": plan["group_by"], "interpretation": describe(plan),
        "rows": [], "chart": {}, "answer": "", "confidence": 0.0,
        "caveats": [], "understood": True,
    }
    if not rows:
        result["answer"] = "This version has no rows yet."
        result["understood"] = False
        result["confidence"] = 0.0
        result["caveats"].append("crawl something into this dataset first")
        return result

    working = list(rows)[:limit]
    for item in plan["filters"]:
        before = len(working)
        working = _apply_filter(working, item["field"], item["op"], item["value"])
        if not working:
            result["answer"] = (f"No rows match {item['field']} {item['op']} "
                                f"{item['value']!r} ({before} rows considered).")
            result["understood"] = False
            result["confidence"] = 0.3
            result["caveats"].append("the filter matched nothing — check the field "
                                     "name and the value")
            return result

    if plan["years"]:
        for name in plan["columns"]:
            if not any(re.match(r"^\d{4}-\d{2}", str(row.get(name) or ""))
                       for row in working[:50]):
                continue
            years = set(plan["years"])
            filtered = [row for row in working
                        if _year_of(row.get(name)) in years]
            if filtered:
                working = filtered
                result["caveats"].append(f"filtered to {sorted(years)} on {name}")
                break

    field_name = plan["field"]
    numbers = [numeric(row.get(field_name)) for row in working] if field_name else []
    values = [v for v in numbers if v is not None]

    if field_name and not values and plan["metric"] in (
            "mean", "sum", "median", "max", "min", "distribution", "trend"):
        result["answer"] = (f"{field_name} is not numeric in this version, so I "
                            f"cannot compute a {plan['metric']} of it.")
        result["understood"] = False
        result["confidence"] = 0.4
        result["caveats"].append("sample values: " + ", ".join(
            repr(str(row.get(field_name))[:32]) for row in working[:3]))
        return result

    if plan["metric"] == "count" and not plan["group_by"]:
        result["rows"] = [{"rows": len(working)}]
        result["answer"] = f"{len(working):,} row(s)"
        result["chart"] = {"type": "stat", "value": len(working), "label": "rows"}
        result["confidence"] = 0.95
        return result

    if plan["metric"] == "list" and field_name:
        order = sorted(working, key=lambda row: (numeric(row.get(field_name)) is None,
                                                 numeric(row.get(field_name)) or 0.0),
                       reverse=True)
        result["rows"] = order[:plan["limit"] or 10]
        result["answer"] = (f"{len(working):,} row(s) match; showing "
                            f"{len(result['rows'])} with {field_name} "
                            f"as the sort key")
        result["confidence"] = 0.8
        _small_sample(result, len(values))
        return result

    if plan["metric"] == "list" or (plan["metric"] == "count" and not field_name):
        result["rows"] = _counts(working, plan["group_by"], plan["limit"] or 20)
        result["answer"] = (f"{len(working):,} row(s)" if not plan["group_by"]
                            else f"{len(working):,} row(s) grouped by "
                                 f"{plan['group_by']}")
        result["confidence"] = 0.8
        return result

    if plan["metric"] == "count":
        result["rows"] = _counts(working, plan["group_by"], plan["limit"] or 20)
        if plan["group_by"]:
            result["answer"] = (f"{len(working):,} row(s) grouped by "
                                f"{plan['group_by']}: "
                                + ", ".join(f"{r[plan['group_by']]}={r['count']:,}"
                                            for r in result["rows"][:5]))
            result["chart"] = _bar(plan["group_by"], "count", result["rows"])
        else:
            result["rows"] = [{"rows": len(working)}]
            result["answer"] = f"{len(working):,} row(s)"
            result["chart"] = {"type": "stat", "value": len(working), "label": "rows"}
        result["confidence"] = 0.9
        return result

    if plan["metric"] == "trend":
        rows_out = _trend(working, field_name)
        result["rows"] = rows_out
        result["answer"] = _trend_sentence(rows_out, field_name)
        result["chart"] = {"type": "line", "x": "period", "y": field_name,
                           "points": [{"x": r["period"], "y": r[field_name]}
                                      for r in rows_out]}
        result["confidence"] = 0.85 if len(rows_out) > 2 else 0.6
        if len(rows_out) <= 1:
            result["caveats"].append("no dates found: fell back to row order")
        return result

    if plan["metric"] == "distribution":
        buckets = _buckets(values)
        result["rows"] = buckets
        result["answer"] = (f"{field_name}: min {fmt(min(values))}, median "
                            f"{fmt(statistics.median(values))}, max {fmt(max(values))} "
                            f"(n={len(values):,})")
        result["chart"] = {"type": "histogram", "x": field_name, "y": "count",
                           "points": [{"x": b["bucket"], "y": b["count"]}
                                      for b in buckets]}
        result["confidence"] = 0.85
        _small_sample(result, len(values))
        return result

    if plan["group_by"]:
        rows_out = _grouped(working, plan["group_by"], field_name, plan["metric"],
                            plan["limit"] or 10)
        result["rows"] = rows_out
        result["answer"] = (f"{plan['metric']} of {field_name} by {plan['group_by']}: "
                            + ", ".join(f"{r[plan['group_by']]} = {fmt(r[field_name])}"
                                        for r in rows_out[:5]))
        result["chart"] = _bar(plan["group_by"], field_name, rows_out)
        result["confidence"] = 0.85
        _small_sample(result, len(values))
        return result

    if not values:
        result["answer"] = ("I could not find a numeric field to aggregate. Try "
                            + " · ".join(examples(rows)[:3]))
        result["understood"] = False
        result["confidence"] = 0.2
        return result

    aggregate = {"mean": statistics.fmean, "median": statistics.median, "sum": sum,
                 "max": max, "min": min}[plan["metric"]](values)
    result["answer"] = (f"{plan['metric']} of {field_name} = {fmt(aggregate)} "
                        f"(n={len(values):,})")
    result["chart"] = {"type": "stat", "value": aggregate, "label": plan["metric"],
                       "field": field_name}
    result["confidence"] = 0.9
    if plan["metric"] in ("max", "min"):
        ordered = sorted(working, key=lambda row: (numeric(row.get(field_name))
                                                   if numeric(row.get(field_name))
                                                   is not None else
                                                   (float("-inf") if plan["metric"]
                                                    == "max" else float("inf"))),
                         reverse=plan["metric"] == "max")
        result["rows"] = ordered[:plan["limit"] or 5]
    _small_sample(result, len(values))
    return result


def _small_sample(result: dict[str, Any], count: int) -> None:
    if count < 8:
        result["caveats"].append(f"only {count} numeric value(s) — treat as indicative")
        result["confidence"] = min(result["confidence"], 0.6)


def _year_of(value: Any) -> int | None:
    match = re.match(r"^(\d{4})", str(value or ""))
    if match:
        return int(match.group(1))
    if isinstance(value, (int, float)) and 1_000_000_000 < value < 4_000_000_000:
        import datetime as _dt

        return _dt.datetime.fromtimestamp(float(value), tz=_dt.UTC).year
    return None


def _counts(rows: Sequence[dict[str, Any]], group_by: str, limit: int
            ) -> list[dict[str, Any]]:
    if not group_by:
        return [{"rows": len(rows)}]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(group_by) or "(empty)")[:60]
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{group_by: key, "count": count} for key, count in ordered]


def _grouped(rows: Sequence[dict[str, Any]], group_by: str, field_name: str,
             metric: str, limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        value = numeric(row.get(field_name))
        if value is None:
            continue
        key = str(row.get(group_by) or "(empty)")[:60]
        buckets.setdefault(key, []).append(value)
    out = []
    for key, values in buckets.items():
        if metric == "sum":
            value = sum(values)
        elif metric == "median":
            value = statistics.median(values)
        elif metric == "min":
            value = min(values)
        elif metric == "max":
            value = max(values)
        else:
            value = statistics.fmean(values)
        out.append({group_by: key, field_name: round(value, 4), "n": len(values)})
    out.sort(key=lambda row: row[field_name], reverse=metric != "min")
    return out[:limit]


def _trend(rows: Sequence[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for index, row in enumerate(rows):
        period = ""
        for name in ("fetched_at", "published_at", "published", "date", "updated_at",
                     "crawled_at"):
            stamp = str(row.get(name) or "")
            match = re.match(r"^(\d{4})-(\d{2})", stamp)
            if match:
                period = f"{match.group(1)}-{match.group(2)}"
                break
            if row.get(name) is not None and not stamp:
                year = _year_of(row.get(name))
                if year:
                    period = str(year)
                    break
        period = period or f"row {index + 1}"
        value = numeric(row.get(field_name))
        if value is not None:
            buckets.setdefault(period, []).append(value)
    return [{"period": period, field_name: round(statistics.fmean(values), 4),
             "n": len(values)} for period, values in sorted(buckets.items())
            if not period.startswith("row ")][:200] or [
        {"period": period, field_name: round(statistics.fmean(values), 4),
         "n": len(values)} for period, values in sorted(buckets.items())][:200]


def _trend_sentence(rows: Sequence[dict[str, Any]], field_name: str) -> str:
    if len(rows) < 2:
        return f"only {len(rows)} period(s) of {field_name} — no trend to draw yet"
    first, last = rows[0][field_name], rows[-1][field_name]
    direction = "up" if last > first else ("down" if last < first else "flat")
    change = (last - first) / first * 100 if first else 0.0
    return (f"{field_name} is {direction} {abs(change):.1f}% between "
            f"{rows[0]['period']} and {rows[-1]['period']} "
            f"({fmt(first)} → {fmt(last)}, n={sum(r['n'] for r in rows):,})")


def _buckets(values: Sequence[float], *, bins: int = 8) -> list[dict[str, Any]]:
    if not values:
        return []
    low, high = min(values), max(values)
    if math.isclose(low, high, abs_tol=1e-9):
        return [{"bucket": fmt(low), "count": len(values)}]
    width = (high - low) / bins
    out = []
    for index in range(bins):
        start = low + index * width
        end = start + width
        count = sum(1 for value in values
                    if start <= value < end or (index == bins - 1 and value == high))
        out.append({"bucket": f"{fmt(round(start, 2))}–{fmt(round(end, 2))}",
                    "count": count})
    return out


def _bar(x: str, y: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "bar", "x": x, "y": y,
            "points": [{"x": str(row.get(x)), "y": row.get(y, 0)} for row in rows]}


def caveats_for(dataset_quality: dict[str, Any] | None, rows: Sequence[dict[str, Any]],
                *, sensitivity: str = "") -> list[str]:
    """Warnings attached to every answer, driven by stored data quality."""
    quality = dataset_quality or {}
    out: list[str] = []
    if len(rows) < 10:
        out.append(f"only {len(rows)} row(s) in this version")
    for warning in (quality.get("warnings") or [])[:3]:
        out.append(str(warning))
    mix = quality.get("currency_mix") or {}
    for name, codes in mix.items():
        out.append(CAVEAT_CURRENCY.format(field=name, codes=sorted(codes)))
    if sensitivity == "confidential":
        out.append("this dataset is marked confidential: answers stay tenant-scoped")
    return out
