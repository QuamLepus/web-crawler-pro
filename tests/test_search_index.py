"""The platform's own search index (src/search_index).

Fully offline. Every dataset is synthesised here: a gzipped Curlie TSV, an
Open PageRank CSV, a multi-stream bz2 Wikipedia dump and a gzipped Common Crawl
WET file. The point is to prove the *pipeline* is real — parsing, sharding,
resume, BM25, authority blending, host collapsing, the provider protocol and
the HTTP surface — without depending on 300 GB of downloads or on the network.

What these tests cannot prove is that the real datasets still parse; the
parsers are written tolerantly (columns located by name, records skipped rather
than fatal) precisely because those files change shape between releases.
"""

from __future__ import annotations

import bz2
import gzip
import json

import fakeredis
import pytest
import redis as real_redis

from src.search_index.models import LEVELS, Doc, host_of, level_spec


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


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
CURLIE_STRUCTURE = """\
1\tTop/Science/Astronomy\t1200\tAstronomy sites\t\t
2\tTop/Science/Energy/Solar\t800\tSolar energy\tUS\tCalifornia
3\tTop/Computers/Software/OpenSource\t5000\tOpen source software\t\t
"""

CURLIE_CONTENT = """\
http://solar.example/\tSolar Alliance\tSolar power news, research and policy\t2
http://wind.example/\tWind Watch\tWind energy directory and turbine data\t2
http://astro.example/\tAstro Blog\tAmateur astronomy, telescopes and star charts\t1
https://nasa.example/\tNASA\tSpace exploration, astronomy and astrophysics\t1
http://oss.example/\tOpen Source Hub\tDirectory of open source software projects\t3
http://spam.example/\tCheap Solar Panels Buy Now\tcheap solar cheap solar cheap\t2
"""

PAGERANK_CSV = """\
Rank,Domain,Open Page Rank,Reachable
1,nasa.example,9.6,yes
2,oss.example,7.1,yes
3,solar.example,4.2,yes
4,astro.example,3.3,yes
5,spam.example,0.1,yes
"""

TRANCO_CSV = """\
rank,domain
1,nasa.example
2,oss.example
3,wind.example
"""


def _wikipedia_page(title: str, body: str) -> str:
    return (f"  <page>\n    <title>{title}</title>\n    <ns>0</ns>\n"
            f"    <revision>\n      <text xml:space=\"preserve\">{body}</text>\n"
            f"    </revision>\n  </page>\n")


WIKI_PAGES = [
    _wikipedia_page(
        "Solar energy",
        "{{Infobox energy|name=Solar}}"
        "Solar energy is radiant light and heat from the Sun that is harnessed "
        "using a range of ever-evolving technologies such as solar heating, "
        "photovoltaics, solar thermal energy, solar architecture and artificial "
        "photosynthesis. It is an important source of renewable energy and its "
        "technologies are broadly characterised as either passive solar or "
        "active solar depending on how they capture and distribute solar energy. "
        "The International Energy Agency said in 2021 that solar power would "
        "become the largest source of electricity worldwide. <!-- a comment -->"
        "<ref>Some citation here.</ref> See also [[Wind power|wind power]]."),
    _wikipedia_page(
        "Wind power",
        "Wind power is the use of wind to provide mechanical power through wind "
        "turbines to turn electric generators. Wind power is a popular, "
        "sustainable, renewable energy source that has a much smaller impact on "
        "the environment than burning fossil fuels. Wind farms consist of many "
        "individual wind turbines which are connected to the electric power "
        "transmission network. Historically wind power was used in sails and "
        "windmills, and it remains a growing industry across the world today."),
    _wikipedia_page(
        "Open-source software",
        "Open-source software is computer software that is released under a "
        "license in which the copyright holder grants users the rights to use, "
        "study, change and distribute the software and its source code to anyone "
        "and for any purpose. Open-source software may be developed in a "
        "collaborative public manner. The open-source model permits the free "
        "redistribution of the finished product and it is a prominent example of "
        "open collaboration across many different communities of developers."),
]


def _wet_record(url: str, text: str) -> bytes:
    payload = (f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n{text}").encode()
    header = (b"WARC/1.0\r\n"
              b"WARC-Type: conversion\r\n"
              b"WARC-Date: 2026-09-20T12:00:00Z\r\n"
              b"WARC-Target-URI: " + url.encode() + b"\r\n"
              b"Content-Type: text/plain\r\n"
              b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n")
    return header + payload + b"\r\n\r\n"


def _wet_bytes() -> bytes:
    """Three WET records, each long enough to clear the 400-char page gate."""
    records = [
        _wet_record(
            "https://solarpanel.example/guide",
            "A complete guide to choosing solar panels for your home. We "
            "compare monocrystalline and polycrystalline modules, explain "
            "inverter sizing, and show how roof angle and shading affect the "
            "annual yield of a residential array in different climates around "
            "the world, with worked examples for three typical households. "
            "We also cover warranty terms, degradation rates and what to ask "
            "an installer before signing a contract for a rooftop system."),
        _wet_record(
            "https://solarpanel.example/pricing",
            "Solar panel pricing changes every quarter. This page tracks the "
            "installed cost per watt for residential systems in dozens of "
            "markets and explains what drives the differences between regions "
            "and between installers working in the same city, including "
            "permitting fees, labour rates and the cost of capital, and we "
            "publish the raw dataset behind every chart for researchers so that "
            "the numbers can be checked independently and reused freely."),
        _wet_record(
            "https://marineparts.example/catalog",
            "Marine parts catalog with propellers, shafts, rudders and "
            "through-hull fittings for sailboats and motorboats of every "
            "size, plus rigging hardware, deck equipment and safety gear for "
            "refits. Every part is listed with its material, shaft diameter "
            "and the hull types it is known to fit in production boats, with "
            "drawings and part numbers for the most common configurations, plus "
            "a cross-reference table for older discontinued product lines."),
    ]
    return gzip.compress(b"".join(records))


@pytest.fixture
def raw_dir(tmp_path):
    """A ``raw/`` directory populated with every synthetic dataset."""
    directory = tmp_path / "raw"
    directory.mkdir()
    (directory / "rdf-curlie-s.tsv.gz").write_bytes(
        gzip.compress(CURLIE_STRUCTURE.encode()))
    (directory / "rdf-curlie-c.tsv.gz").write_bytes(
        gzip.compress(CURLIE_CONTENT.encode()))
    (directory / "top10milliondomains.csv").write_text(PAGERANK_CSV, encoding="utf-8")
    (directory / "top-1m.csv").write_text(TRANCO_CSV, encoding="utf-8")
    (directory / "enwiki-20261001-pages-articles-multistream.xml.bz2").write_bytes(
        bz2.compress(b"<mediawiki>\n" + WIKI_PAGES[0].encode()) +
        bz2.compress((WIKI_PAGES[1] + WIKI_PAGES[2]).encode()))
    (directory / "CC-MAIN-2026-39-000000.warc.wet.gz").write_bytes(_wet_bytes())
    return directory


@pytest.fixture
def index_dir(tmp_path, raw_dir):
    """``data-dir`` with ``raw/`` inside it, plus SEARCH_INDEX_DIR pointed at it."""
    from src.config import get_config

    root = tmp_path / "index"
    (root / "raw").mkdir(parents=True, exist_ok=True)
    for path in raw_dir.iterdir():
        (root / "raw" / path.name).write_bytes(path.read_bytes())
    get_config().SEARCH_INDEX_DIR = str(root)
    yield root
    get_config().SEARCH_INDEX_DIR = "data/search-index"


# --------------------------------------------------------------------------- #
# models + text
# --------------------------------------------------------------------------- #
def test_host_of_normalises():
    assert host_of("https://WWW.Example.com:443/path?q=1") == "example.com"
    assert host_of("http://user:pw@host.org/x") == "host.org"
    assert host_of("not a url") == ""


def test_levels_table_is_complete():
    assert sorted(LEVELS) == [0, 1, 2, 3]
    assert level_spec(0).sources == ("curlie",)
    assert level_spec(2).max_pages >= level_spec(2).max_pages
    assert level_spec(99) is LEVELS[3]          # clamps instead of KeyError
    assert level_spec(-1) is LEVELS[0]
    assert level_spec(3).max_pages == 100_000_000


def test_query_parser_understands_operators():
    from src.search_index.text import parse_query

    parsed = parse_query('"solar panel" -blog site:.edu source:wikipedia')
    assert parsed.phrases == [["solar", "panel"]]
    assert parsed.excludes == ["blog"]
    assert parsed.site == ".edu"
    assert parsed.source == "wikipedia"
    assert parsed.terms == []

    plain = parse_query("Renewable Energy Research")
    assert plain.terms == ["renewable", "energy", "research"]


def test_stemmer_is_conservative():
    from src.search_index.text import stem

    assert stem("energies") == "energy"
    assert stem("panels") == "panel"
    assert stem("bus") == "bus"          # must not become "bu"
    assert stem("data") == "data"


def test_highlight_marks_the_matching_terms():
    from src.search_index.text import highlight

    out = highlight("Solar panels convert sunlight into electricity today.",
                    ["solar", "panel"])
    assert "[Solar]" in out and "[panels]" in out


# --------------------------------------------------------------------------- #
# document store
# --------------------------------------------------------------------------- #
def test_store_dedupes_by_url_and_maps_rowids(tmp_path):
    from src.search_index.store import DocStore

    with DocStore(tmp_path / "docs.sqlite3") as store:
        docs = [Doc(url="https://a.example/1", title="A1"),
                Doc(url="https://a.example/2", title="A2"),
                Doc(url="https://a.example/1", title="dupe")]
        rowids = store.add_batch(docs)
        assert rowids[0] > 0 and rowids[1] > 0
        assert rowids[2] == -1, "duplicate URL must not create a second row"
        assert store.duplicates == 1

        fetched = store.fetch_map([rowids[1], rowids[0]])
        assert fetched[rowids[1]].title == "A2"
        assert fetched[rowids[0]].title == "A1"
        assert store.count() == 2
        assert store.source_counts() == {"": 2}


def test_store_backfills_authority(tmp_path):
    from src.search_index.store import DocStore

    with DocStore(tmp_path / "docs.sqlite3") as store:
        store.add_batch([Doc(url="https://x.example/a", source="curlie"),
                         Doc(url="https://x.example/b", source="curlie"),
                         Doc(url="https://y.example/a", source="curlie")])
        updated = store.set_authority("x.example", 7.5)
        assert updated == 2
        assert store.get_by_url("https://x.example/a").authority == 7.5
        assert store.get_by_url("https://y.example/a").authority == 0.0


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #
def test_bm25_shard_ranks_and_round_trips(tmp_path):
    from src.search_index.bm25 import BM25Shard

    texts = [
        "solar panels convert sunlight into electricity for homes",
        "wind turbines generate electricity from moving air currents",
        "solar energy policy and solar research funding worldwide",
    ]
    shard = BM25Shard().build([10, 11, 12], texts, shard_id=0, source="test")
    rowids, scores = shard.search(["solar"], 3)
    assert list(rowids) == [12, 10], "the doc with two 'solar' hits wins"
    assert scores[0] > scores[1]

    meta = shard.save(tmp_path / "shard-00000")
    assert meta.doc_count == 3 and meta.term_count > 0

    reloaded = BM25Shard.load(tmp_path / "shard-00000")
    rowids2, _ = reloaded.search(["solar"], 3)
    assert list(rowids2) == [12, 10]

    empty_rowids, _ = reloaded.search(["nonexistentword"], 3)
    assert len(empty_rowids) == 0


def test_sharded_bm25_merges_across_shards(tmp_path):
    from src.search_index.bm25 import BM25Shard, ShardedBM25
    from src.search_index.text import parse_query

    root = tmp_path / "index"
    BM25Shard().build([1, 2], ["solar power for homes", "wind power for farms"],
                      shard_id=0, source="a").save(root / "shard-00000")
    BM25Shard().build([3, 4], ["solar research and solar policy", "tidal power"],
                      shard_id=1, source="b").save(root / "shard-00001")
    (root / "shards.json").write_text(json.dumps(
        {"shards": [(root / "shard-00000" / "meta.json") and
                    json.loads((root / "shard-00000" / "meta.json").read_text()),
                    json.loads((root / "shard-00001" / "meta.json").read_text())]}),
        encoding="utf-8")

    index = ShardedBM25(root)
    assert index.shard_count == 2 and index.doc_count == 4
    hits = index.search(parse_query("solar"), 10)
    assert next(hit.rowid for hit in hits) == 3    # two mentions beats one
    assert 1 in [hit.rowid for hit in hits]

    only_b = index.search(parse_query("solar"), 10, sources=["b"])
    assert [hit.rowid for hit in only_b] == [3]


# --------------------------------------------------------------------------- #
# sources: Curlie
# --------------------------------------------------------------------------- #
def test_curlie_parser_joins_category_path():
    from src.search_index.sources.curlie import parse_categories, parse_sites

    categories = parse_categories(CURLIE_STRUCTURE.splitlines())
    assert categories["2"].path == "Top/Science/Energy/Solar"
    assert categories["2"].words == "science energy solar"
    assert categories["2"].geo_words == "us california"
    assert categories["1"].site_count == 1200

    docs = list(parse_sites(CURLIE_CONTENT.splitlines(), categories))
    assert len(docs) == 6
    solar = docs[0]
    assert solar.url == "http://solar.example/"
    assert solar.source == "curlie"
    assert "science energy solar" in solar.body   # category path is indexed
    assert solar.host == "solar.example"


def test_curlie_parser_survives_missing_structure():
    from src.search_index.sources.curlie import parse_sites

    docs = list(parse_sites(CURLIE_CONTENT.splitlines(), {}))
    assert len(docs) == 6                       # no crash, just no category text
    assert docs[0].body == docs[0].snippet


def test_curlie_parser_handles_bare_domains_and_blanks():
    from src.search_index.sources.curlie import parse_sites

    rows = ["bare.example.org\tBare\tA site with no scheme\t",
            "\t\t\t", "", "\t\t"]
    docs = list(parse_sites(rows, {}))
    assert len(docs) == 1
    assert docs[0].url == "https://bare.example.org"


# --------------------------------------------------------------------------- #
# sources: authority
# --------------------------------------------------------------------------- #
def test_openpagerank_parser_finds_columns_by_name():
    from src.search_index.sources.authority import parse_openpagerank

    rows = list(parse_openpagerank(PAGERANK_CSV.splitlines()))
    assert dict(rows)["nasa.example"] == 9.6
    assert dict(rows)["spam.example"] == 0.1
    assert len(rows) == 5


def test_openpagerank_parser_tolerates_a_renamed_header():
    from src.search_index.sources.authority import parse_openpagerank

    csv = "Domain,Rank,Open Page Rank\nexample.org,12,5.5\n"
    assert dict(parse_openpagerank(csv.splitlines()))["example.org"] == 5.5


def test_tranco_parser_log_compresses_ranks():
    from src.search_index.sources.authority import parse_tranco

    rows = dict(parse_tranco(TRANCO_CSV.splitlines()))
    assert rows["nasa.example"] == pytest.approx(10.0)
    assert 0.0 <= rows["wind.example"] < 10.0
    assert rows["nasa.example"] > rows["wind.example"]


def test_authority_table_keeps_the_strongest_signal(tmp_path):
    from src.search_index.sources.authority import AuthorityTable

    table = AuthorityTable()
    (tmp_path / "top10milliondomains.csv").write_text(PAGERANK_CSV, encoding="utf-8")
    (tmp_path / "top-1m.csv").write_text(TRANCO_CSV, encoding="utf-8")
    assert table.load_openpagerank(tmp_path / "top10milliondomains.csv") == 5
    assert len(table) == 5
    assert table.get("NASA.example") == 9.6        # case-insensitive
    assert table.get("wind.example") == 0.0        # absent from PageRank
    assert table.load_tranco(tmp_path / "top-1m.csv") == 3
    assert table.get("wind.example") > 0.0         # but Tranco covers it
    assert table.get("nasa.example") == 10.0       # max, not overwritten down
    assert table.load_openpagerank(tmp_path / "missing.csv") == 0
    assert "hosts" in table.stats()


# --------------------------------------------------------------------------- #
# sources: Wikipedia
# --------------------------------------------------------------------------- #
def test_wikitext_cleaner_strips_the_noise():
    from src.search_index.sources.wikipedia import clean_wikitext

    cleaned = clean_wikitext(
        "{{Infobox|year=2026}}<!--hidden-->Solar energy is <b>good</b>."
        "<ref>citation</ref> See [[Wind power|wind power]] and [[Solar]]."
        "{| class=table\n|row\n|}\n'''bold'''")
    assert "Infobox" not in cleaned and "citation" not in cleaned
    assert "hidden" not in cleaned and "row" not in cleaned
    assert "wind power" in cleaned and "Solar" in cleaned
    assert "bold" in cleaned and "'''" not in cleaned


def test_wikipedia_reader_streams_multistream_bz2(tmp_path):
    from src.search_index.sources.wikipedia import (
        iter_wikipedia_docs,
        iter_wikipedia_pages,
    )

    path = tmp_path / "dump.xml.bz2"
    path.write_bytes(bz2.compress(b"<mediawiki>\n" + WIKI_PAGES[0].encode()) +
                     bz2.compress((WIKI_PAGES[1] + WIKI_PAGES[2]).encode()))

    pages = list(iter_wikipedia_pages(path))
    assert [p.title for p in pages] == ["Solar energy", "Wind power",
                                        "Open-source software"]
    assert "photosynthesis" in pages[0].text
    assert "{{" not in pages[0].text and "<ref" not in pages[0].text

    docs = list(iter_wikipedia_docs(path))
    assert docs[0].url == "https://en.wikipedia.org/wiki/Solar_energy"
    assert docs[0].source == "wikipedia"
    assert docs[0].authority == 6.0

    # block slicing: shard 1 starts at page 100, so a 3-page dump yields nothing
    assert list(iter_wikipedia_pages(path, start_block=1)) == []
    assert len(list(iter_wikipedia_pages(path, start_block=0, max_pages=2))) == 2


def test_wikipedia_reader_skips_redirects_and_other_namespaces(tmp_path):
    from src.search_index.sources.wikipedia import iter_wikipedia_pages

    body = (_wikipedia_page(
        "Real Page",
        "Real Page is an article whose body is comfortably longer than the "
        "minimum length the reader enforces, so that it survives the stub "
        "filter and shows up in the results alongside the other real articles "
        "in this synthetic dump used by the test suite.") +
            _wikipedia_page("Old Name", "#REDIRECT [[Real Page]]") +
            _wikipedia_page("Talk:Real Page", "talk talk talk").replace(
                "<ns>0</ns>", "<ns>1</ns>"))
    path = tmp_path / "d.xml.bz2"
    path.write_bytes(bz2.compress(("<mediawiki>" + body).encode()))
    assert [p.title for p in iter_wikipedia_pages(path, min_chars=0)] \
        == ["Real Page"]


# --------------------------------------------------------------------------- #
# sources: Common Crawl
# --------------------------------------------------------------------------- #
def test_wet_streaming_reader(tmp_path):
    from src.search_index.sources.commoncrawl import iter_records_from_file

    path = tmp_path / "CC-MAIN-x-000.warc.wet.gz"
    path.write_bytes(_wet_bytes())
    records = list(iter_records_from_file(path))
    assert len(records) == 3
    assert records[0].target_uri == "https://solarpanel.example/guide"
    assert "monocrystalline" in records[0].text
    assert not records[0].text.startswith("HTTP/1.1"), "HTTP headers must be dropped"

    assert len(list(iter_records_from_file(path, max_records=1))) == 1


def test_chunk_reader_handles_split_boundaries():
    from src.search_index.sources.commoncrawl import ChunkReader

    reader = ChunkReader([b"abc", b"def\r\n\r\n", b"payload", b"tail"])
    assert reader.read_until(b"\r\n\r\n") == b"abcdef\r\n\r\n"
    assert reader.read_exact(7) == b"payload"
    assert reader.read_until(b"\r\n\r\n") == b"tail"


def test_commoncrawl_source_with_injected_downloader():
    from src.search_index.sources.commoncrawl import CommonCrawlSource

    payload = _wet_bytes()
    source = CommonCrawlSource("CC-MAIN-2026-39", downloader=lambda url: payload)
    docs = list(source.iter_file("CC-MAIN-2026-39-000000.warc.wet.gz"))
    assert len(docs) == 3
    assert docs[0].source == "commoncrawl"
    assert docs[0].host == "solarpanel.example"
    # no <title> in a WET record, so the first sentence becomes the title
    assert docs[0].title == "A complete guide to choosing solar panels for your home"
    assert len(list(source.iter_file("x", max_pages=1))) == 1


def test_commoncrawl_source_respects_offline_mode(monkeypatch):
    from src.search_index.download import OfflineError
    from src.search_index.sources.commoncrawl import CommonCrawlSource

    monkeypatch.setenv("WCP_OFFLINE", "1")
    source = CommonCrawlSource("CC-MAIN-2026-39")
    with pytest.raises(OfflineError):
        list(source.iter_file("any/path.warc.wet.gz"))


def test_download_helpers_refuse_to_touch_the_network(monkeypatch):
    from src.search_index.download import OfflineError, resumable_download

    monkeypatch.setenv("WCP_OFFLINE", "1")
    with pytest.raises(OfflineError):
        resumable_download("https://example.com/x", "/tmp/should-not-exist")


# --------------------------------------------------------------------------- #
# indexer
# --------------------------------------------------------------------------- #
def test_build_level_zero_from_fixtures(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=index_dir, shard_docs=2,
                           download=False, body_chars=512)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        stats = builder.build()

    assert stats.docs == 6, stats.sources
    assert stats.shards == 3, "6 docs at 2 per shard"
    assert stats.sources == {"curlie": 6}
    assert stats.state["curlie"]["status"] == "done"
    assert (index_dir / "index" / "shards.json").exists()


def test_rebuild_is_idempotent(index_dir):
    """Re-running a finished build must not duplicate a single document."""
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=index_dir, shard_docs=100,
                           download=False)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        assert builder.build().docs == 6
    with SearchIndexBuilder(index_dir, options=options) as builder:
        stats = builder.build()
    assert stats.docs == 6, "second build re-indexed everything"
    assert stats.shards == 1


def test_build_applies_authority_from_raw_files(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=index_dir, shard_docs=100,
                           download=False)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        builder.build()
        store = builder.store
        nasa = store.get_by_url("https://nasa.example/")
        spam = store.get_by_url("http://spam.example/")
    assert nasa.authority >= 9.6, "PageRank 9.6, and Tranco rank 1 lifts it to 10"
    assert spam.authority == 0.1


def test_backfill_authority_scores_existing_documents(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=index_dir, shard_docs=100,
                           download=False, authority=False)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        builder.build()
        assert builder.store.get_by_url("https://nasa.example/").authority == 0.0
        updated = builder.backfill_authority()
    assert updated >= 5


def test_build_wikipedia_and_commoncrawl(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=2, data_dir=index_dir,
                           sources=("wikipedia",), shard_docs=2, download=False)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        stats = builder.build()
    assert stats.docs == 3
    assert stats.state["wikipedia"]["status"] == "done"

    options = BuildOptions(level=2, data_dir=index_dir,
                           sources=("commoncrawl",), shard_docs=2, download=False,
                           commoncrawl_snapshot="CC-MAIN-2026-39")
    # Common Crawl streams from the network by design, so the test injects
    # both the WET file list and the file bytes.
    import src.search_index.sources.commoncrawl as cc

    class _FakeCC(cc.CommonCrawlSource):
        def __init__(self, snapshot=None, **kw):
            super().__init__(snapshot, downloader=lambda url: _wet_bytes(), **kw)

        def paths(self):
            return ["CC-MAIN-2026-39-000000.warc.wet.gz"]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cc, "CommonCrawlSource", _FakeCC)
    try:
        with SearchIndexBuilder(index_dir, options=options) as builder:
            stats = builder.build()
    finally:
        monkeypatch.undo()
    assert stats.docs == 6, stats.sources          # 3 wikipedia + 3 crawled
    assert stats.sources.get("commoncrawl") == 3
    assert stats.sources.get("wikipedia") == 3


def test_build_reports_missing_sources_instead_of_crashing(tmp_path):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    (tmp_path / "raw").mkdir()
    options = BuildOptions(level=0, data_dir=tmp_path, download=False)
    with SearchIndexBuilder(tmp_path, options=options) as builder:
        stats = builder.build()
    assert stats.docs == 0
    assert stats.state["curlie"]["status"] == "failed"
    assert "curlie.org/download" in stats.state["curlie"]["error"]


def test_add_documents_indexes_the_platforms_own_crawl_output(index_dir):
    """Crawl today, find it tomorrow: arbitrary docs can be added anytime."""
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=index_dir, shard_docs=5, download=False)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        written = builder.add_documents([
            Doc(url="https://mine.example/report", title="Internal report",
                body="A long enough body about tidal barrages and their cost."),
        ], source="seed")
    assert written == 1
    assert builder.store.get_by_url("https://mine.example/report").source == "seed"


def test_quality_gate_rejects_junk():
    from src.search_index.indexer import quick_quality

    ok, score = quick_quality("a b c d e f g h i j k l m n o p", source="commoncrawl")
    assert not ok and score == 0.0

    good = " ".join(["the renewable energy sector is growing and it is cheap"] * 40)
    ok, score = quick_quality(good, source="commoncrawl")
    assert ok and score > 0.0

    ok, _ = quick_quality("Solar power research", source="curlie")
    assert ok, "short editorial descriptions are fine"


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
@pytest.fixture
def built_index(index_dir):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    options = BuildOptions(level=0, data_dir=index_dir, shard_docs=100,
                           download=False)
    with SearchIndexBuilder(index_dir, options=options) as builder:
        builder.build()
    return index_dir


def test_query_returns_ranked_results(built_index):
    from src.search_index.query import SearchIndex

    with SearchIndex(f"{built_index}/index") as index:
        results = index.search("solar", 5)
        assert results, "the index answered nothing"
        assert results[0].doc.host in ("solar.example", "spam.example")
        assert results[0].bm25 > 0
        assert "[" in results[0].snippet, "snippet must highlight the match"


def test_authority_weight_promotes_the_authoritative_site(built_index):
    """The whole reason PageRank is here: beat keyword stuffing."""
    from src.search_index.query import SearchIndex

    def scores(weight: float) -> dict[str, float]:
        index = SearchIndex(f"{built_index}/index", authority_weight=weight)
        try:
            return {r.doc.host: r.score
                    for r in index.search("solar", 10, collapse_host=False)}
        finally:
            index.close()

    light, heavy = scores(0.0), scores(3.0)
    # spam.example shouts "solar" three times and has 0.1 authority;
    # solar.example mentions it honestly and has 4.2.
    assert light["spam.example"] / light["solar.example"] > \
        heavy["spam.example"] / heavy["solar.example"]
    assert heavy["solar.example"] > heavy["spam.example"]


def test_host_collapsing_returns_websites_not_pages(built_index):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder
    from src.search_index.query import SearchIndex

    # two pages on the same host — discovery wants the site once, not twice
    options = BuildOptions(level=0, data_dir=built_index, shard_docs=100,
                           download=False)
    with SearchIndexBuilder(built_index, options=options) as builder:
        builder.add_documents([
            Doc(url="https://dup.example/a", title="Solar A",
                body="A page about solar panels and their efficiency."),
            Doc(url="https://dup.example/b", title="Solar B",
                body="Another page about solar panels and their cost."),
        ], source="seed")

    with SearchIndex(f"{built_index}/index") as index:
        collapsed = [r.doc.host for r in index.search("solar", 20)]
        expanded = [r.doc.host
                    for r in index.search("solar", 20, collapse_host=False)]
    assert expanded.count("dup.example") == 2
    assert collapsed.count("dup.example") == 1
    assert len(collapsed) == len(set(collapsed))
    assert len(collapsed) < len(expanded)


# --------------------------------------------------------------------------- #
# provider / discovery integration
# --------------------------------------------------------------------------- #
def test_own_provider_implements_the_search_protocol(built_index):
    from src.discovery.providers import SearchHit
    from src.search_index.provider import OwnIndexProvider

    provider = OwnIndexProvider(built_index)
    assert provider.name == "own"
    assert provider.available

    hits = provider.search("solar energy", 5)
    assert hits and all(isinstance(h, SearchHit) for h in hits)
    assert all(h.provider == "own" for h in hits)
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    assert any("solar.example" == h.url.rstrip("/").split("//")[-1] for h in hits)

    assert provider.stats()["available"] is True
    provider.close()


def test_own_provider_is_quiet_when_there_is_no_index(tmp_path):
    from src.search_index.provider import OwnIndexProvider

    provider = OwnIndexProvider(tmp_path / "nothing")
    assert not provider.available
    assert provider.search("anything") == []
    assert provider.stats()["available"] is False


def test_build_providers_registers_own_index(built_index, monkeypatch):
    from src.config import get_config
    from src.discovery.providers import build_providers

    get_config().DISCOVERY_USE_OWN_INDEX = True
    names = [p.name for p in build_providers()]
    assert "own" in names
    assert names.index("own") < names.index("duckduckgo"), \
        "our own index must be tried before a third party"

    get_config().DISCOVERY_USE_OWN_INDEX = False
    assert "own" not in [p.name for p in build_providers()]

    get_config().DISCOVERY_USE_OWN_INDEX = False
    assert "own" in [p.name for p in build_providers(names=["own"])]
    get_config().DISCOVERY_USE_OWN_INDEX = True


def test_discovery_uses_the_own_index_end_to_end(built_index, monkeypatch):
    """The real payoff: topic -> websites with no third-party search engine."""
    from src.config import get_config
    from src.discovery.service import preview_discovery
    from src.search_index.provider import OwnIndexProvider

    get_config().DISCOVERY_USE_OWN_INDEX = True
    monkeypatch.setattr("src.discovery.service.build_providers",
                        lambda **kw: [OwnIndexProvider(built_index)])
    result = preview_discovery(topic="solar energy research", count=3)
    assert result["candidates"], "discovery found nothing in our own index"
    assert all(c["domain"] for c in result["candidates"])


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def _api_client():
    from fastapi.testclient import TestClient

    from src.api.main import create_api_app

    return TestClient(create_api_app(enable_graphql=False,
                                     enable_rate_limit=False))


def _auth_headers(client):
    tok = client.post("/auth/signup", json={
        "org_name": "Search Labs", "email": "own@example.com",
        "password": "s3cret-passw0rd!"}).json()
    key = client.post("/keys",
                      headers={"Authorization": f"Bearer {tok['access_token']}"},
                      json={"name": "ci", "scopes": ["read", "crawl"]}).json()["key"]
    return {"X-API-Key": key}


def test_search_index_endpoints(built_index):
    with _api_client() as client:
        headers = _auth_headers(client)

        stats = client.get("/api/v1/search-index/stats", headers=headers)
        assert stats.status_code == 200, stats.text
        assert stats.json()["available"] is True
        assert stats.json()["docs"] == 6
        assert "index_dir" not in stats.json()
        assert "error" not in stats.json().get("extras", {})

        levels = client.get("/api/v1/search-index/levels", headers=headers)
        assert levels.status_code == 200
        assert [lv["level"] for lv in levels.json()["levels"]] == [0, 1, 2, 3]

        got = client.get("/api/v1/search-index/query", headers=headers,
                         params={"q": "solar energy", "limit": 3})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["count"] <= 3
        assert body["results"]
        assert body["results"][0]["rank"] == 1
        assert "snippet" in body["results"][0]

        posted = client.post("/api/v1/search-index/query", headers=headers,
                             json={"q": "open source", "limit": 2,
                                   "sources": ["curlie"]})
        assert posted.status_code == 200, posted.text
        assert posted.json()["count"] >= 1

        assert client.get("/api/v1/search-index/query", headers=headers,
                          params={"q": "x" * 3000}).status_code == 422


def test_search_index_endpoints_need_auth(built_index):
    with _api_client() as client:
        assert client.get("/api/v1/search-index/stats").status_code == 401


def test_search_index_query_503_without_an_index(tmp_path):
    from src.config import get_config

    get_config().SEARCH_INDEX_DIR = str(tmp_path / "empty")
    try:
        with _api_client() as client:
            headers = _auth_headers(client)
            resp = client.get("/api/v1/search-index/query", headers=headers,
                              params={"q": "anything"})
            assert resp.status_code == 503
    finally:
        get_config().SEARCH_INDEX_DIR = "data/search-index"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_levels_and_sources(capsys):
    from src.cli import main

    assert main(["search-index", "levels"]) == 0
    out = capsys.readouterr().out
    assert "Level" in out and "Curlie" in out

    assert main(["search-index", "sources", "--level", "2"]) == 0
    out = capsys.readouterr().out
    assert "curlie.org" in out and "commoncrawl.org" in out


def test_cli_status_before_and_after_build(index_dir, capsys):
    from src.cli import main

    assert main(["search-index", "status", "--data-dir", str(index_dir)]) == 1
    assert "No index" in capsys.readouterr().out

    assert main(["search-index", "build", "--level", "0", "--no-download",
                 "--data-dir", str(index_dir), "--shard-docs", "2"]) == 0
    assert main(["search-index", "status", "--data-dir", str(index_dir)]) == 0
    out = capsys.readouterr().out
    assert "6" in out and "curlie" in out


def test_cli_query_round_trip(built_index, capsys):
    from src.cli import main

    assert main(["search-index", "query", "solar energy", "--limit", "2",
                 "--data-dir", str(built_index)]) == 0
    out = capsys.readouterr().out
    assert "solar" in out.lower()


# --------------------------------------------------------------------------- #
# merging parallel builds
# --------------------------------------------------------------------------- #
def _build_part(root, docs, *, shard_docs=2):
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    root.mkdir(parents=True, exist_ok=True)
    options = BuildOptions(level=0, data_dir=root, shard_docs=shard_docs,
                           download=False)
    with SearchIndexBuilder(root, options=options) as builder:
        builder.add_documents(docs, source="seed")
        builder.save_state()
    return root


def test_merge_remaps_rowids_and_drops_duplicates(tmp_path):
    """The hard part of parallel builds: rowids change when stores merge."""
    from src.search_index.merge import merge_parts
    from src.search_index.query import SearchIndex

    part_a = _build_part(tmp_path / "partA", [
        Doc(url="https://one.example/a", title="Solar One",
            body="A page about solar panels and how they are installed."),
        Doc(url="https://two.example/a", title="Tidal Two",
            body="A page about tidal barrages and their electricity output."),
    ])
    part_b = _build_part(tmp_path / "partB", [
        Doc(url="https://one.example/a", title="Solar One (duplicate)",
            body="The same page, crawled again by a different shard."),
        Doc(url="https://three.example/a", title="Wind Three",
            body="A page about wind turbines and their capacity factors."),
    ])

    merged = merge_parts([part_a, part_b], tmp_path / "merged")
    assert merged.docs == 3, "the duplicate URL must survive exactly once"
    assert merged.shards == 2, "one shard per part"

    with SearchIndex(f"{tmp_path}/merged/index") as index:
        solar = index.search("solar panels", 5, collapse_host=False)
        wind = index.search("wind turbines", 5, collapse_host=False)
    # if rowids were not remapped these would point at the wrong documents
    assert [r.doc.title for r in solar] == ["Solar One"]
    assert [r.doc.title for r in wind] == ["Wind Three"]


def test_merge_cli(tmp_path, capsys):
    from src.cli import main

    _build_part(tmp_path / "p0", [
        Doc(url="https://x.example/1", title="Solar X",
            body="A page about solar panels and their efficiency ratings.")])
    assert main(["search-index", "merge", str(tmp_path / "p0"),
                 str(tmp_path / "missing"),
                 "--out", str(tmp_path / "all")]) == 0
    out = capsys.readouterr().out
    assert "Merged: 1 documents" in out


def test_curlie_build_shards_by_row_so_workers_do_not_overlap(tmp_path):
    """Parallel builds must produce disjoint slices, not N copies of Curlie."""
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    root = tmp_path / "curlie-only"
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "rdf-a-s.tsv.gz").write_bytes(
        gzip.compress(CURLIE_STRUCTURE.encode()))
    (root / "raw" / "rdf-a-c.tsv.gz").write_bytes(
        gzip.compress(CURLIE_CONTENT.encode()))

    seen: dict[int, list[str]] = {}
    for shard in (0, 1):
        options = BuildOptions(level=0, data_dir=root, shard_docs=100,
                               download=False, authority=False,
                               shard_index=shard, shard_count=2)
        part = tmp_path / f"part{shard}"
        (part / "raw").mkdir(parents=True)
        for path in (root / "raw").iterdir():
            (part / "raw" / path.name).write_bytes(path.read_bytes())
        with SearchIndexBuilder(part, options=options) as builder:
            builder.build()
            seen[shard] = sorted(d.url for d in builder.store.iter_all())

    assert seen[0] and seen[1]
    assert not set(seen[0]) & set(seen[1]), "shards overlapped"
    assert len(seen[0]) + len(seen[1]) == 6
