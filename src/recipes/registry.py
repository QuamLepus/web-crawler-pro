"""Recipe registry — the network effect.

Every scraper in the world breaks the same way, on the same sites, for the
same reasons, and every team fixes it alone. A shared, versioned, *tested*
registry is how that stops: install a recipe instead of writing one, publish
the fix you already made, and pin versions so an upstream change never
silently alters your pipeline.

Layout on disk (``data/recipes`` by default, ``WCP_RECIPE_DIR`` overrides)::

    data/recipes/<name>/<version>.json
    data/recipes/index.json          # name -> versions, hosts, digest
    data/recipes/published/<name>@<version>.json   # exportable, shareable

The registry is deliberately a directory of JSON files: it diffs in git,
syncs over rsync, and a registry server for it is a static file host.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

from src.recipes.models import Recipe, semver_key, sort_versions
from src.recipes.runtime import test_recipe

logger = logging.getLogger("wcp.recipes.registry")

DEFAULT_ROOT = "data/recipes"


class RecipeNotFound(KeyError):
    pass


class RecipeRegistry:
    """Filesystem-backed recipe store with a searchable index."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("WCP_RECIPE_DIR", DEFAULT_ROOT))
        self.index_path = self.root / "index.json"

    # -- index --------------------------------------------------------------- #
    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"recipes": {}}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("recipe index unreadable — rebuilding")
            return {"recipes": {}}

    def _save_index(self, index: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(index, indent=2, sort_keys=True),
                                   encoding="utf-8")

    def _rebuild_index(self) -> dict[str, Any]:
        index: dict[str, Any] = {"recipes": {}}
        for path in sorted(self.root.glob("*/*.json")):
            if path.parent.name in ("published", "index"):
                continue
            try:
                recipe = Recipe.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
            entry = index["recipes"].setdefault(recipe.name, {"versions": {},
                                                             "hosts": []})
            entry["versions"][recipe.version] = {
                "checksum": recipe.checksum, "digest": recipe.digest(),
                "fields": len(recipe.fields), "tests": len(recipe.tests),
                "installed_at": time.time(),
            }
            entry["hosts"] = sorted({*entry["hosts"], *recipe.hosts})
        self._save_index(index)
        return index

    # -- writes -------------------------------------------------------------- #
    def install(self, recipe: Recipe, *, source: str = "", verify: bool = True,
                run_tests: bool = False) -> dict[str, Any]:
        """Install (or upgrade) a recipe.

        ``verify`` refuses a recipe whose checksum does not match its body —
        the check that makes a shared registry mean something. ``run_tests``
        additionally refuses a recipe that fails its own fixtures.
        """
        if verify and not recipe.verify():
            raise ValueError(f"{recipe.ref}: checksum does not match the recipe body")
        problems = recipe.validate()
        blocking = [p for p in problems if "no tests" not in p]
        if blocking:
            raise ValueError(f"{recipe.ref} is invalid: " + "; ".join(blocking))
        health = None
        if run_tests and recipe.tests:
            health = test_recipe(recipe)
            if health.failed:
                raise ValueError(f"{recipe.ref} fails its own tests: "
                                 f"{health.failures[:2]}")
        target = self.root / recipe.name / f"{recipe.version}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        recipe.updated_at = time.time()
        target.write_text(json.dumps(recipe.to_dict(), indent=2), encoding="utf-8")
        index = self._load_index()
        entry = index["recipes"].setdefault(recipe.name, {"versions": {}, "hosts": []})
        entry["versions"][recipe.version] = {
            "checksum": recipe.checksum, "digest": recipe.digest(),
            "fields": len(recipe.fields), "tests": len(recipe.tests),
            "installed_at": time.time(), "source": source,
            "health": health.verdict if health else "untested",
        }
        entry["hosts"] = sorted({*entry.get("hosts", []), *recipe.hosts})
        entry["latest"] = sort_versions(list(entry["versions"]))[0]
        self._save_index(index)
        return {"installed": recipe.ref, "path": str(target),
                "problems": [p for p in problems if "no tests" in p],
                "health": health.to_dict() if health else None}

    def remove(self, name: str, version: str | None = None) -> int:
        removed = 0
        directory = self.root / name
        if not directory.exists():
            return 0
        for path in directory.glob("*.json"):
            if version and path.stem != version:
                continue
            path.unlink()
            removed += 1
        index = self._load_index()
        if version:
            index.get("recipes", {}).get(name, {}).get("versions", {}).pop(version, None)
        else:
            index.get("recipes", {}).pop(name, None)
        self._save_index(index)
        if not any(directory.iterdir()):
            directory.rmdir()
        return removed

    def publish(self, name: str, version: str | None = None) -> dict[str, Any]:
        """Write an exportable artefact (``published/<name>@<version>.json``)."""
        recipe = self.get(name, version)
        out_dir = self.root / "published"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}@{recipe.version}.json"
        payload = recipe.to_dict()
        payload["published_at"] = time.time()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"artifact": str(path), "ref": recipe.ref,
                "digest": recipe.digest(), "checksum": recipe.checksum}

    # -- reads --------------------------------------------------------------- #
    def _path(self, name: str, version: str) -> Path:
        return self.root / name / f"{version}.json"

    def versions(self, name: str) -> list[str]:
        directory = self.root / name
        if not directory.exists():
            return []
        return sort_versions([p.stem for p in directory.glob("*.json")])

    def get(self, name: str, version: str | None = None) -> Recipe:
        versions = self.versions(name)
        if not versions:
            raise RecipeNotFound(name)
        chosen = version or versions[0]
        path = self._path(name, chosen)
        if not path.exists():
            raise RecipeNotFound(f"{name}@{chosen}")
        recipe = Recipe.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if not recipe.checksum:
            recipe.checksum = recipe.digest()
        return recipe

    def list(self) -> list[dict[str, Any]]:
        index = self._load_index()
        if not index.get("recipes"):
            index = self._rebuild_index()
        out: list[dict[str, Any]] = []
        for name, entry in sorted(index.get("recipes", {}).items()):
            versions = sort_versions(list(entry.get("versions", {})))
            if not versions:
                continue
            latest = versions[0]
            meta = entry["versions"][latest]
            out.append({
                "name": name,
                "latest": latest,
                "ref": f"{name}@{latest}",
                "versions": versions,
                "hosts": entry.get("hosts", []),
                "fields": meta.get("fields", 0),
                "tests": meta.get("tests", 0),
                "health": meta.get("health", "untested"),
                "checksum": meta.get("checksum", ""),
            })
        return out

    def for_host(self, host: str, *, prefer: str | None = None) -> Recipe | None:
        """Which recipe applies to a host (exact, then subdomain match)."""
        host = (host or "").lower().lstrip("www.")
        best: tuple[int, str, str] | None = None
        for row in self.list():
            for declared in row["hosts"]:
                declared = declared.lower()
                if host == declared:
                    score = 3
                elif host.endswith("." + declared):
                    score = 2
                elif declared in host:
                    score = 1
                else:
                    continue
                key = (score, row["latest"], row["name"])
                if best is None or key > best:
                    best = key
        if not best:
            return None
        return self.get(best[2], prefer or best[1])

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        q = (query or "").lower().strip()
        rows = self.list()
        if not q:
            return rows[:limit]
        def score(row: dict[str, Any]) -> int:
            hay = f"{row['name']} {' '.join(row['hosts'])}"
            return 2 if q in row["name"] else (1 if q in hay else 0)
        rows = sorted(rows, key=lambda r: (-score(r), r["name"]))
        return [r for r in rows if score(r) or not q][:limit]

    def validate_all(self) -> list[dict[str, Any]]:
        """Audit every installed recipe: integrity + its own tests."""
        out = []
        for row in self.list():
            recipe = self.get(row["name"])
            health = test_recipe(recipe) if recipe.tests else None
            out.append({
                "ref": recipe.ref,
                "checksum_ok": recipe.verify(),
                "problems": recipe.validate(),
                "health": health.to_dict() if health else None,
            })
        return out

    def stats(self) -> dict[str, Any]:
        rows = self.list()
        return {
            "recipes": len(rows),
            "versions": sum(len(r["versions"]) for r in rows),
            "hosts": len({h for r in rows for h in r["hosts"]}),
            "tested": sum(1 for r in rows if r["tests"]),
            "root": str(self.root),
        }

    def export_all(self) -> list[dict[str, Any]]:
        return [self.get(row["name"]).to_dict() for row in self.list()]


def load_recipe_file(path: str | Path) -> Recipe:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Recipe.from_dict(payload)


def install_many(registry: RecipeRegistry, recipes: Iterable[Recipe], *,
                 run_tests: bool = True) -> list[dict[str, Any]]:
    results = []
    for recipe in recipes:
        try:
            results.append(registry.install(recipe, run_tests=run_tests))
        except ValueError as exc:
            results.append({"installed": recipe.ref, "error": str(exc)})
    return results
