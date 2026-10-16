"""A first-party web search index — the platform's own replacement for DuckDuckGo.

This package exists to remove the one third-party dependency that everything
upstream of a crawl quietly rested on: *"give me a topic, give me N websites"*
used to mean an HTTP call to somebody else's search engine. With
:mod:`src.search_index` the platform answers that question from an index it
built itself, from data that is free to download, and it keeps working with the
network unplugged.

Why this is affordable
----------------------
The trick is that **ranking does not require crawling**. Three public datasets
already contain the editorial and authority signal a crawler would otherwise
have to rediscover the hard way:

``Curlie`` (~200 MB, CC-BY)
    ~2.9M human-edited sites with a written description *and* a category path
    (``Top/Science/Astronomy``). Not SEO sludge — an editor looked at it.
``Open PageRank`` (117 MB, free forever)
    Authority 0-10 for the top 10M domains, so we never have to build a link
    graph ourselves.
``Tranco`` (~15 MB)
    A community-merged popularity list that catches sites PageRank misses.

Together that is **~330 MB of download** and it already beats a general search
engine *for our narrow job*, because the category path matches a topic directly
and there is no spam, no rate limit, no API key and no ToS risk.

Four levels, all $0
-------------------
=====  ==============================  =================  ============  ==========
Level  What gets indexed               Download           Disk          Effort
=====  ==============================  =================  ============  ==========
0      Curlie + PageRank + Tranco      ~330 MB            ~3 GB         2-3 days
1      + Wikipedia (7.2M articles)     ~27 GB             ~40 GB        3-5 days
2      + Common Crawl, 10M pages       ~28 GB             ~150 GB       1-2 weeks
3      + Common Crawl, 100M pages      ~280 GB            0.5-1 TB      1-2 months
=====  ==============================  =================  ============  ==========

The ceiling of "free" is around level 3: past ~100M pages you are paying for
disk and servers, not for software. Compute is free the whole way — GitHub
Actions gives **unlimited minutes to public repositories**, so the big levels
run as a sharded matrix workflow (see ``.github/workflows/search-index.yml``).

Layers
------
``src.search_index.models``
    :class:`Doc`, :class:`LevelSpec` and the :data:`LEVELS` table.
``src.search_index.text``
    Tokenizer, light stemmer, query parser (``"phrase"``, ``-exclusion``,
    ``site:host``) and snippet highlighting.
``src.search_index.store``
    SQLite document store — stdlib only, random access by rowid, URL
    de-duplication for free, and it survives 100M rows.
``src.search_index.bm25``
    Per-shard BM25 in CSR form (numpy), memory-mapped at query time, with
    top-k merging across shards like a real distributed index.
``src.search_index.sources``
    Streaming readers for Curlie, Open PageRank, Tranco, Wikipedia dumps and
    Common Crawl WET files. Every one of them is resumable.
``src.search_index.indexer``
    ``SearchIndexBuilder`` — checkpoints after every shard so a 6-hour GitHub
    Actions job that gets killed at 5h55m loses at most one shard.
``src.search_index.provider``
    :class:`OwnIndexProvider`, a drop-in :class:`~src.discovery.providers.SearchProvider`.

Zero new hard dependencies: numpy (already present via pandas/pyarrow) and the
standard library. ``bm25s`` / ``duckdb`` are used automatically if installed,
but nothing here requires them.
"""

from __future__ import annotations

from src.search_index.models import LEVELS, Doc, IndexStats, LevelSpec
from src.search_index.provider import OwnIndexProvider

__all__ = [
    "LEVELS",
    "Doc",
    "IndexStats",
    "LevelSpec",
    "OwnIndexProvider",
]
