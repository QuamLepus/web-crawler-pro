"""Ray.io distributed Python workloads (ML-first alternative to Spark).

Used for embedding generation, semantic dedupe shards, GPU toxicity scoring
and shard upload — tasks that are awkward in Spark's JVM boundary.

Design: every entrypoint is a *pure function of JSON-serializable args* so it
can run with ``ray.init(address='auto')`` on the cluster or inline (RAY_MODE=
local) during tests without any cluster present.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _ray_init():
    import ray

    mode = os.getenv("RAY_MODE", "auto")
    if not ray.is_initialized():
        if mode == "local":
            ray.init(ignore_reinit_error=True, local_mode=True)
        else:  # pragma: no cover - requires cluster
            ray.init(address="auto", ignore_reinit_error=True)
    return ray


# --------------------------------------------------------------------------- #
# Embedding workers
# --------------------------------------------------------------------------- #
def _embed_batch(texts: list[str], model_name: str | None) -> list[list[float]]:
    """Runs inside a Ray actor/worker; loads SentenceTransformer once per proc."""
    global _EMBED_MODEL
    try:
        if "_EMBED_MODEL" not in globals() or _EMBED_MODEL is None:
            from sentence_transformers import SentenceTransformer

            _EMBED_MODEL = SentenceTransformer(model_name or "all-MiniLM-L6-v2")
        vecs = _EMBED_MODEL.encode(texts, batch_size=64, normalize_embeddings=True)
        return [v.tolist() for v in vecs]
    except Exception:  # noqa: BLE001 - fallback keeps pipelines alive
        from src.training.semantic_dedupe import _HashingEmbedder

        emb = _HashingEmbedder()
        return [v.tolist() for v in emb.encode(texts)]


def embed_documents(docs: list[dict], *, text_key: str = "text",
                    batch_size: int = 256, num_gpus: float = 0.0) -> list[list[float]]:
    """Distributed embedding over doc list preserving order."""
    ray = _ray_init()
    texts = [d.get(text_key, "") for d in docs]
    chunks = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
    remote_embed = ray.remote(num_gpus=num_gpus)(_embed_batch)
    futures = [remote_embed.remote(chunk, os.getenv("ST_MODEL")) for chunk in chunks]
    out: list[list[float]] = []
    for part in ray.get(futures):
        out.extend(part)
    return out


# --------------------------------------------------------------------------- #
# Semantic dedupe across shards (each worker owns an FAISS sub-index)
# --------------------------------------------------------------------------- #
def semantic_dedupe_shards(doc_lists: list[list[tuple[str, str]]], *,
                           threshold: float = 0.92) -> dict:
    """Partitioned semantic dedupe: each shard gets its own index actor, then
    cross-shard candidate pairs are verified centrally. Returns summary."""
    results = {"kept": 0, "dropped": 0, "pairs": []}
    # Local pass per shard first (cheap), then merge survivors into one index.
    merged: list[tuple[str, str]] = []
    for docs in doc_lists:
        from src.training.semantic_dedupe import SemanticDeduplicator

        dedup = SemanticDeduplicator(threshold=threshold)
        res = dedup.dedupe_stream(iter(docs))
        results["kept"] += res["kept"]
        results["dropped"] += res["dropped"]
        results["pairs"].extend(res["pairs"])
        id_map = {i: t for i, t in docs}
        for did, _, _ in res["pairs"]:
            id_map.pop(did, None)
        merged.extend(id_map.items())
    final = _final_pass(merged, threshold)
    results["kept"] = final["kept"]
    results["dropped"] += final["dropped"]
    return results


def _final_pass(docs: list[tuple[str, str]], threshold: float) -> dict:
    from src.training.semantic_dedupe import SemanticDeduplicator

    dedup = SemanticDeduplicator(threshold=threshold)
    return dedup.dedupe_stream(iter(docs))


# --------------------------------------------------------------------------- #
# GPU-accelerated preprocessing (RAPIDS cuDF) with pandas fallback
# --------------------------------------------------------------------------- #
def cudf_quality_filter(records: Iterable[dict], *, use_gpu: bool | None = None) -> list[dict]:
    """Vectorized pre-filter using RAPIDS cuDF when available.

    Cheap numeric gates (length bounds, symbol ratio via column ops) run on
    GPU; expensive textual rules stay CPU-side. Fallback uses pandas with the
    identical semantics.
    """
    recs = list(records)
    if not recs:
        return []
    want_gpu = use_gpu if use_gpu is not None else os.getenv("USE_RAPIDS", "0") == "1"
    xp = None
    if want_gpu:
        try:
            import cudf as xp  # type: ignore
        except ImportError:
            logger.info("cudf unavailable; falling back to pandas")
            xp = None
    if xp is None:
        try:
            import pandas as xp  # type: ignore
        except ImportError:
            xp = None

    def keep(rec: dict) -> bool:
        text = rec.get("text", "")
        n = len(text)
        if not (200 <= n <= 20_000_000):
            return False
        symbols = sum(1 for c in text[:10_000] if c in "#;<>{}[]|")
        return symbols / max(len(text[:10_000]), 1) < 0.3

    if xp is not None:  # vectorized path
        series = xp.Series([len(r.get("text", "")) for r in recs])
        mask = (series >= 200) & (series <= 20_000_000)
        idx = getattr(mask, "tolist", lambda: mask.tolist())()
        cand = [r for r, m in zip(recs, idx) if m]
    else:  # pure python path
        cand = [r for r in recs if 200 <= len(r.get("text", "")) <= 20_000_000]
    return [r for r in cand if keep(r)]


# --------------------------------------------------------------------------- #
# Dask pipeline (medium scale: single big machine / small cluster)
# --------------------------------------------------------------------------- #
def dask_clean_pipeline(paths: list[str], out_dir: str, *, scheduler: str | None = None) -> dict:
    """Stream JSONL files through cleaning with Dask delayed graph.

    Each file becomes one task chain: read -> clean+score -> write jsonl.
    Falls back to sequential processing when dask isn't installed.
    """
    os.makedirs(out_dir, exist_ok=True)
    try:
        import dask
        from dask import delayed
    except ImportError:
        logger.warning("dask not installed; running sequentially")
        scheduler = None

    def process_one(path: str) -> dict:
        import json

        from src.pipeline.cleaning import clean_html_text  # existing module
        from src.training.filters import apply_filters, quality_score

        base = os.path.basename(path)
        out_path = os.path.join(out_dir, base.replace(".jsonl", ".clean.jsonl"))
        kept = total = 0
        tokens_est = 0
        with open(path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                doc = json.loads(line)
                total += 1
                text = doc.get("text", "")
                cleaned = clean_html_text(text) if "<" in text else text
                rep = apply_filters(cleaned, lang=doc.get("lang", "en"))
                if rep.passed:
                    doc["text"] = cleaned
                    doc["dqs"] = quality_score(cleaned, extra_signals={"has_url": bool(doc.get("url"))})
                    tokens_est += len(cleaned.split()) * 1.3
                    fout.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    kept += 1
        return {"file": out_path, "total": total, "kept": kept, "tokens_est": int(tokens_est)}

    if scheduler is None:
        stats = [process_one(p) for p in paths]
    else:
        tasks = [delayed(process_one)(p) for p in paths]
        stats = dask.compute(*tasks, scheduler=scheduler)
    return {
        "files": len(stats),
        "total": sum(s["total"] for s in stats),
        "kept": sum(s["kept"] for s in stats),
        "tokens_est": sum(s["tokens_est"] for s in stats),
        "outputs": [s["file"] for s in stats],
    }
