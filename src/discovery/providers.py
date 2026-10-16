"""Search backends that turn a *query string* into candidate URLs.

Every provider implements the same tiny protocol::

    provider.search(query, limit=10) -> list[SearchHit]

so :mod:`src.discovery.service` can fan one query out to several of them and
let :mod:`src.discovery.ranker` reward agreement between independent sources.

Cost & legality notes
---------------------
* :class:`DuckDuckGoHTMLProvider` — free, no API key. It reads the public
  ``html.duckduckgo.com/html/`` endpoint. Fine for personal / low-volume use;
  for production traffic use a licensed API (Brave / SerpAPI / Tavily), which
  this module enables automatically when their key is present.
* :class:`CommonCrawlProvider` — free, no key, but it matches a *URL regex*
  rather than keywords, so it is off by default.
* :class:`SeedProvider` — the user's own seed URLs, optionally expanded through
  ``/sitemap.xml``.
* :class:`LLMSuggestionProvider` — asks a model for well-known sites on the
  topic. Off by default (``DISCOVERY_USE_LLM=true``).
* :class:`~src.search_index.provider.OwnIndexProvider` — **the platform's own
  index** (``src.search_index``). Built from Curlie + Open PageRank and
  optionally Wikipedia / Common Crawl, so it needs no API key, has no rate
  limit, and works offline. It is registered automatically as soon as an index
  exists under ``SEARCH_INDEX_DIR`` (``DISCOVERY_USE_OWN_INDEX=false`` to opt
  out, ``DISCOVERY_PROVIDERS=own`` to force it and use nothing else).

``WCP_OFFLINE=1`` (set by the test suite) makes every network provider return
an empty list instead of dialling out.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

Fetcher = Callable[..., bytes]


def offline() -> bool:
    """True when the process must not open outbound sockets (tests / CI)."""
    return os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes")


def _requests_get(url: str, *, timeout: int = 20, headers: dict | None = None,
                  data: dict | None = None, params: dict | None = None,
                  json_body: dict | None = None) -> bytes:  # pragma: no cover
    """Single ``requests`` shim shared by every provider.

    Also doubles as the injectable ``fetcher`` signature: a test double only
    has to accept these keyword arguments.
    """
    import requests

    kwargs: dict = {"headers": headers or {}, "timeout": timeout}
    if params:
        kwargs["params"] = params
    if json_body is not None:
        resp = requests.post(url, json=json_body, **kwargs)
    elif data is not None:
        resp = requests.post(url, data=data, **kwargs)
    else:
        resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    return resp.content


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchHit:
    """One search result, normalised across providers."""

    url: str
    title: str = ""
    snippet: str = ""
    provider: str = ""
    rank: int = 0            # 1-based position inside that provider's results
    query: str = ""

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "snippet": self.snippet,
                "provider": self.provider, "rank": self.rank,
                "query": self.query}


class SearchProvider(Protocol):
    """Minimal contract every discovery backend implements."""

    name: str

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        ...


def _clean_url(raw: str) -> str:
    """Normalise a provider URL; unwrap DuckDuckGo's redirect hop."""
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    if parsed.scheme not in ("http", "https"):
        return ""
    # drop tracking junk, keep the page
    return url.split("#", 1)[0]


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


# --------------------------------------------------------------------------- #
# DuckDuckGo — free, no API key
# --------------------------------------------------------------------------- #
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"


class DuckDuckGoHTMLProvider:
    """Scrape DuckDuckGo's no-JS HTML endpoint (no key, no cost)."""

    name = "duckduckgo"

    def __init__(self, fetcher: Fetcher | None = None, *, timeout: int = 20,
                 user_agent: str | None = None) -> None:
        self._fetch = fetcher or _requests_get
        self.timeout = timeout
        self.user_agent = user_agent or (
            "Mozilla/5.0 (compatible; WebCrawlerPro/2.1; +topic-discovery)")

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if offline():
            logger.debug("WCP_OFFLINE set — %s disabled", self.name)
            return []
        try:
            raw = self._fetch(
                DDG_ENDPOINT, timeout=self.timeout,
                headers={"User-Agent": self.user_agent,
                         "Accept-Language": "en-US,en;q=0.9"},
                data={"q": query, "kl": "us-en"})
        except Exception as exc:  # noqa: BLE001 - one dead provider is fine
            logger.warning("duckduckgo search failed for %r: %s", query, exc)
            return []
        return self._parse(raw, query, limit)

    # -- parsing kept separate so tests can exercise it without network ----- #
    def _parse(self, raw: bytes | str, query: str = "",
               limit: int = 10) -> list[SearchHit]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover - bs4 is a hard dependency
            logger.warning("beautifulsoup4 missing; cannot parse DDG results")
            return []
        html = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for anchor in soup.select("a.result__a"):
            url = _clean_url(anchor.get("href", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            snippet_node = anchor.find_parent(class_="result")
            snippet = ""
            if snippet_node is not None:
                sn = snippet_node.select_one(".result__snippet")
                if sn is not None:
                    snippet = _text(sn)
            hits.append(SearchHit(url=url, title=_text(anchor),
                                  snippet=snippet, provider=self.name,
                                  rank=len(hits) + 1, query=query))
            if len(hits) >= limit:
                break
        if not hits:  # layout change fallback: any redirect link
            for anchor in soup.find_all("a", href=True):
                url = _clean_url(anchor["href"])
                if not url or url in seen or "duckduckgo.com" in urlparse(url).netloc:
                    continue
                seen.add(url)
                hits.append(SearchHit(url=url, title=_text(anchor),
                                      provider=self.name, rank=len(hits) + 1,
                                      query=query))
                if len(hits) >= limit:
                    break
        return hits


# --------------------------------------------------------------------------- #
# Keyed JSON APIs — enabled automatically when a key is present
# --------------------------------------------------------------------------- #
class _JsonApiProvider:
    """Shared plumbing for the JSON search APIs (GET or POST + api key)."""

    name = "json"
    endpoint = ""
    env_key = ""

    def __init__(self, api_key: str | None = None,
                 fetcher: Fetcher | None = None, *, timeout: int = 20) -> None:
        self.api_key = api_key if api_key is not None else os.getenv(self.env_key, "")
        self._fetch = fetcher or _requests_get
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not offline()

    def _request(self, query: str, limit: int) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, payload: dict, query: str, limit: int) -> list[SearchHit]:
        raise NotImplementedError

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if not self.enabled:
            return []
        try:
            raw = self._request(query, limit)
            payload = json.loads(
                raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s search failed for %r: %s", self.name, query, exc)
            return []
        return self._parse(payload, query, limit)


class BraveSearchProvider(_JsonApiProvider):
    """Brave Search API — free tier available, ``BRAVE_API_KEY``."""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    env_key = "BRAVE_API_KEY"

    def _request(self, query: str, limit: int):  # pragma: no cover - net
        return self._fetch(
            self.endpoint, timeout=self.timeout,
            headers={"X-Subscription-Token": self.api_key,
                     "Accept": "application/json"},
            params={"q": query, "count": min(limit, 20)})

    def _parse(self, payload: dict, query: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        for i, r in enumerate(payload.get("web", {}).get("results", [])[:limit], 1):
            url = _clean_url(r.get("url", ""))
            if url:
                out.append(SearchHit(url=url, title=r.get("title", ""),
                                     snippet=r.get("description", ""),
                                     provider=self.name, rank=i, query=query))
        return out


class SerpAPIProvider(_JsonApiProvider):
    """SerpAPI (Google/Bing results as JSON) — ``SERPAPI_KEY``."""

    name = "serpapi"
    endpoint = "https://serpapi.com/search.json"
    env_key = "SERPAPI_KEY"

    def _request(self, query: str, limit: int):  # pragma: no cover - net
        return self._fetch(
            self.endpoint, timeout=self.timeout,
            params={"q": query, "num": min(limit, 20),
                    "api_key": self.api_key})

    def _parse(self, payload: dict, query: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        for i, r in enumerate(payload.get("organic_results", [])[:limit], 1):
            url = _clean_url(r.get("link", ""))
            if url:
                out.append(SearchHit(url=url, title=r.get("title", ""),
                                     snippet=r.get("snippet", ""),
                                     provider=self.name, rank=i, query=query))
        return out


class TavilyProvider(_JsonApiProvider):
    """Tavily Search API (built for LLM agents) — ``TAVILY_API_KEY``."""

    name = "tavily"
    endpoint = "https://api.tavily.com/search"
    env_key = "TAVILY_API_KEY"

    def _request(self, query: str, limit: int):  # pragma: no cover - net
        return self._fetch(
            self.endpoint, timeout=self.timeout,
            json_body={"api_key": self.api_key, "query": query,
                       "max_results": min(limit, 20),
                       "search_depth": "basic"})

    def _parse(self, payload: dict, query: str, limit: int) -> list[SearchHit]:
        out: list[SearchHit] = []
        for i, r in enumerate(payload.get("results", [])[:limit], 1):
            url = _clean_url(r.get("url", ""))
            if url:
                out.append(SearchHit(url=url, title=r.get("title", ""),
                                     snippet=r.get("content", ""),
                                     provider=self.name, rank=i, query=query))
        return out


# --------------------------------------------------------------------------- #
# CommonCrawl — free, historical, URL-regex based
# --------------------------------------------------------------------------- #
class CommonCrawlProvider:
    """Find pages whose *URL* matches the topic keywords in the CC index."""

    name = "commoncrawl"

    def __init__(self, fetcher: Fetcher | None = None, *,
                 max_hits: int = 100) -> None:
        self._fetch = fetcher
        self.max_hits = max_hits

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if offline():
            return []
        from src.ingestion.commoncrawl import query_cc_index

        pattern = self._url_regex(query)
        if not pattern:
            return []
        try:
            hits = query_cc_index(pattern, max_hits=min(limit, self.max_hits),
                                  fetcher=self._fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("commoncrawl discovery failed for %r: %s", query, exc)
            return []
        return [SearchHit(url=h.url, title="", snippet="", provider=self.name,
                          rank=i, query=query)
                for i, h in enumerate(hits, 1)]

    @staticmethod
    def _url_regex(query: str) -> str:
        """``"best budget laptops"`` -> ``.*(budget|laptops).*``."""
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        if not words:
            return ""
        return ".*(" + "|".join(dict.fromkeys(words[:6])) + ").*"


# --------------------------------------------------------------------------- #
# User seeds (+ optional sitemap expansion)
# --------------------------------------------------------------------------- #
class SeedProvider:
    """The caller's own seed URLs, optionally expanded via ``/sitemap.xml``.

    Seeds always outrank search results (they are an explicit instruction), so
    :class:`~src.discovery.ranker.Ranker` marks them ``seeded=True``.
    """

    name = "seed"

    def __init__(self, seeds: Sequence[str] = (), *, expand: bool = True,
                 fetcher: Fetcher | None = None, max_per_seed: int = 25,
                 timeout: int = 20) -> None:
        self.seeds = [s for s in seeds if s]
        self.expand = expand
        self._fetch = fetcher or _requests_get
        self.max_per_seed = max_per_seed
        self.timeout = timeout

    def search(self, query: str = "", limit: int = 10) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for seed in self.seeds[:limit]:
            url = _clean_url(seed)
            if not url:
                continue
            hits.append(SearchHit(url=url, title="", snippet="",
                                  provider=self.name, rank=len(hits) + 1,
                                  query=query))
            if self.expand:
                hits.extend(self._from_sitemap(url, query, start=len(hits)))
        return hits[:max(limit * 5, limit)]

    def _from_sitemap(self, seed: str, query: str,
                      start: int = 0) -> list[SearchHit]:
        if offline():
            return []
        sitemap = urljoin(seed if seed.endswith("/") else seed + "/",
                          "sitemap.xml")
        try:
            raw = self._fetch(sitemap, timeout=self.timeout)
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            from src.ingestion.registry import parse_sitemap

            leaves, _children = parse_sitemap(text, sitemap)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sitemap probe failed for %s: %s", seed, exc)
            return []
        return [SearchHit(url=_clean_url(u), title="", snippet="",
                          provider=self.name, rank=start + i + 1, query=query)
                for i, u in enumerate(leaves[:self.max_per_seed])]


# --------------------------------------------------------------------------- #
# LLM knowledge — optional, off by default
# --------------------------------------------------------------------------- #
LLM_SUGGEST_PROMPT = """You are helping a web crawler pick sources.

Topic: {topic}

List the {n} websites that are genuinely the best, most authoritative and most
up-to-date sources for this topic. Prefer primary/official sources and
established publications over SEO blogs and aggregators.

Reply with EXACTLY one JSON array of objects, no prose:
[{{"url": "https://...", "title": "...", "why": "one short sentence"}}]
"""


class LLMSuggestionProvider:
    """Ask a chat model which sites matter for the topic (model knowledge).

    Enabled with ``DISCOVERY_USE_LLM=true``; silently inert when LangChain or
    an API key is missing, so discovery never hard-fails on it.
    """

    name = "llm"

    def __init__(self, llm=None, model_name: str | None = None) -> None:
        self._llm = llm
        self.model_name = model_name or os.getenv("LLM_MODEL", "gpt-4o-mini")

    @property
    def enabled(self) -> bool:
        if self._llm is not None:
            return True
        return os.getenv("DISCOVERY_USE_LLM", "").lower() == "true" and bool(
            os.getenv("OPENAI_API_KEY"))

    def _get_llm(self):  # pragma: no cover - needs an API key
        if self._llm is not None:
            return self._llm
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=self.model_name, temperature=0)

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if not self.enabled:
            return []
        try:
            llm = self._get_llm()
            raw = llm.invoke(LLM_SUGGEST_PROMPT.format(topic=query, n=limit))
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm suggestion failed for %r: %s", query, exc)
            return []
        content = getattr(raw, "content", raw)
        return self._parse(content, query, limit)

    @staticmethod
    def _parse(content, query: str, limit: int) -> list[SearchHit]:
        from src.ai.agents.agent_crawler import _first_json_object

        if isinstance(content, list):   # some providers return content blocks
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content)
        text = str(content)
        start, end = text.find("["), text.rfind("]")
        snippet = _first_json_object(text) if start == -1 else text[start:end + 1]
        if start != -1:
            try:
                items = json.loads(snippet)
            except Exception:  # noqa: BLE001
                items = []
        else:
            try:
                items = [json.loads(snippet)]
            except Exception:  # noqa: BLE001
                items = []
        out: list[SearchHit] = []
        for i, item in enumerate(items[:limit], 1):
            if not isinstance(item, dict):
                continue
            url = _clean_url(str(item.get("url", "")))
            if url:
                out.append(SearchHit(url=url, title=str(item.get("title", "")),
                                     snippet=str(item.get("why", "")),
                                     provider=LLMSuggestionProvider.name,
                                     rank=i, query=query))
        return out


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def build_providers(*, names: Iterable[str] | None = None, seeds: Sequence[str] = (),
                    fetcher: Fetcher | None = None, llm=None,
                    timeout: int | None = None) -> list[SearchProvider]:
    """Instantiate the configured providers.

    ``names`` comes from ``Config.DISCOVERY_PROVIDERS`` (``"duckduckgo"`` by
    default). Keyed providers are added on top whenever their credential is
    present in the environment — no configuration needed beyond the key.
    """
    from src.config import get_config

    cfg = get_config()
    timeout = timeout or cfg.DISCOVERY_TIMEOUT
    wanted = [n.lower() for n in (names if names is not None
                                  else cfg.DISCOVERY_PROVIDERS)]
    providers: list[SearchProvider] = []

    # Seeds first: they are an explicit instruction and must never be dropped.
    if seeds:
        providers.append(SeedProvider(seeds, fetcher=fetcher, timeout=timeout))

    # Our own index goes *first* when it exists: it is free, unlimited and
    # offline, so there is no reason to spend a third-party query we can avoid.
    own_requested = any(w in wanted for w in ("own", "ownindex", "own_index", "local"))
    if own_requested or cfg.DISCOVERY_USE_OWN_INDEX:
        from src.search_index.provider import OwnIndexProvider

        own = OwnIndexProvider()
        if own_requested or own.available:
            providers.append(own)

    if "duckduckgo" in wanted or "ddg" in wanted:
        providers.append(DuckDuckGoHTMLProvider(fetcher=fetcher, timeout=timeout))
    if "brave" in wanted or cfg.BRAVE_API_KEY:
        providers.append(BraveSearchProvider(fetcher=fetcher, timeout=timeout))
    if "serpapi" in wanted or cfg.SERPAPI_KEY:
        providers.append(SerpAPIProvider(fetcher=fetcher, timeout=timeout))
    if "tavily" in wanted or cfg.TAVILY_API_KEY:
        providers.append(TavilyProvider(fetcher=fetcher, timeout=timeout))
    if "commoncrawl" in wanted or cfg.DISCOVERY_USE_COMMONCRAWL:
        providers.append(CommonCrawlProvider(fetcher=fetcher))

    llm_provider = LLMSuggestionProvider(llm=llm)
    if "llm" in wanted or cfg.DISCOVERY_USE_LLM:
        providers.append(llm_provider)

    return providers


def collect_hits(providers: Sequence[SearchProvider], queries: Sequence[str],
                 limit: int = 10) -> list[SearchHit]:
    """Fan every query out to every provider; never raises on a dead backend."""
    hits: list[SearchHit] = []
    for provider in providers:
        for query in queries:
            try:
                hits.extend(provider.search(query, limit=limit))
            except Exception as exc:  # noqa: BLE001 - one bad backend is fine
                logger.warning("provider %s failed on %r: %s",
                               getattr(provider, "name", "?"), query, exc)
    return hits
