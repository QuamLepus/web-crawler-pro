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
the typed URL, page-limit, and robots-policy shape; dictionaries matching the
REST API step schema are also accepted.

For `update`, omitted parameters remain unchanged. In particular,
`client.workflows.update(id, schedule_interval_minutes=None)` explicitly turns
off recurrence, while omitting that argument preserves the current schedule.
The API returns an HTTP `409` if deletion is attempted while a run is queued or
running. Historical runs are retained when a workflow is deleted.

The SDK does not start Celery workers or Beat. Recurring and asynchronous
workflow execution requires those services to be configured in the deployment.
For other API methods, see `Client` and `ResearchResource` in the package.
