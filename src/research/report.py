"""Citation-first extractive report construction; no unsupported synthesis."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit


def make_report(topic: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build source-backed candidate claims from crawled text.

    Each claim is an exact source sentence. The confidence value is only a
    relevance/coverage heuristic and must never be represented as truth
    probability or independent verification.
    """
    terms = {w.casefold() for w in re.findall(r"[\w-]+", topic) if len(w) > 2}
    evidence = []
    valid_items = []
    for item in items:
        parsed_url = urlsplit(str(item.get("url") or ""))
        if (parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname
                or parsed_url.username or parsed_url.password):
            continue
        valid_items.append(item)
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunks = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
                  if 35 <= len(s.strip()) <= 700]
        scored = sorted(((sum(t in s.casefold() for t in terms), s) for s in chunks),
                        key=lambda pair: (-pair[0], pair[1]))
        for score, sentence in scored[:2]:
            evidence.append((score, sentence, {
                "url": item.get("url"),
                "collected_at": item.get("fetched_at"),
                "content_sha256": digest,
                "excerpt": sentence,
            }))
    evidence.sort(key=lambda row: (-row[0], row[1]))
    claims = []
    seen = set()
    for score, sentence, source in evidence:
        key = (sentence.casefold(), source.get("url"))
        if key in seen:
            continue
        seen.add(key)
        claims.append({"claim": sentence, "citations": [source],
                       "confidence": round(min(0.9, 0.35 + 0.1 * min(score, 5)), 2),
                       "confidence_kind": "heuristic-term-overlap-not-probability"})
        if len(claims) >= 30:
            break
    urls = list(dict.fromkeys(str(item.get("url")) for item in valid_items))
    return {
        "topic": topic,
        "summary": (f"Collected {len(items)} page records from {len(urls)} distinct source URLs. "
                    "The findings below are verbatim extractive sentences; review the cited pages "
                    "before relying on them."),
        "claims": claims,
        "sources": [{"url": item.get("url"), "collected_at": item.get("fetched_at"),
                     "content_sha256": hashlib.sha256(
                         str(item.get("text") or "").encode("utf-8")).hexdigest()}
                    for item in valid_items],
        "verification": {
            "method": "exact-excerpt-presence-in-crawled-text",
            "independent_fact_check": False,
            "claim_confidence_is_calibrated": False,
        },
    }
