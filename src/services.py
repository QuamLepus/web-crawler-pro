"""One facade over every new platform layer.

The API routers, the web console, the MCP server, the CLI and the Celery
workers all need the same wired-up objects: the provenance ledger, the watch
service, the dataset store, the workflow engine, the graph, the recipe
registry, the enterprise controls. Building them in one place (and injecting
the pieces the tests replace) is the difference between a platform and a pile
of modules.

Everything here is lazy: constructing :class:`PlatformServices` touches
nothing but Redis, so importing the API does not require a search index, an
object store or a network connection. Every optional dependency degrades to a
documented no-op instead of an exception — the same rule the rest of the
codebase follows.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from src.cost.simulator import CrawlSpec, PriceBook, estimate, pages_for_budget, preset_list
from src.doctor.service import DoctorRecorder, Span, diagnose
from src.enterprise.audit import AuditLog
from src.enterprise.governance import (
    RetentionPolicy,
    apply_retention,
    bump_usage,
    export_tenant,
    plan_retention,
    restore_tenant,
    usage_report,
)
from src.enterprise.rbac import can, matrix
from src.graph.service import GraphService, GraphStore
from src.marketplace.agents import AgentMarket, AgentManifest
from src.optimizer.policy import PageSignals, plan as plan_crawl
from src.provenance.ledger import ProvenanceLedger, record_for_page
from src.provenance.manifest import (
    build_manifest,
    manifest_markdown,
    signed_manifest,
    verify_inclusion,
    verify_manifest,
)
from src.provenance.models import ChainStep, ProvenanceRecord
from src.provenance.snapshots import SnapshotStore
from src.recipes.registry import RecipeRegistry
from src.recipes.runtime import apply_recipe, test_recipe
from src.research.agent import ResearchAgent
from src.ask.service import AskBudget, AskService
from src.studio.service import Dataset, DatasetStore, summarise
from src.watch.diff import classify
from src.watch.service import default_fetcher
from src.watch.service import WatchService
from src.workflows.engine import WorkflowDeps, dry_run, run_workflow
from src.workflows.store import WorkflowStore

logger = logging.getLogger("wcp.services")

#: cap on in-process crawl work, so a workflow cannot become an outage
CRAWL_PAGE_LIMIT = 500


class PlatformServices:
    """Wired-up platform layers, lazily constructed on first use."""

    def __init__(self, redis_client, *, tenant_default: str = "public",
                 fetcher: Callable[..., Any] | None = None,
                 manifest_secret: str = "",
                 snapshot_root: str | Path | None = None,
                 recipe_root: str | Path | None = None,
                 dataset_root: str | Path | None = None) -> None:
        self.redis = redis_client
        self.tenant_default = tenant_default
        self.fetcher = fetcher or default_fetcher
        self.manifest_secret = manifest_secret or os.getenv(
            "WCP_MANIFEST_SECRET", "dev-manifest-secret-change-me")
        self._cache: dict[str, Any] = {
            "snapshot_root": snapshot_root,
            "recipe_root": recipe_root,
            "dataset_root": dataset_root,
        }

    # ------------------------------------------------------------------ #
    # lazily built layers
    # ------------------------------------------------------------------ #
    def _get(self, key: str, factory: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    @property
    def ledger(self) -> ProvenanceLedger:
        return self._get("ledger", lambda: ProvenanceLedger(self.redis))

    @property
    def snapshots(self) -> SnapshotStore:
        return self._get("snapshots", lambda: SnapshotStore(
            self._cache.get("snapshot_root")))

    @property
    def watches(self) -> WatchService:
        def build() -> WatchService:
            return WatchService(self.redis, ledger=self.ledger,
                                snapshots=self.snapshots, fetcher=self.fetcher,
                                emit=self.emit)
        return self._get("watches", build)

    @property
    def datasets(self) -> DatasetStore:
        return self._get("datasets", lambda: DatasetStore(
            self.redis, root=self._cache.get("dataset_root")))

    @property
    def workflows(self) -> WorkflowStore:
        return self._get("workflows", lambda: WorkflowStore(self.redis))

    @property
    def graph(self) -> GraphService:
        def build() -> GraphService:
            return GraphService(self.redis,
                                store=GraphStore(self.redis))
        return self._get("graph", build)

    @property
    def recipes(self) -> RecipeRegistry:
        return self._get("recipes", lambda: RecipeRegistry(
            self._cache.get("recipe_root")))

    @property
    def agents(self) -> AgentMarket:
        return self._get("agents", lambda: AgentMarket(self.redis))

    @property
    def audit(self) -> AuditLog:
        return self._get("audit", lambda: AuditLog(self.redis))

    @property
    def doctor(self) -> DoctorRecorder:
        return self._get("doctor", lambda: DoctorRecorder(self.redis))

    @property
    def price_book(self) -> PriceBook:
        return self._get("price_book", PriceBook.from_env)

    # ------------------------------------------------------------------ #
    # own search index
    # ------------------------------------------------------------------ #
    def own_index(self) -> Any:
        """The own index, or ``None`` when it has not been built.

        Returning ``None`` (rather than raising) is what lets every caller
        degrade honestly: "search is unavailable because no index has been
        built" is a state the UI renders, not an error it hides.
        """
        if "index" in self._cache:
            return self._cache["index"]
        try:
            from src.config import get_config
            from src.search_index.query import SearchIndex

            cfg = get_config()
            index = SearchIndex(
                f"{cfg.SEARCH_INDEX_DIR}/index",
                collapse_host=cfg.SEARCH_INDEX_COLLAPSE_HOST,
                authority_weight=cfg.SEARCH_INDEX_AUTHORITY_WEIGHT,
                snippet_width=cfg.SEARCH_INDEX_SNIPPET_WIDTH,
                max_loaded_shards=cfg.SEARCH_INDEX_MAX_LOADED_SHARDS,
                rerank=cfg.SEARCH_INDEX_RERANK,
                rerank_weight=cfg.SEARCH_INDEX_RERANK_WEIGHT,
                rerank_candidates=cfg.SEARCH_INDEX_RERANK_CANDIDATES,
                embed_model=cfg.SEARCH_INDEX_EMBED_MODEL,
                spell=cfg.SEARCH_INDEX_SPELL,
            )
            self._cache["index"] = index if index.available else None
        except Exception as exc:  # noqa: BLE001 - an index is optional
            logger.debug("own search index unavailable: %s", exc)
            self._cache["index"] = None
        return self._cache["index"]

    def search(self, query: str, *, limit: int = 10,
               collapse_host: bool | None = None, sources: Sequence[str] = (),
               spell: bool | None = None, rerank: str | None = None) -> dict[str, Any]:
        """Query the own index; returns hits plus why they may be missing."""
        index = self.own_index()
        if index is None:
            return {"query": query, "hits": [], "available": False,
                    "reason": "no own search index has been built "
                              "(run `wcp search-index build --level 0`)",
                    "corpus_fallback": self.corpus_search(query, limit=limit)}
        try:
            results = index.search(query, limit=limit, collapse_host=collapse_host,
                                   sources=list(sources), spell=spell,
                                   rerank=rerank) or []
            stats = index.stats()
            return {
                "query": query,
                "hits": [r.to_dict() if hasattr(r, "to_dict") else dict(r)
                         for r in results],
                "available": True,
                "stats": stats.to_dict() if hasattr(stats, "to_dict") else {},
            }
        except Exception as exc:  # noqa: BLE001
            return {"query": query, "hits": [], "available": False,
                    "reason": f"index query failed: {exc}",
                    "corpus_fallback": self.corpus_search(query, limit=limit)}

    def corpus_search(self, query: str, *, tenant: str = "", limit: int = 10
                      ) -> list[dict[str, Any]]:
        """Search what we have already collected (no index, no network).

        This is the honest fallback: the platform's own provenance ledger is a
        corpus, and answering from it costs nothing and needs no index.
        """
        tenant = tenant or self.tenant_default
        needle_terms = {t for t in query.lower().split() if len(t) > 2}
        hits: list[dict[str, Any]] = []
        try:
            records = self.ledger.recent(tenant, limit=300)
        except Exception:  # noqa: BLE001
            return hits
        for record in records:
            text = " ".join([
                record.source_url,
                str(record.extra.get("title", "")),
                str(record.extra.get("text", ""))[:2000],
            ]).lower()
            overlap = sum(1 for t in needle_terms if t in text)
            if not overlap:
                continue
            hits.append({
                "url": record.source_url,
                "title": str(record.extra.get("title", "") or record.source_url),
                "snippet": str(record.extra.get("text", ""))[:240],
                "host": record.source_url.split("/")[2] if "//" in record.source_url
                else "",
                "source": "corpus",
                "score": overlap / max(1, len(needle_terms)),
                "provenance_id": record.record_id,
                "content_sha256": record.content_sha256,
            })
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    # ------------------------------------------------------------------ #
    # ask / research
    # ------------------------------------------------------------------ #
    def _ask_service(self, tenant: str) -> AskService:
        def search(query: str, limit: int = 10, **_: Any) -> list[dict[str, Any]]:
            result = self.search(query, limit=limit)
            hits = result.get("hits") or []
            if not hits:
                hits = result.get("corpus_fallback") or []
            return hits

        return AskService(
            self.redis, search=search,
            fetch=self._fetch_for_ask, ledger=self.ledger,
            snapshots=self.snapshots, price_book=self.price_book,
        )

    def _fetch_for_ask(self, url: str) -> tuple[int, str, dict[str, str]]:
        return self.fetcher(url)

    def ask(self, question: str, *, budget: AskBudget | dict | None = None,
            mode: str = "auto", tenant: str = "",
            urls: Sequence[str] | None = None) -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        answer = self._ask_service(tenant).ask(
            question, budget=budget, mode=mode, tenant=tenant, urls=urls)
        bump_usage(self.redis, tenant, "ask")
        return answer.to_dict()

    def ask_plan(self, question: str) -> dict[str, Any]:
        return self._ask_service(self.tenant_default).plan_question(question)

    def research(self, question: str, *, budget: AskBudget | dict | None = None,
                 tenant: str = "", urls: Sequence[str] | None = None,
                 subquestions: Sequence[str] | None = None,
                 mode: str = "auto") -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        agent = ResearchAgent(self._ask_service(tenant), graph=self.graph,
                              ledger=self.ledger,
                              manifest_secret=self.manifest_secret)
        report = agent.run(question, budget=budget, tenant=tenant, urls=urls,
                           subquestions=subquestions, mode=mode)
        bump_usage(self.redis, tenant, "research")
        return report.to_dict()

    # ------------------------------------------------------------------ #
    # watch / provenance / domain score / cost
    # ------------------------------------------------------------------ #
    def emit(self, tenant: str, event: str, payload: dict[str, Any]) -> Any:
        """Webhook fan-out, if the billing/gateway layer is wired."""
        try:
            from src.gateway.webhooks.service import WebhookService

            return WebhookService(self.redis).emit(tenant, event, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("webhook emit skipped: %s", exc)
            return None

    def watch_once(self, url: str, *, kinds: Sequence[str] | None = None,
                   tenant: str = "", name: str = "") -> dict[str, Any]:
        """Fetch a URL now, diff against the previous snapshot, return events."""
        tenant = tenant or self.tenant_default
        watchers = self.watches
        watch = watchers.create(tenant, name or f"ad-hoc: {url}", [url],
                                kinds=list(kinds or ["price_change", "new_product",
                                                     "removed_product",
                                                     "policy_change",
                                                     "announcement", "jobs_change",
                                                     "content_update"]),
                                schedule="1h", adaptive=False)
        result = watchers.run(tenant, watch.watch_id, force=True)
        bump_usage(self.redis, tenant, "watch")
        return {**result, "watch_id": watch.watch_id, "ad_hoc": True}

    def diff_text(self, before: str, after: str, *, url: str = "",
                  kinds: Sequence[str] | None = None) -> list[dict[str, Any]]:
        return [e.to_dict() for e in classify(before, after, url=url, kinds=kinds)]

    def provenance_manifest(self, dataset: str, *, tenant: str = "",
                            version: str = "", sign: bool = True,
                            markdown: bool = False) -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        records = self.ledger.for_dataset(tenant, dataset, version=version)
        if not records:
            records = self.ledger.recent(tenant, limit=200)
        manifest = build_manifest(
            dataset=dataset or "ask", version=version or "1", records=records,
            created_by="platform", tenant=tenant)
        if sign:
            manifest = signed_manifest(manifest, self.manifest_secret)
        out = manifest
        if markdown:
            out = {**manifest, "markdown": manifest_markdown(manifest)}
        return out

    def verify_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        ok, reason = verify_manifest(manifest, self.manifest_secret)
        return {"ok": ok, "reason": reason,
                "records": manifest.get("record_count"),
                "merkle_root": manifest.get("merkle_root")}

    def lineage(self, record_id: str, *, tenant: str = "") -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        record = self.ledger.get(tenant, record_id)
        if record is None:
            raise KeyError(record_id)
        siblings = self.ledger.origin_of_content(tenant, record.content_sha256) \
            if record.content_sha256 else []
        return {
            "record": record.to_dict(),
            "replay": self.snapshots.replay(record),
            "same_content": [r.to_dict() for r in siblings][:20],
            "chain": [s.to_dict() for s in record.chain],
            "explain": _explain_record(record),
        }

    def domain_score(self, host: str, *, tenant: str = "") -> dict[str, Any]:
        from src.search_index.domain_score import score_from_index

        tenant = tenant or self.tenant_default
        return score_from_index(self.own_index(), host, ledger=self.ledger,
                                tenant=tenant).to_dict()

    def cost_estimate(self, spec: dict[str, Any] | None = None,
                      **overrides: Any) -> dict[str, Any]:
        payload = dict(spec or {})
        payload.update(overrides)
        return estimate(CrawlSpec.from_dict(payload), price_book=self.price_book).to_dict()

    def cost_presets(self) -> list[dict[str, Any]]:
        return preset_list()

    def pages_for_budget(self, budget_usd: float,
                         spec: dict[str, Any] | None = None) -> dict[str, Any]:
        return pages_for_budget(budget_usd, CrawlSpec.from_dict(spec),
                                price_book=self.price_book)

    def plan_crawl(self, signals: Sequence[dict[str, Any]], **kwargs: Any
                   ) -> dict[str, Any]:
        return plan_crawl([PageSignals.from_dict(s) for s in signals],
                          price_book=self.price_book, **kwargs).to_dict()

    def recipes_list(self) -> list[dict[str, Any]]:
        try:
            return self.recipes.list()
        except Exception as exc:  # noqa: BLE001
            logger.debug("recipe list failed: %s", exc)
            return []

    def recipe_test(self, name: str) -> dict[str, Any]:
        recipe = self.recipes.get(name)
        return {"recipe": recipe.ref, "health": test_recipe(recipe).to_dict(),
                "problems": recipe.validate()}

    def extract_with_recipe(self, html: str, *, url: str = "",
                            name: str = "") -> dict[str, Any]:
        """Apply the best recipe for a URL's host, with the healing report."""
        host = url.split("/")[2] if "//" in (url or "") else url
        try:
            recipe = self.recipes.for_host(host) if not name else self.recipes.get(name)
        except Exception:  # noqa: BLE001
            recipe = None
        if recipe is None:
            return {"ok": False, "reason": f"no recipe matches {host!r}",
                    "recipes": [r["name"] for r in self.recipes_list()][:10]}
        return apply_recipe(recipe, html, page_url=url).to_dict()

    # ------------------------------------------------------------------ #
    # crawl (in-process, polite, provenance-recording)
    # ------------------------------------------------------------------ #
    def crawl(self, urls: Sequence[str], *, max_pages: int = 50,
              stealth: bool = False, render_share: float = 0.0,
              priority: str = "normal", tenant: str = "",
              extract_links: bool = True) -> list[dict[str, Any]]:
        """A bounded breadth-first fetch that records provenance.

        This is the workflow engine's crawler. It is intentionally simple and
        in-process: the distributed Celery path stays the production one, and
        this path exists so a workflow, an ``/ask`` budget or a monitor can
        fetch a handful of pages without a broker.
        """
        tenant = tenant or self.tenant_default
        budget = max(1, min(int(max_pages), CRAWL_PAGE_LIMIT))
        queue: list[tuple[str, int]] = [(u, 0) for u in urls if u]
        seen: set[str] = set()
        pages: list[dict[str, Any]] = []
        started = time.time()

        while queue and len(pages) < budget:
            url, depth = queue.pop(0)
            if url in seen or depth > 3:
                continue
            seen.add(url)
            try:
                status, body, headers = self.fetcher(url)
            except Exception as exc:  # noqa: BLE001
                self.doctor.record(tenant, Span(url=url, ok=False,
                                                cause="unknown",
                                                phases={"ttfb": 0.0}))
                logger.debug("crawl fetch failed %s: %s", url, exc)
                continue
            span = Span(url=url, status=status, bytes=len(body or ""),
                        phases={"ttfb": 0.0, "transfer": 0.0},
                        ok=status < 400 and bool(body))
            if not span.ok:
                span.cause = "http_4xx" if 400 <= status < 500 else "empty_body"
            self.doctor.record(tenant, span)
            if not span.ok:
                continue

            snapshot = self.snapshots.put(body, url=url, status=status,
                                          content_type=headers.get("content-type", ""))
            record = self.ledger.record(record_for_page(
                tenant=tenant, url=url, body=body, status=status, method="http",
                snapshot_id=snapshot.snapshot_id, extractor="crawl",
                extractor_version="1", confidence=0.6,
                extra={"title": _title_of(body), "text": _text_of(body)[:6000],
                       "depth": depth}))
            pages.append({"url": url, "status": status, "html": body,
                          "title": _title_of(body), "text": _text_of(body),
                          "snapshot_id": snapshot.snapshot_id,
                          "provenance_id": record.record_id,
                          "fetched_at": record.fetched_at, "depth": depth})
            if extract_links and depth < 3:
                for link in _links(body, url)[:20]:
                    if link not in seen:
                        queue.append((link, depth + 1))
            if time.time() - started > 300:      # hard stop
                logger.warning("in-process crawl hit its 300s ceiling")
                break
        bump_usage(self.redis, tenant, "crawl", len(pages))
        return pages

    # ------------------------------------------------------------------ #
    # datasets / workflows / agents
    # ------------------------------------------------------------------ #
    def dataset_records(self, name: str, *, tenant: str = "",
                        version: str = "") -> list[dict[str, Any]]:
        tenant = tenant or self.tenant_default
        dataset = self.datasets.get(tenant, name)
        if dataset is None:
            return []
        return self.datasets.rows(dataset, version or None)

    def ask_dataset(self, tenant: str, name: str, question: str, *,
                    version: str = "", limit: int = 5000) -> dict[str, Any]:
        """Plain-English question over a dataset version (see src/studio/nlq.py).

        Deterministic and offline: no model call, and the answer carries the
        parse, the caveats and the working rows so it can be checked.
        """
        from src.studio.nlq import answer as nlq_answer
        from src.studio.nlq import caveats_for, examples
        from src.studio.service import quality_report

        tenant = tenant or self.tenant_default
        dataset = self.datasets.get(tenant, name)
        if dataset is None:
            raise KeyError(f"dataset {name!r} not found")
        rows = self.datasets.rows(dataset, version or None, limit=limit)
        target = version or (dataset.latest.version if dataset.latest else "")
        result = nlq_answer(question, rows, dataset=dataset.name, version=target,
                            limit=limit)
        result["caveats"] = list(result.get("caveats") or []) + caveats_for(
            quality_report(rows, key=dataset.primary_key), rows)
        result["caveats"] = result["caveats"][:6]
        if not result.get("understood"):
            result["examples"] = examples(rows)
        return result

    def workflow_deps(self, tenant: str = "") -> WorkflowDeps:
        """Real collaborators for the workflow engine."""
        tenant = tenant or self.tenant_default

        def discovery(topic: str, count: int = 25) -> dict[str, Any]:
            from src.discovery.service import preview_discovery

            if os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes"):
                # Offline: answer from the corpus instead of pretending to search.
                hits = self.corpus_search(topic, tenant=tenant, limit=count)
                return {"candidates": [{"url": h["url"], "host": h.get("host", ""),
                                        "score": h.get("score", 0.5),
                                        "title": h.get("title", "")}
                                       for h in hits]}
            return preview_discovery(topic=topic, count=count)

        def crawl(urls: Sequence[str], **kwargs: Any) -> list[dict[str, Any]]:
            return self.crawl(urls, tenant=tenant, **kwargs)

        def extract(pages: Sequence[dict[str, Any]], *, recipe: str = "",
                    schema: Any = None, llm: bool = False) -> list[dict[str, Any]]:
            rows = []
            for page in pages:
                html = page.get("html", "")
                url = page.get("url", "")
                if recipe or self.recipes_list():
                    result = self.extract_with_recipe(html, url=url, name=recipe)
                    if result.get("ok"):
                        rows.append({**result["values"], "url": url,
                                     "provenance_id": page.get("provenance_id", ""),
                                     "confidence": result.get("confidence", 0.5)})
                        continue
                rows.append({"url": url, "title": page.get("title", ""),
                             "text": (page.get("text") or "")[:2000],
                             "provenance_id": page.get("provenance_id", ""),
                             "fetched_at": page.get("fetched_at"),
                             "confidence": 0.4})
            return rows

        def clean(records: Sequence[dict[str, Any]], *, normalize: bool = True,
                  drop_empty: bool = True) -> list[dict[str, Any]]:
            try:
                from src.pipeline.cleaning import normalize_item

                rows = [normalize_item(dict(r)) for r in records]
            except Exception:  # noqa: BLE001
                rows = [dict(r) for r in records]
            if drop_empty:
                rows = [r for r in rows if any(v not in (None, "", []) for v in r.values())]
            return rows

        def dedupe(records: Sequence[dict[str, Any]], *, mode: str = "exact",
                   threshold: float = 0.8) -> list[dict[str, Any]]:
            if mode == "exact":
                from src.workflows.engine import _exact_dedupe

                return _exact_dedupe(records)
            try:
                from src.pipeline.dedupe import SimHashDeduper

                deduper = SimHashDeduper(max_distance=int(threshold)
                                         if threshold > 1 else 3)
                out = []
                for row in records:
                    text = str(row.get("text") or row.get("description") or
                               row.get("title") or "")
                    if text and deduper.is_duplicate(text):
                        continue
                    out.append(row)
                return out
            except Exception as exc:  # noqa: BLE001
                logger.debug("dedupe fallback: %s", exc)
                from src.workflows.engine import _exact_dedupe

                return _exact_dedupe(records)

        def graph_write(records: Sequence[dict[str, Any]], kinds: Any = None
                        ) -> dict[str, Any]:
            touched = 0
            for row in records[:50]:
                text = str(row.get("text") or row.get("title") or "")
                if not text:
                    continue
                result = self.graph.ingest_text(
                    tenant, text[:6000], url=str(row.get("url", "")),
                    record_id=str(row.get("provenance_id", "")))
                touched += len(result.get("entities", []))
            return {"entities": touched}

        def dataset_write(name: str, records: Sequence[dict[str, Any]], *,
                          primary_key: str = "url", live: bool = True
                          ) -> dict[str, Any]:
            dataset = self.datasets.get(tenant, name)
            if dataset is None:
                dataset = self.datasets.create(tenant, name, kind="workflow")
            manifest = None
            try:
                ids = [str(r.get("provenance_id")) for r in records
                       if r.get("provenance_id")]
                p_records = [self.ledger.get(tenant, i) for i in ids[:500]]
                p_records = [r for r in p_records if r is not None]
                if p_records:
                    manifest = signed_manifest(build_manifest(
                        dataset=name, version="auto", records=p_records,
                        tenant=tenant, created_by="workflow"), self.manifest_secret)
            except Exception as exc:  # noqa: BLE001
                logger.debug("manifest build failed: %s", exc)
            result = (self.datasets.refresh if live else self.datasets.add_version)
            payload = result(dataset, list(records), source="workflow") \
                if live else {"version": result(dataset, list(records),
                                                source="workflow"), "changed": True}
            version = payload.get("version") or {}
            if manifest and isinstance(version, dict):
                version["manifest"] = manifest
            bump_usage(self.redis, tenant, "dataset_write")
            return payload

        def analysis(records: Sequence[dict[str, Any]], *, numeric_fields: Any = None,
                     top: int = 5) -> dict[str, Any]:
            fields = list(numeric_fields or [])
            if not fields:
                fields = [k for k, v in (records[0] if records else {}).items()
                          if isinstance(v, (int, float))]
            return summarise(records, numeric_fields=fields, top=top)

        def report(name: str, records: Sequence[dict[str, Any]]) -> str:
            dataset = self.datasets.get(tenant, name) if name else None
            if dataset is not None:
                payload, _ = self.datasets.export(dataset, fmt="markdown")
                return payload
            return "# workflow report\n\n" + summarise(records).__repr__()

        def notify(event: str, payload: dict[str, Any]) -> Any:
            return self.emit(tenant, event, payload)

        def webhook_post(url: str, payload: dict[str, Any]) -> Any:
            if os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes"):
                return "skipped: offline"
            import httpx

            resp = httpx.post(url, json=payload, timeout=10.0)
            return resp.status_code

        def export(name: str, fmt: str = "csv") -> tuple[str, str]:
            dataset = self.datasets.get(tenant, name)
            if dataset is None:
                raise KeyError(f"dataset {name!r} not found")
            bump_usage(self.redis, tenant, "export")
            return self.datasets.export(dataset, fmt=fmt)

        return WorkflowDeps(
            discovery=discovery, crawl=crawl, extract=extract, clean=clean,
            dedupe=dedupe, graph_write=graph_write, dataset_write=dataset_write,
            analysis=analysis, report=report, notify=notify,
            webhook_post=webhook_post, export=export,
            recipes=self.recipes, max_seconds=900.0,
        )

    def run_workflow(self, tenant: str, workflow_id: str) -> dict[str, Any]:
        workflow = self.workflows.get(tenant, workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        run = run_workflow(workflow, self.workflow_deps(tenant))
        self.workflows.save_run(run)
        workflow.runs += 1
        workflow.last_run_at = run.started_at
        self.workflows.save(workflow)
        bump_usage(self.redis, tenant, "workflow")
        return run.to_dict()

    def workflow_preview(self, workflow_id: str, *, tenant: str = "") -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        workflow = self.workflows.get(tenant, workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        return dry_run(workflow)

    # ------------------------------------------------------------------ #
    # enterprise
    # ------------------------------------------------------------------ #
    def retention_policy(self, tenant: str = "") -> RetentionPolicy:
        tenant = tenant or self.tenant_default
        raw = None
        try:
            raw = self.redis.get(f"wcp:retention:{tenant}")
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            import json

            try:
                return RetentionPolicy.from_dict(json.loads(
                    raw.decode() if isinstance(raw, bytes) else raw))
            except Exception:  # noqa: BLE001
                pass
        return RetentionPolicy()

    def set_retention_policy(self, policy: RetentionPolicy | dict[str, Any],
                             tenant: str = "") -> dict[str, Any]:
        import json

        tenant = tenant or self.tenant_default
        resolved = (policy if isinstance(policy, RetentionPolicy)
                    else RetentionPolicy.from_dict(policy))
        self.redis.set(f"wcp:retention:{tenant}",
                       json.dumps(resolved.to_dict()))
        self.audit.log("retention.update", tenant=tenant, actor="api",
                       policy=resolved.to_dict())
        return {"policy": resolved.to_dict(), "problems": resolved.validate()}

    def retention_plan(self, tenant: str = "") -> list[dict[str, Any]]:
        tenant = tenant or self.tenant_default
        policy = self.retention_policy(tenant)
        try:
            snapshots = [type("S", (), {
                "stored_at": p.stat().st_mtime, "size": p.stat().st_size})()
                for p in self.snapshots.root.rglob("*.html.gz")]
        except Exception:  # noqa: BLE001
            snapshots = []
        datasets = self.datasets.list(tenant, limit=200)
        return [a.to_dict() for a in
                plan_retention(snapshots=snapshots, datasets=datasets, policy=policy)]

    def retention_apply(self, tenant: str = "") -> dict[str, Any]:
        from src.enterprise.governance import RetentionAction

        tenant = tenant or self.tenant_default
        policy = self.retention_policy(tenant)
        datasets = self.datasets.list(tenant, limit=200)
        actions = [RetentionAction(**row) for row in self.retention_plan(tenant)]
        result = apply_retention(actions, snapshot_store=self.snapshots,
                                 dataset_store=self.datasets, datasets=datasets,
                                 policy=policy)
        self.audit.log("retention.run", tenant=tenant, actor="system",
                       **{"result.total_deleted": result.get("total_deleted", 0)})
        return result

    def usage(self, tenant: str = "") -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        try:
            from src.gateway.billing.plans import get_plan

            plan = get_plan("free")
            org = None
            try:
                from src.gateway.auth.users import UserStore

                org = UserStore(self.redis).get_org(tenant)
            except Exception:  # noqa: BLE001
                org = None
            if org is not None:
                plan = get_plan(getattr(org, "plan", "free"))
        except Exception:  # noqa: BLE001
            plan = None
        return usage_report(self.redis, tenant, plan=plan)

    def backup(self, tenant: str = "", *, limit: int = 20_000) -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        result = export_tenant(self.redis, tenant, limit=limit)
        self.audit.log("backup.create", tenant=tenant, actor="api",
                       keys=result["keys"], bytes=result["bytes"],
                       checksum=result["checksum"])
        return {"tenant": tenant, "keys": result["keys"], "bytes": result["bytes"],
                "checksum": result["checksum"], "created_at": result["created_at"]}

    def restore(self, payload: dict[str, Any], *, dry_run: bool = True,
                tenant: str = "") -> dict[str, Any]:
        result = restore_tenant(self.redis, bytes(payload.get("archive") or b""),
                               dry_run=dry_run, tenant=tenant)
        self.audit.log("backup.restore", tenant=tenant or self.tenant_default,
                       actor="api", dry_run=dry_run, keys=result["keys"])
        return result

    def install_agent(self, tenant: str, slug: str, *,
                      config: dict[str, Any] | None = None) -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        result = self.agents.install(tenant, slug, config=config)
        workflow = result.get("workflow") or {}
        from src.workflows.models import Workflow

        stored = self.workflows.save(Workflow.from_dict(workflow))
        result["workflow_id"] = stored.workflow_id
        self.audit.log("workflow.create", tenant=tenant, actor="api",
                       agent=slug, workflow_id=stored.workflow_id)
        return result

    # ------------------------------------------------------------------ #
    # health summary (used by the console and the status page)
    # ------------------------------------------------------------------ #
    def health(self, tenant: str = "") -> dict[str, Any]:
        tenant = tenant or self.tenant_default
        checks: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            checks["redis"] = bool(self.redis.ping())
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc}"
        index = self.own_index()
        checks["search_index"] = "available" if index is not None else "not built"
        try:
            checks["ledger_records"] = int(
                self.redis.zcard(f"wcp:prov:{tenant}:all") or 0)
        except Exception:  # noqa: BLE001
            checks["ledger_records"] = None
        try:
            checks["watches"] = len(self.watches.list(tenant, limit=500))
        except Exception:  # noqa: BLE001
            checks["watches"] = None
        try:
            checks["datasets"] = len(self.datasets.list(tenant, limit=500))
        except Exception:  # noqa: BLE001
            checks["datasets"] = None
        try:
            checks["workflows"] = len(self.workflows.list(tenant, limit=500))
        except Exception:  # noqa: BLE001
            checks["workflows"] = None
        try:
            checks["recipes"] = len(self.recipes_list())
        except Exception:  # noqa: BLE001
            checks["recipes"] = None
        try:
            report = diagnose(self.doctor.recent(tenant, limit=200))
            checks["crawl_health"] = {"verdict": report.verdict,
                                      "samples": report.samples,
                                      "success_rate": round(report.success_rate, 3),
                                      "p95_ms": round(report.p95_ms, 1)}
        except Exception:  # noqa: BLE001
            checks["crawl_health"] = None
        checks["checked_ms"] = round((time.perf_counter() - started) * 1000, 2)
        checks["offline_mode"] = os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes")
        return checks

    def rbac_matrix(self) -> list[dict[str, Any]]:
        return matrix()

    def can(self, role: str, permission: str) -> bool:
        return bool(can(role, permission))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _title_of(html: str) -> str:
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", flags=re.I | re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip()[:200] if m else ""


def _text_of(html: str) -> str:
    from src.watch.diff import content_lines

    return "\n".join(content_lines(html))[:20_000]


def _links(html: str, base: str, *, limit: int = 40) -> list[str]:
    import re
    from urllib.parse import urljoin, urlparse

    out: list[str] = []
    base_host = urlparse(base).hostname or ""
    for match in re.finditer(r'href=["\']([^"\'#]+)["\']', html or "", flags=re.I):
        href = match.group(1).strip()
        if href.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(base, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.hostname != base_host:      # stay on the site
            continue
        if any(absolute.lower().endswith(ext) for ext in
               (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".svg", ".mp4", ".gz")):
            continue
        if absolute not in out:
            out.append(absolute)
        if len(out) >= limit:
            break
    return out


def _explain_record(record: ProvenanceRecord) -> list[str]:
    """Human sentences explaining a record's history."""
    out = [f"Collected from {record.source_url} with HTTP {record.http_status} "
           f"({record.collection_method})."]
    out.append(f"robots.txt verdict at collection time: {record.robots_verdict}.")
    out.append(f"Licence recorded as {record.license}"
               + (f" ({record.license_url})" if record.license_url else "") + ".")
    if record.content_sha256:
        out.append(f"Content hash {record.content_sha256[:23]}… — the exact bytes are "
                   "in the snapshot store and can be replayed.")
    if record.extractor:
        out.append(f"Extracted by {record.extractor}@{record.extractor_version or '?'}"
                   + (f" using model {record.llm_model_id}" if record.llm_model_id else "")
                   + "." + (f" Prompt hash {record.prompt_hash[:16]}…"
                            if record.prompt_hash else ""))
    for step in record.chain:
        out.append(f"Chain step '{step.step}' by {step.tool or 'unknown'}"
                   + (f" v{step.version}" if step.version else "") + ".")
    if record.confidence:
        out.append(f"Extraction confidence {record.confidence:.2f}.")
    return out


def chain_for(step: str, *, tool: str = "", version: str = "",
              model: str = "", prompt_hash: str = "", **detail: Any) -> ChainStep:
    return ChainStep(step=step, tool=tool, version=version, model=model,
                     prompt_hash=prompt_hash, detail=detail)


def verify_record_in_manifest(manifest: dict[str, Any], record: ProvenanceRecord
                              ) -> dict[str, Any]:
    """Prove one record belongs to a signed manifest (inclusion proof).

    This is the answer to "can you prove this row came from the dataset you
    sold me" — without shipping the dataset back.
    """
    from src.provenance.manifest import inclusion_proof

    leaves = manifest.get("record_hashes_sample") or []
    target = record.record_hash or record.payload_hash
    if target in leaves:
        proof = inclusion_proof(leaves, leaves.index(target))
        return {"in": target in leaves, "proof_length": len(proof),
                "root": manifest.get("merkle_root"),
                "note": "sample proof: the manifest stores the first five hashes"}
    return {"in": False, "note": "manifest sample does not include this record; "
                                 "request the full manifest to verify"}
