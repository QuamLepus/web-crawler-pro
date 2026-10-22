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


def test_crawl_job_listing_forwards_pagination_and_state_filter():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.url.query.decode()))
        return httpx.Response(200, json={"items": [], "total": 0, "page": 2, "page_size": 7})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        result = client.list_jobs(page=2, page_size=7, state="running")

    assert result["page"] == 2
    assert calls == [("GET", "/api/v1/jobs", "page=2&page_size=7&state=running")]


def test_research_cancellation_uses_encoded_tenant_endpoint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"research_id": "research_abc", "status": "cancelled"})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        result = client.research.cancel("research_aaaaaaaaaaaaaaaaaaaa")

    assert result["status"] == "cancelled"
    assert calls == [("POST", "/api/v1/research/research_aaaaaaaaaaaaaaaaaaaa/cancel")]


def test_watch_deletion_accepts_no_content_and_uses_tenant_endpoint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(204)

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        result = client.delete_watch("watch_aaaaaaaaaaaaaaaaaaaa")

    assert result == {}
    assert calls == [("DELETE", "/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa")]


def test_watch_listing_uses_tenant_endpoint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"items": [], "total": 0})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        result = client.list_watches()

    assert result == {"items": [], "total": 0}
    assert calls == [("GET", "/api/v1/watch")]


def test_watch_change_and_snapshot_limits_are_forwarded():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"items": [], "total": 0})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.watch_changes("watch_aaaaaaaaaaaaaaaaaaaa", limit=12)
        client.watch_snapshots("watch_aaaaaaaaaaaaaaaaaaaa", limit=6)

    assert calls == [
        ("GET", "/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/changes?limit=12"),
        ("GET", "/api/v1/watch/watch_aaaaaaaaaaaaaaaaaaaa/snapshots?limit=6"),
    ]


def test_studio_saved_query_methods_follow_tenant_api_contract():
    calls = []

    def handler(request):
        calls.append({
            "method": request.method,
            "path": request.url.raw_path.decode(),
            "body": request.read().decode() if request.content else None,
        })
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(201 if request.method == "POST" else 200,
                              json={"items": [], "total": 0})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.save_studio_query("Solar", "solar panels", limit=30, sources=["news"],
                                 group_by="source", metric="avg_score")
        client.list_studio_queries(page=2, page_size=7)
        assert client.delete_studio_query("saved/a") == {}

    assert calls == [
        {"method": "POST", "path": "/api/v1/studio/saved-queries",
         "body": '{"name":"Solar","q":"solar panels","limit":30,"sources":["news"],'
                 '"group_by":"source","metric":"avg_score"}'},
        {"method": "GET", "path": "/api/v1/studio/saved-queries?page=2&page_size=7",
         "body": None},
        {"method": "DELETE", "path": "/api/v1/studio/saved-queries/saved%2Fa",
         "body": None},
    ]


def test_semantic_search_forwards_filters_and_preserves_api_defaults():
    import json

    calls = []

    def handler(request):
        calls.append({"method": request.method, "path": request.url.path,
                      "body": json.loads(request.read())})
        return httpx.Response(200, json={"results": [], "took_ms": 1})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.semantic_search("solar panels", top_k=25,
                               filter={"language": "en", "year": 2025})
        client.semantic_search("clean energy")

    assert calls == [
        {"method": "POST", "path": "/api/v1/search",
         "body": {"query": "solar panels", "top_k": 25,
                  "filter": {"language": "en", "year": 2025}}},
        {"method": "POST", "path": "/api/v1/search", "body": {"query": "clean energy"}},
    ]


def test_own_search_index_levels_reads_roadmap_metadata():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"levels": [{"level": 0, "name": "starter"}]})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        assert client.get_search_index_levels() == {"levels": [{"level": 0, "name": "starter"}]}

    assert calls == [("GET", "/api/v1/search-index/levels")]


def test_own_search_index_spell_corrects_query_with_encoded_text():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={
            "query": "web inteligence", "corrected_query": "web intelligence",
            "corrections": [{"from": "inteligence", "to": "intelligence"}],
            "trained": True, "terms": 100,
        })

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        result = client.correct_search_index_query("web inteligence")

    assert result["corrected_query"] == "web intelligence"
    assert calls == [("GET", "/api/v1/search-index/spell?q=web+inteligence")]


def test_billing_methods_read_plans_and_current_tenant_usage():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        payload = {"tiers": {"starter": {}}} if request.url.path.endswith("/billing/plans") \
            else {"tenant": "tenant-1", "requests": 12}
        return httpx.Response(200, json=payload)

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        assert client.list_billing_plans() == {"tiers": {"starter": {}}}
        assert client.get_billing_usage() == {"tenant": "tenant-1", "requests": 12}

    assert calls == [
        ("GET", "/api/v1/billing/plans"),
        ("GET", "/api/v1/billing/usage"),
    ]


def test_marketplace_methods_search_listings_and_encode_dataset_ids():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json=[{"dataset_id": "ds_public", "status": "published"}])

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.list_marketplace_bounties(limit=7)
        client.search_marketplace_datasets(q="climate change", tag="energy",
                                           max_price_cents=500, limit=5)
        client.get_marketplace_dataset("dataset:1")
        client.get_marketplace_delivery("dlv_0123456789ab")

    assert calls == [
        ("GET", "/api/v1/marketplace/bounties?limit=7"),
        ("GET", "/api/v1/marketplace/datasets?q=climate+change&tag=energy&max_price_cents=500&limit=5"),
        ("GET", "/api/v1/marketplace/datasets/dataset%3A1"),
        ("GET", "/api/v1/marketplace/deliveries/dlv_0123456789ab"),
    ]


def test_provenance_methods_store_retrieve_and_export_tenant_records():
    import json

    calls = []
    record = {"record_id": "record:one", "source_url": "https://example.org/data",
              "fetched_at": "2026-10-23T00:00:00Z", "content_sha256": "a" * 64}

    def handler(request):
        calls.append({"method": request.method, "path": request.url.raw_path.decode(),
                      "body": json.loads(request.read()) if request.content else None})
        return httpx.Response(200, json={"record_id": "record:one", "stored": True})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.store_provenance_record(record)
        client.get_provenance_record("record:one")
        client.export_provenance_manifest("dataset-1", limit=9)

    assert calls == [
        {"method": "POST", "path": "/api/v1/provenance/records", "body": record},
        {"method": "GET", "path": "/api/v1/provenance/records/record%3Aone", "body": None},
        {"method": "POST", "path": "/api/v1/provenance/manifest?limit=9",
         "body": {"dataset_id": "dataset-1"}},
    ]


def test_discovery_job_methods_keep_crawling_behind_approval():
    import json

    calls = []

    def handler(request):
        calls.append({"method": request.method, "path": request.url.raw_path.decode(),
                      "body": json.loads(request.read()) if request.content else None})
        payload = [{"domain": "example.org"}] if request.url.path.endswith("/candidates") \
            else {"job_id": "disc_aaaaaaaaaaaa", "state": "awaiting_approval"}
        return httpx.Response(202 if request.method == "POST"
                              and request.url.path == "/api/v1/discovery" else 200,
                              json=payload)

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.create_discovery("AI medical devices", count=7,
                                seeds=["https://example.org"], language="en",
                                max_pages_per_site=3, per_domain=2)
        client.list_discoveries(page=2, page_size=7)
        client.get_discovery("disc_aaaaaaaaaaaa")
        client.discovery_candidates("disc_aaaaaaaaaaaa", only_selected=True)
        client.cancel_discovery("disc_aaaaaaaaaaaa")

    assert calls == [
        {"method": "POST", "path": "/api/v1/discovery",
         "body": {"topic": "AI medical devices", "count": 7,
                  "seeds": ["https://example.org"], "language": "en", "auto": False,
                  "max_pages_per_site": 3, "per_domain": 2, "obey_robots": True}},
        {"method": "GET", "path": "/api/v1/discovery?page=2&page_size=7", "body": None},
        {"method": "GET", "path": "/api/v1/discovery/disc_aaaaaaaaaaaa", "body": None},
        {"method": "GET",
         "path": "/api/v1/discovery/disc_aaaaaaaaaaaa/candidates?only_selected=true",
         "body": None},
        {"method": "POST", "path": "/api/v1/discovery/disc_aaaaaaaaaaaa/cancel",
         "body": None},
    ]


def test_discovery_review_methods_forward_explicit_approval_and_results():
    import json

    calls = []

    def handler(request):
        calls.append({"method": request.method, "path": request.url.raw_path.decode(),
                      "body": json.loads(request.read()) if request.content else None})
        if request.url.path.endswith("/results"):
            payload = {"sites_total": 1}
        elif "/candidates/" in request.url.path:
            payload = [{"domain": "example.org", "rejected": True}]
        else:
            payload = {"job_id": "disc_aaaaaaaaaaaa", "state": "crawling"}
        return httpx.Response(200, json=payload)

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.drop_discovery_candidate("disc_aaaaaaaaaaaa", "example.org")
        client.approve_discovery("disc_aaaaaaaaaaaa", ["example.org"])
        client.get_discovery_results("disc_aaaaaaaaaaaa")

    assert calls == [
        {"method": "DELETE",
         "path": "/api/v1/discovery/disc_aaaaaaaaaaaa/candidates/example.org", "body": None},
        {"method": "POST", "path": "/api/v1/discovery/disc_aaaaaaaaaaaa/approve",
         "body": {"domains": ["example.org"]}},
        {"method": "GET", "path": "/api/v1/discovery/disc_aaaaaaaaaaaa/results",
         "body": None},
    ]


def test_discovery_preview_forwards_dry_run_options_and_defaults():
    import json

    calls = []

    def handler(request):
        calls.append({"method": request.method, "path": request.url.path,
                      "body": json.loads(request.read())})
        return httpx.Response(200, json={"topic": "clean energy", "candidates": []})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.preview_discovery("AI medical devices", count=7,
                                 seeds=["https://example.org"], language="en", per_domain=2)
        client.preview_discovery("clean energy")

    assert calls == [
        {"method": "POST", "path": "/api/v1/discovery/preview",
         "body": {"topic": "AI medical devices", "count": 7,
                  "seeds": ["https://example.org"], "language": "en", "per_domain": 2}},
        {"method": "POST", "path": "/api/v1/discovery/preview",
         "body": {"topic": "clean energy"}},
    ]


def test_knowledge_graph_methods_follow_api_contract():
    import json

    calls = []

    def handler(request):
        calls.append({
            "method": request.method, "path": request.url.raw_path.decode(),
            "body": json.loads(request.read()) if request.content else None,
        })
        return httpx.Response(201 if request.method == "POST"
                              and request.url.path.endswith("/relations") else 200,
                              json={"entity_id": "ent_aaaaaaaaaaaaaaaaaaaaaaaa"})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.list_graph(limit=12)
        client.upsert_graph_entity("Open Knowledge", entity_type="Project",
                                   aliases=["OK"], properties={"region": "global"})
        client.get_graph_entity("ent_aaaaaaaaaaaaaaaaaaaaaaaa")
        client.add_graph_relation("ent_aaaaaaaaaaaaaaaaaaaaaaaa",
                                  "ent_bbbbbbbbbbbbbbbbbbbbbbbb", "supports",
                                  properties={"confidence": 0.9})

    assert calls == [
        {"method": "GET", "path": "/api/v1/graph?limit=12", "body": None},
        {"method": "POST", "path": "/api/v1/graph/entities",
         "body": {"name": "Open Knowledge", "entity_type": "Project", "aliases": ["OK"],
                  "properties": {"region": "global"}}},
        {"method": "GET", "path": "/api/v1/graph/entities/ent_aaaaaaaaaaaaaaaaaaaaaaaa",
         "body": None},
        {"method": "POST", "path": "/api/v1/graph/relations",
         "body": {"source_id": "ent_aaaaaaaaaaaaaaaaaaaaaaaa",
                  "target_id": "ent_bbbbbbbbbbbbbbbbbbbbbbbb", "relation": "supports",
                  "properties": {"confidence": 0.9}}},
    ]


def test_rag_query_encodes_query_and_forwards_optional_controls():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"answer": "Supported answer", "sources": []})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.rag_query("solar panels & storage", top_k=7, include_sources=False)
        client.rag_query("clean energy")

    assert calls == [
        ("GET", "/api/v1/search/rag?q=solar+panels+%26+storage&top_k=7&include_sources=false"),
        ("GET", "/api/v1/search/rag?q=clean+energy"),
    ]


def test_webhook_methods_handle_secret_creation_and_list_responses():
    import json

    calls = []

    def handler(request):
        body = json.loads(request.read()) if request.content else None
        calls.append({"method": request.method, "path": request.url.raw_path.decode(),
                      "body": body})
        if request.method == "POST" and request.url.path.endswith("/test"):
            return httpx.Response(200, json={"queued_deliveries": ["dlv_test"]})
        if request.method == "POST":
            return httpx.Response(201, json={"id": "012345abcdef", "secret": "whsec-test-only"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": "012345abcdef"})
        return httpx.Response(200, json=[{"id": "012345abcdef"}])

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        created = client.create_webhook("https://hooks.example/receiver",
                                        ["research.completed"], description="Research")
        listed = client.list_webhooks()
        tested = client.test_webhook("012345abcdef", event="research.completed",
                                     data={"research_id": "research_test"})
        deliveries = client.list_webhook_deliveries("012345abcdef", limit=12)
        deleted = client.delete_webhook("012345abcdef")

    assert created["id"] == "012345abcdef"
    assert listed == [{"id": "012345abcdef"}]
    assert tested == {"queued_deliveries": ["dlv_test"]}
    assert deliveries == [{"id": "012345abcdef"}]
    assert deleted == {"deleted": "012345abcdef"}
    assert calls == [
        {"method": "POST", "path": "/api/v1/webhooks",
         "body": {"url": "https://hooks.example/receiver", "events": ["research.completed"],
                  "description": "Research"}},
        {"method": "GET", "path": "/api/v1/webhooks", "body": None},
        {"method": "POST", "path": "/api/v1/webhooks/test",
         "body": {"subscription_id": "012345abcdef", "event": "research.completed",
                  "data": {"research_id": "research_test"}}},
        {"method": "GET", "path": "/api/v1/webhooks/012345abcdef/deliveries?limit=12",
         "body": None},
        {"method": "DELETE", "path": "/api/v1/webhooks/012345abcdef", "body": None},
    ]


def test_studio_analytics_and_snapshot_methods_follow_api_contract():
    import json

    calls = []

    def handler(request):
        body = json.loads(request.read()) if request.content else None
        calls.append({"method": request.method, "path": request.url.raw_path.decode(),
                      "body": body})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(201, json={"snapshot_id": "snapshot_aaaaaaaaaaaaaaaaaaaa"})
        return httpx.Response(200, json={"items": [], "total": 0})

    with Client("secret", base_url="https://api.example",
                transport=httpx.MockTransport(handler)) as client:
        client.studio_analytics(group_by="host", metric="avg_quality", source="wiki & docs",
                                limit=10, offset=2)
        client.create_studio_snapshot("Solar", "solar panels", [
            {"rank": 1, "url": "https://example.org", "score": 0.8}],
            group_by="source", metric="avg_score")
        client.list_studio_snapshots()
        client.get_studio_snapshot("snapshot_aaaaaaaaaaaaaaaaaaaa")
        assert client.delete_studio_snapshot("snapshot_aaaaaaaaaaaaaaaaaaaa") == {}

    assert calls == [
        {"method": "GET",
         "path": "/api/v1/studio/analytics?group_by=host&metric=avg_quality&source=wiki+%26+docs&limit=10&offset=2",
         "body": None},
        {"method": "POST", "path": "/api/v1/studio/snapshots",
         "body": {"name": "Solar", "q": "solar panels", "sources": [],
                  "group_by": "source", "metric": "avg_score",
                  "rows": [{"rank": 1, "url": "https://example.org", "score": 0.8}]}},
        {"method": "GET", "path": "/api/v1/studio/snapshots", "body": None},
        {"method": "GET", "path": "/api/v1/studio/snapshots/snapshot_aaaaaaaaaaaaaaaaaaaa",
         "body": None},
        {"method": "DELETE", "path": "/api/v1/studio/snapshots/snapshot_aaaaaaaaaaaaaaaaaaaa",
         "body": None},
    ]
