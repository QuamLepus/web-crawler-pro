# Sequential crawl workflows

Workflows are tenant-scoped, ordered crawl recipes. They are not a general
purpose workflow engine: definitions accept one to ten HTTP(S) crawl steps,
with at most 25 pages per step. Step definitions are snapshotted into each run,
so editing a recipe does not alter a run already queued or in progress.

All endpoints below require an authenticated tenant API key with crawl access.

| Method and path | Behavior |
|---|---|
| `POST /api/v1/workflows` | Create a recipe (`name`, `steps`, optional `schedule_interval_minutes`). |
| `GET /api/v1/workflows` and `GET /api/v1/workflows/{workflow_id}` | List or read the authenticated tenant's recipes. |
| `PUT /api/v1/workflows/{workflow_id}` | Update one or more of `name`, `steps`, or `schedule_interval_minutes`. A `null` schedule disables recurrence; an omitted field is unchanged. |
| `DELETE /api/v1/workflows/{workflow_id}` | Delete the definition and schedule. Returns `409` while any run is queued or running. Historical run records remain available under the tenant's run history. |
| `POST /api/v1/workflows/{workflow_id}/runs` | Queue a run using the current definition snapshot. |
| `PATCH /api/v1/workflows/{workflow_id}/schedule` | Set or disable the recurring interval without replacing other definition fields. |

The web console supports renaming and deletion; step-by-step definition edits are
available through the API, TypeScript/Python SDKs, and MCP tools so that
per-step page limits and robots settings are not silently flattened by a
simplified form.

Definition edits, run creation, schedule changes, deletion, and scheduled dispatch
coordinate through a short-lived Redis lock. This prevents a scheduled dispatch
from racing with an edit or deletion; if the lock is held, callers receive `409`
and can retry. Stale active-run index entries are pruned when a run record has
expired or reached a terminal status. Redis must be available to API and worker
processes. Scheduled runs additionally require Celery Beat and workflow workers.

Cancellation is cooperative: a crawl step already executing is allowed to
finish before the workflow stops. Deletion does not delete historical run data,
and is rejected rather than silently cancelling queued or running work. This
increment adds editing and deletion, but not DAG branching, arbitrary code, or
mid-request crawl interruption.
