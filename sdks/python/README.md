# Web Intelligence Python SDK

Early synchronous Python client for the Web Intelligence OS REST API. Requires
Python 3.10+ and `httpx`.

```bash
pip install -e sdks/python
```

## Quick start

```python
from webintelligence import Client, WorkflowStep

with Client(api_key="YOUR_API_KEY", base_url="https://your-host") as client:
    created = client.workflows.create(
        "Morning refresh",
        [WorkflowStep("https://example.org/news", max_pages=4)],
        schedule_interval_minutes=60,
    )
    workflow_id = created["workflow_id"]
    run = client.workflows.run(workflow_id)
    print(client.workflows.get_run(run["run_id"])["status"])
```

`client.workflows` supports `create`, `list`, `get`, `update`, `delete`,
`set_schedule`, `run`, `list_runs`, `get_run`, and `cancel_run`. Methods use
snake_case payload fields and escape IDs in URL paths. `WorkflowStep` provides
the typed URL, page-limit, robots-policy, and optional `step_id`/`depends_on`
shape; dictionaries matching the REST API step schema are also accepted. Omit
dependencies for legacy sequential order; use `depends_on=[]` for a DAG root.
DAG nodes are processed serially, and failure skips descendants while independent
branches continue.

For `update`, omitted parameters remain unchanged. In particular,
`client.workflows.update(id, schedule_interval_minutes=None)` explicitly turns
off recurrence, while omitting that argument preserves the current schedule.
The API returns an HTTP `409` if deletion is attempted while a run is queued or
running. Historical runs are retained when a workflow is deleted.

The SDK does not start Celery workers or Beat. Recurring and asynchronous
workflow execution requires those services to be configured in the deployment.
Discovery methods include `create_discovery` (queues candidate generation for human review, quota charged by requested count, no crawl), `list_discoveries`, `get_discovery`, `discovery_candidates`, `drop_discovery_candidate`, `cancel_discovery`, `approve_discovery` (starts crawls for explicitly selected domains), `get_discovery_results`, and `preview_discovery` (provider dry-run that creates no job). The client also exposes `list_graph`, `upsert_graph_entity`, `get_graph_entity`, and `add_graph_relation` for tenant-isolated graph data (exact normalized alias matching, not fuzzy resolution), plus `semantic_search`, `rag_query`, `create_crawl`, `list_jobs`, `get_job`, and `cancel_job`; `list_jobs`
supports bounded pagination and optional state filtering. Crawl
cancellation is cooperative, so an in-flight page may finish before the job
stops. Watch methods include `list_watches`, `create_watch`, `run_watch`, `watch_changes`,
`watch_snapshots`, and `delete_watch`; change history accepts up to 100 items,
snapshots up to 20, and stopping a watch retains its snapshots. Snapshot history is tenant-scoped,
capped at 20 versions, and
text-only. `client.research` supports `create`, `get`, paginated `list`, and
cooperative `cancel`; cancellation stops at the next source boundary, so an
active source crawl may finish first. The client also exposes `save_studio_query`,
`list_studio_queries`, and `delete_studio_query` for tenant-scoped saved Data Studio queries,
plus `studio_analytics` and create/list/get/delete methods for bounded result snapshots.
Webhook methods cover create/list/delete, signed test delivery, and recent delivery attempts.
Webhook creation returns a signing secret once; store it securely and avoid logging it.
`get_search_index_levels()` reads the platform index build-level roadmap without querying the index. Billing methods read public plan details and the authenticated tenant's current-period usage; they do not create checkout sessions or change a plan. Marketplace methods search published dataset listings, read published listings or drafts owned by the authenticated tenant, and list open public bounties. These methods are read-only and never purchase, submit, or award work. Provenance methods store and retrieve caller-supplied records and export manifests when the API signing key is configured; records do not fetch sources or verify permissions.
For other API methods, see `Client` and `ResearchResource` in the package.
