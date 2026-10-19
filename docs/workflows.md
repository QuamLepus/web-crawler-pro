# Crawl workflows

Workflows are tenant-scoped crawl recipes. Definitions accept one to ten
HTTP(S) crawl steps, with at most 25 pages per step. Legacy workflows remain
sequential and fail-fast. A workflow can opt into a bounded dependency DAG by
assigning each step a unique `step_id` and an explicit `depends_on` list. An
empty list makes a root step; dependencies must refer to known steps and form
an acyclic graph. Omitted `depends_on` retains the legacy previous-step default.

DAG steps are dispatched serially by one worker, not in parallel. A step runs
only after all dependencies complete. If a DAG step fails, its descendants are
marked skipped, independent branches continue, and the overall run ends failed.
Cancellation remains cooperative. Step definitions are snapshotted into each
run, so editing a recipe does not alter a run already queued or in progress.

All endpoints below require an authenticated tenant API key with crawl access.

| Method and path | Behavior |
|---|---|
| `POST /api/v1/workflows` | Create a recipe (`name`, `steps`, optional `schedule_interval_minutes`). |
| `GET /api/v1/workflows` and `GET /api/v1/workflows/{workflow_id}` | List or read the authenticated tenant's recipes. |
| `PUT /api/v1/workflows/{workflow_id}` | Update one or more of `name`, `steps`, or `schedule_interval_minutes`. A `null` schedule disables recurrence; an omitted field is unchanged. |
| `DELETE /api/v1/workflows/{workflow_id}` | Delete the definition and schedule. Returns `409` while any run is queued or running. Historical run records remain available under the tenant's run history. |
| `POST /api/v1/workflows/{workflow_id}/runs` | Queue a run using the current definition snapshot. |
| `GET /api/v1/workflows/runs` | List paginated runs; optional `q` matches all case-insensitive name tokens, or an exact run/workflow ID; `status` filters state, and `date_from`/`date_to` bound creation dates (inclusive). |
| `GET /api/v1/workflows/runs/{run_id}` | Read a run owned by the authenticated tenant. |
| `PATCH /api/v1/workflows/{workflow_id}/schedule` | Set or disable the recurring interval without replacing other definition fields. |

Run history is retained for 30 days. Unfiltered requests and date-only filters
use the tenant's creation-time sorted set; date bounds are inclusive. Status
filters use per-tenant, per-status sorted sets. Name search tokenizes the saved
workflow name and query case-insensitively and requires every query token to
match; exact run IDs and workflow IDs remain supported. Combined indexed filters
intersect the relevant sorted sets, then fetch only the requested page of run
records. Tenant IDs are part of every secondary-index key.

Status/name/workflow indexes are maintained when runs are created or change
status. Existing tenants are lazily backfilled on the first request that needs
one of those indexes; while the backfill lock is held, requests fall back to a
linear tenant scan. This is a one-time per-tenant cost after deployment. Date-
only and exact run-ID requests do not trigger backfill. Retention pruning and
index TTLs remain aligned with the run-history retention period.

The reproducible benchmark is at `scripts/benchmark_workflow_run_filters.py`.
It exercises the authenticated API route through FastAPI's test client over
fakeredis, with three repetitions per warm case and a separate first-query
backfill measurement:

| Synthetic runs | Unfiltered page p50 | Name-token search p50 | Status filter p50 | Four-day date filter p50 | Name-search records read |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 6.51 ms | 6.50 ms | 9.15 ms | 6.87 ms | 1 |
| 10,000 | 6.37 ms | 17.90 ms | 15.51 ms | 6.27 ms | 10 |
| 50,000 | 7.40 ms | 80.84 ms | 85.00 ms | 7.80 ms | 20 |

The name query matched 1, 10, and 50 runs respectively; the status query matched
200, 2,000, and 10,000, and the four-day date query matched 136, 1,336, and
6,668. Warm filtered queries returned only page-sized records (up to 20); a
selective name query returned only its matching rows. At 50,000 records the
first query requiring indexes took 11.08 seconds and read the tenant's 50,000
records to build them (plus the returned page). This one-time cold cost is
important when planning upgrades and should be measured on production Redis.

These are same-process synthetic fakeredis/TestClient results—not production
Redis latency, memory use, or concurrent-load forecasts. For historical
comparison, the pre-index scan baseline (three repetitions) had sparse-name /
status / four-day-date p50 latencies of 48 / 43 / 47 ms at 1,000 runs, 345 / 398
/ 352 ms at 10,000, and 1,741 / 1,811 / 1,957 ms at 50,000; every filtered
query read all retained runs.

The web console provides a simple URL-list form for sequential recipes, an
advanced JSON editor for DAG step definitions, and a read-only visual dependency
preview. The preview colors and labels nodes with the latest run's per-step status
when available; a selector lets users choose another recent run for that workflow.
Its saved-workflow editor retains per-step IDs, dependencies,
page limits, and robots settings. Recent runs show settled, active, queued,
failed, skipped, and cancelled step counts plus each step's status and dependencies.
Recent run history is paginated (20 runs per page); DAG selectors show runs on
the current page, and a selected historical overlay remains available while
browsing other pages. The newest page is selected by default.
The API, TypeScript/Python SDKs, and MCP tools expose the same definition
controls.

Definition edits, run creation, schedule changes, deletion, and scheduled dispatch
coordinate through a short-lived Redis lock. This prevents a scheduled dispatch
from racing with an edit or deletion; if the lock is held, callers receive `409`
and can retry. Stale active-run index entries are pruned when a run record has
expired or reached a terminal status. Redis must be available to API and worker
processes. Scheduled runs additionally require Celery Beat and workflow workers.

Cancellation is cooperative: a crawl step already executing is allowed to
finish before the workflow stops. Deletion does not delete historical run data,
and is rejected rather than silently cancelling queued or running work. Workflows still do not support parallel task execution, arbitrary code,
conditional predicates, or mid-request crawl interruption.
