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
| `GET /api/v1/workflows/runs` | List paginated runs; optional `q` searches workflow name/run ID/workflow ID, `status` filters state, and `date_from`/`date_to` bound creation dates (inclusive). |
| `GET /api/v1/workflows/runs/{run_id}` | Read a run owned by the authenticated tenant. |
| `PATCH /api/v1/workflows/{workflow_id}/schedule` | Set or disable the recurring interval without replacing other definition fields. |

Filtered run-history requests scan the tenant's run index to compute an exact
match count; unfiltered requests use Redis range pagination. A reproducible
in-memory baseline is available at `scripts/benchmark_workflow_run_filters.py`.
It ran the authenticated API route through FastAPI's test client over fakeredis,
with three repetitions per case:

| Synthetic runs | Unfiltered page p50 | Sparse name search p50 | Status filter p50 | Four-day date filter p50 | Records read per filtered query |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 6.32 ms | 48.12 ms | 43.06 ms | 47.24 ms | 1,000 |
| 10,000 | 8.12 ms | 345.40 ms | 398.08 ms | 351.75 ms | 10,000 |
| 50,000 | 7.90 ms | 1,740.69 ms | 1,810.76 ms | 1,957.23 ms | 50,000 |

This is a same-process synthetic baseline, not a production Redis or concurrent
load test. It shows filtered latency and record reads growing linearly while
unfiltered range pagination remains approximately flat. Decision: add
filter-oriented tenant indexes before treating these filters as suitable for
high-volume run histories; sparse name search in particular is already about
1.7 seconds at 50,000 synthetic records. The in-memory measurements should not
be used as a production latency forecast.

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
