"""Explicitly allow-listed, server-managed agent implementations.

Managed agents run trusted application code only. Their inputs are bounded and
are never persisted by this module; publisher-defined cards are not executable.
"""
from __future__ import annotations

import json
from typing import Any

from src.agent_marketplace.service import AgentMarketplaceError

DATA_CURATOR_ID = "data-curator"
DATA_CURATOR_ENGINE = "wcp-data-curator-heuristic-v1"

_MANAGED = {
    DATA_CURATOR_ID: {
        "agent_id": DATA_CURATOR_ID,
        "version": "1.0.0",
        "summary": (
            "Scores supplied text or JSON records with deterministic heuristic "
            "quality and safety signals; it does not use an external model."),
        "capabilities": ["quality-scoring", "heuristic-safety-signals"],
        "execution_engine": DATA_CURATOR_ENGINE,
    },
}


def list_managed_agents() -> list[dict[str, Any]]:
    """Return public metadata for the server-owned allow-list."""
    return [dict(agent) for agent in _MANAGED.values()]


def run_managed_agent(agent_id: str, records: list[Any]) -> dict[str, Any]:
    """Execute one bounded built-in agent; never imports publisher code."""
    if agent_id != DATA_CURATOR_ID:
        raise AgentMarketplaceError("managed agent not found")

    from src.ai.agents.data_curator import DataCurator, heuristic_score

    def offline_score(prompt: str) -> str:
        marker = "\nRECORD:\n"
        _, separator, record = prompt.partition(marker)
        if not separator:
            return json.dumps({})
        return json.dumps(heuristic_score(record.rstrip("\n")),
                          ensure_ascii=False)

    report = DataCurator(score_fn=offline_score).curate(records)
    results = []
    for index, result in enumerate(report.results):
        results.append({
            "record_index": index,
            "relevance": result["relevance"],
            "coherence": result["coherence"],
            "density": result["density"],
            "safety": result["safety"],
            "overall": result["overall"],
            "decision": result["decision"],
            "reason": result["reason"],
        })
    return {
        "agent_id": DATA_CURATOR_ID,
        "execution_engine": DATA_CURATOR_ENGINE,
        "total": report.total,
        "kept": report.kept,
        "dropped": report.dropped,
        "reviewed": report.reviewed,
        "mean_quality": report.mean_quality,
        "results": results,
        "persisted": False,
    }
