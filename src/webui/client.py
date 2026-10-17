"""A tiny server-side client for our own API.

Every console page is rendered by the server, which calls the public API
over HTTP with the caller's own JWT. That is a real network hop per page —
deliberately. It means the UI can never drift from the documented API
because it *is* a client of it, the same way a customer's code is.

Failures never raise: each method returns a :class:`Result` so a page can
render a friendly empty state ("the scheduler is unreachable") instead of a
500.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Result:
    """Either the parsed JSON body, or why we could not get one."""

    ok: bool
    data: Any = None
    error: str = ""
    status: int = 200

    @property
    def failed(self) -> bool:
        return not self.ok

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default

    def __getitem__(self, key: str) -> Any:
        if isinstance(self.data, dict):
            return self.data[key]
        raise KeyError(key)


@dataclass
class Api:
    """Thin, typed-ish wrapper over the endpoints the console needs."""

    base_url: str
    token: str = ""
    timeout: float = 20.0
    app: Any = field(default=None, repr=False)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    # -- plumbing ---------------------------------------------------------- #
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _new_client(self) -> httpx.AsyncClient:
        """Build a client for one call.

        When the console is mounted *on* the API (the normal case) the calls
        never leave the process: we hand httpx an ASGI transport pointed at
        the same application. No socket, no loopback round trip, no chance of
        the app deadlocking on itself under a single-worker test client.
        Pointing the console at a remote API is still one argument away.
        """
        if self.app is not None:
            from httpx import ASGITransport

            transport = ASGITransport(app=self.app, raise_app_exceptions=False)
            return httpx.AsyncClient(
                transport=transport,
                base_url=self.base_url,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, follow_redirects=True
        )

    async def _call(self, method: str, path: str, **kw) -> Result:
        try:
            async with self._new_client() as client:
                res = await client.request(method, path, headers=self._headers, **kw)
        except httpx.TimeoutException:
            return Result(False, error="the API did not answer in time")
        except Exception as exc:  # noqa: BLE001 - a page must still render
            return Result(False, error=f"the API is unreachable ({exc})")

        if res.status_code == 401:
            return Result(False, error="session expired", status=401)
        try:
            body = res.json()
        except ValueError:
            body = res.text
        if res.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            return Result(
                False,
                error=detail or f"HTTP {res.status_code}",
                status=res.status_code,
                data=body,
            )
        return Result(True, data=body, status=res.status_code)

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._call("GET", path, params=params or {})

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._call("POST", path, json=body or {})

    def patch(self, path: str, body: dict | None = None) -> Any:
        return self._call("PATCH", path, json=body or {})

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path)

    # -- auth -------------------------------------------------------------- #
    @staticmethod
    def anonymous(base_url: str, app: Any = None) -> Api:
        return Api(base_url=base_url, app=app)

    def login(self, email: str, password: str) -> Any:
        return self.post("/api/auth/login", {"email": email, "password": password})

    def signup(self, org_name: str, email: str, password: str) -> Any:
        return self.post(
            "/api/auth/signup",
            {"org_name": org_name, "email": email, "password": password},
        )

    def me(self) -> Any:
        return self.get("/api/auth/me")

    # -- jobs -------------------------------------------------------------- #
    def jobs(self, limit: int = 25) -> Any:
        return self.get("/api/v1/jobs", {"limit": limit})

    def job(self, job_id: str) -> Any:
        return self.get(f"/api/v1/jobs/{job_id}")

    def crawl(self, url: str, max_pages: int = 10, **kw) -> Any:
        return self.post(
            "/api/v1/jobs/crawl", {"url": url, "max_pages": max_pages, **kw}
        )

    def cancel_job(self, job_id: str) -> Any:
        return self.post(f"/api/v1/jobs/{job_id}/cancel")

    # -- knowledge graph ---------------------------------------------------- #
    def graph(self, limit: int = 200) -> Any:
        return self.get("/api/v1/graph", {"limit": limit})

    def graph_entity(self, **body) -> Any:
        return self.post("/api/v1/graph/entities", body)

    def graph_relation(self, **body) -> Any:
        return self.post("/api/v1/graph/relations", body)

    # -- research ----------------------------------------------------------- #
    def research_create(self, **body) -> Any:
        return self.post("/api/v1/research", body)

    def research_list(self) -> Any:
        return self.get("/api/v1/research")

    def research(self, research_id: str) -> Any:
        return self.get(f"/api/v1/research/{research_id}")

    # -- monitoring -------------------------------------------------------- #
    def watches(self) -> Any:
        return self.get("/api/v1/watch")

    def watch_create(self, url: str, interval_minutes: int = 60) -> Any:
        return self.post("/api/v1/watch", {"url": url, "interval_minutes": interval_minutes})

    def watch_run(self, watch_id: str) -> Any:
        return self.post(f"/api/v1/watch/{watch_id}/run")

    def watch_changes(self, watch_id: str) -> Any:
        return self.get(f"/api/v1/watch/{watch_id}/changes")

    def watch_delete(self, watch_id: str) -> Any:
        return self.delete(f"/api/v1/watch/{watch_id}")

    # -- discovery --------------------------------------------------------- #
    def discovery_preview(self, topic: str, count: int = 10, **kw) -> Any:
        return self.post(
            "/api/v1/discovery/preview", {"topic": topic, "count": count, **kw}
        )

    def discovery_create(self, topic: str, count: int = 10, **kw) -> Any:
        return self.post("/api/v1/discovery", {"topic": topic, "count": count, **kw})

    def discovery_list(self) -> Any:
        return self.get("/api/v1/discovery")

    def discovery(self, job_id: str) -> Any:
        return self.get(f"/api/v1/discovery/{job_id}")

    def discovery_candidates(self, job_id: str) -> Any:
        return self.get(f"/api/v1/discovery/{job_id}/candidates")

    def discovery_results(self, job_id: str) -> Any:
        return self.get(f"/api/v1/discovery/{job_id}/results")

    def discovery_approve(self, job_id: str, domains: list[str] | None = None) -> Any:
        body: dict = {}
        if domains:
            body["domains"] = domains
        return self.post(f"/api/v1/discovery/{job_id}/approve", body)

    def discovery_reject(self, job_id: str, domain: str) -> Any:
        return self.delete(f"/api/v1/discovery/{job_id}/candidates/{domain}")

    def discovery_cancel(self, job_id: str) -> Any:
        return self.post(f"/api/v1/discovery/{job_id}/cancel")

    # -- grounded intelligence --------------------------------------------- #
    def ask(self, question: str, max_pages: int = 50, max_seconds: int = 60) -> Any:
        return self.post("/api/v1/ask", {"question": question,
            "budget": {"max_pages": max_pages, "max_seconds": max_seconds}})

    # -- search ------------------------------------------------------------ #
    def vector_search(self, query: str, top_k: int = 10) -> Any:
        return self.post("/api/v1/search", {"query": query, "top_k": top_k})

    def index_query(self, q: str, limit: int = 10, **kw) -> Any:
        params: dict = {"q": q, "limit": limit}
        params.update({k: v for k, v in kw.items() if v not in (None, "")})
        return self.get("/api/v1/search-index/query", params)

    def index_stats(self) -> Any:
        return self.get("/api/v1/search-index/stats")

    def index_levels(self) -> Any:
        return self.get("/api/v1/search-index/levels")

    def index_spell(self, q: str) -> Any:
        return self.get("/api/v1/search-index/spell", {"q": q})

    # -- marketplace ------------------------------------------------------- #
    def datasets(self, tag: str = "", q: str = "") -> Any:
        return self.get("/api/v1/marketplace/datasets", {"tag": tag, "q": q})

    def dataset(self, dataset_id: str) -> Any:
        return self.get(f"/api/v1/marketplace/datasets/{dataset_id}")

    def dataset_create(self, **body) -> Any:
        return self.post("/api/v1/marketplace/datasets", body)

    def dataset_purchase(self, dataset_id: str) -> Any:
        return self.post(f"/api/v1/marketplace/datasets/{dataset_id}/purchase")

    def bounties(self) -> Any:
        return self.get("/api/v1/marketplace/bounties")

    def bounty_create(self, **body) -> Any:
        return self.post("/api/v1/marketplace/bounties", body)

    # -- billing / keys / webhooks ----------------------------------------- #
    def plans(self) -> Any:
        return self.get("/api/v1/billing/plans")

    def usage(self) -> Any:
        return self.get("/api/v1/billing/usage")

    def invoices(self) -> Any:
        return self.get("/api/v1/billing/invoice")

    def keys(self) -> Any:
        return self.get("/keys")

    def create_key(self, name: str, scopes: list[str]) -> Any:
        return self.post("/api/auth/api-keys", {"name": name, "scopes": scopes})

    def webhooks(self) -> Any:
        return self.get("/api/v1/webhooks")

    def webhook_create(self, url: str, events: list[str], description: str = "") -> Any:
        return self.post(
            "/api/v1/webhooks",
            {"url": url, "events": events, "description": description},
        )

    def webhook_delete(self, sub_id: str) -> Any:
        return self.delete(f"/api/v1/webhooks/{sub_id}")

    def webhook_test(self, sub_id: str) -> Any:
        return self.post(f"/api/v1/webhooks/{sub_id}/test")

    # -- ops --------------------------------------------------------------- #
    def health(self) -> Any:
        return self.get("/healthz")
