"""Tests for the dependency-free selector engine and the NLQ grammar.

These two modules exist so the platform does not depend on luck: extraction
must work without BeautifulSoup installed, and "ask your data" must never
invent a number it cannot compute. Both are exercised here with fixtures that
look like real pages and real questions.
"""

from __future__ import annotations

import pytest

from src.recipes.dom import Element, find_by_text, parse, select_all, select_text
from src.studio import nlq

# --------------------------------------------------------------------------- #
# selector engine
# --------------------------------------------------------------------------- #
SHOP = """
<html><head><title>Shop</title>
<meta property="product:price:amount" content="1099.00">
</head><body>
<nav><a href="/">Home</a><a href="/cart">Cart</a></nav>
<div class="grid">
  <article class="card promoted" data-sku="PX10P">
    <h2 class="title">Pixel 10 Pro</h2>
    <span class="price" data-currency="USD">$1,099.00</span>
    <a class="buy" href="/buy/px10p">Buy now</a>
  </article>
  <article class="card" data-sku="PX9">
    <h2 class="title">Pixel 9</h2>
    <span class="price" data-currency="USD">$799.00</span>
    <a class="buy" href="/buy/px9">Buy now</a>
  </article>
</div>
<footer><p>© 2026 Shop. All rights reserved.</p></footer>
</body></html>
"""


def test_parse_builds_a_tree_and_ignores_scripts():
    tree = parse("<div><script>var x = 1;</script><p>hello</p></div>")
    assert tree.tag == "#document"
    text = tree.text
    assert "hello" in text and "var x" not in text


def test_class_id_and_attribute_selectors():
    tree = parse(SHOP)
    assert [e.text for e in select_all(tree, "h2.title")] == ["Pixel 10 Pro", "Pixel 9"]
    assert select_all(tree, "article.promoted")[0].get("data-sku") == "PX10P"
    assert select_all(tree, "article[data-sku=PX9]")[0].text.startswith("Pixel 9")
    assert len(select_all(tree, "span.price[data-currency=USD]")) == 2


def test_combinators_and_comma_lists():
    tree = parse(SHOP)
    assert len(select_all(tree, "div.grid > article")) == 2
    assert len(select_all(tree, "nav a, footer p")) == 3
    assert [e.text for e in select_all(tree, ".grid .title")] == ["Pixel 10 Pro", "Pixel 9"]


def test_reading_attributes_and_first_last():
    tree = parse(SHOP)
    assert select_text(tree, "a.buy@href") == ["/buy/px10p", "/buy/px9"]
    assert select_text(tree, "a.buy::attr(href)") == ["/buy/px10p", "/buy/px9"]
    assert select_text(tree, "h2.title:first") == ["Pixel 10 Pro"]
    assert select_text(tree, "h2.title:last") == ["Pixel 9"]


def test_attribute_operators_and_missing_tags_are_tolerated():
    tree = parse(SHOP)
    assert len(select_all(tree, "[class*=card]")) == 2
    assert select_all(tree, "table td") == []
    assert select_all(tree, "not-a-real-selector!!!") == []


def test_find_by_text_is_the_healing_primitive():
    tree = parse('<div><span>Price</span><span>$12.50</span></div>')
    nodes = find_by_text(tree, "Price", tags=("span",))
    assert nodes and nodes[0].text == "Price"
    assert find_by_text(tree, "Nothing here") == []


def test_element_is_json_friendly_for_debugging():
    tree = parse("<div class='a'><p>hi</p></div>")
    row = next(e for e in tree.iter() if e.tag == "div").to_dict(depth=1)
    assert row["attrs"]["class"] == "a" and row["children"][0]["text"] == "hi"
    assert isinstance(row, dict)


# --------------------------------------------------------------------------- #
# NLQ — the grammar
# --------------------------------------------------------------------------- #
ROWS = [
    {"url": "https://shop.test/a", "brand": "Google", "price": "$1,099.00",
     "fetched_at": "2026-01-05T09:00:00Z", "title": "Pixel 10 Pro"},
    {"url": "https://shop.test/b", "brand": "Google", "price": "$799.00",
     "fetched_at": "2026-01-05T09:05:00Z", "title": "Pixel 9"},
    {"url": "https://shop.test/c", "brand": "Samsung", "price": "$1,299.00",
     "fetched_at": "2026-02-11T09:00:00Z", "title": "Galaxy S24 Ultra"},
    {"url": "https://shop.test/d", "brand": "Samsung", "price": "$499.00",
     "fetched_at": "2026-02-12T09:00:00Z", "title": "Galaxy A55"},
    {"url": "https://shop.test/e", "brand": "Google", "price": "$1,099.00",
     "fetched_at": "2026-03-01T09:00:00Z", "title": "Pixel 10 Pro refresh"},
]


@pytest.mark.parametrize("question,expected", [
    ("how many rows are there?", "5"),
    ("what is the average price?", "959"),
    ("what is the highest price?", "1,299"),
    ("what is the cheapest price?", "499"),
    ("total price", "4,795"),
])
def test_aggregate_questions(question, expected):
    answer = nlq.answer(question, ROWS, dataset="prices", version="v1")
    assert expected in answer["answer"].replace("average", ""), answer
    assert answer["confidence"] >= 0.6
    assert answer["interpretation"]


def test_group_by_returns_rows_and_a_chart_spec():
    answer = nlq.answer("average price by brand", ROWS)
    # ordered by the value being measured, highest first
    assert [row["brand"] for row in answer["rows"]] == ["Google", "Samsung"]
    assert answer["rows"][0]["price"] == pytest.approx(999.0)
    assert answer["chart"]["type"] == "bar"
    assert answer["chart"]["points"][0]["x"] == "Google"


def test_filters_and_quoted_values():
    answer = nlq.answer("how many rows where brand is Google?", ROWS)
    assert "3" in answer["answer"]
    quoted = nlq.answer("show the rows with brand 'Samsung'", ROWS)
    assert quoted["metric"] in ("list", "count")
    assert all(row.get("brand") == "Samsung" for row in quoted["rows"]
               if "brand" in row) or quoted["rows"]


def test_numeric_filter_and_no_match_is_stated_not_guessed():
    answer = nlq.answer("list the rows where price > 1000", ROWS)
    assert answer["metric"] == "list"
    empty = nlq.answer("list the rows where price > 99000", ROWS)
    assert empty["understood"] is False
    assert "No rows match" in empty["answer"]


def test_trend_uses_dates_and_says_so():
    answer = nlq.answer("show price over time", ROWS)
    assert answer["chart"]["type"] == "line"
    periods = [row["period"] for row in answer["rows"]]
    assert periods == sorted(periods) and periods[0].startswith("2026-")
    assert "between 2026-01 and 2026-03" in answer["answer"]


def test_distribution_and_small_sample_caveat():
    answer = nlq.answer("distribution of price", ROWS)
    assert answer["chart"]["type"] == "histogram"
    assert sum(row["count"] for row in answer["rows"]) == len(ROWS)
    assert any("indicative" in c for c in answer["caveats"])


def test_price_objects_and_european_numbers_still_parse():
    rows = [{"price": {"amount": 899.0, "currency": "USD"}},
            {"price": {"amount": 1199.0, "currency": "USD"}}]
    assert nlq.answer("average price", rows)["answer"].endswith("(n=2)")
    assert nlq.numeric("1.199,00") == 1199.0
    assert nlq.numeric("12,999") == 12999.0
    assert nlq.numeric("$1,299.00") == 1299.0
    assert nlq.numeric(True) is None      # a boolean is not a number


def test_a_non_numeric_field_is_refused_with_the_reason():
    answer = nlq.answer("average title", ROWS)
    assert answer["understood"] is False
    assert "not numeric" in answer["answer"]


def test_examples_match_the_actual_columns():
    questions = nlq.examples(ROWS)
    assert any("price" in q for q in questions)
    parsed = [nlq.parse(q, ROWS) for q in questions]
    assert all(plan["metric"] != "list" or plan["field"] for plan in parsed)
    assert {"price", "brand"} <= set(nlq.columns(ROWS))


def test_caveats_carry_the_quality_warnings():
    quality = {"warnings": ["2 duplicate url value(s)"],
               "currency_mix": {"price": {"USD": 4, "BDT": 1}}}
    caveats = nlq.caveats_for(quality, ROWS)
    assert any("duplicate" in c for c in caveats)
    assert any("USD" in c and "BDT" in c for c in caveats)


def test_empty_dataset_answers_honestly():
    answer = nlq.answer("how many rows are there?", [])
    assert answer["understood"] is False
    assert "no rows" in answer["answer"].lower()
