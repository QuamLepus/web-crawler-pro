"""Tests for Data Studio (living datasets) and the recipe registry.

The behaviours worth pinning:

* a version is immutable and carries quality metrics + a checksum;
* a *refresh* that changes nothing must not create a version (otherwise a
  daily monitor produces 365 identical versions a year);
* the diff is row-level and says which fields moved;
* a recipe that fails its own fixtures must not install;
* a recipe whose selectors broke heals via labels **and says so**.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from src.recipes.models import Field, Recipe, sort_versions
from src.recipes.models import TestCase as RecipeTestCase
from src.recipes.registry import RecipeRegistry
from src.recipes.runtime import apply_recipe, draft_from_html
from src.recipes.runtime import test_recipe as run_recipe_tests
from src.studio.service import DatasetStore, quality_report, summarise


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
PAGE_V1 = """
<html><head><title>Pixel 9 — Shop</title>
<meta name="description" content="Flagship phone">
</head><body>
<h1>Pixel 9</h1>
<div class="price">$799</div>
<div class="author">Ada Lovelace</div>
<time datetime="2026-01-05">5 January 2026</time>
</body></html>
"""

PAGE_V2 = """
<html><head><title>Pixel 10 — Shop</title>
<meta name="description" content="New flagship phone">
</head><body>
<h1>Pixel 10</h1>
<div class="price">$1,099</div>
<div class="author">Ada Lovelace</div>
<time datetime="2026-02-05">5 February 2026</time>
</body></html>
"""

#: same as V2 but the price class was renamed — the heal path has to find it
PAGE_HEAL = """
<html><head><title>Pixel 10 — Shop</title></head><body>
<h1>Pixel 10</h1>
<div class="product__cost">$1,099</div>
</body></html>
"""


def _recipe() -> Recipe:
    return Recipe(
        name="pixel-shop",
        version="1.0.0",
        description="Pixel product pages",
        hosts=["shop.test"],
        author="tests",
        fields=[
            Field(name="title", selectors=["h1"], required=True),
            Field(name="price", selectors=[".price", "[data-price]"],
                  transform="price", required=True, fallback_labels=["Price"]),
            Field(name="author", selectors=[".author"], required=False),
        ],
        tests=[RecipeTestCase(name="v1", html=PAGE_V1,
                        expect={"title": "Pixel 9", "price": {"amount": 799.0,
                                                              "currency": "USD"}})],
    )


# --------------------------------------------------------------------------- #
# recipes
# --------------------------------------------------------------------------- #
def test_recipe_digest_changes_when_the_body_changes():
    recipe = _recipe()
    assert recipe.verify() is True
    recipe.fields[0].selectors = ["h1.new"]
    assert recipe.verify() is False          # edited after being addressed


def test_recipe_validation_requires_tests_and_a_required_field():
    bare = Recipe(name="bare", hosts=["x.test"],
                  fields=[Field(name="title", selectors=["h1"])])
    problems = bare.validate()
    assert any("no tests" in p for p in problems)
    assert any("required" in p for p in problems)


def test_apply_recipe_extracts_and_reports_coverage():
    result = apply_recipe(_recipe(), PAGE_V1, page_url="https://shop.test/p/1")
    assert result.ok
    assert result.values["title"] == "Pixel 9"
    assert result.values["price"] == {"amount": 799.0, "currency": "USD"}
    assert result.missing == []
    assert result.confidence > 0.8


def test_apply_recipe_heals_via_label_and_says_so():
    result = apply_recipe(_recipe(), PAGE_HEAL, page_url="https://shop.test/p/2")
    assert "price" in result.healed
    assert result.values["price"]["amount"] == 1099.0
    assert result.values["title"] == "Pixel 10"
    assert "price" in result.values


def test_recipe_tests_pass_and_fail_visibly():
    recipe = _recipe()
    assert run_recipe_tests(recipe).verdict == "pass"

    stale = _recipe()
    stale.tests = [RecipeTestCase(name="stale",
                                  html="<html><body><p>nothing</p></body></html>",
                                  expect={"title": "Pixel 9"}, strict=True)]
    health = run_recipe_tests(stale)
    assert health.verdict == "fail"
    assert health.failures and "title" in health.failures[0]["problems"][0]


def test_registry_installs_versions_and_refuses_invalid(tmp_path):
    registry = RecipeRegistry(tmp_path / "recipes")
    result = registry.install(_recipe(), run_tests=True)
    assert result["installed"] == "pixel-shop@1.0.0"

    newer = _recipe()
    newer.version = "1.1.0"
    newer.checksum = ""
    newer.fields[1].selectors = [".price-now", ".price"]
    registry.install(newer, run_tests=True)

    assert registry.versions("pixel-shop") == ["1.1.0", "1.0.0"]
    assert registry.get("pixel-shop").version == "1.1.0"
    assert registry.get("pixel-shop", "1.0.0").version == "1.0.0"
    assert registry.for_host("www.shop.test") is not None
    assert registry.for_host("other.test") is None

    broken = _recipe()
    broken.version = "2.0.0"
    broken.checksum = "sha256:deadbeef"
    with pytest.raises(ValueError):
        registry.install(broken)


def test_registry_publish_and_removal(tmp_path):
    registry = RecipeRegistry(tmp_path / "recipes")
    registry.install(_recipe())
    artifact = registry.publish("pixel-shop")
    payload = json.loads(open(artifact["artifact"], encoding="utf-8").read())
    assert payload["name"] == "pixel-shop" and payload["checksum"]
    assert payload["digest"].startswith("sha256:")

    assert registry.remove("pixel-shop", "1.0.0") == 1
    assert registry.versions("pixel-shop") == []
    assert registry.stats()["recipes"] == 0


def test_draft_recipe_from_html_is_never_publishable_as_is(tmp_path):
    draft = draft_from_html("shop-draft", PAGE_V1, host="shop.test")
    assert draft.version == "0.1.0"
    problems = draft.validate()
    assert any("no tests" in p for p in problems)   # a draft must admit it is a draft


def test_semver_sorting():
    assert sort_versions(["1.0.0", "10.0.0", "2.1.0"]) == ["10.0.0", "2.1.0", "1.0.0"]


# --------------------------------------------------------------------------- #
# datasets
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    return DatasetStore(fakeredis.FakeStrictRedis(decode_responses=True),
                        root=tmp_path / "datasets")


ROWS_V1 = [
    {"url": "https://shop.test/a", "sku": "A", "price": 799.0, "title": "Pixel 9",
     "fetched_at": 1_700_000_000},
    {"url": "https://shop.test/b", "sku": "B", "price": 999.0, "title": "Pixel 9 Pro",
     "fetched_at": 1_700_000_000},
]
ROWS_V2 = [
    {"url": "https://shop.test/a", "sku": "A", "price": 699.0, "title": "Pixel 9",
     "fetched_at": 1_700_100_000},          # price changed
    {"url": "https://shop.test/b", "sku": "B", "price": 999.0, "title": "Pixel 9 Pro",
     "fetched_at": 1_700_100_000},
    {"url": "https://shop.test/c", "sku": "C", "price": 1099.0, "title": "Pixel 10",
     "fetched_at": 1_700_100_000},          # new row
]


def test_dataset_versions_carry_quality_and_checksums(store):
    dataset = store.create("t1", "prices", primary_key="url")
    v1 = store.add_version(dataset, ROWS_V1, note="first crawl")
    assert v1.version == "v1" and v1.rows == 2
    assert v1.checksum.startswith("sha256:")
    assert v1.quality["rows"] == 2
    assert v1.quality["completeness"]["price"] == 1.0
    assert dataset.latest.version == "v1"
    assert store.rows(dataset) == ROWS_V1

    v2 = store.add_version(dataset, ROWS_V2, source="watch:abc")
    assert v2.version == "v2"
    assert dataset.versions[0].rows == 2      # v1 is immutable
    assert any("added" in line for line in v2.changelog)
    assert any("changed" in line for line in v2.changelog)


def test_refresh_only_creates_a_version_when_something_moved(store):
    dataset = store.create("t1", "prices", primary_key="url")
    first = store.refresh(dataset, ROWS_V1)
    assert first["changed"] is True and first["version"]["version"] == "v1"

    same = store.refresh(dataset, ROWS_V1)
    assert same["changed"] is False
    assert same["reason"].startswith("no change")
    assert len(dataset.versions) == 1          # the whole point

    moved = store.refresh(dataset, ROWS_V2)
    assert moved["changed"] is True
    assert moved["version"]["version"] == "v2"
    assert moved["moved"] == 2                 # one changed + one added


def test_diff_reports_row_level_moves(store):
    dataset = store.create("t1", "prices", primary_key="url")
    store.add_version(dataset, ROWS_V1)
    store.add_version(dataset, ROWS_V2)
    diff = store.diff(dataset, "v1", "v2").to_dict()
    assert diff["added"] == 1 and diff["changed"] == 1 and diff["unchanged"] == 1
    assert diff["field_changes"]["price"] == 1
    changed = next(r for r in diff["rows"] if r["status"] == "changed")
    assert changed["changes"]["price"] == {"from": 799.0, "to": 699.0}
    assert diff["summary"]


def test_exports_are_stable_and_readable(store):
    dataset = store.create("t1", "prices", primary_key="url")
    store.add_version(dataset, ROWS_V1)
    csv_text, ctype = store.export(dataset, fmt="csv")
    assert ctype == "text/csv"
    assert "sku" in csv_text.splitlines()[0] and "Pixel 9 Pro" in csv_text

    markdown, ctype = store.export(dataset, fmt="markdown")
    assert ctype == "text/markdown"
    assert "# prices" in markdown and "Quality score" in markdown

    as_json, _ = store.export(dataset, fmt="json")
    assert json.loads(as_json)[0]["sku"] == "A"

    ndjson, _ = store.export(dataset, fmt="jsonl")
    assert len(ndjson.strip().splitlines()) == 2


def test_quality_report_flags_duplicates_and_missing_fields():
    rows = [{"url": "https://a.test", "price": 1.0},
            {"url": "https://a.test", "price": None}]
    report = quality_report(rows, key="url")
    assert report["duplicate_rate"] == 0.5
    assert report["completeness"]["price"] == 0.5
    assert report["score"] < 1.0


def test_summarise_answers_numeric_questions_without_an_llm():
    rows = ROWS_V2 + [{"url": "https://shop.test/d", "sku": "D", "price": 499.0,
                       "title": "Pixel 8a", "fetched_at": 1_700_100_000}]
    digest = summarise(rows, numeric_fields=["price"])
    assert digest["numeric"]["price"]["min"] == 499.0
    assert digest["numeric"]["price"]["max"] == 1099.0
    assert digest["numeric"]["price"]["count"] == 4
