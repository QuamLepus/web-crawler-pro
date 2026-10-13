"""Trillion-token pipeline: WebDataset shards, sequence packing, data mixing.

Stage outputs (all idempotent, resumable via Redis bookkeeping):

  raw/      ingested JSONL documents
  clean/    filtered + DQS-scored docs
  dedupe/   MinHash+semantic survivors
  shards/   WebDataset tar files (~100 MB each) ready for training

WebDataset layout per sample::

  {shard}/{sample}.text     original text (debuggable)
  {shard}/{sample}.tokens   int32 little-endian packed row (4096)
  {shard}/{sample}.json     metadata: source, url, dqs, token_count

Supports two training formats:
  * causal LM        -> plain packed token rows
  * instruction-tune -> {"prompt", "completion"} chat records rendered with a
    ChatML-style template and loss-masked segments.

DataMixing combines N sources by weight with replacement sampling and an
optional temperature exponent (w_i^(1/T)) - the standard LLM "data recipe"
control used in GLM/Llama training runs.
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import struct
import tarfile
import time
from dataclasses import dataclass, field
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

SHARD_TARGET_BYTES = 100 * 1024 * 1024  # ~100MB WebDataset shards
SEQ_LEN = int(os.getenv("PACK_SEQ_LEN", "4096"))


# --------------------------------------------------------------------------- #
# Pipeline monitor counters (Redis-backed when available)
# --------------------------------------------------------------------------- #
class PipelineMonitor:
    """Tracks docs/tokens at every stage; powers the dashboard & SLO alerts."""

    KEY = "pipeline:stats"
    STAGES = ("ingested", "cleaned", "deduplicated", "filtered", "tokenized", "sharded")

    def __init__(self, redis_client=None) -> None:
        self.r = redis_client
        self._mem: dict[str, int] = {}

    def incr(self, stage: str, *, docs: int = 0, tokens: int = 0, bytes_: int = 0) -> None:
        assert stage in self.STAGES, f"unknown stage {stage}"
        updates = {}
        if docs:
            updates[f"{stage}:docs"] = docs
        if tokens:
            updates[f"{stage}:tokens"] = tokens
        if bytes_:
            updates[f"{stage}:bytes"] = bytes_
        if self.r is not None:
            pipe = self.r.pipeline()
            for k, v in updates.items():
                pipe.hincrby(self.KEY, k, v)
            pipe.execute()
        else:
            for k, v in updates.items():
                self._mem[k] = self._mem.get(k, 0) + v

    def snapshot(self) -> dict:
        if self.r is not None:
            data = {k.decode(): int(v) for k, v in self.r.hgetall(self.KEY).items()}
        else:
            data = dict(self._mem)
        out: dict[str, dict[str, int]] = {s: {} for s in self.STAGES}
        for k, v in data.items():
            stage, metric = k.split(":", 1)
            out.setdefault(stage, {})[metric] = v
        total_tokens = sum(d.get("tokens", 0) for d in out.values())
        return {
            "stages": out,
            "total_tokens": total_tokens,
            "trillion_token_progress": round(total_tokens / 1e12, 6),
            "updated_at": time.time(),
        }


# --------------------------------------------------------------------------- #
# WebDataset writer
# --------------------------------------------------------------------------- #
@dataclass
class ShardWriter:
    """Buffers samples into ~100MB tar shards; returns written shard paths."""

    out_dir: str
    prefix: str = "shard"
    seq_len: int = SEQ_LEN
    target_bytes: int = SHARD_TARGET_BYTES
    _tar: tarfile.TarFile | None = field(default=None, repr=False)
    _buf: io.BytesIO | None = field(default=None, repr=False)
    _index: int = 0
    _shard_n: int = 0
    _samples: int = 0
    _tokens: int = 0
    closed_shards: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)

    def _open_new(self) -> None:
        self._buf = io.BytesIO()
        self._tar = tarfile.open(fileobj=self._buf, mode="w")
        self._index = 0

    def add(self, text: str, token_ids: list[int], meta: dict | None = None) -> None:
        if self._tar is None:
            self._open_new()
        assert self._tar is not None and self._buf is not None
        base = f"{self.prefix}-{self._shard_n:06d}/{self._index:08d}"
        payload_text = text.encode("utf-8")
        packed = self._pad_row(token_ids)
        payload_tok = struct.pack(f"<{len(packed)}i", *packed)
        meta_json = json.dumps(
            {**(meta or {}), "token_count": len(token_ids), "row_len": len(packed)},
            ensure_ascii=False,
        ).encode("utf-8")
        for ext, payload in (("text", payload_text), ("tokens", payload_tok), ("json", meta_json)):
            info = tarfile.TarInfo(f"{base}.{ext}")
            info.size = len(payload)
            info.mtime = int(time.time())
            self._tar.addfile(info, io.BytesIO(payload))
        self._index += 1
        self._samples += 1
        self._tokens += len(token_ids)
        if self._buf.tell() >= self.target_bytes:
            self.rotate()

    @staticmethod
    def _pad_row(ids: list[int], eos_id: int = 0, seq_len: int = SEQ_LEN) -> list[int]:
        row = list(ids[:seq_len])
        if len(row) < seq_len:
            row += [eos_id] * (seq_len - len(row))
        return row

    def rotate(self) -> str | None:
        """Close current tar, flush to disk, start next shard."""
        if self._tar is None or self._index == 0:
            return None
        assert self._buf is not None
        self._tar.close()
        path = os.path.join(self.out_dir, f"{self.prefix}-{self._shard_n:06d}.tar")
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(self._buf.getvalue())
        os.replace(tmp, path)
        size = os.path.getsize(path)
        logger.info("wrote shard %s (%.1f MB, %d samples)", path, size / 1e6, self._index)
        self.closed_shards.append(path)
        self._shard_n += 1
        self._tar = None
        self._buf = None
        return path

    def close(self) -> list[str]:
        self.rotate()
        return self.closed_shards

    def stats(self) -> dict:
        return {"shards": len(self.closed_shards), "samples": self._samples, "tokens": self._tokens}


# --------------------------------------------------------------------------- #
# Instruction-tuning format
# --------------------------------------------------------------------------- #
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"


def render_chatml(messages: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Render ChatML and return (text, loss_spans) where spans cover assistant
    replies only (standard instruct masking: no loss on prompt tokens)."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for msg in messages:
        role = msg["role"]
        seg = f"{IM_START}{role}\n{msg['content']}{IM_END}\n"
        if role == "assistant":
            content_start = pos + len(IM_START) + len(role) + 1
            content_end = content_start + len(msg["content"])
            spans.append((content_start, content_end))
        parts.append(seg)
        pos += len(seg)
    return "".join(parts), spans


def load_jsonl(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


# --------------------------------------------------------------------------- #
# Tokenization stage driver
# --------------------------------------------------------------------------- #
class TokenizeStage:
    """Encode cleaned docs -> packed rows -> WebDataset shards."""

    def __init__(self, tokenizer, monitor: PipelineMonitor | None = None,
                 seq_len: int = SEQ_LEN) -> None:
        self.tok = tokenizer
        self.monitor = monitor or PipelineMonitor()
        self.seq_len = seq_len

    def run(self, docs: Iterable[dict], out_dir: str, *, prefix: str = "shard",
             format: str = "causal") -> dict:
        """docs items need 'text' (causal) or 'messages' (instruct)."""
        writer = ShardWriter(out_dir=out_dir, prefix=prefix, seq_len=self.seq_len)
        total_tokens = 0
        n_docs = 0
        for doc in docs:
            if format == "instruct":
                messages = doc.get("messages") or [
                    {"role": "user", "content": doc.get("prompt", "")},
                    {"role": "assistant", "content": doc.get("completion", "")},
                ]
                text, spans = render_chatml(messages)
                ids = self.tok.encode_ids(text)
                meta = {"format": "instruct", "loss_spans": spans}
            else:
                text = doc.get("text", "")
                if not text:
                    continue
                ids = self.tok.encode_ids(text)
                ids.append(self.tok.eos_id)
                meta = {"format": "causal"}
            meta.update({
                "url": doc.get("url"),
                "source": doc.get("source"),
                "dqs": doc.get("dqs"),
                "lang": doc.get("lang"),
            })
            # pack long docs into multiple rows inside one shard entry stream
            for i in range(0, max(len(ids), 1), self.seq_len):
                chunk = ids[i : i + self.seq_len]
                writer.add(text if i == 0 else "", chunk, meta)
                total_tokens += len(chunk)
            n_docs += 1
        shards = writer.close()
        self.monitor.incr("tokenized", docs=n_docs, tokens=total_tokens)
        self.monitor.incr("sharded", docs=len(shards), tokens=total_tokens)
        return {"docs": n_docs, "tokens": total_tokens, "shards": shards, **writer.stats()}


# --------------------------------------------------------------------------- #
# Data mixing
# --------------------------------------------------------------------------- #
@dataclass
class MixSource:
    name: str
    path: str            # jsonl file or directory of jsonl shards
    weight: float
    seed_override: dict | None = None  # e.g. {"min_dqs": 70}


@dataclass
class DataMix:
    """Weighted sampler across sources producing a single doc stream.

    temperature T>1 flattens weights toward uniform (more diversity);
    T<1 sharpens toward the highest-weight source. Weights renormalized.
    """

    sources: list[MixSource]
    temperature: float = 1.0
    rng_seed: int = 1337

    def normalized_weights(self) -> list[float]:
        ws = [max(s.weight, 0.0) ** (1.0 / max(self.temperature, 1e-6)) for s in self.sources]
        total = sum(ws)
        if total <= 0:
            raise ValueError("mix has zero total weight")
        return [w / total for w in ws]

    def stream(self, limit: int | None = None) -> Iterator[dict]:
        rng = random.Random(self.rng_seed)
        pools: list[list[dict]] = []
        for src in self.sources:
            docs = [d for d in load_jsonl(src.path) if self._passes(d, src)]
            if not docs:
                raise ValueError(f"source {src.name} produced no docs")
            pools.append(docs)
        weights = self.normalized_weights()
        produced = 0
        while limit is None or produced < limit:
            pool = rng.choices(pools, weights=weights, k=1)[0]
            doc = dict(rng.choice(pool))
            yield doc
            produced += 1
            if limit is not None and produced >= limit:
                return

    @staticmethod
    def _passes(doc: dict, src: MixSource) -> bool:
        ov = src.seed_override or {}
        if "min_dqs" in ov and float(doc.get("dqs") or 0) < ov["min_dqs"]:
            return False
        if "langs" in ov and doc.get("lang") not in ov["langs"]:
            return False
        return True

    def recipe_report(self) -> dict:
        ws = self.normalized_weights()
        return {
            "temperature": self.temperature,
            "sources": [
                {"name": s.name, "weight": s.weight, "effective": round(w, 4)}
                for s, w in zip(self.sources, ws)
            ],
        }
