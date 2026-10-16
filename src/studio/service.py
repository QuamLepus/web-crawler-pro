"""Data Studio — living datasets: versioned, diffable, exportable, provable.

A dataset here is not a CSV someone downloaded once. It is a named collection
with:

* **immutable versions** (``v1``, ``v2`` …) — every refresh appends, nothing is
  overwritten, so "what did this look like last Tuesday" always has an answer;
* **a diff between any two versions** — added / removed / changed rows, keyed
  by a primary key you choose;
* **quality indicators** — completeness per field, duplicate rate, freshness,
  and which rows came from which host;
* **a signed provenance manifest per version** (:mod:`src.provenance.manifest`);
* **a changelog** — for a *living* dataset, each refresh records what moved,
  which is what makes "monitor this market" a feature rather than a cron job.

Storage is files (JSONL per version) plus a Redis index for metadata: a
dataset you cannot ``cat`` is a dataset you cannot trust.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import statistics
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger("wcp.studio")

DEFAULT_ROOT = "data/datasets"


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class DatasetVersion:
    version: str
    rows: int
    created_at: float = field(default_factory=time.time)
    path: str = ""
    checksum: str = ""
    manifest: dict[str, Any] | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    changelog: list[str] = field(default_factory=list)
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = round(self.created_at, 3)
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> DatasetVersion:
        base = cls(version=row.get("version", "v1"), rows=int(row.get("rows", 0)))
        for k, v in row.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base


@dataclass
class Dataset:
    name: str
    tenant: str
    dataset_id: str = ""
    description: str = ""
    kind: str = "custom"                 # custom | watch | research | crawl
    primary_key: str = "url"
    fields: list[str] = field(default_factory=list)
    versions: list[DatasetVersion] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    watch_id: str = ""
    live: bool = True
    license_policy: str = "redistributable-only"
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.dataset_id:
            self.dataset_id = "ds_" + uuid.uuid4().hex[:14]
        self.versions = [v if isinstance(v, DatasetVersion) else DatasetVersion.from_dict(v)
                         for v in self.versions]

    @property
    def latest(self) -> DatasetVersion | None:
        return self.versions[-1] if self.versions else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dataset_id": self.dataset_id,
            "tenant": self.tenant,
            "description": self.description,
            "kind": self.kind,
            "primary_key": self.primary_key,
            "fields": list(self.fields),
            "versions": [v.to_dict() for v in self.versions],
            "latest": self.latest.to_dict() if self.latest else None,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
            "watch_id": self.watch_id,
            "live": self.live,
            "license_policy": self.license_policy,
            "tags": list(self.tags),
            "rows": self.latest.rows if self.latest else 0,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Dataset:
        base = cls(name=row.get("name", ""), tenant=row.get("tenant", ""))
        for k, v in row.items():
            if k in ("latest", "rows", "versions"):
                continue
            if hasattr(base, k):
                setattr(base, k, v)
        base.versions = [DatasetVersion.from_dict(v) for v in row.get("versions") or []]
        base.__post_init__()
        return base


@dataclass
class RowDiff:
    key: str
    status: str                      # added | removed | changed | same
    changes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetDiff:
    dataset: str
    from_version: str
    to_version: str
    added: int = 0
    removed: int = 0
    changed: int = 0
    unchanged: int = 0
    field_changes: dict[str, int] = field(default_factory=dict)
    rows: list[RowDiff] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "added": self.added, "removed": self.removed, "changed": self.changed,
            "unchanged": self.unchanged,
            "field_changes": dict(sorted(self.field_changes.items(),
                                         key=lambda kv: -kv[1])),
            "rows": [r.to_dict() for r in self.rows[:200]],
            "summary": list(self.summary),
        }


# --------------------------------------------------------------------------- #
# quality
# --------------------------------------------------------------------------- #
def quality_report(rows: Sequence[dict[str, Any]], *, key: str = "url") -> dict[str, Any]:
    """Completeness, duplicates, freshness and source mix for a version."""
    n = len(rows)
    if not n:
        return {"rows": 0, "completeness": {}, "duplicate_rate": 0.0,
                "freshness_days": None, "sources": {}, "score": 0.0}
    fields = sorted({k for row in rows for k in row})
    completeness = {
        f: round(sum(1 for row in rows if row.get(f) not in (None, "", [])) / n, 4)
        for f in fields
    }
    keys = [str(row.get(key, "")) for row in rows]
    duplicates = len(keys) - len(set(keys))
    stamps = [float(row.get("fetched_at") or row.get("updated_at") or 0) for row in rows]
    stamps = [s for s in stamps if s]
    freshness = ((time.time() - max(stamps)) / 86400.0) if stamps else None
    hosts = Counter(_host(row.get("url") or row.get("source_url") or "")
                    for row in rows)
    mean_completeness = statistics.fmean(completeness.values()) if completeness else 0.0
    dup_penalty = duplicates / n
    fresh_penalty = 0.0 if freshness is None else min(0.3, freshness / 365.0)
    score = max(0.0, min(1.0, mean_completeness - dup_penalty - fresh_penalty + 0.1))
    return {
        "rows": n,
        "fields": len(fields),
        "completeness": completeness,
        "completeness_mean": round(mean_completeness, 4),
        "duplicate_rate": round(dup_penalty, 4),
        "freshness_days": round(freshness, 2) if freshness is not None else None,
        "sources": dict(hosts.most_common(10)),
        "score": round(score, 4),
    }


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def summarise(rows: Sequence[dict[str, Any]], *, numeric_fields: Sequence[str] = (),
              top: int = 5) -> dict[str, Any]:
    """Statistical digest — the "what are the top trends" question, answered
    without a language model (so it is free, instant and reproducible)."""
    out: dict[str, Any] = {"rows": len(rows), "numeric": {}, "categorical": {}}
    for field_name in numeric_fields:
        values = []
        for row in rows:
            value = row.get(field_name)
            if isinstance(value, (int, float)):
                values.append(float(value))
            elif isinstance(value, dict) and "amount" in value:
                values.append(float(value["amount"]))
        if not values:
            continue
        values.sort()
        out["numeric"][field_name] = {
            "count": len(values),
            "min": round(values[0], 4),
            "max": round(values[-1], 4),
            "mean": round(statistics.fmean(values), 4),
            "median": round(statistics.median(values), 4),
            "p90": round(values[int(len(values) * 0.9) - 1], 4) if len(values) > 1 else
            round(values[0], 4),
        }
    for field_name in {k for row in rows[:200] for k in row} - set(numeric_fields):
        counts = Counter(str(row.get(field_name)) for row in rows
                         if row.get(field_name) not in (None, "", []))
        if 1 < len(counts) <= 25 and sum(counts.values()) >= max(3, len(rows) // 4):
            out["categorical"][field_name] = dict(counts.most_common(top))
    return out


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class DatasetStore:
    """Versioned dataset store (files + a Redis metadata index)."""

    def __init__(self, redis_client=None, *, root: str | Path | None = None,
                 prefix: str = "wcp:studio") -> None:
        self.r = redis_client
        self.root = Path(root or os.getenv("WCP_DATASET_DIR", DEFAULT_ROOT))
        self.prefix = prefix

    # -- paths --------------------------------------------------------------- #
    def _dir(self, tenant: str, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80]
        return self.root / (tenant or "public") / (safe or "dataset")

    def _rows_path(self, tenant: str, name: str, version: str) -> Path:
        return self._dir(tenant, name) / f"{version}.jsonl"

    def _k(self, tenant: str, *parts: str) -> str:
        return ":".join([self.prefix, tenant or "public", *parts])

    @staticmethod
    def _dec(raw) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- CRUD ---------------------------------------------------------------- #
    def create(self, tenant: str, name: str, *, description: str = "",
               primary_key: str = "url", kind: str = "custom",
               license_policy: str = "redistributable-only",
               watch_id: str = "", tags: Sequence[str] = ()) -> Dataset:
        dataset = Dataset(name=name.strip()[:120] or "dataset", tenant=tenant,
                          description=description[:500], primary_key=primary_key,
                          kind=kind, license_policy=license_policy,
                          watch_id=watch_id, tags=list(tags))
        self._save(dataset)
        return dataset

    def _save(self, dataset: Dataset) -> None:
        if self.r is None:
            return
        dataset.updated_at = time.time()
        row = json.loads(json.dumps(dataset.to_dict(), default=str))
        row.pop("latest", None)
        row.pop("rows", None)
        self.r.set(self._k(dataset.tenant, "ds", dataset.dataset_id),
                   json.dumps(row, separators=(",", ":")))
        self.r.zadd(self._k(dataset.tenant, "datasets"),
                    {dataset.dataset_id: dataset.created_at})

    def get(self, tenant: str, ref: str) -> Dataset | None:
        if self.r is None:
            return None
        raw = self.r.get(self._k(tenant, "ds", ref))
        if not raw:
            ids = [self._dec(i) for i in
                   self.r.zrange(self._k(tenant, "datasets"), 0, -1)]
            for did in ids:
                payload = self.r.get(self._k(tenant, "ds", did))
                if not payload:
                    continue
                candidate = Dataset.from_dict(json.loads(payload))
                if candidate.name == ref:
                    return candidate
            return None
        return Dataset.from_dict(json.loads(raw))

    def list(self, tenant: str, *, limit: int = 100) -> list[Dataset]:
        if self.r is None:
            return []
        ids = [self._dec(i) for i in
               self.r.zrevrange(self._k(tenant, "datasets"), 0, max(0, limit - 1))]
        out = []
        for did in ids:
            raw = self.r.get(self._k(tenant, "ds", did))
            if raw:
                out.append(Dataset.from_dict(json.loads(raw)))
        return out

    # -- versioning ---------------------------------------------------------- #
    def add_version(self, dataset: Dataset, rows: Sequence[dict[str, Any]], *,
                    note: str = "", source: str = "",
                    manifest: dict[str, Any] | None = None) -> DatasetVersion:
        """Append an immutable version, with quality metrics and a changelog."""
        version = f"v{len(dataset.versions) + 1}"
        path = self._rows_path(dataset.tenant, dataset.name, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, default=str) + "\n")

        previous = dataset.latest
        changed, changelog = 0, []
        if previous:
            diff = self.diff_rows(self._read_rows(previous.path),
                                  list(rows), key=dataset.primary_key)
            changed = diff["added"] + diff["removed"] + diff["changed"]
            changelog = diff["summary"]
        version_obj = DatasetVersion(
            version=version,
            rows=len(rows),
            path=str(path),
            checksum=_file_checksum(path),
            manifest=manifest,
            quality=quality_report(rows, key=dataset.primary_key),
            changelog=changelog,
            source=source,
            note=note or ("initial version" if not previous
                          else f"{changed} row(s) moved"),
        )
        dataset.versions.append(version_obj)
        dataset.updated_at = time.time()
        dataset.fields = sorted({k for row in rows[:500] for k in row})
        self._save(dataset)
        return version_obj

    def refresh(self, dataset: Dataset, rows: Sequence[dict[str, Any]], *,
                note: str = "", source: str = "") -> dict[str, Any]:
        """Living dataset: add a version only when the data actually changed.

        A no-op refresh must not create a version — otherwise a daily monitor
        produces 365 identical versions a year and the history becomes noise.
        """
        previous = dataset.latest
        if previous is None:
            version = self.add_version(dataset, rows, note=note or "initial version",
                                       source=source)
            return {"version": version.to_dict(), "changed": True,
                    "reason": "initial version"}
        before = self._read_rows(previous.path)
        diff = self.diff_rows(before, list(rows), key=dataset.primary_key)
        moved = diff["added"] + diff["removed"] + diff["changed"]
        if not moved:
            return {"version": previous.to_dict(), "changed": False,
                    "reason": "no change — no version appended",
                    "rows": len(rows)}
        version = self.add_version(dataset, rows, note=note, source=source)
        return {"version": version.to_dict(), "changed": True, "rows": len(rows),
                "moved": moved, "diff": diff}

    def _read_rows(self, path: str | Path) -> list[dict[str, Any]]:
        p = Path(path)
        if not p.exists():
            return []
        out = []
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def rows(self, dataset: Dataset, version: str | None = None,
             *, limit: int = 10_000) -> list[dict[str, Any]]:
        target = self._find_version(dataset, version)
        if target is None:
            return []
        return self._read_rows(target.path)[:limit]

    def iter_rows(self, dataset: Dataset, version: str | None = None
                  ) -> Iterator[dict[str, Any]]:
        target = self._find_version(dataset, version)
        if target is None:
            return
        with Path(target.path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    def _find_version(self, dataset: Dataset, version: str | None
                      ) -> DatasetVersion | None:
        if not dataset.versions:
            return None
        if not version or version == "latest":
            return dataset.latest
        for v in dataset.versions:
            if v.version == version:
                return v
        return None

    # -- diff ---------------------------------------------------------------- #
    @staticmethod
    def diff_rows(before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]],
                  *, key: str = "url", max_rows: int = 200) -> dict[str, Any]:
        """Row-level diff keyed on one field, with a per-field change count."""
        old = {str(r.get(key, f"row-{i}")): r for i, r in enumerate(before)}
        new = {str(r.get(key, f"row-{i}")): r for i, r in enumerate(after)}
        added = [k for k in new if k not in old]
        removed = [k for k in old if k not in new]
        changed: list[dict[str, Any]] = []
        field_changes: Counter[str] = Counter()
        unchanged = 0
        for k in old.keys() & new.keys():
            deltas = {}
            for f in set(old[k]) | set(new[k]):
                if f in ("fetched_at", "updated_at", "crawled_at"):
                    continue
                if old[k].get(f) != new[k].get(f):
                    deltas[f] = {"from": old[k].get(f), "to": new[k].get(f)}
                    field_changes[f] += 1
            if deltas:
                changed.append({"key": k, "changes": deltas})
            else:
                unchanged += 1

        summary: list[str] = []
        if added:
            summary.append(f"{len(added)} row(s) added")
        if removed:
            summary.append(f"{len(removed)} row(s) removed")
        if changed:
            top_fields = ", ".join(f for f, _ in field_changes.most_common(3))
            summary.append(f"{len(changed)} row(s) changed ({top_fields})")
        if not summary:
            summary.append("no changes")
        return {
            "added": len(added), "removed": len(removed), "changed": len(changed),
            "unchanged": unchanged,
            "field_changes": dict(field_changes),
            "rows": ([{"key": k, "status": "added"} for k in added[:max_rows]]
                     + [{"key": k, "status": "removed"} for k in removed[:max_rows]]
                     + [{"key": c["key"], "status": "changed",
                         "changes": c["changes"]} for c in changed[:max_rows]]),
            "summary": summary,
        }

    def diff(self, dataset: Dataset, from_version: str, to_version: str,
             *, max_rows: int = 200) -> DatasetDiff:
        old = self._find_version(dataset, from_version)
        new = self._find_version(dataset, to_version)
        if old is None or new is None:
            raise KeyError(f"{from_version} or {to_version} not found")
        raw = self.diff_rows(self._read_rows(old.path), self._read_rows(new.path),
                             key=dataset.primary_key, max_rows=max_rows)
        return DatasetDiff(
            dataset=dataset.name, from_version=old.version, to_version=new.version,
            added=raw["added"], removed=raw["removed"], changed=raw["changed"],
            unchanged=raw["unchanged"], field_changes=raw["field_changes"],
            rows=[RowDiff(key=r["key"], status=r["status"],
                          changes=r.get("changes") or {}) for r in raw["rows"]],
            summary=raw["summary"],
        )

    # -- exports ------------------------------------------------------------- #
    def export(self, dataset: Dataset, *, version: str | None = None,
               fmt: str = "jsonl", limit: int = 100_000,
               fields: Sequence[str] | None = None,
               filter_query: str = "") -> tuple[str, str]:
        """Return ``(payload, content_type)`` for csv/json/jsonl/markdown."""
        rows = self.rows(dataset, version, limit=limit)
        if filter_query:
            rows = [r for r in rows if filter_query.lower() in
                    json.dumps(r, default=str).lower()]
        if fields:
            rows = [{k: r.get(k) for k in fields if k in r} for r in rows]
        target = self._find_version(dataset, version)
        stamp = target.version if target else "latest"
        if fmt == "csv":
            buffer = io.StringIO()
            columns = list(fields) if fields else sorted(
                {k for row in rows[:500] for k in row})
            writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _flat(v) for k, v in row.items()})
            return buffer.getvalue(), "text/csv"
        if fmt == "markdown":
            return self._markdown(dataset, rows, target), "text/markdown"
        if fmt == "json":
            return json.dumps(rows, indent=2, default=str), "application/json"
        return ("\n".join(json.dumps(r, default=str) for r in rows),
                "application/x-ndjson")

    @staticmethod
    def _markdown(dataset: Dataset, rows: Sequence[dict[str, Any]],
                  version: DatasetVersion | None) -> str:
        lines = [f"# {dataset.name}", ""]
        if dataset.description:
            lines += [dataset.description, ""]
        if version:
            lines += [f"- **Version:** {version.version} ({version.rows:,} rows)",
                      f"- **Quality score:** "
                      f"{(version.quality or {}).get('score', 'n/a')}",
                      f"- **Checksum:** `{version.checksum[:23]}…`",
                      f"- **Changelog:** {'; '.join(version.changelog) or 'n/a'}", ""]
        if rows:
            columns = list(rows[0].keys())[:8]
            lines.append("| " + " | ".join(columns) + " |")
            lines.append("|" + "---|" * len(columns))
            for row in rows[:50]:
                cells = []
                for c in columns:
                    value = row.get(c)
                    if isinstance(value, dict):
                        value = value.get("amount", value)
                    cells.append(str(value)[:60].replace("|", "\\|"))
                lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines) + "\n"

    # -- maintenance --------------------------------------------------------- #
    def delete(self, dataset: Dataset) -> bool:
        if self.r is not None:
            pipe = self.r.pipeline()
            pipe.delete(self._k(dataset.tenant, "ds", dataset.dataset_id))
            pipe.zrem(self._k(dataset.tenant, "datasets"), dataset.dataset_id)
            pipe.execute()
        directory = self._dir(dataset.tenant, dataset.name)
        if directory.exists():
            for path in directory.glob("*"):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
        return True

    def stats(self, tenant: str) -> dict[str, Any]:
        datasets = self.list(tenant, limit=500)
        return {
            "datasets": len(datasets),
            "versions": sum(len(d.versions) for d in datasets),
            "rows": sum((d.latest.rows if d.latest else 0) for d in datasets),
            "live": sum(1 for d in datasets if d.live),
            "bytes": sum(_size(v.path) for d in datasets for v in d.versions),
        }


def _flat(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def _file_checksum(path: Path) -> str:
    import hashlib

    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()[:32]


def _size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0
