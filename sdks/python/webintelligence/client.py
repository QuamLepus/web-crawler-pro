"""Synchronous HTTPX client; no API key is logged or included in exceptions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx


WebhookEvent = Literal[
    "crawl.completed", "crawl.failed", "crawl.cancelled", "data.ready",
    "export.ready", "quota.warning", "dataset.sold", "discovery.completed",
    "discovery.approved", "web.changed", "research.completed", "research.failed",
    "research.cancelled",
]


class _UnsetType:
    """Sentinel type used to distinguish omitted update fields from null."""


_UNSET = _UnsetType()


@dataclass(frozen=True)
class WorkflowStep:
    """One HTTP(S) crawl step; explicit dependencies enable bounded DAG branching."""
    url: str
    max_pages: int = 3
    obey_robots: bool = True
    step_id: str | None = None
    depends_on: list[str] | None = None


class WorkflowResource:
    """Tenant-scoped workflow and run methods exposed at ``client.workflows``."""
    def __init__(self, client: Client) -> None:
        self._client = client

    @staticmethod
    def _steps_payload(steps: list[WorkflowStep | Mapping[str, Any]]) -> list[dict[str, Any]]:
        payloads = []
        for step in steps:
            if not isinstance(step, WorkflowStep):
                payloads.append(dict(step))
                continue
            item: dict[str, Any] = {"url": step.url, "max_pages": step.max_pages,
                                    "obey_robots": step.obey_robots}
            if step.step_id is not None:
                item["step_id"] = step.step_id
            if step.depends_on is not None:
                item["depends_on"] = step.depends_on
            payloads.append(item)
        return payloads

    @staticmethod
    def _id(value: str) -> str:
        return quote(value, safe="")

    def create(self, name: str, steps: list[WorkflowStep | Mapping[str, Any]], *,
               schedule_interval_minutes: int | None = None) -> dict[str, Any]:
        return self._client._request("POST", "workflows", json={
            "name": name, "steps": self._steps_payload(steps),
            "schedule_interval_minutes": schedule_interval_minutes,
        })

    def list(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._client._request("GET", "workflows", params={
            "page": page, "page_size": page_size})

    def get(self, workflow_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"workflows/{self._id(workflow_id)}")

    def update(self, workflow_id: str, *, name: str | None = None,
               steps: list[WorkflowStep | Mapping[str, Any]] | None = None,
               schedule_interval_minutes: int | None | _UnsetType = _UNSET) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if steps is not None:
            body["steps"] = self._steps_payload(steps)
        if schedule_interval_minutes is not _UNSET:
            body["schedule_interval_minutes"] = schedule_interval_minutes
        if not body:
            raise ValueError("provide at least one field to update")
        return self._client._request("PUT", f"workflows/{self._id(workflow_id)}", json=body)

    def delete(self, workflow_id: str) -> dict[str, Any]:
        return self._client._request("DELETE", f"workflows/{self._id(workflow_id)}")

    def set_schedule(self, workflow_id: str, interval_minutes: int | None) -> dict[str, Any]:
        return self._client._request("PATCH", f"workflows/{self._id(workflow_id)}/schedule",
                                     json={"interval_minutes": interval_minutes})

    def run(self, workflow_id: str) -> dict[str, Any]:
        return self._client._request("POST", f"workflows/{self._id(workflow_id)}/runs")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"workflows/runs/{self._id(run_id)}")

    def list_runs(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._client._request("GET", "workflows/runs", params={
            "page": page, "page_size": page_size})

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._client._request("POST", f"workflows/runs/{self._id(run_id)}/cancel")


class WebIntelligenceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 response: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class ResearchResource:
    """Research job methods exposed at ``client.research``."""
    def __init__(self, client: Client) -> None:
        self._client = client

    def create(self, topic: str, *, sources: int = 5,
               max_pages_per_source: int = 3,
               seeds: list[str] | None = None) -> dict[str, Any]:
        return self._client._request("POST", "research", json={
            "topic": topic, "sources": sources,
            "max_pages_per_source": max_pages_per_source,
            "seeds": seeds or [],
        })

    def get(self, research_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"research/{quote(research_id, safe='')}")

    def cancel(self, research_id: str) -> dict[str, Any]:
        """Request cooperative cancellation of a research job."""
        return self._client._request("POST",
                                     f"research/{quote(research_id, safe='')}/cancel")

    def list(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._client._request("GET", "research", params={
            "page": page, "page_size": page_size})


class Client:
    def __init__(self, api_key: str, *, base_url: str = "http://localhost:8000",
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1/",
                                  headers={"X-API-Key": api_key,
                                           "Accept": "application/json"},
                                  timeout=timeout, transport=transport)
        self.research = ResearchResource(self)
        self.workflows = WorkflowResource(self)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise WebIntelligenceError(f"API request failed: {type(exc).__name__}") from exc
        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text[:500]}
        if response.is_error:
            detail = response.reason_phrase
            if isinstance(payload, dict):
                error = payload.get("error")
                detail = (error.get("message") if isinstance(error, dict) else None) or payload.get("detail") or detail
            raise WebIntelligenceError(str(detail), status_code=response.status_code,
                                       response=payload)
        if not isinstance(payload, (dict, list)):
            raise WebIntelligenceError("API returned neither a JSON object nor array",
                                       status_code=response.status_code, response=payload)
        return payload

    def semantic_search(self, query: str, *, top_k: int | None = None,
                        filter: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run tenant-scoped semantic search over indexed documents."""
        body: dict[str, Any] = {"query": query}
        if top_k is not None:
            body["top_k"] = top_k
        if filter is not None:
            body["filter"] = filter
        return self._request("POST", "search", json=body)

    def get_search_index_levels(self) -> dict[str, Any]:
        """Read the available levels for the platform's own search index."""
        return self._request("GET", "search-index/levels")

    def get_search_index_stats(self) -> dict[str, Any]:
        """Read public build/availability metrics without internal filesystem paths."""
        return self._request("GET", "search-index/stats")

    def correct_search_index_query(self, query: str) -> dict[str, Any]:
        """Check spelling against the platform's own-index vocabulary."""
        return self._request("GET", "search-index/spell", params={"q": query})

    def list_billing_plans(self) -> dict[str, Any]:
        """Read public plan and pricing information."""
        return self._request("GET", "billing/plans")

    def get_billing_usage(self) -> dict[str, Any]:
        """Read the authenticated tenant's current-period usage summary."""
        return self._request("GET", "billing/usage")

    def get_billing_invoice_estimate(self) -> dict[str, Any]:
        """Read the API's current-period estimate, not a Stripe-issued invoice."""
        return self._request("GET", "billing/invoice")

    def search_marketplace_datasets(self, *, q: str = "", tag: str = "",
                                    max_price_cents: int | None = None,
                                    limit: int = 20) -> list[dict[str, Any]]:
        """Search published public marketplace listings; does not purchase."""
        return self._request("GET", "marketplace/datasets", params={
            "q": q, "tag": tag, "max_price_cents": max_price_cents, "limit": limit})

    def get_marketplace_dataset(self, dataset_id: str) -> dict[str, Any]:
        """Retrieve a published listing or one owned by this tenant."""
        return self._request("GET", f"marketplace/datasets/{quote(dataset_id, safe='')}")

    def get_marketplace_delivery(self, delivery_id: str) -> dict[str, Any]:
        """Retrieve a non-expired delivery manifest belonging to this tenant."""
        return self._request("GET", f"marketplace/deliveries/{quote(delivery_id, safe='')}")

    def list_marketplace_bounties(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List open public marketplace bounties; does not submit or award work."""
        return self._request("GET", "marketplace/bounties", params={"limit": limit})

    def store_provenance_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Store caller-supplied provenance metadata for this tenant."""
        return self._request("POST", "provenance/records", json=record)

    def get_provenance_record(self, record_id: str) -> dict[str, Any]:
        """Retrieve a tenant-owned provenance record."""
        return self._request("GET", f"provenance/records/{quote(record_id, safe='')}")

    def export_provenance_manifest(self, dataset_id: str, *,
                                   limit: int = 1000) -> dict[str, Any]:
        """Export a signed manifest; the API requires its signing key to be configured."""
        return self._request("POST", f"provenance/manifest?limit={limit}",
                             json={"dataset_id": dataset_id})

    def create_discovery(self, topic: str, *, count: int = 10,
                         seeds: list[str] | None = None, language: str = "en",
                         max_pages_per_site: int = 5,
                         per_domain: int = 1) -> dict[str, Any]:
        """Queue candidate discovery for manual review; does not start crawls.

        The API charges ``count`` requests from quota when the job is created.
        Crawling remains gated behind an explicit approval action.
        """
        return self._request("POST", "discovery", json={
            "topic": topic, "count": count, "seeds": seeds or [],
            "language": language, "auto": False,
            "max_pages_per_site": max_pages_per_site,
            "per_domain": per_domain, "obey_robots": True,
        })

    def list_discoveries(self, *, page: int = 1,
                         page_size: int = 20) -> dict[str, Any]:
        """List discovery jobs belonging to the authenticated tenant."""
        return self._request("GET", "discovery", params={
            "page": page, "page_size": page_size,
        })

    def get_discovery(self, job_id: str) -> dict[str, Any]:
        """Read status for one tenant-owned discovery job."""
        return self._request("GET", f"discovery/{quote(job_id, safe='')}")

    def discovery_candidates(self, job_id: str, *,
                             only_selected: bool = False) -> list[dict[str, Any]]:
        """Read ranked candidates for a tenant-owned discovery job."""
        params = {"only_selected": True} if only_selected else None
        return self._request("GET", f"discovery/{quote(job_id, safe='')}/candidates",
                             params=params)

    def cancel_discovery(self, job_id: str) -> dict[str, Any]:
        """Cancel a tenant-owned discovery job before approval."""
        return self._request("POST", f"discovery/{quote(job_id, safe='')}/cancel")

    def drop_discovery_candidate(self, job_id: str, domain: str) -> list[dict[str, Any]]:
        """Remove one domain from a tenant-owned, not-yet-approved shortlist."""
        return self._request("DELETE",
                             f"discovery/{quote(job_id, safe='')}/candidates/"
                             f"{quote(domain, safe='')}")

    def approve_discovery(self, job_id: str, domains: list[str]) -> dict[str, Any]:
        """Start crawls for explicitly selected candidate domains."""
        return self._request("POST", f"discovery/{quote(job_id, safe='')}/approve",
                             json={"domains": domains})

    def get_discovery_results(self, job_id: str) -> dict[str, Any]:
        """Read the per-site report for a tenant-owned discovery job."""
        return self._request("GET", f"discovery/{quote(job_id, safe='')}/results")

    def preview_discovery(self, topic: str, *, count: int | None = None,
                          seeds: list[str] | None = None, language: str | None = None,
                          per_domain: int | None = None) -> dict[str, Any]:
        """Preview ranked discovery candidates without creating a crawl job."""
        body: dict[str, Any] = {"topic": topic}
        if count is not None:
            body["count"] = count
        if seeds is not None:
            body["seeds"] = seeds
        if language is not None:
            body["language"] = language
        if per_domain is not None:
            body["per_domain"] = per_domain
        return self._request("POST", "discovery/preview", json=body)

    def list_graph(self, *, limit: int = 100) -> dict[str, Any]:
        """List the authenticated tenant's bounded knowledge graph."""
        return self._request("GET", "graph", params={"limit": limit})

    def upsert_graph_entity(self, name: str, *, entity_type: str = "Entity",
                            aliases: list[str] | None = None,
                            properties: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create or exactly resolve a tenant graph entity and merge its fields."""
        return self._request("POST", "graph/entities", json={
            "name": name, "entity_type": entity_type,
            "aliases": aliases or [], "properties": properties or {},
        })

    def get_graph_entity(self, entity_id: str) -> dict[str, Any]:
        """Retrieve a tenant graph entity and its relationships."""
        return self._request("GET", f"graph/entities/{quote(entity_id, safe='')}")

    def add_graph_relation(self, source_id: str, target_id: str, relation: str, *,
                           properties: dict[str, Any] | None = None) -> dict[str, Any]:
        """Connect two existing graph entities owned by the authenticated tenant."""
        return self._request("POST", "graph/relations", json={
            "source_id": source_id, "target_id": target_id,
            "relation": relation, "properties": properties or {},
        })

    def rag_query(self, query: str, *, top_k: int | None = None,
                  include_sources: bool | None = None) -> dict[str, Any]:
        """Synthesize a tenant-scoped answer from retrieved indexed content."""
        params: dict[str, Any] = {"q": query}
        if top_k is not None:
            params["top_k"] = top_k
        if include_sources is not None:
            params["include_sources"] = include_sources
        return self._request("GET", "search/rag", params=params)

    def ask(self, question: str, *, max_pages: int = 50, max_seconds: int = 60,
            top_k: int = 8) -> dict[str, Any]:
        """Retrieve an answer from indexed content (does not start a crawl)."""
        return self._request("POST", "ask", json={"question": question,
            "budget": {"max_pages": max_pages, "max_seconds": max_seconds},
            "top_k": top_k})

    def create_crawl(self, url: str, *, max_pages: int = 10,
                     obey_robots: bool = True) -> dict[str, Any]:
        return self._request("POST", "jobs/crawl", json={"url": url,
            "max_pages": max_pages, "obey_robots": obey_robots})

    def list_jobs(self, *, page: int = 1, page_size: int = 20,
                  state: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if state is not None:
            params["state"] = state
        return self._request("GET", "jobs", params=params)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"jobs/{quote(job_id, safe='')}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Request cooperative cancellation of a crawl job."""
        return self._request("POST", f"jobs/{quote(job_id, safe='')}/cancel")

    def list_watches(self) -> dict[str, Any]:
        """List page watches belonging to the authenticated tenant."""
        return self._request("GET", "watch")

    def create_watch(self, url: str, *, interval_minutes: int = 60) -> dict[str, Any]:
        return self._request("POST", "watch", json={"url": url,
                             "interval_minutes": interval_minutes})

    def run_watch(self, watch_id: str) -> dict[str, Any]:
        return self._request("POST", f"watch/{watch_id}/run")

    def watch_changes(self, watch_id: str, *, limit: int | None = None) -> dict[str, Any]:
        params = {"limit": limit} if limit is not None else None
        return self._request("GET", f"watch/{watch_id}/changes", params=params)

    def watch_snapshots(self, watch_id: str, *,
                        limit: int | None = None) -> dict[str, Any]:
        """List retained text snapshots, optionally limiting the count to 20."""
        params = {"limit": limit} if limit is not None else None
        return self._request("GET", f"watch/{watch_id}/snapshots", params=params)

    def create_webhook(self, url: str, events: list[WebhookEvent], *,
                       description: str = "") -> dict[str, Any]:
        """Register a webhook; the returned signing secret is shown only once.

        Store the returned secret securely. The SDK does not log or persist it.
        """
        return self._request("POST", "webhooks", json={
            "url": url, "events": events, "description": description,
        })

    def list_webhooks(self) -> list[dict[str, Any]]:
        """List webhook subscriptions for the authenticated tenant."""
        return self._request("GET", "webhooks")

    def delete_webhook(self, subscription_id: str) -> dict[str, Any]:
        """Delete a webhook subscription belonging to the authenticated tenant."""
        return self._request("DELETE", f"webhooks/{quote(subscription_id, safe='')}")

    def test_webhook(self, subscription_id: str, *,
                     event: WebhookEvent = "crawl.completed",
                     data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Queue a signed test delivery for one tenant-owned subscription."""
        body: dict[str, Any] = {"subscription_id": subscription_id, "event": event}
        if data is not None:
            body["data"] = data
        return self._request("POST", "webhooks/test", json=body)

    def list_webhook_deliveries(self, subscription_id: str, *,
                                limit: int = 50) -> list[dict[str, Any]]:
        """List recent delivery attempts (API limit is 1–200)."""
        return self._request("GET", f"webhooks/{quote(subscription_id, safe='')}/deliveries",
                             params={"limit": limit})

    def save_studio_query(self, name: str, q: str, *, limit: int = 25,
                          sources: list[str] | None = None,
                          group_by: str = "host", metric: str = "count") -> dict[str, Any]:
        """Save a bounded Data Studio query for the authenticated tenant."""
        return self._request("POST", "studio/saved-queries", json={
            "name": name, "q": q, "limit": limit, "sources": sources or [],
            "group_by": group_by, "metric": metric,
        })

    def list_studio_queries(self, *, page: int = 1,
                            page_size: int = 20) -> dict[str, Any]:
        """List saved Data Studio queries belonging to the authenticated tenant."""
        return self._request("GET", "studio/saved-queries", params={
            "page": page, "page_size": page_size,
        })

    def delete_studio_query(self, saved_id: str) -> dict[str, Any]:
        """Delete one saved Data Studio query belonging to the authenticated tenant."""
        return self._request("DELETE", f"studio/saved-queries/{quote(saved_id, safe='')}")

    def studio_analytics(self, *, group_by: str | None = None,
                         metric: str | None = None, source: str | None = None,
                         host: str | None = None, lang: str | None = None,
                         indexed_from: str | None = None, indexed_to: str | None = None,
                         limit: int | None = None, offset: int | None = None) -> dict[str, Any]:
        """Aggregate the shared indexed corpus using the bounded Studio analytics API."""
        params = {
            key: value for key, value in {
                "group_by": group_by, "metric": metric, "source": source,
                "host": host, "lang": lang, "indexed_from": indexed_from,
                "indexed_to": indexed_to, "limit": limit, "offset": offset,
            }.items() if value is not None
        }
        return self._request("GET", "studio/analytics", params=params)

    def create_studio_snapshot(self, name: str, q: str,
                               rows: list[dict[str, Any]], *,
                               sources: list[str] | None = None,
                               group_by: str = "host",
                               metric: str = "count") -> dict[str, Any]:
        """Persist a bounded result sample as a tenant-scoped Studio snapshot."""
        return self._request("POST", "studio/snapshots", json={
            "name": name, "q": q, "sources": sources or [],
            "group_by": group_by, "metric": metric, "rows": rows,
        })

    def list_studio_snapshots(self) -> dict[str, Any]:
        """List saved Studio result snapshots belonging to the authenticated tenant."""
        return self._request("GET", "studio/snapshots")

    def get_studio_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Retrieve one tenant-scoped Studio result snapshot."""
        return self._request("GET", f"studio/snapshots/{quote(snapshot_id, safe='')}")

    def delete_studio_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Delete one tenant-scoped Studio result snapshot."""
        return self._request("DELETE", f"studio/snapshots/{quote(snapshot_id, safe='')}")

    def delete_watch(self, watch_id: str) -> dict[str, Any]:
        """Stop a page watch; existing snapshots remain available."""
        return self._request("DELETE", f"watch/{quote(watch_id, safe='')}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
