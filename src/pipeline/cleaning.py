"""Data cleaning & normalization (Pandas + Polars).

`normalize_item` is dependency-light (always available) and handles:
  - whitespace/unicode normalization of text fields
  - URL canonicalization
  - ISO-8601 timestamps
  - numeric coercion for common metrics

`clean_dataframe` offers a Polars fast-path with a Pandas fallback for
bulk-cleaning exported datasets.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

_WS_RE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    """NFC-normalize unicode, strip control chars, collapse whitespace."""
    if not isinstance(value, str):
        value = str(value)
    value = unicodedata.normalize("NFC", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in "\n\t ")
    return _WS_RE.sub(" ", value).strip()


def normalize_item(item: dict) -> dict:
    """Return a cleaned copy of a crawl item (pure-python, no df deps)."""
    out = dict(item)
    for key in ("title", "text"):
        if key in out and out[key] is not None:
            out[key] = clean_text(out[key])
    if "url" in out:
        from src.frontier.url_frontier import URLFrontier
        out["url"] = URLFrontier.normalize(out["url"])
    ts = out.get("fetched_at")
    if isinstance(ts, str) and ts:
        try:
            out["fetched_at"] = datetime.fromisoformat(ts).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            out["fetched_at"] = datetime.now(timezone.utc).isoformat()
    elif not ts:
        out["fetched_at"] = datetime.now(timezone.utc).isoformat()
    for num_key in ("status_code", "depth"):
        if num_key in out:
            try:
                out[num_key] = int(out[num_key])
            except (TypeError, ValueError):
                pass
    return out


def clean_dataframe(df):
    """Bulk-clean a DataFrame (pandas) or LazyFrame/DataFrame (polars)."""
    try:
        import polars as pl

        def _clean_expr(col: str):
            return (
                pl.col(col)
                .str.strip_chars()
                .str.replace_all(r"\s+", " ")
            )
        for col in ("title", "text"):
            if col in df.columns:
                df = df.with_columns(_clean_expr(col))
        return df
    except ImportError:
        pass

    import pandas as pd
    df = df.copy()
    for col in ("title", "text"):
        if col in df.columns:
            df[col] = (
                df[col].astype(str)
                .str.replace(r"\s+", " ", regex=True)
                .str.strip()
            )
    if "fetched_at" in df.columns:
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True)
    return df.drop_duplicates(subset=["url"], keep="last") if "url" in df.columns else df
