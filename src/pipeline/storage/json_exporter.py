"""JSON / JSONL exporters."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


class JSONExporter:
    def __init__(self, output_dir: str | None = None):
        self.output_dir = output_dir or os.getenv("CRAWLER_OUTPUT_DIR", "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def save(self, item: dict) -> str:
        """Append one item to today's JSONL shard; returns the file path."""
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = os.path.join(self.output_dir, f"items-{day}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return path

    def save_batch(self, items: list[dict], filename: str) -> str:
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=2, default=str)
        return path
