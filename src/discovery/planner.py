"""Topic -> search queries.

A single topic string is a weak search key: ``"best budget laptops 2026"``
fans out into ``"best budget laptops 2026 review"``, ``"cheap laptop buying
guide"`` and a few more, and the union of their results is a far better
candidate pool than any one query.

Two planners ship here:

* :func:`template_queries` — deterministic, free, offline. Always available.
* :func:`llm_queries` — asks a chat model for queries. Used automatically when
  ``DISCOVERY_USE_LLM=true`` and an API key exists; falls back to templates on
  any error so discovery can never be blocked by a model call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.ai.agents.agent_crawler import HeuristicPlanner

logger = logging.getLogger("wcp.discovery.planner")

# Query shapes that reliably surface *source* pages rather than shop listings.
_TEMPLATES: tuple[str, ...] = (
    "{topic}",
    "{topic} review",
    "best {topic}",
    "{topic} buying guide",
    "{topic} comparison",
    "{topic} explained",
    "{topic} documentation",
    "{topic} blog",
    "learn {topic}",
    "{topic} roundup",
)


@dataclass
class QueryPlan:
    """A topic expanded into search queries plus its scoring keywords."""

    topic: str
    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    source: str = "template"      # "template" | "llm" | "llm+template"

    def to_dict(self) -> dict:
        return {"topic": self.topic, "queries": self.queries,
                "keywords": self.keywords, "source": self.source}


def topic_keywords(topic: str) -> list[str]:
    """Content words of ``topic`` (reuses the agent crawler's stopword set)."""
    return list(dict.fromkeys(HeuristicPlanner(topic).keywords))


def template_queries(topic: str, *, max_queries: int = 6) -> list[str]:
    """Deterministic query expansion — no model, no network."""
    topic = re.sub(r"\s+", " ", (topic or "").strip())
    if not topic:
        return []
    lowered = topic.lower()
    out: list[str] = []
    seen: set[str] = set()
    for tpl in _TEMPLATES:
        if tpl.startswith("best ") and lowered.startswith("best "):
            continue  # "best best budget laptops" helps nobody
        q = tpl.format(topic=topic)
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
        if len(out) >= max_queries:
            break
    return out


LLM_QUERY_PROMPT = """You are planning web searches for a crawler.

Topic: {topic}

Write {n} short web-search queries that together would surface the most
authoritative, most up-to-date web pages about this topic. Use different
angles (official docs, reviews, comparisons, tutorials, news).

Reply with EXACTLY one JSON object, no prose:
{{"queries": ["...", "..."]}}
"""


def llm_queries(topic: str, *, max_queries: int = 6, llm=None) -> list[str]:
    """Model-generated queries. Returns ``[]`` when no model is reachable."""
    try:
        if llm is None:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=__import__("os").getenv("LLM_MODEL", "gpt-4o-mini"),
                temperature=0)
        raw = llm.invoke(LLM_QUERY_PROMPT.format(topic=topic, n=max_queries))
        content = getattr(raw, "content", raw)
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content)
        from src.ai.agents.agent_crawler import _first_json_object

        data = __import__("json").loads(_first_json_object(str(content)))
        queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
        return queries[:max_queries]
    except Exception as exc:  # noqa: BLE001 - never block discovery on the LLM
        logger.info("llm query planning unavailable (%s); using templates", exc)
        return []


def plan_queries(topic: str, *, max_queries: int = 6, llm=None,
                 use_llm: bool | None = None) -> QueryPlan:
    """Expand ``topic`` into queries.

    With ``use_llm`` unset the planner follows ``DISCOVERY_USE_LLM``. LLM
    queries are *merged ahead of* the template queries, so a weak or partial
    model answer still leaves a usable plan.
    """
    from src.config import get_config

    topic = re.sub(r"\s+", " ", (topic or "").strip())
    if use_llm is None:
        use_llm = get_config().DISCOVERY_USE_LLM

    queries: list[str] = []
    source = "template"
    if use_llm or llm is not None:
        try:
            generated = llm_queries(topic, max_queries=max_queries, llm=llm)
        except Exception:  # noqa: BLE001 - defensive; llm_queries already guards
            generated = []
        if generated:
            queries.extend(generated)
            source = "llm"

    planned = [q for q in queries if q]
    for q in template_queries(topic, max_queries=max_queries):
        if q.lower() not in {p.lower() for p in planned}:
            planned.append(q)
        if len(planned) >= max_queries:
            break
    if queries and source == "llm" and len(planned) > len(queries):
        source = "llm+template"

    return QueryPlan(topic=topic, queries=planned[:max_queries],
                     keywords=topic_keywords(topic), source=source)
