"""Streaming readers for the four free datasets the index is built from.

Every reader here is a *generator over* :class:`~src.search_index.models.Doc`
and every one of them can be resumed, because every one of them is bigger than
a comfortable single sitting:

============  =====================================================  =========
Source        What it gives us                                       Size
============  =====================================================  =========
curlie        human-edited sites + description + category path       ~200 MB
authority     Open PageRank / Tranco popularity, 0-10 normalised     ~130 MB
wikipedia     encyclopedic text, block-seekable bz2 multistream      ~27 GB
commoncrawl   raw web pages (WET), one gzipped file at a time        ~28 GB/10M
============  =====================================================  =========

None of them needs an API key and all of them are free to redistribute for
this purpose (Curlie is CC-BY, Wikipedia is CC-BY-SA, Common Crawl is public
data on an AWS Open Data bucket with no egress charge).
"""

from __future__ import annotations

from src.search_index.sources.authority import (
    AUTHORITY_SOURCES,
    AuthorityTable,
    parse_openpagerank,
    parse_tranco,
)
from src.search_index.sources.commoncrawl import (
    CommonCrawlSource,
    iter_warc_records,
)
from src.search_index.sources.curlie import (
    CURLIE_DOWNLOAD_URL,
    Category,
    extract_curlie,
    parse_categories,
    parse_sites,
)
from src.search_index.sources.wikipedia import (
    clean_wikitext,
    iter_wikipedia_pages,
    page_to_doc,
)

__all__ = [
    "AUTHORITY_SOURCES",
    "CURLIE_DOWNLOAD_URL",
    "AuthorityTable",
    "Category",
    "CommonCrawlSource",
    "clean_wikitext",
    "extract_curlie",
    "iter_warc_records",
    "iter_wikipedia_pages",
    "page_to_doc",
    "parse_categories",
    "parse_openpagerank",
    "parse_sites",
    "parse_tranco",
]
