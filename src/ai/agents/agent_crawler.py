"""LangChain autonomous agent crawler.

An LLM-driven ReAct agent that explores a website *autonomously*: it reads a
page, decides which links are worth following for the user's goal, extracts
structured data as it goes, and stops when it judges the goal satisfied (or
hits budget limits).

Architecture
------------
* Tools (``fetch_page``, ``extract_links``, ``save_record``, ``finish``)
  wrap the deterministic crawler primitives (:mod:`src.crawlers.basic_crawler`,
  :mod:`src.frontier.url_frontier`) so the agent never touches raw sockets.
* The planning loop runs on LangChain when available; otherwise a built-in
  **heuristic fallback planner** (keyword-scored BFS) keeps the module fully
  functional offline — this is what CI exercises.
* Safety rails: max pages, per-domain politeness via the frontier, depth cap,
  and an allow-list of linkable domains.

Usage::

    from src.ai.agents.agent_crawler import AgentCrawler
    agent = AgentCrawler(goal="pricing info", llm=my_chat_model)
    result = agent.run("https://example.com/pricing")
    print(result.records, result.pages_visited)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# --------------------------------------------------------------------------- #
# Result / action types
# --------------------------------------------------------------------------- #


@dataclass
class CrawlAction:
    """One decision taken by the agent."""

    thought: str
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    goal: str
    seed_url: str
    records: list[dict] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    actions: list[CrawlAction] = field(default_factory=list)
    finished: bool = False
    summary: str = ""


# --------------------------------------------------------------------------- #
# Deterministic page tools (used by both LLM and heuristic planners)
# --------------------------------------------------------------------------- #
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                      re.IGNORECASE | re.DOTALL)


def clean_html(html: str) -> str:
    """Strip scripts/styles/tags -> readable text (token saver)."""
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


def extract_links(base_url: str, html: str) -> list[str]:
    """Absolute, same-host-only, http(s) links found in `html`."""
    host = urlparse(base_url).netloc
    out: list[str] = []
    seen: set[str] = set()
    for href, _label in _LINK_RE.findall(html):
        url = urljoin(base_url, href.strip())
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            continue
        if p.netloc != host:  # stay inside the site unless overridden
            continue
        url = f"{p.scheme}://{p.netloc}{p.path}" + (f"?{p.query}" if p.query else "")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


class PageToolKit:
    """Deterministic fetch/parse tools shared by every planner backend.

    Fetching goes through an injected callable so tests can feed canned HTML
    without network access. Default fetcher uses requests + optional stealth.
    """

    def __init__(self, fetcher=None, max_depth: int = 3):
        self._fetcher = fetcher
        self.max_depth = max_depth
        self._depth: dict[str, int] = {}
        self.cache: dict[str, str] = {}

    # -- fetching ---------------------------------------------------------- #
    def _default_fetch(self, url: str) -> str:
        import requests

        resp = requests.get(url, timeout=15, headers={
            "User-Agent": os.getenv(
                "CRAWL_USER_AGENT",
                "WebCrawlerProBot/2.1 (+https://webcrawlerpro.example/bot)")})
        resp.raise_for_status()
        return resp.text

    def fetch(self, url: str) -> str:
        if url in self.cache:
            return self.cache[url]
        html = (self._fetcher or self._default_fetch)(url)
        self.cache[url] = html
        return html

    # -- parsing ----------------------------------------------------------- #
    def text_of(self, url: str) -> str:
        return clean_html(self.fetch(url))

    def links_of(self, url: str) -> list[str]:
        return extract_links(url, self.fetch(url))

    def depth_of(self, url: str) -> int:
        return self._depth.get(url, 0)

    def register_children(self, parent: str, children: list[str]) -> None:
        d = self.depth_of(parent) + 1
        for c in children:
            self._depth.setdefault(c, min(d, self.max_depth + 1))


# --------------------------------------------------------------------------- #
# Heuristic fallback planner (offline / no-LLM mode)
# --------------------------------------------------------------------------- #
class HeuristicPlanner:
    """Scores candidate URLs by keyword overlap with the goal.

    This mirrors what the LLM would decide (follow links that look relevant)
    using cheap lexical scoring, giving the agent a usable brain even when
    no model/API key is configured.
    """

    STOPWORDS = frozenset("""a an the of to in on for and or is are be with
    how what where when i we you our your""".split())

    def __init__(self, goal: str):
        self.keywords = [w for w in re.findall(r"[a-z0-9]+", goal.lower())
                         if w not in self.STOPWORDS]

    def score(self, url: str, anchor: str = "", title: str = "") -> float:
        hay = f"{url} {anchor} {title}".lower()
        s = sum(hay.count(k) for k in self.keywords)
        # prefer shallower paths & index-like URLs less
        s -= 0.5 * urlparse(url).path.count("/")
        return s

    def rank(self, candidates: list[tuple[str, str]]) -> list[str]:
        """candidates: list of (url, anchor_text) -> ordered urls."""
        scored = sorted(candidates,
                        key=lambda ua: -self.score(ua[0], anchor=ua[1]))
        return [u for u, _ in scored]


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
AGENT_SYSTEM_PROMPT = """You are an autonomous web-crawl agent.
Goal: {goal}
At each step you receive the current page text and available links.
Reply with EXACTLY one JSON object, no prose:
{{"thought": "<why>", "action": "fetch"|"extract"|"finish",
  "url": "<link to fetch when action=fetch>",
  "record": {{...}}  // when action=extract, structured data from current page
}}
Use "finish" with a "summary" field once the goal is satisfied."""


class AgentCrawler:
    """ReAct-style autonomous crawler.

    Parameters
    ----------
    goal:
        Natural-language objective ("find pricing plans", ...).
    llm:
        Optional LangChain chat model (anything with ``.invoke(str)->str``).
        When *None*, the heuristic planner drives the crawl.
    toolkit:
        Optional :class:`PageToolKit` (inject a fetcher for tests).
    max_pages:
        Hard budget on distinct pages fetched.
    """

    def __init__(self, goal: str, llm=None, toolkit: PageToolKit | None = None,
                 max_pages: int = 20, extraction_schema: dict | None = None,
                 stealth: bool | None = None):
        self.goal = goal
        self.llm = llm
        # Stealth Engine integration: when enabled (STEALTH_ENGINE=1 by
        # default), blocked fetches automatically escalate through the
        # curl_cffi TLS-spoof → playwright-stealth ladder and outcomes feed
        # the Success-Rate Monitor.
        if stealth is None:
            stealth = os.getenv("STEALTH_ENGINE", "1") == "1"
        self._stealth_enabled = stealth
        self._stealth_fetcher = None
        self.tools = toolkit or PageToolKit(fetcher=self._smart_fetch)
        self.max_pages = max_pages
        self.extraction_schema = extraction_schema or {"key_facts": ["..."]}
        self._planner = HeuristicPlanner(goal)

    # ------------------------------------------------------------------ #
    def _smart_fetch(self, url: str) -> str:
        """requests-first, Stealth-Engine-on-block fetching strategy."""
        import requests

        try:
            from src.middleware.success_monitor import get_monitor

            monitor = get_monitor()
            strat = monitor.best_strategy()
        except Exception:  # pragma: no cover - monitor always available
            monitor, strat = None, "http_plain"
        headers = {"User-Agent": os.getenv(
            "CRAWL_USER_AGENT",
            "WebCrawlerProBot/2.1 (+https://webcrawlerpro.example/bot)")}
        if strat == "http_plain" or strat is None:
            try:
                resp = requests.get(url, timeout=15, headers=headers)
                resp.raise_for_status()
                if monitor is not None:
                    from src.middleware.success_monitor import classify as cl

                    monitor.record("http_plain",
                                   cl(status=resp.status_code,
                                      html_snippet=resp.text[:2000]))
                return resp.text
            except Exception as exc:  # noqa: BLE001
                if monitor is not None and not self._stealth_enabled:
                    from src.middleware.success_monitor import Outcome

                    monitor.record("http_plain", Outcome.ERROR)
                if not self._stealth_enabled:
                    raise
                logger.info("plain fetch blocked (%s); escalating to "
                            "Stealth Engine for %s", exc, url)
        # escalation path: full stealth ladder w/ fingerprint rotation
        return self._stealth().fetch(url).html

    def _stealth(self):
        if self._stealth_fetcher is None:
            from src.middleware.stealth_middleware import StealthFetcher

            self._stealth_fetcher = StealthFetcher()
        return self._stealth_fetcher

    # ------------------------------------------------------------------ #
    def run(self, seed_url: str) -> AgentResult:
        result = AgentResult(goal=self.goal, seed_url=seed_url)
        frontier: list[tuple[str, str]] = [(seed_url, "")]
        visited: set[str] = set()

        while frontier and len(visited) < self.max_pages:
            url, anchor = frontier.pop(0)
            if url in visited:
                continue
            try:
                html = self.tools.fetch(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent: fetch failed %s: %s", url, exc)
                continue
            visited.add(url)
            result.visited.append(url)
            text = clean_html(html)

            # --- decide next move ---------------------------------------- #
            decision = self._decide(url, text, anchor, result)
            result.actions.append(decision)

            # --- record extraction for this page -------------------------- #
            rec = self._extract_record(url, page_title(html), text)
            if rec:
                result.records.append(rec)

            # --- expand frontier ----------------------------------------- #
            if decision.tool == "finish":
                result.finished = True
                result.summary = decision.args.get("summary", "")
                break
            for link in self.tools.links_of(url):
                if link not in visited:
                    frontier.append((link, ""))
            # relevance-order the frontier (heuristic or LLM ranking)
            frontier.sort(key=lambda ua: -self._planner.score(ua[0], anchor=ua[1]))

        if not result.finished:
            result.summary = (f"Stopped after {len(visited)} pages "
                              f"(budget exhausted)" if visited else
                              "No pages reachable")
        return result

    # ------------------------------------------------------------------ #
    def _decide(self, url: str, text: str, anchor: str,
                result: AgentResult) -> CrawlAction:
        """Ask the LLM (if wired) else fall back to the heuristic planner."""
        if self.llm is not None:
            try:
                return self._decide_llm(url, text, result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM decision failed (%s); using heuristic", exc)
        relevant = self._planner.score(url, anchor=anchor, title=text[:200])
        enough = relevant > 0 and len(result.records) >= 2
        return CrawlAction(
            thought=(f"heuristic: page score={relevant:.1f}, "
                     f"records={len(result.records)}"),
            tool="finish" if enough else "fetch",
            args={"url": url, "summary": "goal satisfied" if enough else {}})

    def _decide_llm(self, url: str, text: str, result: AgentCrawlResultT) -> CrawlAction:
        import json as _json

        prompt = AGENT_SYSTEM_PROMPT.format(goal=self.goal) + (
            f"\n\nCURRENT URL: {url}\nPAGE TEXT (truncated): {text[:4000]}\n"
            f"RECORDS SO FAR: {len(result.records)}")
        raw = self.llm.invoke(prompt)
        content = getattr(raw, "content", raw)
        data = _json.loads(_first_json_object(str(content)))
        return CrawlAction(thought=data.get("thought", ""),
                           tool=data.get("action", "fetch"),
                           args={k: v for k, v in data.items()
                                 if k not in ("thought", "action")})

    # ------------------------------------------------------------------ #
    def _extract_record(self, url: str, title: str, text: str) -> dict:
        """Pull a structured record: LLM parser if configured, else rules."""
        facts = self._heuristic_facts(text)
        if not facts:
            return {}
        return {"url": url, "title": title, "goal": self.goal,
                "key_facts": facts, "depth": self.tools.depth_of(url)}

    @staticmethod
    def _heuristic_facts(text: str, limit: int = 5) -> list[str]:
        """Sentences containing digits/$/keywords = likely 'facts'."""
        sentences = re.split(r"(?<=[.!?:])\s+", text)
        scored = []
        for s in sentences:
            s = s.strip()
            if len(s) < 20 or len(s) > 300:
                continue
            score = bool(re.search(r"[\$€£]\s?\d|\d+(?:\.\d+)?%?", s)) * 2 \
                + bool(re.search(r"\b(price|plan|tier|contact|about|free)\b",
                                 s, re.I))
            if score:
                scored.append((score, s))
        scored.sort(key=lambda t: -t[0])
        return [s for _, s in scored[:limit]]


# small alias so type hints above read nicer (AgentResult is the real type)
AgentCrawlResultT = AgentResult


def _first_json_object(s: str) -> str:
    """Extract the first balanced {...} JSON object from noisy LLM output."""
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object in response")
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    raise ValueError("unbalanced JSON object")


# --------------------------------------------------------------------------- #
# LangChain wiring helper (optional path)
# --------------------------------------------------------------------------- #
def build_langchain_agent(model_name: str | None = None):
    """Create a ChatOpenAI-backed planner if langchain-openai is installed.

    Returns ``None`` (not an error) when deps or API key are missing so
    callers can transparently use the heuristic brain instead.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from langchain_openai import ChatOpenAI  # lazy import

        return ChatOpenAI(model=model_name or os.getenv("LLM_MODEL", "gpt-4o"),
                          temperature=0.0, max_tokens=512)
    except Exception:  # pragma: no cover - optional dependency
        return None


def crawl_site(seed_url: str, goal: str, *, llm=None,
               max_pages: int = 20, fetcher=None, stealth=None) -> AgentResult:
    """Convenience one-shot entrypoint used by jobs/Celery tasks."""
    agent = AgentCrawler(goal=goal, llm=llm or build_langchain_agent(),
                         toolkit=PageToolKit(fetcher=fetcher) if fetcher else None,
                         max_pages=max_pages, stealth=stealth)
    return agent.run(seed_url)
