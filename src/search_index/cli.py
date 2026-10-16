"""``wcp search-index …`` — build, inspect and query the platform's own index.

    wcp search-index levels                      # what each level costs
    wcp search-index sources --level 2           # exactly what gets downloaded
    wcp search-index build  --level 0            # ~330 MB, 2-3 days of *waiting*
    wcp search-index build  --level 2 --shard-index 3 --shard-count 20
    wcp search-index status                      # how far along is it
    wcp search-index query  "renewable energy"   # does it actually work
    wcp search-index authority                   # backfill PageRank scores
    wcp search-index merge part0 part1 --out all # combine parallel builds
"""

from __future__ import annotations

import argparse
import json
import time

from src.search_index.download import human_bytes
from src.search_index.models import LEVELS, LevelSpec, level_spec
from src.search_index.sources.authority import AUTHORITY_SOURCES

_FREE_COMPUTE_NOTE = (
    "Compute is free: GitHub Actions gives public repositories unlimited "
    "minutes, so levels 2 and 3 are meant to run as a sharded matrix "
    "workflow (.github/workflows/search-index.yml).")


# --------------------------------------------------------------------------- #
# levels
# --------------------------------------------------------------------------- #
def cmd_levels(args: argparse.Namespace) -> int:
    print(_bold("The $0 roadmap — how big can our own index get for free?"))
    print()
    header = f"{'Lvl':<4}{'Download':<12}{'Disk':<12}{'Effort':<13}What gets indexed"
    print(_dim(header))
    print(_dim("-" * len(header)))
    for level in sorted(LEVELS):
        spec = LEVELS[level]
        print(f"{level:<4}{spec.download:<12}{spec.disk:<12}{spec.effort:<13}{spec.name}")
    print()
    level = getattr(args, "level", None)
    if level is not None:
        spec = level_spec(int(level))
        print(_bold(f"Level {spec.level}: {spec.name}"))
        print(f"  sources : {', '.join(spec.sources)}")
        print(f"  download: {spec.download}")
        print(f"  disk    : {spec.disk}")
        print(f"  effort  : {spec.effort}")
        print(f"  {spec.summary}")
    else:
        print(_dim(_FREE_COMPUTE_NOTE))
        print(_dim("Past level 3 the cost is disk and servers, not software."))
    return 0


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
def _source_rows(spec: LevelSpec) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if "curlie" in spec.sources:
        rows.append(("curlie", "https://curlie.org/download", "~200 MB"))
    rows.append(("openpagerank", AUTHORITY_SOURCES["openpagerank"][0],
                 AUTHORITY_SOURCES["openpagerank"][2]))
    rows.append(("tranco", AUTHORITY_SOURCES["tranco"][0],
                 AUTHORITY_SOURCES["tranco"][2]))
    if "wikipedia" in spec.sources:
        rows.append(("wikipedia",
                     ("https://dumps.wikimedia.org/enwiki/*/"
                      "enwiki-*-pages-articles-multistream.xml.bz2"),
                     "~26.9 GB"))
    if "commoncrawl" in spec.sources:
        rows.append(("commoncrawl",
                     ("https://data.commoncrawl.org/crawl-data/"
                      "CC-MAIN-*/wet.paths.gz"),
                     f"~{spec.max_pages * 2800 // 1_000_000_000 + 1} GB of WET"))
    return rows


def cmd_sources(args: argparse.Namespace) -> int:
    spec = level_spec(int(args.level))
    print(_bold(f"Level {spec.level} downloads ({spec.download} total)"))
    print()
    for name, url, size in _source_rows(spec):
        print(f"  {name:<14}{size:<14}{_dim(url)}")
    print()
    print(_dim("All free, no API key. Curlie is CC-BY, Wikipedia CC-BY-SA, "
               "Common Crawl is public data on an AWS Open Data bucket."))
    print(_dim("Drop the downloaded files into <data-dir>/raw/ to skip the "
               "download step entirely — the builder picks them up."))
    return 0


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    from src.search_index.indexer import BuildOptions, SearchIndexBuilder

    spec = level_spec(int(args.level))
    options = BuildOptions(
        level=int(args.level),
        data_dir=args.data_dir,
        sources=tuple(s for s in (args.sources or "").split(",") if s),
        shard_docs=int(args.shard_docs),
        max_pages=int(args.max_pages or 0),
        body_chars=int(args.body_chars),
        language=args.language,
        shard_index=int(args.shard_index),
        shard_count=int(args.shard_count),
        download=not args.no_download,
        quality_filter=not args.no_quality_filter,
        authority=not args.no_authority,
        keep_body=not args.drop_body,
        wikipedia_date=args.wikipedia_date or "",
        commoncrawl_snapshot=args.snapshot or "",
    )
    if args.fresh:
        import shutil

        shutil.rmtree(args.data_dir, ignore_errors=True)

    print(_bold(f"Building own search index — level {spec.level}"))
    print(f"  directory    : {args.data_dir}")
    print(f"  sources      : {', '.join(options.resolved_sources())}")
    print(f"  expect       : {spec.download} download, {spec.disk} on disk")
    if options.shard_count > 1:
        print(f"  this shard   : {options.shard_index + 1} of {options.shard_count}")
    print(_dim("  Ctrl-C is safe: progress is checkpointed after every shard."))
    print()

    started = time.time()
    builder = SearchIndexBuilder(args.data_dir, options=options,
                                 progress=lambda msg: print(f"  · {msg}", flush=True))
    try:
        stats = builder.build()
    finally:
        builder.close()

    elapsed = time.time() - started
    print()
    print(_green(f"Done in {elapsed / 60:.1f} min — "
                 f"{stats.docs:,} documents in {stats.shards} shard(s)"))
    for name, count in sorted(stats.sources.items()):
        print(f"  {name:<14}{count:>12,}")
    print()
    print(_dim("Point discovery at it:  DISCOVERY_PROVIDERS=own"))
    print(_dim(f"Try it:  python -m src.cli search-index query \"{args.probe}\" "
               f"--data-dir {args.data_dir}"))
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> int:
    from src.search_index.indexer import SearchIndexBuilder

    path = args.data_dir
    builder = SearchIndexBuilder(path, options=_options_from_args(args))
    try:
        stats = builder.stats()
    finally:
        builder.close()

    if not stats.ready:
        print(_yellow(f"No index at {path} yet."))
        print(_dim("Build one with:  python -m src.cli search-index build --level 0"))
        return 1

    print(_bold("Own search index"))
    print(f"  directory : {stats.index_dir}")
    print(f"  level     : {stats.level}")
    print(f"  documents : {stats.docs:,}")
    print(f"  shards    : {stats.shards}")
    print(f"  terms     : {stats.terms:,}")
    if stats.sources:
        print("  by source :")
        for name, count in sorted(stats.sources.items()):
            print(f"      {name:<14}{count:>12,}")
    if stats.state:
        print("  progress  :")
        for name, data in sorted(stats.state.items()):
            flag = data.get("status", "?")
            extra = f" ({data['error']})" if data.get("error") else ""
            print(f"      {name:<14}{flag:<9}{data.get('docs', 0):>10,}{extra}")
    print(f"  doc store : {human_bytes(_docstore_bytes(path))}")
    return 0


def _docstore_bytes(data_dir: str) -> int:
    from pathlib import Path

    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(data_dir) / f"docs.sqlite3{suffix}"
        if candidate.exists():
            total += candidate.stat().st_size
    return total


# ---------------------------------------------------------------------------- #
# merge
# ---------------------------------------------------------------------------- #
def cmd_merge(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    from src.search_index.merge import IndexMerger

    print(_bold(f"Merging {len(args.parts)} index part(s) into {args.out}"))
    with IndexMerger(args.out) as merger:
        for part in args.parts:
            if not _Path(part).exists():
                print(_yellow(f"  skipping missing part: {part}"))
                continue
            merger.add_part(part)
            print(f"  · {part} -> {merger.report.docs:,} docs so far")
        stats = merger.finalize()
    print()
    print(_green(f"Merged: {stats.docs:,} documents in {stats.shards} shard(s)"))
    print(_dim(f"  duplicates dropped: {merger.report.duplicates:,}"))
    for name, count in sorted(stats.sources.items()):
        print(f"  {name:<14}{count:>12,}")
    return 0


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
def cmd_query(args: argparse.Namespace) -> int:
    from src.search_index.query import SearchIndex

    index_root = f"{args.data_dir}/index"
    index = SearchIndex(index_root,
                        collapse_host=not args.no_collapse,
                        authority_weight=args.authority_weight,
                        snippet_width=args.snippet_width)
    try:
        if not index.available:
            print(_yellow(f"No usable index at {index_root}."))
            print(_dim("Build one with:  python -m src.cli search-index build --level 0"))
            return 1
        started = time.perf_counter()
        results = index.search(args.query, args.limit,
                               sources=tuple(s for s in (args.sources or "").split(",") if s) or None)
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        index.close()

    if not results:
        print(_yellow(f"Nothing matched {args.query!r}."))
        return 0

    print(_bold(f"{len(results)} result(s) for {args.query!r}  "
                f"({elapsed_ms:.1f} ms, host-collapsed: "
                f"{'yes' if not args.no_collapse else 'no'})"))
    print()
    for rank, result in enumerate(results, 1):
        doc = result.doc
        print(f"{rank:>3}. {_green(doc.title or doc.host)}")
        print(f"     {_dim(doc.url)}")
        print(f"     {result.snippet}")
        print(_dim(f"     score {result.score:.3f}  bm25 {result.bm25:.3f}  "
                   f"authority {doc.authority:.2f}/10  source {doc.source}"))
        print()
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    return 0


# --------------------------------------------------------------------------- #
# authority
# --------------------------------------------------------------------------- #
def cmd_authority(args: argparse.Namespace) -> int:
    from src.search_index.indexer import SearchIndexBuilder

    builder = SearchIndexBuilder(args.data_dir, options=_options_from_args(args))
    try:
        loaded = builder.load_authority()
        if not loaded:
            print(_yellow(f"No authority files in {args.data_dir}/raw."))
            for name, (url, filename, size) in AUTHORITY_SOURCES.items():
                print(_dim(f"  {name}: {url}  ({size}) -> save as raw/{filename}"))
            return 1
        print(_bold(f"Loaded {loaded:,} authority scores"))
        updated = builder.backfill_authority()
        print(_green(f"Updated authority on {updated:,} documents"))
    finally:
        builder.close()
    return 0


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def _options_from_args(args: argparse.Namespace):
    from src.search_index.indexer import BuildOptions

    return BuildOptions(
        level=int(getattr(args, "level", 0) or 0),
        data_dir=getattr(args, "data_dir", "data/search-index"),
        language=getattr(args, "language", "en"),
    )


def add_search_index_commands(sub: argparse._SubParsersAction) -> None:
    """Register the ``search-index`` command group on ``sub``."""
    si = sub.add_parser(
        "search-index",
        help="build, inspect and query the platform's own search index",
        description="Replace DuckDuckGo with an index we build ourselves from "
                    "free datasets (Curlie, Open PageRank, Wikipedia, Common Crawl).")
    si_sub = si.add_subparsers(dest="si_command", required=True)

    lv = si_sub.add_parser("levels", help="what each level costs and contains")
    lv.add_argument("--level", type=int, default=None,
                    help="show full detail for one level")
    lv.set_defaults(func=cmd_levels)

    sc = si_sub.add_parser("sources", help="list the datasets a level downloads")
    sc.add_argument("--level", type=int, default=0)
    sc.set_defaults(func=cmd_sources)

    b = si_sub.add_parser("build", help="build (or resume) the index")
    b.add_argument("--level", type=int, default=0, help="0..3 (default 0)")
    b.add_argument("--data-dir", default="data/search-index",
                   help="where the index lives (default data/search-index)")
    b.add_argument("--sources", default="",
                   help="comma-separated override, e.g. curlie,wikipedia")
    b.add_argument("--shard-docs", type=int, default=100_000,
                   help="documents per BM25 shard (default 100000)")
    b.add_argument("--max-pages", type=int, default=0,
                   help="Common Crawl page budget override (0 = level default)")
    b.add_argument("--body-chars", type=int, default=512,
                   help="characters of body text indexed (default 512)")
    b.add_argument("--language", default="en")
    b.add_argument("--shard-index", type=int, default=0,
                   help="this worker's slice (for parallel builds)")
    b.add_argument("--shard-count", type=int, default=1,
                   help="how many workers in total")
    b.add_argument("--no-download", action="store_true",
                   help="only use files already in <data-dir>/raw")
    b.add_argument("--no-authority", action="store_true",
                   help="skip Open PageRank / Tranco")
    b.add_argument("--no-quality-filter", action="store_true")
    b.add_argument("--drop-body", action="store_true",
                   help="do not keep body text in the document store")
    b.add_argument("--fresh", action="store_true",
                   help="delete the directory and start over")
    b.add_argument("--wikipedia-date", default="", help="e.g. 20261001")
    b.add_argument("--snapshot", default="", help="CC-MAIN snapshot, e.g. CC-MAIN-2026-39")
    b.add_argument("--probe", default="open source search engine",
                   help="example query printed at the end")
    b.set_defaults(func=cmd_build)

    st = si_sub.add_parser("status", help="how far along is the build")
    st.add_argument("--data-dir", default="data/search-index")
    st.set_defaults(func=cmd_status)

    q = si_sub.add_parser("query", help="search the index")
    q.add_argument("query", help='supports "phrases", -exclusions, site:host, source:x')
    q.add_argument("--limit", "-n", type=int, default=10)
    q.add_argument("--data-dir", default="data/search-index")
    q.add_argument("--sources", default="", help="comma-separated source filter")
    q.add_argument("--no-collapse", action="store_true",
                   help="return pages instead of one result per website")
    q.add_argument("--authority-weight", type=float, default=0.35)
    q.add_argument("--snippet-width", type=int, default=240)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_query)

    mg = si_sub.add_parser("merge",
                           help="combine parallel-built parts into one index")
    mg.add_argument("parts", nargs="+",
                    help="index directories produced by parallel `build` runs")
    mg.add_argument("--out", required=True, help="merged index directory")
    mg.set_defaults(func=cmd_merge)

    au = si_sub.add_parser("authority",
                           help="backfill Open PageRank / Tranco scores")
    au.add_argument("--data-dir", default="data/search-index")
    au.set_defaults(func=cmd_authority)


# --------------------------------------------------------------------------- #
# tiny terminal helpers (kept local so the CLI module stays import-light)
# --------------------------------------------------------------------------- #
def _wrap(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str) -> str:
    return _wrap(text, "1")


def _dim(text: str) -> str:
    return _wrap(text, "2")


def _green(text: str) -> str:
    return _wrap(text, "32")


def _yellow(text: str) -> str:
    return _wrap(text, "33")
