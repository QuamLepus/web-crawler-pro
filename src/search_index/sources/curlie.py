"""Curlie — the human-edited web directory that DMOZ used to be.

This is the single highest-value dataset in the whole plan and it is only
~200 MB. Curlie ships two TSVs:

``rdf-*-s.tsv`` (structure)
    ``category_id``, ``category_path``, ``site_count``, ``description``,
    ``geo1``, ``geo2``
``rdf-*-c.tsv`` (content)
    ``url``, ``title``, ``description``, ``category_id``

Join them and every one of ~2.9M sites arrives with a *sentence a human wrote
about it* and a *category path* like ``Top/Science/Astronomy``. That category
path is the reason this beats a general search engine for topic discovery: when
someone asks for "astronomy", we are matching an editor's taxonomy, not an
SEO-optimised page.

Download: https://curlie.org/download  ->  https://curlie.org/directory-dl
License: CC-BY (attribution required; we attribute in the README).
"""

from __future__ import annotations

import logging
import re
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from src.search_index.download import iter_lines, resumable_download
from src.search_index.models import Doc

logger = logging.getLogger(__name__)

CURLIE_DOWNLOAD_URL = "https://curlie.org/directory-dl"
CURLIE_INFO_URL = "https://curlie.org/download"


@dataclass(slots=True)
class Category:
    """One node of the Curlie taxonomy."""

    category_id: str
    path: str = ""
    description: str = ""
    site_count: int = 0
    geo1: str = ""
    geo2: str = ""

    @property
    def words(self) -> str:
        """``Top/Science/Astronomy`` -> ``"science astronomy"``.

        ``Top`` is dropped (it prefixes every path), ``_`` and ``-`` become
        spaces, and camel-cased words are split so ``ComputerScience`` also
        matches a ``computer science`` query.
        """
        parts = [p for p in self.path.split("/") if p and p.lower() != "top"]
        words: list[str] = []
        for part in parts:
            part = part.replace("_", " ").replace("-", " ")
            part = _CAMEL_RE.sub(lambda m: m.group(1) + " " + m.group(2), part)
            words.extend(part.split())
        return " ".join(words).lower()

    @property
    def geo_words(self) -> str:
        return " ".join(g for g in (self.geo1, self.geo2) if g).lower()


_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_categories(lines: Iterable[str]) -> dict[str, Category]:
    """Parse the ``-s.tsv`` structure file into ``{category_id: Category}``."""
    out: dict[str, Category] = {}
    for line in lines:
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        category_id = fields[0].strip()
        if not category_id:
            continue
        try:
            site_count = int(fields[2]) if len(fields) > 2 and fields[2].strip().isdigit() else 0
        except ValueError:  # pragma: no cover - defensive
            site_count = 0
        out[category_id] = Category(
            category_id=category_id,
            path=fields[1].strip(),
            site_count=site_count,
            description=fields[3].strip() if len(fields) > 3 else "",
            geo1=fields[4].strip() if len(fields) > 4 else "",
            geo2=fields[5].strip() if len(fields) > 5 else "",
        )
    return out


def parse_sites(lines: Iterable[str],
                categories: dict[str, Category] | None = None) -> Iterator[Doc]:
    """Parse the ``-c.tsv`` content file into :class:`Doc` objects.

    Rows whose category is unknown are still emitted — a site without a
    taxonomy is worth more than no site at all.
    """
    categories = categories or {}
    for line in lines:
        if not line.strip():
            continue
        fields = line.split("\t")
        if not fields:
            continue
        url = fields[0].strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        title = fields[1].strip() if len(fields) > 1 else ""
        description = fields[2].strip() if len(fields) > 2 else ""
        category_id = fields[3].strip() if len(fields) > 3 else ""

        category = categories.get(category_id)
        body_parts = [description]
        if category is not None:
            body_parts.append(category.words)
            if category.geo_words:
                body_parts.append(category.geo_words)
            if category.description and category.description != description:
                body_parts.append(category.description)
        body = " ".join(p for p in body_parts if p).strip()

        if not title and not body:
            continue
        yield Doc(
            url=url,
            title=title or (category.words if category else ""),
            snippet=description,
            body=body,
            source="curlie",
            lang="en",
        )


# --------------------------------------------------------------------------- #
# Download & extract
# --------------------------------------------------------------------------- #
def extract_curlie(archive: str | Path, dest_dir: str | Path) -> tuple[Path, Path]:
    """Unpack ``directory-dl`` and return ``(structure_tsv, content_tsv)``.

    The download is a tar/compressed archive whose member names match
    ``rdf-*-s.tsv*`` (structure) and ``rdf-*-c.tsv*`` (content).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    structure: Path | None = None
    content: Path | None = None
    archive_path = Path(archive)

    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if not member.isfile() or not name.startswith("rdf-"):
                    continue
                target = dest_dir / name
                if target.exists() and target.stat().st_size >= member.size:
                    pass  # already extracted; keep going so we can report paths
                else:
                    source = tar.extractfile(member)
                    if source is None:  # pragma: no cover - defensive
                        continue
                    with source, target.open("wb") as out:
                        out.write(source.read())
                if "-s.tsv" in name:
                    structure = target
                elif "-c.tsv" in name:
                    content = target
    else:
        # Some mirrors serve the two TSVs directly instead of an archive.
        for path in sorted(dest_dir.glob("rdf-*")):
            if "-s.tsv" in path.name:
                structure = path
            elif "-c.tsv" in path.name:
                content = path

    if structure is None or content is None:
        raise FileNotFoundError(
            f"could not find curlie rdf-*-s.tsv / rdf-*-c.tsv inside {archive}")
    return structure, content


def download_curlie(dest_dir: str | Path, *, url: str = CURLIE_DOWNLOAD_URL,
                    fetcher=None) -> tuple[Path, Path]:
    """Download and unpack Curlie; returns ``(structure_tsv, content_tsv)``."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / "curlie-directory.tar"
    if not any(dest_dir.glob("rdf-*-c.tsv*")):
        resumable_download(url, archive, label="curlie directory", fetcher=fetcher)
    return extract_curlie(archive, dest_dir)


def iter_curlie_docs(structure_tsv: str | Path | None,
                     content_tsv: str | Path) -> Iterator[Doc]:
    """Yield every Curlie site as a :class:`Doc`."""
    categories: dict[str, Category] = {}
    if structure_tsv and Path(structure_tsv).exists():
        categories = parse_categories(iter_lines(structure_tsv))
        logger.info("curlie: %d categories", len(categories))
    yield from parse_sites(iter_lines(content_tsv), categories)
