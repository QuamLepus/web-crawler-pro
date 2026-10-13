"""CSV exporter (stdlib csv; UTF-8 with BOM for Excel compatibility)."""

from __future__ import annotations

import csv
import os


class CSVExporter:
    def __init__(self, output_dir: str | None = None):
        self.output_dir = output_dir or os.getenv("CRAWLER_OUTPUT_DIR", "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def save(self, items: list[dict], filename: str = "items.csv") -> str:
        path = os.path.join(self.output_dir, filename)
        if not items:
            open(path, "w").close()
            return path
        fieldnames: list[str] = []
        for it in items:
            for key in it:
                if key not in fieldnames:
                    fieldnames.append(key)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for it in items:
                writer.writerow({k: (str(v) if isinstance(v, (list, dict)) else v)
                                 for k, v in it.items()})
        return path
