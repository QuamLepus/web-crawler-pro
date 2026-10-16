"""Topic-driven website discovery.

This package adds the one half of "give me a topic and a number, go find the
best N websites and crawl them" that the platform never had: turning a *topic
string* into a ranked list of *websites*.

The crawl half already existed (:mod:`workers.crawl_tasks` + the cleaning /
dedupe pipeline), so this package stops at producing candidate URLs and hands
them to the existing machinery.

Pipeline
--------
    topic ──plan──> search queries ──search──> raw hits
          ──rank──> top-N *distinct domains* ──crawl──> dataset

Layers
------
``src.discovery.providers``
    Pluggable search backends behind one :meth:`SearchProvider.search`
    protocol. DuckDuckGo's HTML endpoint needs no API key; Brave / SerpAPI /
    Tavily activate automatically when a key is present, and CommonCrawl can
    be switched on for historical coverage.
``src.discovery.planner``
    Topic -> several search queries (LLM when configured, deterministic
    templates otherwise).
``src.discovery.ranker``
    Scores and de-duplicates hits, reusing the keyword scorer from
    :mod:`src.ai.agents.agent_crawler` and the C4/Gopher rules from
    :mod:`src.training.filters`.
``src.discovery.service``
    Orchestration + Redis-backed job records (``wcp:v1:discovery:*``).

Everything degrades: with no network, no API keys and no LLM the package still
runs end-to-end — it simply returns fewer candidates.
"""

from __future__ import annotations

from src.discovery.planner import QueryPlan, plan_queries
from src.discovery.providers import (
    BraveSearchProvider,
    CommonCrawlProvider,
    DuckDuckGoHTMLProvider,
    LLMSuggestionProvider,
    SearchHit,
    SearchProvider,
    SeedProvider,
    SerpAPIProvider,
    TavilyProvider,
    build_providers,
)
from src.discovery.ranker import Candidate, Ranker
from src.discovery.service import (
    DiscoveryRequest,
    DiscoveryService,
    DiscoveryState,
    run_discovery,
    run_discovery_and_crawl,
)

__all__ = [
    "BraveSearchProvider",
    "Candidate",
    "CommonCrawlProvider",
    "DiscoveryRequest",
    "DiscoveryService",
    "DiscoveryState",
    "DuckDuckGoHTMLProvider",
    "LLMSuggestionProvider",
    "QueryPlan",
    "Ranker",
    "SearchHit",
    "SearchProvider",
    "SeedProvider",
    "SerpAPIProvider",
    "TavilyProvider",
    "build_providers",
    "plan_queries",
    "run_discovery",
    "run_discovery_and_crawl",
]
