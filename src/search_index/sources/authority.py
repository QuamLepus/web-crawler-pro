"""Authority scores: Open PageRank and Tranco.

A search engine lives or dies on its ability to tell an authoritative page from
a page that merely repeats the query a lot. Building that signal ourselves means
building a link graph over billions of edges — the single most expensive part of
any crawler-based engine. We skip it entirely, because two free datasets already
contain the answer:

**Open PageRank** (domcop.com) — free forever, 0-10 score for the top 10M
domains, derived from Common Crawl + Common Search. 117 MB CSV.

**Tranco** (tranco-list.eu) — a research-grade list that merges Alexa, Umbrella,
Majestic and Quantcast and deliberately resists manipulation. Used here as a
fallback for domains PageRank has never scored, and for the long tail of
non-English sites it covers better.

Both are normalised to the same 0-10 scale so the ranker can blend them.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from src.search_index.download import iter_lines
from src.search_index.models import host_of

logger = logging.getLogger(__name__)

OPENPAGERANK_URL = "https://www.domcop.com/files/top/top10milliondomains.csv.zip"
TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"

#: ``name -> (url, parser)`` so the CLI can list what it would download.
AUTHORITY_SOURCES: dict[str, tuple[str, str, str]] = {
    "openpagerank": (OPENPAGERANK_URL, "top10milliondomains.csv.zip", "~117 MB"),
    "tranco": (TRANCO_URL, "top-1m.csv.zip", "~15 MB"),
}

_MAX_RANK = 1_000_000.0


def _norm(host: str) -> str:
    """Normalise a host for lookup.

    ``str.lstrip("www.")`` is a trap: it strips *characters*, so
    ``"wind.example"`` becomes ``"ind.example"``. Slice the prefix instead.
    """
    host = (host or "").strip().lower()
    return host.removeprefix("www.")


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def _pick_column(header: list[str], *needles: str) -> int:
    for needle in needles:
        for i, name in enumerate(header):
            if needle in name:
                return i
    return -1


def parse_openpagerank(lines: Iterable[str]) -> Iterator[tuple[str, float]]:
    """Parse the Open PageRank CSV -> ``(host, 0-10)``.

    The header has moved around between releases ("Domain", "Rank",
    "Open Page Rank", "Reachable", ...), so columns are located by name and the
    parser degrades instead of dying.
    """
    domain_col = rank_col = -1
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if domain_col < 0:
            header = [h.strip().strip('"').lower() for h in _csv_split(line)]
            if any(needle in " ".join(header)
                   for needle in ("domain", "rank", "page rank")):
                domain_col = _pick_column(header, "domain", "url", "host")
                rank_col = _pick_column(header, "open page rank", "pagerank", "rank")
                domain_col = max(domain_col, 0)
                rank_col = (1 if len(header) > 1 else 0) if rank_col < 0 else rank_col
                continue
            # no recognisable header: older domcop layout is Rank,Domain,Score
            first = header[0] if header else ""
            if first.isdigit():
                domain_col, rank_col = 1, 2
        parts = _csv_split(line)
        if len(parts) <= max(domain_col, rank_col):
            continue
        host = host_of("https://" + parts[domain_col].strip().strip('"').lower())
        if not host:
            continue
        try:
            score = float(parts[rank_col].strip().strip('"'))
        except ValueError:
            continue
        yield host, max(0.0, min(10.0, score))


def _csv_split(line: str) -> list[str]:
    """Split one CSV line, honouring a single level of double quotes.

    Hand-rolled rather than using :mod:`csv` because these files are tens of
    millions of lines long and we only need one row at a time.
    """
    out: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "," and not in_quotes:
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    out.append("".join(current))
    return out


def parse_tranco(lines: Iterable[str]) -> Iterator[tuple[str, float]]:
    """Parse ``top-1m.csv`` (``rank,domain``) -> ``(host, 0-10)``.

    Ranks are log-compressed: rank 1 maps to 10.0, rank 1,000,000 to 0.0.
    """
    for raw in lines:
        line = raw.strip()
        if not line or line.lower().startswith(("rank,", "domain,")):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            parts = line.split(";")
        if len(parts) < 2:
            continue
        try:
            rank = float(parts[0].strip())
        except ValueError:
            continue
        host = host_of("https://" + parts[1].strip().strip('"').lower())
        if not host or rank <= 0:
            continue
        score = 10.0 * (1.0 - math.log10(min(rank, _MAX_RANK)) / math.log10(_MAX_RANK))
        yield host, max(0.0, min(10.0, score))


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AuthorityTable:
    """``host -> 0-10`` merged from every authority file we could load."""

    scores: dict[str, float] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)

    def load_openpagerank(self, path: str | Path) -> int:
        return self._load(path, parse_openpagerank, "openpagerank")

    def load_tranco(self, path: str | Path) -> int:
        return self._load(path, parse_tranco, "tranco")

    def _load(self, path, parser, label: str) -> int:
        path = Path(path)
        if not path.exists():
            logger.warning("authority file missing: %s", path)
            return 0
        count = 0
        for host, score in parser(iter_lines(path)):
            # keep the strongest signal; never let a weaker list overwrite it
            if score > self.scores.get(host, -1.0):
                self.scores[host] = score
            count += 1
        if count:
            self.loaded.append(label)
        logger.info("authority: %s -> %d hosts (total %d)",
                    label, count, len(self.scores))
        return count

    def get(self, host: str) -> float:
        return self.scores.get(_norm(host), 0.0)

    def __len__(self) -> int:
        return len(self.scores)

    def __contains__(self, host: str) -> bool:
        return _norm(host) in self.scores

    def stats(self) -> dict:
        if not self.scores:
            return {"hosts": 0, "loaded": list(self.loaded)}
        values = list(self.scores.values())
        return {
            "hosts": len(self.scores),
            "loaded": list(self.loaded),
            "mean": round(sum(values) / len(values), 3),
            "max": round(max(values), 3),
        }
