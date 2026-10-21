import httpx
import pytest

from sdks.python.webintelligence import Client, WebIntelligenceError


def test_sdk_request_contract_and_auth_header():
    seen = {}
    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-API-Key")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"answer": "supported", "citations": []})
    with Client("secret", base_url="https://api.example", transport=httpx.MockTransport(handler)) as client:
        assert client.ask("what is indexed?")["answer"] == "supported"
    assert seen["url"] == "https://api.example/api/v1/ask"
    assert seen["key"] == "secret"
    assert '"question":"what is indexed?"' in seen["body"]


def test_research_resource_uses_current_api_contract():
    seen = {}
    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(202, json={"research_id": "research_x", "status": "queued"})
    with Client("secret", base_url="https://api.example", transport=httpx.MockTransport(handler)) as client:
        result = client.research.create("AI chip market", sources=4,
                                       max_pages_per_source=2)
    assert result["status"] == "queued"
    assert seen["path"] == "/api/v1/research"
    assert '"sources":4' in seen["body"]


def test_workflow_resource_create_update_and_delete_contracts():
    import json

    from sdks.python.webintelligence import WorkflowStep

    calls = []

    def handler(request):
        calls.append({
            "method": request.method,
            "raw_path": request.url.raw_path.decode(),
            "body": json.loads(request.read()) if request.content else None,
            "query": request.url.query.decode(),
        })
        return httpx.Response(200, json={"workflow_id": "workflow_abcd", "deleted": True})

    with Client("secret", base_url="https://api.example", transport=httpx.MockTransport(handler)) as client:
        client.workflows.create("Daily", [WorkflowStep("https://example.org", max_pages=4)],
                                schedule_interval_minutes=60)
        client.workflows.update("workflow/a", name="Renamed", steps=[
            WorkflowStep("https://new.example", obey_robots=False)])
        client.workflows.update("workflow/a", schedule_interval_minutes=None)
        client.workflows.delete("workflow/a")

    assert calls[0]["method"] == "POST"
    assert calls[0]["raw_path"] == "/api/v1/workflows"
    assert calls[0]["body"] == {"name": "Daily", "schedule_interval_minutes": 60,
                                 "steps": [{"url": "https://example.org", "max_pages": 4,
                                            "obey_robots": True}]}
    assert calls[1]["method"] == "PUT"
    assert calls[1]["raw_path"] == "/api/v1/workflows/workflow%2Fa"
    assert calls[1]["body"] == {"name": "Renamed", "steps": [
        {"url": "https://new.example", "max_pages": 3, "obey_robots": False}]}
    assert calls[2]["body"] == {"schedule_interval_minutes": None}
    assert calls[3]["method"] == "DELETE"
    assert calls[3]["raw_path"] == "/api/v1/workflows/workflow%2Fa"


def test_workflow_step_serializes_explicit_dependencies():
    import json

    from sdks.python.webintelligence import WorkflowStep

    captured = {}

    def handler(request):
        captured["steps"] = json.loads(request.read())["steps"]
        return httpx.Response(201, json={"workflow_id": "workflow_dag"})

    with Client("secret", base_url="https://api.example", transport=httpx.MockTransport(handler)) as client:
        client.workflows.create("DAG", [
            WorkflowStep("https://root.example", step_id="root", depends_on=[]),
            WorkflowStep("https://child.example", step_id="child", depends_on=["root"]),
        ])
    assert captured["steps"] == [
        {"url": "https://root.example", "max_pages": 3, "obey_robots": True,
         "step_id": "root", "depends_on": []},
        {"url": "https://child.example", "max_pages": 3, "obey_robots": True,
         "step_id": "child", "depends_on": ["root"]},
    ]


def test_workflow_resource_lists_runs_schedules_and_cancels():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.url.query.decode(),
                      request.read().decode()))
        return httpx.Response(200, json={"items": [], "total": 0})

    with Client("secret", base_url="https://api.example", transport=httpx.MockTransport(handler)) as client:
        client.workflows.list(page=2, page_size=7)
        client.workflows.get("workflow_a")
        client.workflows.set_schedule("workflow_a", None)
        client.workflows.run("workflow_a")
        client.workflows.list_runs(page=3, page_size=4)
        client.workflows.get_run("wfrun_a")
        client.workflows.cancel_run("wfrun_a")
        with pytest.raises(ValueError):
            client.workflows.update("workflow_a")

    assert calls == [
        ("GET", "/api/v1/workflows", "page=2&page_size=7", ""),
        ("GET", "/api/v1/workflows/workflow_a", "", ""),
        ("PATCH", "/api/v1/workflows/workflow_a/schedule", "",
         '{"interval_minutes":null}'),
        ("POST", "/api/v1/workflows/workflow_a/runs", "", ""),
        ("GET", "/api/v1/workflows/runs", "page=3&page_size=4", ""),
        ("GET", "/api/v1/workflows/runs/wfrun_a", "", ""),
        ("POST", "/api/v1/workflows/runs/wfrun_a/cancel", "", ""),
    ]


def test_sdk_redacts_transport_errors_and_surfaces_http_status():
    def handler(_request):
        return httpx.Response(401, json={"error": {"message": "authentication required"}})
    client = Client("top-secret", transport=httpx.MockTransport(handler))
    with pytest.raises(WebIntelligenceError) as exc:
        client.ask("query")
    assert exc.value.status_code == 401
    assert "top-secret" not in str(exc.value)
    client.close()


def test_crawl_job_cancellation_uses_encoded_tenant_endpoint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"job_id": "job_a", "state": "cancelled"})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        result = client.cancel_job("job/a")

    assert result["state"] == "cancelled"
    assert calls == [("POST", "/api/v1/jobs/job%2Fa/cancel")]
