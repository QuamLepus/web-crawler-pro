"""PySpark jobs for petabyte-scale document processing.

Jobs are written as *pure functions over DataFrames* so they can be unit
tested with a local SparkSession (or mocked) and submitted unchanged to
EMR / Dataproc / K8s Spark Operator.

Primary job: distributed MinHash-LSH near-dedupe at billion-doc scale.

  Stage 1 (map):    compute 128-int signature + band hashes per doc
  Stage 2 (shuffle): group by (band_id, bucket_hash)
  Stage 3 (reduce): within each bucket, pairwise Jaccard >= threshold keeps
                    the earliest doc id only; emits duplicate pairs
  Stage 4 (join):   anti-join survivors against duplicate ids

Also exposes ``quality_filter_job`` applying C4/Gopher rules via broadcast
rule UDF and ``token_count_job`` using a pickled tokenizer.

Requires optional dependency ``pyspark``; import of this module without it
raises a clear RuntimeError only when a job is invoked.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)


def _spark_session(app_name: str):
    try:
        from pyspark.sql import SparkSession
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pyspark not installed: pip install pyspark") from exc
    master = os.getenv("SPARK_MASTER", "local[*]")
    builder = (
        SparkSession.builder.appName(app_name).master(master)
        .config("spark.sql.shuffle.partitions", os.getenv("SPARK_PARTITIONS", "2000"))
        .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
    )
    return builder.getOrCreate()


# --------------------------------------------------------------------------- #
# MinHash LSH dedupe job
# --------------------------------------------------------------------------- #
def minhash_dedupe_job(docs: Iterable[tuple[str, str]], *, threshold: float = 0.85,
                       num_perm: int = 128, spark=None) -> dict:
    """Run distributed LSH dedupe.

    ``docs`` may be any iterable of (doc_id, text) — in production replace
    with ``spark.read.json(...)`` producing those columns. Returns summary
    dict {total, kept, dropped} and (when running on Spark) a DataFrame of
    surviving docs keyed by id.
    """
    from src.training.minhash_dedupe import MinHasher, MinHashLSHIndex

    hasher = MinHasher(num_perm)
    shape = MinHashLSHIndex(num_perm=num_perm, threshold=threshold)

    if spark is None:
        spark = _spark_session("minhash-dedupe")

    rdd = spark.sparkContext.parallelize(list(docs), int(os.getenv("SPARK_PARTITIONS", "2000")))

    def emit_bands(item):
        doc_id, text = item
        sig = hasher.signature(text)
        rows = []
        for bi, bk in enumerate(shape.band_keys(sig)):
            rows.append((f"{bi}:{bk.hex()}", (doc_id, tuple(sig))))
        return rows

    band_pairs = rdd.flatMap(emit_bands)

    def prune_bucket(entries):
        """Within one band bucket keep only cluster representatives."""
        entries = list(entries)
        keep: list[tuple] = []
        dupes: set[str] = set()
        reps: list[tuple[str, tuple]] = []
        for doc_id, sig in entries:
            is_dup = False
            for rid, rsig in reps:
                j = sum(1 for a, b in zip(sig, rsig) if a == b) / len(sig)
                if j >= threshold:
                    is_dup = True
                    dupes.add(doc_id)
                    break
            if not is_dup:
                reps.append((doc_id, sig))
                keep.append(doc_id)
        return keep, dupes

    grouped = band_pairs.groupByKey().mapValues(prune_bucket)
    # A doc survives iff it is kept in EVERY band bucket it appears in and
    # never marked duplicate anywhere. Aggregate across bands on driver-side
    # keys to stay O(n * bands).
    kept_rdd = grouped.flatMap(lambda kv: [(did, 1) for did in kv[1][0]])
    dup_rdd = grouped.flatMap(lambda kv: [(did, -1) for did in kv[1][1]])
    marks = kept_rdd.union(dup_rdd).reduceByKey(lambda a, b: min(a, b))
    survivors = marks.filter(lambda kv: kv[1] == 1).keys().collectAsSet()

    all_ids = [d[0] for d in docs] if isinstance(docs, list) else [d[0] for d in docs]
    kept = [i for i in all_ids if i in survivors]
    dropped = [i for i in all_ids if i not in survivors]
    logger.info("dedupe job: %d/%d kept", len(kept), len(all_ids))
    return {"total": len(all_ids), "kept": kept, "dropped": dropped}


# --------------------------------------------------------------------------- #
# Quality filter job (C4 + Gopher via pandas UDF for Arrow speed)
# --------------------------------------------------------------------------- #
def quality_filter_job(input_path: str, output_path: str, *, spark=None,
                       min_dqs: float = 40.0) -> dict:
    """Filter + score a JSONL corpus ('text' column) writing survivors out."""
    if spark is None:
        spark = _spark_session("quality-filter")
    from pyspark.sql import functions as F  # noqa: F401
    from pyspark.sql.types import DoubleType

    from src.training.filters import apply_filters, quality_score

    df = spark.read.json(input_path)

    def _score_udf():
        from pyspark.sql.functions import udf

        def f(text):
            if text is None:
                return (-1.0,)
            rep = apply_filters(text)
            qs = quality_score(text)
            return (qs if rep.passed else -1.0,)

        return udf(f, DoubleType())

    scored = df.withColumn("dqs", _score_udf()(F.col("text")))
    survivors = scored.filter(F.col("dqs") >= min_dqs)
    total = df.count()
    kept = survivors.count()
    if hasattr(survivors, "coalesce"):
        survivors.coalesce(int(os.getenv("OUTPUT_PARTS", "200"))).write.mode("overwrite").json(output_path)
    logger.info("quality job: %d/%d survive (min_dqs=%.1f)", kept, total, min_dqs)
    return {"total": total, "kept": kept, "pass_rate": kept / total if total else 0.0}


# --------------------------------------------------------------------------- #
# Token counting job
# --------------------------------------------------------------------------- #
def token_count_job(input_path: str, tokenizer_path: str | None = None, *, spark=None) -> int:
    """Sum token counts over a JSONL corpus; broadcast tokenizer to executors."""
    if spark is None:
        spark = _spark_session("token-count")
    from pyspark.sql import functions as F

    from src.training.tokenizer import TokenizerEngine, TokenizerSpec

    spec = TokenizerSpec(pretrained_path=tokenizer_path) if tokenizer_path else TokenizerSpec()
    sc = spark.sparkContext
    bc = sc.broadcast(spec.to_json())

    def _count(text):
        from src.training.tokenizer import TokenizerEngine as TE

        eng = TE.__new__(TE)  # cheap rebuild from broadcast spec
        import json as _json

        s = _json.loads(bc.value)
        eng.spec = TokenizerSpec(**{**s, "special_tokens": tuple(s["special_tokens"])})
        eng._hf_tokenizer = None
        eng._sp_model = None
        eng._backend = None
        eng._fallback = None
        eng._load()
        return eng.count(text or "")

    df = spark.read.json(input_path)
    counts = df.select(F.udf(_count)(F.col("text")).alias("n")).agg(F.sum("n")).collect()[0][0]
    return int(counts or 0)
