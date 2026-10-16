"""v1 router: Data Studio — living datasets, recipes and exports.

    GET    /v1/datasets                  list with quality + latest version
    POST   /v1/datasets                  create
    GET    /v1/datasets/{ref}            one dataset (all versions)
    DELETE /v1/datasets/{ref}
    POST   /v1/datasets/{ref}/rows       append rows (a new immutable version)
    POST   /v1/datasets/{ref}/refresh    append only if the data changed
    GET    /v1/datasets/{ref}/rows       read rows (paginated, filtered)
    POST   /v1/datasets/{ref}/query      aggregate/summarise (the Studio digest)
    GET    /v1/datasets/{ref}/diff       diff two versions
    GET    /v1/datasets/{ref}/export     csv / json / jsonl / markdown
    GET    /v1/datasets/{ref}/manifest   the signed passport for a version
    POST   /v1/datasets/{ref}/verify     verify a version's checksum
    GET    /v1/recipes                   installed extraction recipes
    POST   /v1/recipes                   install a recipe (checksum-verified)
    GET    /v1/recipes/{name}            one recipe + its health
    POST   /v1/recipes/{name}/test       run the recipe's own fixtures
    POST   /v1/recip…/extract            apply the best recipe to HTML
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep

logger = logging.getLogger("wcp.api.studio")
router = APIRouter(tags=["studio"])

CONTENT_TYPES = {"csv": "text/csv", "json": "application/json",
                 "jsonl": "application/x-ndjson", "markdown": "text/markdown"}


class DatasetBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=1000)
    primary_key: str = "url"
    kind: str = "custom"
    license_policy: str = "redistributable-only"
    tags: list[str] = Field(default_factory=list)


class RowsBody(BaseModel):
    rows: list[dict[str, Any]] = Field(..., max_length=200_000)
    note: str = ""
    source: str = "api"


class QueryBody(BaseModel):
    numeric_fields: list[str] = Field(default_factory=list)
    top: int = Field(5, ge=1, le=25)
    filters: dict[str, Any] = Field(default_factory=dict)
    version: str = ""
    limit: int = Field(5000, ge=1, le=200_000)


class AskBody(BaseModel):
    """A question in plain English — answered by the parse, not by a model."""

    question: str = Field(..., min_length=2, max_length=500)
    version: str = ""
    limit: int = Field(5000, ge=1, le=200_000)


class RecipeBody(BaseModel):
    recipe: dict[str, Any]
    run_tests: bool = True


class ExtractBody(BaseModel):
    html: str = Field(..., max_length=3_000_000)
    url: str = ""
    recipe: str = ""


def _platform(request: Request):
    return request.app.state.services.platform


def _dataset(request: Request, ctx: dict, ref: str):
    dataset = _platform(request).datasets.get(ctx["tenant"], ref)
    if dataset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"dataset {ref!r} not found")
    return dataset


# --------------------------------------------------------------------------- #
# datasets
# --------------------------------------------------------------------------- #
@router.get("/datasets", summary="List datasets with quality and versions")
async def list_datasets(request: Request, ctx: CtxDep,
                        limit: Annotated[int, Query(ge=1, le=500)] = 100
                        ) -> dict[str, Any]:
    store = _platform(request).datasets
    tenant = ctx["tenant"]
    datasets = store.list(tenant, limit=limit)
    return {
        "items": [d.to_dict() for d in datasets],
        "stats": store.stats(tenant),
    }


@router.post("/datasets", status_code=status.HTTP_201_CREATED,
             summary="Create a dataset")
async def create_dataset(body: DatasetBody, request: Request, ctx: CtxDep
                         ) -> dict[str, Any]:
    store = _platform(request).datasets
    dataset = store.create(ctx["tenant"], body.name, description=body.description,
                           primary_key=body.primary_key, kind=body.kind,
                           license_policy=body.license_policy, tags=body.tags)
    return dataset.to_dict()


@router.get("/datasets/{ref:path}/latest", include_in_schema=False)
async def dataset_latest(ref: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    return {"name": dataset.name, "latest": (dataset.latest.to_dict()
                                             if dataset.latest else None)}


@router.get("/datasets/{ref:path}/rows", summary="Read rows (filtered, paginated)")
async def dataset_rows(ref: str, request: Request, ctx: CtxDep,
                       version: str = "", limit: int = 1000, offset: int = 0,
                       q: str = "") -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    rows = _platform(request).datasets.rows(dataset, version or None, limit=200_000)
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in str(r).lower()]
    total = len(rows)
    window = rows[offset: offset + max(1, min(limit, 5000))]
    fields = sorted({k for row in rows[:200] for k in row})
    return {"name": dataset.name, "version": version or
            (dataset.latest.version if dataset.latest else ""),
            "rows": window, "total": total, "offset": offset,
            "fields": fields, "columns": list(window[0].keys()) if window else fields}


@router.post("/datasets/{ref:path}/rows", summary="Append rows as a new version")
async def append_rows(ref: str, body: RowsBody, request: Request, ctx: CtxDep
                      ) -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    version = _platform(request).datasets.add_version(
        dataset, body.rows, note=body.note, source=body.source)
    return {"dataset": dataset.name, "version": version.to_dict()}


@router.post("/datasets/{ref:path}/refresh", summary="Living dataset refresh (only if changed)")
async def refresh_dataset(ref: str, body: RowsBody, request: Request, ctx: CtxDep
                          ) -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    result = _platform(request).datasets.refresh(dataset, body.rows,
                                                 note=body.note,
                                                 source=body.source)
    return {"dataset": dataset.name, **result}


@router.post("/datasets/{ref:path}/query", summary="Aggregate / statistical digest")
async def query_dataset(ref: str, body: QueryBody, request: Request, ctx: CtxDep
                        ) -> dict[str, Any]:
    platform = _platform(request)
    dataset = _dataset(request, ctx, ref)
    rows = platform.datasets.rows(dataset, body.version or None, limit=body.limit)
    if body.filters:
        def matches(row: dict[str, Any]) -> bool:
            return all(str(row.get(k)) == str(v) for k, v in body.filters.items())

        rows = [r for r in rows if matches(r)]
    from src.studio.service import quality_report, summarise

    numeric = body.numeric_fields or [k for k, v in (rows[0] if rows else {}).items()
                                      if isinstance(v, (int, float))]
    return {
        "dataset": dataset.name,
        "version": body.version or (dataset.latest.version if dataset.latest else ""),
        "rows": len(rows),
        "summary": summarise(rows, numeric_fields=numeric, top=body.top),
        "quality": quality_report(rows, key=dataset.primary_key),
        "sample": rows[:25],
    }


@router.post("/datasets/{ref:path}/ask",
             summary="Ask a dataset a question in plain English")
async def ask_dataset(ref: str, body: AskBody, request: Request, ctx: CtxDep
                      ) -> dict[str, Any]:
    """Deterministic question answering: the parse and the caveats come back
    with the number, so an answer can be checked instead of trusted."""
    from src.studio.nlq import answer as nlq_answer
    from src.studio.nlq import caveats_for, examples
    from src.studio.service import quality_report

    platform = _platform(request)
    dataset = _dataset(request, ctx, ref)
    rows = platform.datasets.rows(dataset, body.version or None, limit=body.limit)
    target = body.version or (dataset.latest.version if dataset.latest else "")
    result = nlq_answer(body.question, rows, dataset=dataset.name, version=target,
                        limit=body.limit)
    result["caveats"] = list(result.get("caveats") or []) + caveats_for(
        quality_report(rows, key=dataset.primary_key), rows)
    result["caveats"] = result["caveats"][:6]
    if not result.get("understood"):
        result["examples"] = examples(rows)
    return result


@router.get("/datasets/{ref:path}/questions",
            summary="Questions this dataset can actually answer")
async def dataset_questions(ref: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    from src.studio.nlq import examples

    platform = _platform(request)
    dataset = _dataset(request, ctx, ref)
    rows = platform.datasets.rows(dataset, limit=500)
    return {"dataset": dataset.name, "questions": examples(rows),
            "columns": list(rows[0].keys()) if rows else []}


@router.get("/datasets/{ref:path}/diff", summary="Diff two versions (row-level)")
async def dataset_diff(ref: str, request: Request, ctx: CtxDep,
                       from_version: str = Query(..., alias="from"),
                       to_version: str = Query("latest", alias="to"),
                       max_rows: int = 200) -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    target = dataset.latest.version if (to_version == "latest" and dataset.latest) \
        else to_version
    try:
        diff = _platform(request).datasets.diff(dataset, from_version, target,
                                                max_rows=max_rows)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return diff.to_dict()


@router.get("/datasets/{ref:path}/export", summary="Export a version")
async def export_dataset(ref: str, request: Request, ctx: CtxDep,
                         fmt: str = "csv", version: str = "", q: str = "",
                         limit: int = 100_000):
    dataset = _dataset(request, ctx, ref)
    if fmt not in CONTENT_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"format must be one of {sorted(CONTENT_TYPES)}")
    payload, ctype = _platform(request).datasets.export(
        dataset, version=version or None, fmt=fmt, limit=limit, filter_query=q)
    filename = f"{dataset.name}-{version or 'latest'}.{fmt}"
    if fmt == "markdown":
        return PlainTextResponse(payload, media_type=ctype,
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{filename}"'})
    return Response(content=payload, media_type=ctype,
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@router.get("/datasets/{ref:path}/manifest", summary="Signed passport for a version")
async def dataset_manifest(ref: str, request: Request, ctx: CtxDep,
                           version: str = "") -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    if version and dataset.latest:
        target = next((v for v in dataset.versions if v.version == version), None)
        stored = target.manifest if target else None
        if stored:
            return stored
    return _platform(request).provenance_manifest(f"watch:{dataset.watch_id}"
                                                  if dataset.watch_id else dataset.name,
                                                  tenant=ctx["tenant"],
                                                  version=version)


@router.post("/datasets/{ref:path}/verify", summary="Verify a version's checksum")
async def verify_dataset(ref: str, request: Request, ctx: CtxDep,
                         version: str = "") -> dict[str, Any]:
    dataset = _dataset(request, ctx, ref)
    target = next((v for v in dataset.versions
                   if v.version == (version or (dataset.latest.version
                                                if dataset.latest else ""))), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    from src.studio.service import _file_checksum  # noqa: PLC2701 - internal helper

    from pathlib import Path

    actual = _file_checksum(Path(target.path))
    return {"dataset": dataset.name, "version": target.version,
            "recorded_checksum": target.checksum, "actual_checksum": actual,
            "ok": actual == target.checksum}


@router.delete("/datasets/{ref:path}", summary="Delete a dataset and its files")
async def delete_dataset(ref: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    platform = _platform(request)
    dataset = _dataset(request, ctx, ref)
    platform.datasets.delete(dataset)
    try:
        platform.audit.log("dataset.delete", tenant=ctx["tenant"],
                           actor=str(ctx.get("user", "")), dataset=dataset.name)
    except Exception:  # noqa: BLE001
        pass
    return {"deleted": dataset.name}


# --------------------------------------------------------------------------- #
# recipes
# --------------------------------------------------------------------------- #
@router.get("/recipes", summary="Installed extraction recipes")
async def list_recipes(request: Request, ctx: CtxDep, q: str = "") -> dict[str, Any]:
    registry = _platform(request).recipes
    return {"items": registry.search(q), "stats": registry.stats()}


@router.post("/recipes", status_code=status.HTTP_201_CREATED,
             summary="Install a recipe (checksum + tests enforced)")
async def install_recipe(body: RecipeBody, request: Request, ctx: CtxDep
                         ) -> dict[str, Any]:
    from src.recipes.models import Recipe

    registry = _platform(request).recipes
    recipe = Recipe.from_dict(body.recipe)
    try:
        result = registry.install(recipe, run_tests=body.run_tests)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    try:
        _platform(request).audit.log("recipe.install", tenant=ctx["tenant"],
                                     actor=str(ctx.get("user", "")),
                                     recipe=recipe.ref)
    except Exception:  # noqa: BLE001
        pass
    return result


@router.get("/recipes/{name}", summary="One recipe and its test health")
async def get_recipe(name: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    registry = _platform(request).recipes
    try:
        recipe = registry.get(name)
    except Exception:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"recipe {name!r} not found")
    from src.recipes.runtime import test_recipe

    return {**recipe.to_dict(),
            "problems": recipe.validate(),
            "health": test_recipe(recipe).to_dict() if recipe.tests else None,
            "versions": registry.versions(name)}


@router.post("/recipes/{name}/test", summary="Run the recipe's own fixtures")
async def test_recipe_endpoint(name: str, request: Request, ctx: CtxDep
                               ) -> dict[str, Any]:
    try:
        return _platform(request).recipe_test(name)
    except Exception:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"recipe {name!r} not found")


@router.delete("/recipes/{name}", summary="Remove a recipe version")
async def delete_recipe(name: str, request: Request, ctx: CtxDep,
                        version: str = "") -> dict[str, Any]:
    removed = _platform(request).recipes.remove(name, version or None)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"recipe {name!r} not found")
    return {"removed": removed, "name": name, "version": version or "all"}


@router.post("/recipes/extract", summary="Apply the matching recipe to HTML")
async def extract_with_recipe(body: ExtractBody, request: Request, ctx: CtxDep
                              ) -> dict[str, Any]:
    result = _platform(request).extract_with_recipe(body.html, url=body.url,
                                                    name=body.recipe)
    if not result.get("ok") and result.get("reason"):
        return result
    return result
