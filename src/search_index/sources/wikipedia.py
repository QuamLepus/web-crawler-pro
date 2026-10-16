"""Wikipedia dumps — 7.2M clean, encyclopedic articles in a ~27 GB file.

Why bother: Curlie tells you *which sites* exist, but a topic query like
"what is a calorimeter" has no home in a web directory. Wikipedia is the only
free corpus at this scale whose text is already written the way people ask
questions, and it is CC-BY-SA.

The engineering problem is that the dump is a **multi-stream bzip2** file:
hundreds of independently compressed blocks concatenated together, which
``bz2.open`` cannot seek and plain ``BZ2Decompressor`` stops at the end of the
first stream. So we

* read it as a stream of decompressed chunks, rolling a fresh
  ``BZ2Decompressor`` at every stream boundary (``unused_data``), and
* expose ``start_block`` so shard *i* can jump straight to block *i·k* and
  index its own slice — no coordination, no shared state, which is exactly what
  makes it parallelisable across free GitHub Actions runners.

Block-granular seeking is approximate (blocks are ~100 pages and the official
``-multistream-index.txt.bz2`` gives exact byte offsets), but it is exact enough
for sharding and it needs no index file.
"""

from __future__ import annotations

import bz2
import codecs
import html
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from src.search_index.models import Doc

logger = logging.getLogger(__name__)

#: Pages per multistream block in a standard ``pages-articles-multistream`` dump.
PAGES_PER_BLOCK = 100
_CHUNK = 1 << 20

DUMP_URL_TEMPLATE = ("https://dumps.wikimedia.org/{lang}wiki/{date}/"
                     "{lang}wiki-{date}-pages-articles-multistream.xml.bz2")
DUMP_INDEX_URL_TEMPLATE = ("https://dumps.wikimedia.org/{lang}wiki/{date}/"
                           "{lang}wiki-{date}-pages-articles-multistream-index.txt.bz2")


@dataclass(slots=True)
class WikiPage:
    """One parsed article."""

    title: str
    text: str          # cleaned plain text
    position: int = 0  # 1-based page number inside the dump (for resuming)


# --------------------------------------------------------------------------- #
# bz2 multistream handling
# --------------------------------------------------------------------------- #
def bz2_multistream_chunks(fh, chunk: int = _CHUNK) -> Iterator[bytes]:
    """Yield decompressed chunks from a (possibly multi-stream) bz2 file."""
    dec = bz2.BZ2Decompressor()
    while True:
        raw = fh.read(chunk)
        if not raw:
            break
        pending = raw
        while pending:
            if dec.eof:
                dec = bz2.BZ2Decompressor()
            try:
                out = dec.decompress(pending)
            except (OSError, EOFError):  # truncated tail — common in the wild
                return
            if out:
                yield out
            pending = dec.unused_data if dec.eof else b""


# --------------------------------------------------------------------------- #
# Wikitext -> plain text
# --------------------------------------------------------------------------- #
_NS_RE = re.compile(r"<ns>(\d+)</ns>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.DOTALL)
_REDIRECT_RE = re.compile(r"^\s*#\s*redirect\b", re.IGNORECASE)


def _strip_templates(text: str) -> str:
    """Remove ``{{...}}`` with correct nesting (infoboxes, citations, navboxes)."""
    out: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i) and depth:
            depth -= 1
            i += 2
            continue
        if depth == 0:
            out.append(text[i])
        i += 1
    return "".join(out)


def clean_wikitext(raw: str, *, max_chars: int = 20_000) -> str:
    """Turn wikitext into readable prose.

    Not a full MediaWiki parser — a full one is a dependency the size of the
    dump. This handles the constructs that actually occur in bulk: templates,
    refs, tables, comments, external links, bold/italic, headings and entities.
    """
    text = raw
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<nowiki>.*?</nowiki>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<ref[^>]*/\s*>", " ", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\{\|.*?\|\}", " ", text, flags=re.DOTALL)   # tables
    text = _strip_templates(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\[(?:File|Image|Category):[^\]]*\]\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\]", " ", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^=+\s*(.*?)\s*=+\s*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^[|!].*$", " ", text, flags=re.MULTILINE)      # leftover table rows
    text = html.unescape(text)
    text = re.sub(r"&[a-zA-Z]+;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:max_chars]


def _parse_page(chunk: str, position: int, min_chars: int) -> WikiPage | None:
    ns = _NS_RE.search(chunk)
    if ns and ns.group(1) != "0":
        return None                       # talk/project/template namespaces
    title_m = _TITLE_RE.search(chunk)
    if not title_m:
        return None
    title = html.unescape(title_m.group(1)).strip()
    if not title or ":" in title:         # "Wikipedia:..." / "Draft:..."
        return None
    text_m = _TEXT_RE.search(chunk)
    raw = text_m.group(1) if text_m else ""
    if not raw or _REDIRECT_RE.match(raw):
        return None
    text = clean_wikitext(html.unescape(raw))
    if len(text) < min_chars:
        return None
    return WikiPage(title=title, text=text, position=position)


# --------------------------------------------------------------------------- #
# Streaming reader
# --------------------------------------------------------------------------- #
def iter_wikipedia_pages(path: str | Path, *, start_block: int = 0,
                         max_pages: int | None = None,
                         min_chars: int = 250) -> Iterator[WikiPage]:
    """Stream articles out of a (multistream) Wikipedia dump.

    ``start_block`` skips ``start_block * PAGES_PER_BLOCK`` pages, which is how
    one shard indexes its own slice of the dump without reading the rest.
    """
    if max_pages is not None and max_pages <= 0:
        return
    skip = max(0, int(start_block)) * PAGES_PER_BLOCK
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    seen = 0
    emitted = 0

    with open(path, "rb") as fh:
        for data in bz2_multistream_chunks(fh):
            buf += decoder.decode(data)
            while True:
                start = buf.find("<page>")
                if start < 0:
                    # keep a tail long enough to hold a partial "<page>" opener
                    buf = buf[-16:]
                    break
                end = buf.find("</page>", start)
                if end < 0:
                    if start:
                        buf = buf[start:]
                    break
                chunk = buf[start:end + 7]
                buf = buf[end + 7:]
                seen += 1
                if seen <= skip:
                    continue
                page = _parse_page(chunk, seen, min_chars)
                if page is None:
                    continue
                yield page
                emitted += 1
                if max_pages is not None and emitted >= max_pages:
                    return


def page_to_doc(page: WikiPage, *, lang: str = "en",
                max_body: int = 20_000) -> Doc:
    """Convert an article into a :class:`Doc` with its canonical URL."""
    url = f"https://{lang}.wikipedia.org/wiki/{quote(page.title.replace(' ', '_'))}"
    body = page.text[:max_body]
    return Doc(
        url=url,
        title=page.title,
        snippet=body[:300],
        body=body,
        source="wikipedia",
        lang=lang,
        # An encyclopedia is authoritative by construction; the Open PageRank
        # pass may raise this further, but it should never fall to 0.
        authority=6.0,
    )


def iter_wikipedia_docs(path: str | Path, **kwargs) -> Iterator[Doc]:
    """Convenience: pages -> :class:`Doc`."""
    lang = kwargs.pop("lang", "en")
    for page in iter_wikipedia_pages(path, **kwargs):
        yield page_to_doc(page, lang=lang)
