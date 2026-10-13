"""Hugging Face Datasets streaming connector.

Uses ``datasets.load_dataset(..., streaming=True)`` so multi-terabyte corpora
(e.g. ``toshibakun/Refined_OSS_instruct_enhanced``, ``HuggingFaceFW/fineweb``,
``provenance4all/mirrors``) can be consumed with O(1) memory: shards are
downloaded lazily and iterated as generators — exactly what a distributed
collector needs.

The module degrades gracefully: without the optional ``datasets`` package we
still expose the schema/validation helpers (used by the API layer) and raise
an informative error only when actually streaming.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass
class HFStreamConfig:
    """Validated configuration for one streaming job."""

    dataset: str                                  # e.g. "HuggingFaceFW/fineweb"
    subset: str | None = None                     # config name
    split: str = "train"
    text_field: str = "text"                      # which column carries content
    url_field: str | None = None                  # optional provenance column
    token_field: str | None = None                # optional precomputed tokens col
    max_samples: int | None = None                # hard cap per worker shard
    shuffle_seed: int | None = 42                 # deterministic buffered shuffle
    shuffle_buffer_size: int = 10_000
    filters: dict[str, Any] = field(default_factory=dict)  # simple == / range filters

    def validate(self) -> None:
        if not self.dataset or "/" not in self.dataset and "." not in self.dataset:
            raise ValueError(f"invalid dataset id: {self.dataset!r}")
        if not self.text_field:
            raise ValueError("text_field must be set")
        if self.max_samples is not None and self.max_samples <= 0:
            raise ValueError("max_samples must be positive")
        if self.shuffle_buffer_size > 1_000_000:
            raise ValueError("shuffle_buffer_size too large (>1M)")


def _apply_filters(example: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Support ``{"lang": "en"}`` equality and ``{"score": {">=": 0.3}}`` ranges."""
    for key, cond in filters.items():
        val = example.get(key)
        if isinstance(cond, dict):
            for op, bound in cond.items():
                if val is None:
                    return False
                if op == ">=" and not val >= bound:
                    return False
                if op == "<=" and not val <= bound:
                    return False
                if op == ">" and not val > bound:
                    return False
                if op == "<" and not val < bound:
                    return False
                if op == "!=" and val == bound:
                    return False
        elif val != cond:
            return False
    return True


class HFStreamer:
    """Generator-based bridge from HF datasets to our document pipeline."""

    def __init__(self, cfg: HFStreamConfig, token: str | None = None) -> None:
        cfg.validate()
        self.cfg = cfg
        self.token = token or os.getenv("HF_TOKEN")

    def stream(self, *, shard: int = 0, num_shards: int = 1) -> Iterator[dict]:
        """Yield normalized dicts; ``shard``/``num_shards`` partition work across workers.

        Partitioning uses ``split_dataset_by_node`` semantics via
        ``shuffle(seed).shard(...)`` on the iterable so every Celery/Ray worker
        reads a disjoint slice of an infinite stream without coordination.
        """
        try:
            from datasets import load_dataset  # lazy optional import
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "the 'datasets' package is required for HF streaming: pip install datasets"
            ) from exc

        ds = load_dataset(
            self.cfg.dataset,
            self.cfg.subset,
            split=self.cfg.split,
            streaming=True,
        )
        if self.cfg.shuffle_seed is not None:
            ds = ds.shuffle(seed=self.cfg.shuffle_seed, buffer_size=self.cfg.shuffle_buffer_size)
        if num_shards > 1:
            ds = ds.shard(num_shards=num_shards, index=shard, contiguous=False)

        produced = 0
        for example in ds:
            if not _apply_filters(example, self.cfg.filters):
                continue
            text = example.get(self.cfg.text_field)
            if not text:
                continue
            doc: dict[str, Any] = {
                "text": text,
                "source": f"huggingface:{self.cfg.dataset}",
                "metadata": {k: v for k, v in example.items() if k != self.cfg.text_field},
            }
            if self.cfg.url_field and example.get(self.cfg.url_field):
                doc["url"] = example[self.cfg.url_field]
            if self.cfg.token_field and example.get(self.cfg.token_field) is not None:
                doc["token_count"] = example[self.cfg.token_field]
            yield doc
            produced += 1
            if self.cfg.max_samples and produced >= self.cfg.max_samples:
                logger.info("hf stream %s: hit max_samples=%d", self.cfg.dataset, produced)
                return

    def info(self) -> dict[str, Any]:
        """Dataset card metadata (features/description) without downloading data."""
        try:
            from datasets import load_dataset_card  # type: ignore
        except ImportError:
            load_dataset_card = None  # type: ignore[assignment]
        out: dict[str, Any] = {"dataset": self.cfg.dataset}
        if load_dataset_card is not None:  # pragma: no cover - network dependent
            try:
                card = load_dataset_card(self.cfg.dataset)
                out["description"] = str(card)
            except Exception as exc:  # noqa: BLE001
                logger.debug("dataset card fetch failed: %s", exc)
        return out
