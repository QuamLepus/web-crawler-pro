"""MCP server — the platform as a tool other agents can call.

The cheapest distribution channel a data platform has is *being inside
someone else's loop*: Claude Desktop, Cursor, Zed, CrewAI and LangGraph all
speak the Model Context Protocol, and an MCP server listed once gets called
by every one of them, without a sales conversation.

This module implements the server with nothing but the standard library and
the platform's own services, so it runs anywhere the API runs:

    python -m src.mcp.server              # stdio transport (the usual one)

Tools exposed:

============================  ==========================================
``wcp_search``                query the own index (no third-party API)
``wcp_ask``                   cited answer with a hard budget
``wcp_plan_question``         show how a question gets decomposed
``wcp_domain_score``          public quality/authority score for a host
``wcp_watch``                 monitor a URL and return change events
``wcp_provenance``            the passport + signature for a dataset
``wcp_cost_estimate``         what a crawl of N pages will cost
``wcp_recipes``               list installed extraction recipes
============================  ==========================================

Protocol notes: JSON-RPC 2.0, ``initialize`` → ``tools/list`` → ``tools/call``,
contents returned as MCP ``text`` blocks. Errors are returned as JSON-RPC
errors *and* as readable text, because "the agent got a stack trace" is the
fastest way to be uninstalled.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Iterable

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "web-intelligence-os"
SERVER_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# tool registry
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    def descriptor(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


def _schema(**props: Any) -> dict[str, Any]:
    required = [k for k, v in props.items() if v.pop("_required", False)] \
        if False else []
    return {"type": "object", "properties": props, "required": required}


def build_tools(services: Any) -> list[Tool]:
    """Wire the platform services into MCP tools.

    ``services`` is the API's service container (:class:`ApiServices`) or any
    object exposing the same attributes. Every handler is defensive: an MCP
    call must never take the host application down.
    """

    def tool_search(args: dict) -> Any:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        limit = int(args.get("limit", 10))
        return services.own_search(query, limit=limit)  # provided by the container

    def tool_ask(args: dict) -> Any:
        question = str(args.get("question", "")).strip()
        if not question:
            raise ValueError("question is required")
        budget = {
            "max_pages": int(args.get("max_pages", 15)),
            "max_seconds": float(args.get("max_seconds", 30)),
            "max_usd": float(args.get("max_usd", 0.25)),
        }
        return services.ask(question, budget=budget,
                            mode=str(args.get("mode", "auto")))

    def tool_plan(args: dict) -> Any:
        return services.ask_plan(str(args.get("question", "")))

    def tool_domain(args: dict) -> Any:
        host = str(args.get("host", "")).strip()
        if not host:
            raise ValueError("host is required")
        return services.domain_score(host)

    def tool_watch(args: dict) -> Any:
        url = str(args.get("url", "")).strip()
        if not url:
            raise ValueError("url is required")
        return services.watch_once(url, kinds=args.get("kinds"))

    def tool_provenance(args: dict) -> Any:
        return services.provenance_manifest(
            str(args.get("dataset", "ask")),
            version=str(args.get("version", "")) or None)

    def tool_cost(args: dict) -> Any:
        return services.cost_estimate(args.get("spec") or
                                      {"pages": int(args.get("pages", 10_000))})

    def tool_recipes(args: dict) -> Any:
        return services.recipes_list()

    return [
        Tool("wcp_search",
             "Search the platform's own web index (BM25 + authority, no third-party "
             "search API, works offline). Returns ranked results with urls, titles, "
             "snippets and scores.",
             {"query": {"type": "string", "description": "search query"},
              "limit": {"type": "integer", "description": "max results (1-50)",
                        "default": 10}},
             tool_search),
        Tool("wcp_ask",
             "Ask a question and get a cited answer with a confidence score and the "
             "exact cost in USD. Enforces a page/time/dollar budget. Every claim "
             "carries a citation; single-source claims are flagged.",
             {"question": {"type": "string", "description": "the question"},
              "max_pages": {"type": "integer", "default": 15},
              "max_seconds": {"type": "number", "default": 30},
              "max_usd": {"type": "number", "default": 0.25},
              "mode": {"type": "string", "enum": ["auto", "corpus", "web", "index"],
                       "default": "auto"}},
             tool_ask),
        Tool("wcp_plan_question",
             "Show how the platform decomposes a research question into search "
             "queries and a page budget, without spending anything.",
             {"question": {"type": "string"}},
             tool_plan),
        Tool("wcp_domain_score",
             "Public quality score for a domain (authority, index coverage, freshness, "
             "licence clarity) computed from the platform's own index.",
             {"host": {"type": "string", "description": "example.com"}},
             tool_domain),
        Tool("wcp_watch",
             "Fetch one URL now, diff it against the last snapshot and return typed "
             "change events (price_change, new_product, policy_change, announcement, "
             "jobs_change, content_update, cosmetic).",
             {"url": {"type": "string"},
              "kinds": {"type": "array", "items": {"type": "string"}}},
             tool_watch),
        Tool("wcp_provenance",
             "Signed provenance manifest for a dataset version: record count, licence "
             "mix, robots outcomes, Merkle root and HMAC signature.",
             {"dataset": {"type": "string"},
              "version": {"type": "string"}},
             tool_provenance),
        Tool("wcp_cost_estimate",
             "Estimate the cost and wall-clock time of a crawl before running it.",
             {"pages": {"type": "integer", "default": 10000},
              "spec": {"type": "object",
                       "description": "full CrawlSpec override (render_share, "
                                      "proxy_tier, extract_llm_share, ...)"}},
             tool_cost),
        Tool("wcp_recipes",
             "List installed extraction recipes (site-specific extractors with test "
             "checksums).",
             {},
             tool_recipes),
    ]


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class McpServer:
    """Transport-agnostic JSON-RPC 2.0 handler for MCP."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self.tools = {t.name: t for t in tools}

    # -- protocol ------------------------------------------------------------ #
    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        rid = request.get("id")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False},
                                     "resources": {}, "prompts": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Web Intelligence OS: search a self-hosted web index, ask "
                        "budgeted questions with citations, monitor pages for real "
                        "change events, estimate crawl cost and export signed "
                        "provenance manifests."
                    ),
                }
            elif method in ("notifications/initialized", "initialized"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t.descriptor() for t in self.tools.values()]}
            elif method == "tools/call":
                name = params.get("name", "")
                args = params.get("arguments") or {}
                tool = self.tools.get(name)
                if tool is None:
                    raise KeyError(f"unknown tool {name!r}")
                payload = tool.handler(args)
                result = {
                    "content": [{"type": "text", "text": _render(payload)}],
                    "isError": False,
                }
            elif method in ("resources/list", "prompts/list"):
                result = {"resources": []} if method.startswith("resources") \
                    else {"prompts": []}
            else:
                return _error(rid, -32601, f"method not found: {method}")
        except KeyError as exc:
            return _error(rid, -32602, str(exc))
        except ValueError as exc:
            return _error(rid, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(rid, -32603, f"{type(exc).__name__}: {exc}",
                          data={"traceback": traceback.format_exc()[-1500:]})
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    # -- stdio transport ----------------------------------------------------- #
    def serve_stdio(self, stdin=None, stdout=None) -> None:  # pragma: no cover
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
                stdout.flush()
                continue
            response = self.handle(request)
            if response is not None:
                stdout.write(json.dumps(response, default=str) + "\n")
                stdout.flush()


def _render(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    return json.dumps(payload, indent=2, default=str)


def _error(rid: Any, code: int, message: str, data: dict | None = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI glue
    """Entry point: ``python -m src.mcp.server``."""
    from src.mcp.container import container_from_env

    services = container_from_env()
    server = McpServer(build_tools(services))
    server.serve_stdio()
    return 0
