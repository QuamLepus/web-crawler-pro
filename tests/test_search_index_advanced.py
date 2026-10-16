"""The three optional layers on top of the own search index.

1. **Semantic reranking** — BM25 shortlists, a bi-encoder reorders.
2. **Spelling correction** — trained on the index's own vocabulary.
3. **Incremental refresh** — re-ingest a crawl, re-index only what changed.

Fully offline. ``sentence-transformers`` is not installed in CI, so the reranker
tests inject a deterministic fake embedder: they prove the *plumbing* (vector
storage, cosine blending, shard lookup, graceful degradation) rather than the
quality of any particular model. The one thing they do assert about models is
the thing that matters operationally — that a missing model turns reranking off
instead of silently doing nothing useful.
"""

from __future__ import annotations

import gzip
from typing import ClassVar

import fakeredis
import numpy as np
import pytest
import redis as real_redis


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        real_redis, "from_url",
        lambda url, **kw: fakeredis.FakeStrictRedis(
            server=server, decode_responses=kw.get("decode_responses", True)))
    monkeypatch.setattr(
        real_redis.Redis, "from_url",
        classmethod(lambda cls, url, **kw: real_redis.from_url(url, **kw)))
    yield server


# Every row contains "research" on purpose: it gives BM25 four candidates for
# the query "research", so the reranker has something to reorder.
CURLIE_C = """\
http://solar.example/\tSolar Alliance\tSolar power research and energy policy\t2
https://nasa.example/\tNASA\tSpace exploration research, astronomy and astrophysics\t1
http://oss.example/\tOpen Source Hub\tOpen source software research tools, used automobiles\t3
http://cars.example/\tAuto Weekly\tAutomobile reviews, research notes about automobiles\t3
"""


@pytest.fixture
def index_dir(tmp_path, monkeypatch):
    from src.config import get_config

    root = tmp_path / "index"
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "rdf-a-c.tsv.gz").write_bytes(gzip.compress(CURLIE_C.encode()))
    get_config().SEARCH_INDEX_DIR = str(root)
    yield root
    get_config().SEARCH_INDEX_DIR = "data/search-index"


def _build(root, **overrides) -> object:
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=root, shard_docs=100,
                           download=False, authority=False, **overrides)
    with SearchIndexBuilder(root, options=options) as builder:
        builder.build()
    return builder


# =========================================================================== #
# 1. semantic reranking
# =========================================================================== #
class _FakeEmbedder:
    """Deterministic stand-in for a sentence-transformers model.

    Each text is projected onto a 4-d basis where dimension 0 tracks "space",
    dimension 1 "energy", dimension 2 "software" and dimension 3 "vehicles" —
    so cosine similarity genuinely means something in these tests.
    """

    dim = 4
    model_name = "fake/mini-encoder"

    _TOPICS: ClassVar[dict[str, tuple[str, ...]]] = {
        "space": ("nasa", "space", "astronomy", "astrophysics", "exploration"),
        "energy": ("solar", "energy", "power", "renewable"),
        "software": ("software", "open source", "oss", "projects", "directory"),
        "vehicles": ("automobiles", "vehicles", "driving", "auto", "reviews"),
    }

    def encode(self, texts: list[str]):
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, text in enumerate(texts):
            lowered = text.lower()
            for axis, (topic, keywords) in enumerate(self._TOPICS.items()):
                out[i, axis] = sum(1.0 for kw in keywords if kw in lowered)
                if topic in lowered:
                    out[i, axis] += 2.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


def test_noop_reranker_preserves_bm25_order(index_dir):
    from src.search_index.query import SearchIndex
    from src.search_index.rerank import NoopReranker

    _build(index_dir)
    with SearchIndex(f"{index_dir}/index") as index:
        plain = [r.doc.host for r in index.search("power", 10)]
        reranked = NoopReranker().rerank("power", index.search("power", 10), top_k=10)
    assert [r.doc.host for r in reranked] == plain


def test_embedding_reranker_reorders_by_meaning(index_dir):
    """'automobiles' shares no words with 'Auto Weekly' — but means the same."""
    from src.search_index.query import SearchIndex
    from src.search_index.rerank import EmbeddingReranker

    _build(index_dir)
    with SearchIndex(f"{index_dir}/index") as index:
        lexical = index.search("research", 10)
        assert len(lexical) >= 2, "fixture should give BM25 several candidates"
        hosts_before = [r.doc.host for r in lexical]

        reranker = EmbeddingReranker(embedder=_FakeEmbedder(), weight=1.0)
        reranked = reranker.rerank("automobiles", lexical, top_k=5)
        assert next(r.doc.host for r in reranked) == "cars.example"
        assert hosts_before[0] != "cars.example", "BM25 alone must not find it"
        # the semantic score is recorded so callers can inspect it
        assert reranked[0].semantic > 0.0


def test_rerank_blend_weight_moves_the_result(index_dir):
    """weight=0 must be a no-op; weight=1 must be pure semantics."""
    from src.search_index.query import SearchIndex
    from src.search_index.rerank import EmbeddingReranker

    _build(index_dir)
    with SearchIndex(f"{index_dir}/index") as index:
        candidates = index.search("research", 10)
        assert len(candidates) >= 2
        weak = EmbeddingReranker(embedder=_FakeEmbedder(), weight=0.0).rerank(
            "automobiles", list(candidates), top_k=5)
        strong = EmbeddingReranker(embedder=_FakeEmbedder(), weight=1.0).rerank(
            "automobiles", list(candidates), top_k=5)
    assert [r.doc.host for r in weak] == [r.doc.host for r in candidates]
    assert next(r.doc.host for r in strong) == "cars.example"


def test_reranker_refuses_the_lexical_hash_fallback(index_dir, monkeypatch):
    """No sentence-transformers => reranking off, not quietly useless."""
    from src.search_index.rerank import EmbeddingReranker, build_reranker

    class _Hashing:
        dim = 256

        def encode(self, texts):
            return np.zeros((len(texts), 256), dtype="float32")

    monkeypatch.setattr(
        "src.training.semantic_dedupe.make_embedder", lambda name=None: _Hashing())
    reranker = EmbeddingReranker()
    assert not reranker.available
    assert "hashing" in reranker.disabled_reason
    # and it must not crash or reorder when asked anyway
    from src.search_index.query import SearchIndex

    _build(index_dir)
    with SearchIndex(f"{index_dir}/index") as index:
        before = [r.doc.host for r in index.search("solar", 5)]
        assert [r.doc.host for r in reranker.rerank("solar", index.search("solar", 5),
                                                    top_k=5)] == before

    assert build_reranker("nonsense-mode").name == "none"
    assert build_reranker("none").name == "none"


def test_unknown_rerank_mode_falls_back_to_none():
    from src.search_index.rerank import build_reranker

    assert build_reranker("quantum").name == "none"
    assert build_reranker("cross-encoder").name == "cross-encoder"


def test_precomputed_shard_vectors_are_written_and_used(index_dir):
    """`build --embeddings` stores vectors; the reranker reads them back."""
    from src.search_index.query import SearchIndex
    from src.search_index.rerank import ShardEmbeddingReranker

    _build(index_dir, embeddings=True)
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    builder = SearchIndexBuilder(
        index_dir, options=BuildOptions(level=0, data_dir=index_dir,
                                        download=False, embeddings=True))
    # the default (hashing) embedder must be refused, so force the fake one
    builder._embedder = _FakeEmbedder()
    builder._embeddings_disabled = False
    try:
        written = builder.build_embeddings()
        assert written == 1, "expected one shard to receive vectors"
    finally:
        builder.close()

    shard_dir = next((index_dir / "index").glob("shard-*"))
    vectors, meta = __import__(
        "src.search_index.bm25", fromlist=["x"]).BM25Shard.load_embeddings(shard_dir)
    assert meta["dim"] == 4
    assert vectors.shape == (meta["docs"], 4)

    with SearchIndex(f"{index_dir}/index") as index:
        candidates = index.search("research", 10)
        reranker = ShardEmbeddingReranker(f"{index_dir}/index",
                                          embedder=_FakeEmbedder(), weight=1.0)
        reranked = reranker.rerank("automobiles", candidates, top_k=5)
    assert next(r.doc.host for r in reranked) == "cars.example"
    assert reranker._shard_vectors(0) is not None
    assert reranker._shard_vectors(999) is None


def test_search_honours_the_rerank_argument(index_dir):
    from src.search_index.query import SearchIndex

    _build(index_dir)
    from src.search_index.rerank import EmbeddingReranker

    with SearchIndex(f"{index_dir}/index") as index:
        index._reranker = EmbeddingReranker(embedder=_FakeEmbedder(), weight=1.0)
        plain = index.search("automobiles", 5, rerank="none")
        semantic = index.search("automobiles", 5, rerank="embedding")

    assert plain, "the fixture should match 'automobiles' in two documents"
    assert [r.semantic for r in plain] == [0.0] * len(plain), \
        "rerank=none must not score semantics"
    assert all(r.semantic > 0.0 for r in semantic), \
        "rerank=embedding must record a semantic score on every hit"
    assert next(r.doc.host for r in semantic) == "cars.example"
    # sorted by the blended score, semantic first
    assert [r.semantic for r in semantic] == sorted(
        (r.semantic for r in semantic), reverse=True)


# =========================================================================== #
# 2. spelling correction
# =========================================================================== #
def _freqs() -> dict[str, int]:
    """A tiny corpus vocabulary: 'solar' is common, 'solar' typos are not."""
    return {
        "solar": 500, "energy": 400, "panel": 300, "panels": 600,
        "kubernetes": 220, "docker": 210, "wikipedia": 900,
        "astronomy": 150, "renewable": 180, "research": 160,
        "solr": 2, "kubernets": 1, "wikipdia": 1,
    }


def test_spell_checker_corrects_obvious_typos():
    from src.search_index.spell import SpellChecker

    checker = SpellChecker.from_terms(_freqs())
    assert checker.suggest("solr") == "solar"
    assert checker.suggest("kubernets") == "kubernetes"
    assert checker.suggest("wikipdia") == "wikipedia"
    assert checker.suggest("panals") == "panels"


def test_spell_checker_leaves_real_words_and_numbers_alone():
    from src.search_index.spell import SpellChecker

    checker = SpellChecker.from_terms(_freqs())
    assert checker.suggest("solar") is None          # already known
    assert checker.suggest("energy") is None
    assert checker.suggest("gpt4") is None           # digits: never touch
    assert checker.suggest("ai") is None             # too short
    assert checker.suggest("zzzzzzzz") is None       # nothing close enough


def test_spell_checker_is_timid_about_rare_but_real_words():
    """A rare word must not be rewritten into a common neighbour."""
    from src.search_index.spell import SpellChecker

    checker = SpellChecker.from_terms({"solar": 500, "solr": 40})
    assert checker.suggest("solr") is None, "solr is a real term in this corpus"


def test_spell_checker_answers_with_the_word_a_human_wrote():
    """The dictionary is built from stems; the *answer* must not be one.

    "kubernetes" is indexed as "kubernet". Searching with either works, but
    telling someone "showing results for kubernet" is wrong.
    """
    from src.search_index.spell import SpellChecker

    checker = SpellChecker.from_terms({"kubernet": 50, "redis": 40, "solar": 30})
    checker.display = {"kubernet": "kubernetes", "redis": "redis"}
    assert checker.suggest("kubernets") == "kubernetes"
    assert checker.suggest("reddis") == "redis"
    # a stem with no recorded surface form is returned as-is
    assert checker.suggest("solr") == "solar"
    assert checker.show("kubernet") == "kubernetes"
    assert checker.show("solar") == "solar"


def test_index_build_records_surface_forms_for_every_shard(index_dir):
    """`surface.json` is written next to vocab.json and read back by spell."""
    from src.search_index.bm25 import SURFACE_FILE
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.spell import SPELL_FILE, SpellChecker, SpellConfig

    _build(index_dir)
    shard_dirs = sorted((index_dir / "index").glob("shard-*"))
    assert shard_dirs, "the fixture should produce at least one shard"
    for directory in shard_dirs:
        assert (directory / SURFACE_FILE).exists(), f"{directory} has no surfaces"

    with SearchIndexBuilder(index_dir, options=BuildOptions(
            level=0, data_dir=index_dir, download=False)) as builder:
        stats = builder.build_spell(SpellConfig(min_freq=1))

    assert stats["display_forms"] > 0
    checker = SpellChecker.load(index_dir / "index" / SPELL_FILE)
    assert checker is not None and checker.display
    # "solar" is a real word in the corpus and is its own stem
    assert checker.show("solar") == "solar"
    # every suggestion that needs un-stemming gets the human form
    assert checker.suggest("solr") == checker.show(checker.suggest("solr"))


def test_spell_dictionary_comes_from_the_index_vocabulary(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.spell import SPELL_FILE, SpellChecker, SpellConfig

    _build(index_dir)
    with SearchIndexBuilder(index_dir, options=BuildOptions(
            level=0, data_dir=index_dir, download=False)) as builder:
        stats = builder.build_spell(SpellConfig(min_freq=1))

    assert stats["terms"] > 0
    checker = SpellChecker.load(index_dir / "index" / SPELL_FILE)
    assert checker is not None
    assert checker.knows("solar"), "corpus vocabulary must be in the dictionary"
    assert checker.suggest("solar") is None
    assert checker.suggest("solr") == "solar"


def test_spell_cli_trains_and_checks(index_dir, capsys):
    from src.cli import main

    _build(index_dir)
    assert main(["search-index", "spell", "--data-dir", str(index_dir),
                 "--min-freq", "1", "--check", "solr,pannels"]) == 0
    out = capsys.readouterr().out
    assert "solr" in out and "solar" in out
    assert "terms" in out


def test_query_spelling_is_applied_and_reported(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.query import SearchIndex
    from src.search_index.spell import SpellConfig

    _build(index_dir)
    with SearchIndexBuilder(index_dir, options=BuildOptions(
            level=0, data_dir=index_dir, download=False)) as builder:
        builder.build_spell(SpellConfig(min_freq=1))

    with SearchIndex(f"{index_dir}/index") as index:
        corrected, corrections = index.correct("solr")
        assert corrected == "solar"
        assert ("solr", "solar") in corrections
        # and the search pipeline uses it
        assert [r.doc.host for r in index.search("solr", 5)] == ["solar.example"]


def test_spell_operators_are_never_corrected(index_dir):
    """Correcting site:.edu into site:.end would be worse than the typo."""
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.query import SearchIndex
    from src.search_index.spell import SpellConfig

    _build(index_dir)
    with SearchIndexBuilder(index_dir, options=BuildOptions(
            level=0, data_dir=index_dir, download=False)) as builder:
        builder.build_spell(SpellConfig(min_freq=1))
    with SearchIndex(f"{index_dir}/index") as index:
        corrected, _corrections = index.correct("solr site:.edu -energyy")
    assert "site:.edu" in corrected, "a host filter must never be corrected"
    assert "energy" in corrected, "the exclusion should still be corrected"
    assert corrected.startswith("solar")


def test_spell_checker_survives_an_untrained_index(tmp_path):
    from src.search_index.query import SearchIndex

    index = SearchIndex(tmp_path / "index")
    assert index.spell_checker is None
    assert index.correct("anything") == ("anything", [])
    index.close()


# =========================================================================== #
# 3. incremental refresh
# =========================================================================== #
def _wet_record(url: str, text: str) -> bytes:
    payload = f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n{text}".encode()
    header = (b"WARC/1.0\r\nWARC-Type: conversion\r\n"
              b"WARC-Date: 2026-09-20T12:00:00Z\r\n"
              b"WARC-Target-URI: " + url.encode() + b"\r\n"
              b"Content-Type: text/plain\r\n"
              b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n")
    return header + payload + b"\r\n\r\n"


PAGES = {
    "CC-MAIN-2026-39": {
        "https://solar.example/guide":
            "A practical guide to solar panels for households, covering "
            "module types, inverter sizing, roof angle and the annual yield "
            "you can expect in a temperate climate with typical winter "
            "shading. We compare warranties, degradation rates and the "
            "questions you should ask an installer before signing a "
            "contract of any kind, and the page is updated whenever the "
            "building regulations change. Every figure is given with the "
            "assumptions that produced it so you can reproduce the "
            "calculation for your own site, and the spreadsheet used to "
            "produce the tables is linked at the end of the article. ",
        "https://wind.example/guide":
            "A practical guide to small wind turbines for households, "
            "covering tower height, rotor diameter, noise limits and the "
            "annual yield you can expect on an exposed site with steady "
            "prevailing wind. Planning permission and neighbourhood noise "
            "decide most domestic installations, so both are discussed at "
            "some length here too, with worked examples from three typical "
            "rural sites in different wind regimes. Every figure is given "
            "with the assumptions that produced it so you can reproduce the "
            "calculation for your own site, and the spreadsheet used to "
            "produce the tables is linked at the end of the article. ",
    },
    "CC-MAIN-2026-40": {
        # unchanged — the incremental refresh must skip it entirely
        "https://solar.example/guide":
            "A practical guide to solar panels for households, covering "
            "module types, inverter sizing, roof angle and the annual yield "
            "you can expect in a temperate climate with typical winter "
            "shading. We compare warranties, degradation rates and the "
            "questions you should ask an installer before signing a "
            "contract of any kind, and the page is updated whenever the "
            "building regulations change. Every figure is given with the "
            "assumptions that produced it so you can reproduce the "
            "calculation for your own site, and the spreadsheet used to "
            "produce the tables is linked at the end of the article. ",
        # changed — a new paragraph about battery storage
        "https://wind.example/guide":
            "A practical guide to small wind turbines for households, "
            "covering tower height, rotor diameter, noise limits and the "
            "annual yield you can expect on an exposed site. Battery "
            "storage is now covered in detail, including sizing, cycle life "
            "and the cost per kilowatt hour stored over ten years of normal "
            "domestic use in practice, with a table comparing four common "
            "chemistry choices for small off grid installations. Every "
            "figure is given with the assumptions that produced it so you "
            "can reproduce the calculation for your own site, and the "
            "spreadsheet used to produce the tables is linked at the end of "
            "the article. ",
        # brand new
        "https://tidal.example/guide":
            "A practical guide to tidal barrages for coastal communities, "
            "covering head height, turbine choice, sediment transport and "
            "the annual energy you can expect from a scheme of this kind in "
            "a macrotidal estuary with a large tidal range and strong "
            "currents. Fish mortality, construction cost and grid "
            "connection are all covered with worked examples drawn from "
            "schemes already built and operating elsewhere in the world "
            "today. Every figure is given with the assumptions that "
            "produced it so you can reproduce the calculation for your own "
            "site, and the spreadsheet used to produce the tables is linked "
            "at the end of the article. ",
    },
}


def _wet_bytes(snapshot: str) -> bytes:
    return gzip.compress(b"".join(
        _wet_record(url, text) for url, text in PAGES[snapshot].items()))


@pytest.fixture
def cc_index(tmp_path, monkeypatch):
    """An index built from CC-MAIN-2026-39 with an injected WET downloader."""
    import src.search_index.sources.commoncrawl as cc
    from src.config import get_config
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    root = tmp_path / "cc"
    (root / "raw").mkdir(parents=True)
    get_config().SEARCH_INDEX_DIR = str(root)

    class _FakeCC(cc.CommonCrawlSource):
        def __init__(self, snapshot=None, **kw):
            super().__init__(snapshot, downloader=lambda url: _wet_bytes(
                snapshot or "CC-MAIN-2026-39"), **kw)

        def paths(self):
            return [f"{self.snapshot}-000000.warc.wet.gz"]

    monkeypatch.setattr(cc, "CommonCrawlSource", _FakeCC)
    options = BuildOptions(level=2, data_dir=root, sources=("commoncrawl",),
                           shard_docs=100, download=False,
                           commoncrawl_snapshot="CC-MAIN-2026-39",
                           incremental=True)
    with SearchIndexBuilder(root, options=options) as builder:
        builder.build()
    yield root
    get_config().SEARCH_INDEX_DIR = "data/search-index"


def test_first_run_indexes_everything_and_records_state(cc_index):
    """Counters are per-process; the database is what survives a restart."""
    from src.search_index.incremental import ChangeTracker

    with ChangeTracker(cc_index / "incremental.sqlite3") as tracker:
        assert tracker.stats()["tracked_documents"] == 2
        assert tracker.last_snapshot() == "CC-MAIN-2026-39"
        state = tracker.get("https://wind.example/guide")
    assert state is not None and state.rowid > 0


def test_refresh_skips_unchanged_and_reindexes_what_changed(cc_index):
    """The whole point: one page skipped, one updated, one added."""
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.query import SearchIndex

    options = BuildOptions(level=2, data_dir=cc_index,
                           sources=("commoncrawl",), shard_docs=100,
                           download=False, incremental=True,
                           commoncrawl_snapshot="CC-MAIN-2026-40")
    with SearchIndexBuilder(cc_index, options=options) as builder:
        stats = builder.refresh("CC-MAIN-2026-40")
        counters = builder.tracker.counters
        snapshots = builder.tracker.snapshots()

    assert counters.unchanged == 1, "the untouched page must be skipped"
    assert counters.changed == 1, "the edited page must be updated in place"
    assert counters.new == 1, "the new page must be added"
    assert stats.state["refresh"]["shards_rebuilt"] >= 1
    assert snapshots == ["CC-MAIN-2026-39", "CC-MAIN-2026-40"]

    # the store must still hold three documents, not four
    from src.search_index.store import DocStore

    with DocStore(cc_index / "docs.sqlite3") as store:
        assert store.count() == 3
        wind = store.get_by_url("https://wind.example/guide")
    assert wind.snapshot == "CC-MAIN-2026-40"
    assert "Battery storage" in wind.body

    # and the rebuilt shard must now find the new vocabulary
    with SearchIndex(f"{cc_index}/index") as index:
        hits = index.search("battery storage", 5, collapse_host=False)
        assert [r.doc.host for r in hits] == ["wind.example"]
        tidal = index.search("tidal barrage", 5, collapse_host=False)
        assert [r.doc.host for r in tidal] == ["tidal.example"]


def test_refresh_without_changes_rebuilds_nothing(cc_index):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=2, data_dir=cc_index,
                           sources=("commoncrawl",), shard_docs=100,
                           download=False, incremental=True,
                           commoncrawl_snapshot="CC-MAIN-2026-39")
    with SearchIndexBuilder(cc_index, options=options) as builder:
        builder.refresh("CC-MAIN-2026-39")
        assert builder.tracker.counters.unchanged == 2
        assert builder.tracker.counters.changed == 0
        assert builder.tracker.dirty_shards() == []


def test_incremental_can_be_switched_off(cc_index):
    """Without the tracker a new page lands, but an *edited* page does not.

    ``INSERT OR IGNORE`` keeps the first version of a URL forever, so without
    the change tracker a refresh can only add — never update. That is exactly
    the failure mode incremental indexing exists to remove.
    """
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.store import DocStore

    options = BuildOptions(level=2, data_dir=cc_index,
                           sources=("commoncrawl",), shard_docs=100,
                           download=False, incremental=False,
                           commoncrawl_snapshot="CC-MAIN-2026-40")
    with SearchIndexBuilder(cc_index, options=options) as builder:
        builder.refresh("CC-MAIN-2026-40")
    with DocStore(cc_index / "docs.sqlite3") as store:
        assert store.count() == 3, "the new page still lands"
        wind = store.get_by_url("https://wind.example/guide")
    assert "Battery storage" not in wind.body, "the edit never reached the store"
    assert wind.snapshot == "CC-MAIN-2026-39"


def test_change_tracker_classification(tmp_path):
    from src.search_index.incremental import (
        CHANGED,
        NEW,
        UNCHANGED,
        ChangeTracker,
    )

    with ChangeTracker(tmp_path / "state.sqlite3") as tracker:
        assert tracker.classify("https://a.example/", "abc") == NEW
        tracker.record("https://a.example/", 7, "abc", "CC-1", 0)
        assert tracker.classify("https://a.example/", "abc", "CC-1") == UNCHANGED
        assert tracker.classify("https://a.example/", "def", "CC-1") == CHANGED
        tracker.mark_dirty(0, "changed")
        assert tracker.dirty_shards() == [0]
        tracker.clear_dirty([0])
        assert tracker.dirty_shards() == []
        tracker.record_snapshot("CC-1")
        assert tracker.last_snapshot() == "CC-1"
        assert tracker.stats()["tracked_documents"] == 1


def test_content_hash_covers_the_indexed_text():
    from src.search_index.incremental import content_hash

    assert content_hash("same") == content_hash("same")
    assert content_hash("same") != content_hash("different")


def test_status_reports_the_optional_layers(index_dir, capsys):
    from src.cli import main
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.spell import SpellConfig

    _build(index_dir)
    with SearchIndexBuilder(index_dir, options=BuildOptions(
            level=0, data_dir=index_dir, download=False)) as builder:
        builder.build_spell(SpellConfig(min_freq=1))

    assert main(["search-index", "status", "--data-dir", str(index_dir)]) == 0
    out = capsys.readouterr().out
    assert "spelling" in out and "incremental" in out and "embeddings" in out


def test_query_endpoint_surfaces_corrections_and_reranker(index_dir):
    from fastapi.testclient import TestClient

    from src.api.main import create_api_app
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.spell import SpellConfig

    _build(index_dir)
    with SearchIndexBuilder(index_dir, options=BuildOptions(
            level=0, data_dir=index_dir, download=False)) as builder:
        builder.build_spell(SpellConfig(min_freq=1))

    from src.config import get_config

    get_config().SEARCH_INDEX_DIR = str(index_dir)
    with TestClient(create_api_app(enable_graphql=False,
                                   enable_rate_limit=False)) as client:
        tok = client.post("/auth/signup", json={
            "org_name": "Adv", "email": "adv@example.com",
            "password": "s3cret-passw0rd!"}).json()
        key = client.post(
            "/keys", headers={"Authorization": f"Bearer {tok['access_token']}"},
            json={"name": "ci", "scopes": ["read", "crawl"]}).json()["key"]
        headers = {"X-API-Key": key}

        got = client.get("/api/v1/search-index/query", headers=headers,
                         params={"q": "solr"})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["corrected_query"] == "solar"
        assert body["corrections"][0]["from"] == "solr"
        assert body["results"]
        assert "reranker" in body

        spelled = client.get("/api/v1/search-index/spell", headers=headers,
                             params={"q": "solr energi"})
        assert spelled.status_code == 200
        assert spelled.json()["corrected_query"] == "solar energy"
        assert spelled.json()["trained"] is True

        stats = client.get("/api/v1/search-index/stats", headers=headers)
        assert stats.status_code == 200
        extras = stats.json()["extras"]
        assert extras["spell"]["trained"] is True
        assert "incremental" in extras
