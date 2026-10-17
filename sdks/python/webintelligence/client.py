"""Synchronous HTTPX client; no API key is logged or included in exceptions."""
from __future__ import annotations

from typing import Any
import httpx


class WebIntelligenceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 response: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class Client:
    def __init__(self, api_key: str, *, base_url: str = "http://localhost:8000",
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1/",
                                  headers={"X-API-Key": api_key,
                                           "Accept": "application/json"},
                                  timeout=timeout, transport=transport)

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

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
