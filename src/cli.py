"""Command-line entry point.

The headline command is :func:`cmd_discover` — it implements "give the crawler
a topic and a number instead of a list of URLs"::

    python -m src.cli discover \\
        --topic "best budget laptops 2026" \\
        --count 25 \\
        --seed https://www.rtings.com \\
        --max-pages 8 \\
        --auto

Other commands:

    python -m src.cli preview --topic "..." --count 10   # dry run, no crawl
    python -m src.cli crawl   --url https://example.com --max-pages 20
    python -m src.cli status  --job disc_ab12cd34
    python -m src.cli results --job disc_ab12cd34

``discover`` without ``--auto`` stops after ranking and prints the shortlist so
you can review it; add ``--approve`` (or rerun with ``--auto``) to crawl.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime

DEFAULT_OUTPUT_DIR = os.getenv("DISCOVERY_OUTPUT_DIR", "output/discovery")

# The CLI drives several subsystems whose loggers are configured eagerly at
# import time (src.utils.logger.setup_logger). Their DEBUG chatter would bury
# the progress output, so the console stays at WARNING unless --verbose.
_QUIET = "--verbose" not in sys.argv
if _QUIET:
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("CRAWLER_LOG_LEVEL", "WARNING")


# --------------------------------------------------------------------------- #
# tiny console helpers (no extra dependency: `rich` is optional in this repo)
# --------------------------------------------------------------------------- #
def _c(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.getenv("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def green(t: str) -> str:
    return _c(t, "32")


def red(t: str) -> str:
    return _c(t, "31")


def yellow(t: str) -> str:
    return _c(t, "33")


def step(n: int, total: int, label: str, detail: str = "") -> None:
    suffix = f"  {dim(detail)}" if detail else ""
    print(f"[{n}/{total}] {bold(label)}{suffix}", flush=True)


# --------------------------------------------------------------------------- #
# redis helper
# --------------------------------------------------------------------------- #
def _redis():
    """Redis for job records; falls back to fakeredis so the CLI works locally.

    A discovery run is fully usable without a real Redis — you just lose the
    ability to poll it later with ``status``/``results``.
    """
    from src.config import get_config

    url = get_config().REDIS_URL
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except Exception:  # noqa: BLE001
        try:
            import fakeredis
        except ImportError:  # pragma: no cover
            print(yellow("  ! no Redis and no fakeredis — job state is in-memory only"))
            return None
        print(dim("  (redis unreachable — using in-memory fakeredis)"))
        return fakeredis.FakeStrictRedis(decode_responses=True)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_preview(args: argparse.Namespace) -> int:
    """Rank websites for a topic without crawling anything."""
    from src.discovery.service import preview_discovery

    print(bold(f"Topic: {args.topic}"))
    result = preview_discovery(
        topic=args.topic, count=args.count, seeds=list(args.seed or []),
        language=args.language, per_domain=args.per_domain)
    print(dim(f"queries: {', '.join(result['queries'])}"))
    print(dim(f"providers: {', '.join(result['providers']) or 'none available'}"))
    print()
    if not result["candidates"]:
        print(yellow("No candidates found."))
        print(dim("Discovery needs outbound network access. Offline? Set nothing "
                  "— DuckDuckGo is used by default and needs no API key. "
                  "For production use set BRAVE_API_KEY / SERPAPI_KEY / "
                  "TAVILY_API_KEY."))
        return 1
    _print_candidates(result["candidates"])
    print(dim(f"\n{result['total_found']} candidates scored, "
              f"{len(result['candidates'])} shortlisted."))
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def _print_candidates(candidates: list[dict]) -> None:
    for i, c in enumerate(candidates, 1):
        flags = []
        if c.get("seeded"):
            flags.append("seed")
        if c.get("providers"):
            flags.append("+".join(c["providers"]))
        tag = f"  [{', '.join(flags)}]" if flags else ""
        score = float(c.get("score", 0.0))
        print(f"{i:3}. {green(f'{score:.2f}')}  {c['domain']}{dim(tag)}")
        print(f"     {dim(c['url'][:110])}")
        if c.get("reasons"):
            print(f"     {dim('; '.join(c['reasons'][:3]))}")


def cmd_discover(args: argparse.Namespace) -> int:
    """Search -> rank -> (approve) -> crawl -> report."""
    from src.config import get_config
    from src.discovery.service import (
        DiscoveryRequest,
        DiscoveryService,
        DiscoveryState,
        run_discovery,
        run_discovery_and_crawl,
    )

    cfg = get_config()
    if args.max_pages is not None:
        max_pages = args.max_pages
    else:
        max_pages = cfg.DISCOVERY_MAX_PAGES_PER_SITE

    req = DiscoveryRequest(
        topic=args.topic, count=args.count, seeds=list(args.seed or []),
        language=args.language, auto=args.auto,
        max_pages_per_site=max_pages, per_domain=args.per_domain,
        obey_robots=not args.ignore_robots, stealth=args.stealth,
        tenant=args.tenant,
    )

    print(bold("Topic discovery"))
    print(f"  topic      : {req.topic}")
    print(f"  websites   : {req.count}")
    if req.seeds:
        print(f"  seeds      : {', '.join(req.seeds)}")
    print(f"  pages/site : {req.max_pages_per_site}")
    print(f"  mode       : {'automatic' if req.auto else 'review before crawling'}")
    print()

    r = _redis()
    t0 = time.perf_counter()

    step(1, 5, "Searching the web and ranking what we find")
    if req.auto:
        record = run_discovery_and_crawl({**req.to_dict()}, redis_client=r)
        # auto mode already crawled, so `candidates` holds the shortlist only
        candidates = list(record.get("candidates", []))
        detail = f"{len(candidates)} websites"
    else:
        record = run_discovery({**req.to_dict()}, redis_client=r)
        found = record.get("candidates", [])
        found = found if isinstance(found, list) else []
        candidates = [c for c in found if c.get("selected")]
        detail = f"{len(candidates)} of {len(found)} candidates"

    state = record.get("state", "")
    if state == DiscoveryState.failed.value:
        print(red(f"\nDiscovery failed: {record.get('error', 'unknown error')}"))
        return 1

    step(2, 5, "Shortlist built", detail)
    if not candidates:
        step(3, 5, "No candidates", "stopping")
        print()
        print(yellow("No websites found for this topic."))
        print(dim("Discovery needs outbound network access. The default provider "
                  "(DuckDuckGo HTML) needs no API key; for higher volume set "
                  "BRAVE_API_KEY / SERPAPI_KEY / TAVILY_API_KEY."))
        return 1

    step(3, 5, "Shortlist", f"{len(candidates)} websites")
    print()
    _print_candidates(candidates)
    print()

    if not req.auto:
        if not args.approve:
            print(dim("Review the list. Re-run with --approve to crawl these "
                      "sites, or use --auto to skip review."))
            print(dim(f"job id: {record['job_id']}"))
            _write_report(record, [], args.out)
            return 0
        step(4, 5, "Approved — crawling")
        svc = DiscoveryService(r)
        _selected, crawl_ids = svc.approve(record["job_id"])
        record.update(svc.get(record["job_id"]) or {})
        record["crawl_job_ids"] = crawl_ids
        print(dim(f"  dispatched {len(crawl_ids)} crawl jobs through the "
                  "scheduler (poll with: python -m src.cli results "
                  f"--job {record['job_id']})"))
        step(5, 5, "Done",
             f"{len(crawl_ids)} sites queued in {time.perf_counter() - t0:.1f}s")
        _write_report(record, [], args.out)
        print(f"\n{green('✓')} {record['job_id']} — "
              f"{len(crawl_ids)} websites queued for crawling")
        return 0

    # -- auto mode: crawling already happened in-process -------------------- #
    items = record.get("crawled_items", [])
    per_site = record.get("per_site", [])
    step(4, 5, "Crawling",
         f"{record.get('sites_succeeded', 0)}/{record.get('sites_total', 0)} "
         f"sites, {record.get('pages', 0)} pages")
    for site in per_site:
        mark = green("ok") if site["state"] == "succeeded" else red("!!")
        print(f"     {mark} {site['domain']:<38} "
              f"{site['pages']:>3} pages  {site['items']:>4} items"
              + (dim(f"  {site['error'][:60]}") if site.get("error") else ""))
    step(5, 5, "Report",
         f"{record.get('items', 0)} items in {time.perf_counter() - t0:.1f}s")

    paths = _write_report(record, items, args.out)
    print()
    print(f"{green('✓')} {bold(str(record.get('items', 0)))} items from "
          f"{record.get('sites_succeeded', 0)}/{record.get('sites_total', 0)} "
          f"websites")
    for path in paths:
        print(f"   {dim(path)}")
    if args.json:
        printable = {k: v for k, v in record.items()
                     if k not in ("crawled_items", "candidates")}
        print(json.dumps(printable, indent=2, default=str))
    return 0


def _write_report(record: dict, items: list[dict], out_dir: str | None) -> list[str]:
    """Write ``report.json`` + ``items.jsonl``; returns the paths written."""
    base = out_dir or DEFAULT_OUTPUT_DIR
    target = os.path.join(base, record.get("job_id", "discovery"))
    written: list[str] = []
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        print(yellow(f"  ! could not create {target}: {exc}"))
        return written

    report = {k: v for k, v in record.items() if k != "crawled_items"}
    report["generated_at"] = datetime.now(UTC).isoformat()
    report_path = os.path.join(target, "report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    written.append(report_path)

    if items:
        items_path = os.path.join(target, "items.jsonl")
        with open(items_path, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(item, default=str) + "\n" for item in items)
        written.append(items_path)
    return written


def cmd_crawl(args: argparse.Namespace) -> int:
    """Crawl one URL through the same pipeline the API uses."""
    from workers.crawl_tasks import run_crawl

    r = _redis()
    job_id = f"crawl_cli_{int(time.time())}"
    print(bold(f"Crawling {args.url} (max {args.max_pages} pages)"))
    result = run_crawl({"url": args.url, "job_id": job_id, "tenant": args.tenant,
                        "max_pages": args.max_pages,
                        "obey_robots": not args.ignore_robots,
                        "stealth": args.stealth}, redis_client=r)
    print(f"  pages : {result.get('pages_done', 0)}")
    print(f"  items : {result.get('items', 0)}")
    print(f"  sha256: {result.get('data_sha256', '')}")
    for failure in result.get("failures", []):
        print(red(f"  ! {failure}"))
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, "items.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(item, default=str) + "\n" for item in result.get("collected", []))
        print(f"  wrote {path}")
    return 0 if result.get("items") else 1


def cmd_status(args: argparse.Namespace) -> int:
    from src.discovery.service import DiscoveryService

    svc = DiscoveryService(_redis())
    record = svc.get(args.job)
    if record is None:
        print(red(f"no discovery job {args.job!r}"))
        return 1
    print(bold(f"{record['job_id']}  [{record.get('state')}]"))
    print(f"  topic    : {record.get('topic')}")
    print(f"  sites    : {record.get('sites_succeeded', 0)}"
          f"/{record.get('sites_total', 0)} done")
    print(f"  pages    : {record.get('pages', 0)}")
    print(f"  items    : {record.get('items', 0)}")
    if record.get("error"):
        print(red(f"  error   : {record['error']}"))
    if args.json:
        print(json.dumps(record, indent=2, default=str))
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    from src.discovery.service import DiscoveryService

    svc = DiscoveryService(_redis())
    try:
        report = svc.results(args.job)
    except KeyError:
        print(red(f"no discovery job {args.job!r}"))
        return 1
    print(bold(f"{report['job_id']}  [{report['state']}]  {report['topic']}"))
    for site in report["per_site"]:
        mark = green("ok") if site["state"] == "succeeded" else red("!!")
        print(f"  {mark} {site['domain']:<38} {site['pages']:>3} pages "
              f"{site['items']:>4} items"
              + (dim(f"  {site['error'][:60]}") if site.get("error") else ""))
    print(f"  total: {report['pages']} pages, {report['items']} items")
    if report.get("data_sha256"):
        print(dim(f"  digest: {report['data_sha256']}"))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="WebCrawlerPro command line — crawl URLs, let the crawler "
                    "discover the websites for a topic, and build/query the "
                    "platform's own search index (`search-index`).")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--verbose", "-v", action="store_true",
                       help="show debug logs from the crawler subsystems")
        p.add_argument("--json", action="store_true",
                       help="also print the raw JSON report")
        p.add_argument("--out", default=None,
                       help=f"output directory (default {DEFAULT_OUTPUT_DIR})")
        p.add_argument("--tenant", default=os.getenv("WCP_TENANT", "cli"),
                       help="tenant id recorded on the job")

    # discover ------------------------------------------------------------- #
    d = sub.add_parser(
        "discover",
        help="find the best N websites about a topic and crawl them",
        description="Give a topic and a number instead of a list of URLs.")
    d.add_argument("--topic", "-t", required=True, help="what to research")
    d.add_argument("--count", "-n", type=int, default=10,
                   help="how many websites to shortlist (default 10)")
    d.add_argument("--seed", "-s", action="append", default=[], metavar="URL",
                   help="a site you already trust (repeatable)")
    d.add_argument("--language", default="en", help="ISO language code (default en)")
    d.add_argument("--max-pages", type=int, default=None,
                   help="pages to crawl per website "
                        "(default DISCOVERY_MAX_PAGES_PER_SITE, 5)")
    d.add_argument("--per-domain", type=int, default=1,
                   help="max entry points per website (default 1)")
    d.add_argument("--auto", action="store_true",
                   help="crawl immediately, without review")
    d.add_argument("--approve", action="store_true",
                   help="crawl the shortlist (implies reviewing it first)")
    d.add_argument("--ignore-robots", action="store_true",
                   help="do not honour robots.txt")
    d.add_argument("--stealth", action="store_true",
                   help="use the stealth fetch ladder")
    add_common(d)
    d.set_defaults(func=cmd_discover)

    # preview -------------------------------------------------------------- #
    p = sub.add_parser("preview",
                       help="rank websites for a topic without crawling")
    p.add_argument("--topic", "-t", required=True)
    p.add_argument("--count", "-n", type=int, default=10)
    p.add_argument("--seed", "-s", action="append", default=[], metavar="URL")
    p.add_argument("--language", default="en")
    p.add_argument("--per-domain", type=int, default=1)
    add_common(p)
    p.set_defaults(func=cmd_preview)

    # crawl ---------------------------------------------------------------- #
    c = sub.add_parser("crawl", help="crawl a single URL")
    c.add_argument("--url", "-u", required=True)
    c.add_argument("--max-pages", type=int, default=10)
    c.add_argument("--ignore-robots", action="store_true")
    c.add_argument("--stealth", action="store_true")
    add_common(c)
    c.set_defaults(func=cmd_crawl)

    # status / results ------------------------------------------------------ #
    st = sub.add_parser("status", help="show a discovery job's state")
    st.add_argument("--job", "-j", required=True)
    add_common(st)
    st.set_defaults(func=cmd_status)

    rs = sub.add_parser("results", help="aggregated per-site crawl report")
    rs.add_argument("--job", "-j", required=True)
    add_common(rs)
    rs.set_defaults(func=cmd_results)

    # search-index --------------------------------------------------------- #
    from src.search_index.cli import add_search_index_commands

    add_search_index_commands(sub)

    return parser


def _configure_logging(verbose: bool) -> None:
    """Keep the console quiet unless --verbose.

    ``setup_logger`` reads ``Config.LOG_LEVEL``, and every profile class
    hardcodes its own value (Development/Testing -> DEBUG), so setting the
    ``LOG_LEVEL`` env var alone has no effect. Overriding it on the cached
    config *instance* shadows the class attribute and does work; the loop
    below then fixes any logger already configured at import time.
    """
    import logging

    level_name = "DEBUG" if verbose else "WARNING"
    level = getattr(logging, level_name)
    try:
        from src.config import get_config

        get_config().LOG_LEVEL = level_name
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        # Quieting the logs must never be the reason a crawl fails to start.
        logging.getLogger("wcp.cli").debug(
            "could not lower the log level (%s)", exc)
    logging.getLogger().setLevel(level)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith(("src", "workers", "celery", "web_crawler_pro")):
            logging.getLogger(name).setLevel(level)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        print(yellow("\ninterrupted"))
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
