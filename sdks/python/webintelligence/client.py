"""Synchronous HTTPX client; no API key is logged or included in exceptions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


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
        return self._client._request("GET", f"research/{research_id}")

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

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise WebIntelligenceError(f"API request failed: {type(exc).__name__}") from exc
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
        if not isinstance(payload, dict):
            raise WebIntelligenceError("API returned a non-object JSON response",
                                       status_code=response.status_code, response=payload)
        return payload

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

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"jobs/{job_id}")

    def create_watch(self, url: str, *, interval_minutes: int = 60) -> dict[str, Any]:
        return self._request("POST", "watch", json={"url": url,
                             "interval_minutes": interval_minutes})

    def run_watch(self, watch_id: str) -> dict[str, Any]:
        return self._request("POST", f"watch/{watch_id}/run")

    def watch_changes(self, watch_id: str) -> dict[str, Any]:
        return self._request("GET", f"watch/{watch_id}/changes")

    def watch_snapshots(self, watch_id: str) -> dict[str, Any]:
        """List the latest retained text snapshots for a monitored page."""
        return self._request("GET", f"watch/{watch_id}/snapshots")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
