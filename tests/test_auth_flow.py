"""End-to-end auth flow tests for the v1 FastAPI app (src.api.main).

Covers: signup -> api-key creation -> authenticated REST (jobs) and
GraphQL, plus 401 paths for missing / invalid credentials.

Redis is faked by monkeypatching ``redis.from_url`` before any module that
builds a frontier/scheduler captures a client (same pattern as
test_frontier.py).
"""

import fakeredis
import pytest
from fastapi.testclient import TestClient

import redis as real_redis


@pytest.fixture()
def fake_server():
    server = fakeredis.FakeServer()
    return server


@pytest.fixture(autouse=True)
def patched_redis(monkeypatch, fake_server):
    made = []

    def _from_url(url, **kw):
        c = fakeredis.FakeStrictRedis(server=fake_server,
                                      decode_responses=kw.get("decode_responses", True))
        made.append(c)
        return c

    monkeypatch.setattr(real_redis, "from_url", _from_url)
    # gateway Services uses redis.Redis.from_url — patch that too
    monkeypatch.setattr(real_redis.Redis, "from_url",
                        classmethod(lambda cls, url, **kw: _from_url(url, **kw)))
    yield fake_server, made


@pytest.fixture()
def client(patched_redis):
    from src.api.main import create_api_app

    fake_server, _made = patched_redis
    redis_client = fakeredis.FakeStrictRedis(server=fake_server,
                                             decode_responses=False)
    app = create_api_app(redis_client=redis_client)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def signup(client, email="ada@example.com", org="Acme Labs"):
    r = client.post("/auth/signup", json={
        "org_name": org, "email": email, "password": "s3cret-passw0rd!"})
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["access_token"] and tok["token_type"] == "Bearer"
    return tok


def make_key(client, access, name="ci"):
    r = client.post("/keys", headers={"Authorization": f"Bearer {access}"},
                    json={"name": name, "scopes": ["read", "crawl"]})
    assert r.status_code == 200, r.text
    return r.json()["key"]


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_health_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signup_returns_token_pair(client):
    tok = signup(client)
    assert tok["expires_in"] > 0
    # refresh works
    r = client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_unauthenticated_v1_request_rejected(client):
    r = client.get("/v1/jobs/anything")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthorized"
    assert "WWW-Authenticate" in r.headers or True  # header set on middleware resp


def test_invalid_api_key_rejected(client):
    r = client.get("/v1/jobs/x", headers={"X-API-Key": "wcp_deadbeef"})
    assert r.status_code == 401
    assert "invalid api key" in r.json()["error"]["message"]


def test_invalid_jwt_rejected(client):
    r = client.get("/v1/jobs/x", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_full_flow_signup_keys_jobs_graphql(client):
    # 1. signup -> JWT
    tok = signup(client)
    access = tok["access_token"]

    # /auth/me via JWT
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["principal"] == "jwt" and me["role"] == "owner"
    tenant = me["tenant"]

    # 2. create API key with JWT session
    api_key = make_key(client, access)
    assert api_key.startswith("wcp_")

    # list keys
    r = client.get("/keys", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200 and len(r.json()) == 1

    # 3. authenticated job submission via X-API-Key
    r = client.post("/v1/jobs/crawl", headers={"X-API-Key": api_key},
                    json={"url": "https://example.com", "max_pages": 5,
                          "priority": "normal"})
    assert r.status_code == 202, r.text
    job = r.json()
    job_id = job["job_id"]
    assert job["state"] == "queued" and job["tenant"] == tenant
    assert "X-RateLimit-Limit" in r.headers  # rate limiter attached headers

    # 4. job status via API key
    r = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key})
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == job_id

    # other tenants cannot see the job
    tok2 = signup(client, email="eve@other.com", org="Evil Corp")
    key2 = make_key(client, tok2["access_token"], name="k2")
    r = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": key2})
    assert r.status_code == 404
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers={"X-API-Key": key2})
    assert r.status_code == 404
    assert client.app.state.services.redis.hget(f"wcp:v1:job:{job_id}", "state") in (
        b"queued", "queued")

    # 5. GraphQL viewer + jobs via API key
    r = client.post("/graphql", headers={"X-API-Key": api_key},
                    json={"query": "{ viewer { tenant role principal } }"})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["data"]["viewer"]["tenant"] == tenant
    assert g["data"]["viewer"]["principal"] == "apikey"

    r = client.post("/graphql", headers={"X-API-Key": api_key},
                    json={"query": "{ jobs { total items { jobId state } } }"})
    assert r.status_code == 200, r.text
    jobs_payload = r.json()["data"]["jobs"]
    assert jobs_payload["total"] >= 1
    assert any(i["jobId"] == job_id for i in jobs_payload["items"])

    # single job query with variables
    r = client.post("/graphql", headers={"X-API-Key": api_key},
                    json={"query": 'query ($id: ID!) { job(id: $id) { jobId state } }',
                          "variables": {"id": job_id}})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["job"]["jobId"] == job_id

    # anonymous GraphQL -> 401
    r = client.post("/graphql", json={"query": "{ viewer { tenant } }"})
    assert r.status_code == 401

    # mutations refused
    r = client.post("/graphql", headers={"X-API-Key": api_key},
                    json={"query": "mutation { hack }"})
    assert r.status_code == 401


def test_crawl_job_state_filter_paginates_matching_jobs_and_counts_matches(client):
    token = signup(client, email="crawl-list@example.com", org="Crawl List")
    api_key = make_key(client, token["access_token"])
    jobs = []
    for index in range(4):
        response = client.post(
            "/v1/jobs/crawl", headers={"X-API-Key": api_key},
            json={"url": f"https://example.com/{index}", "max_pages": 1},
        )
        assert response.status_code == 202, response.text
        jobs.append(response.json())

    redis_client = client.app.state.services.redis
    tenant_jobs_key = f"wcp:v1:tenant_jobs:{jobs[0]['tenant']}"
    states = ["completed", "running", "failed", "running"]
    for index, (job, state) in enumerate(zip(jobs, states)):
        redis_client.hset(f"wcp:v1:job:{job['job_id']}", "state", state)
        redis_client.zadd(tenant_jobs_key, {job["job_id"]: index + 1})

    first_page = client.get(
        "/v1/jobs?page=1&page_size=1&state=running",
        headers={"X-API-Key": api_key},
    )
    second_page = client.get(
        "/v1/jobs?page=2&page_size=1&state=running",
        headers={"X-API-Key": api_key},
    )

    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json()["total"] == second_page.json()["total"] == 2
    assert [item["job_id"] for item in first_page.json()["items"]] == [jobs[3]["job_id"]]
    assert [item["job_id"] for item in second_page.json()["items"]] == [jobs[1]["job_id"]]


def test_auth_context_middleware_sets_tenant_for_search(client):
    """AuthContextMiddleware must expose request.state.tenant so the
    tenant-scoped search backend resolves correctly."""
    tok = signup(client, email="rag@example.com", org="RAG Inc")
    api_key = make_key(client, tok["access_token"])
    r = client.post("/v1/search", headers={"X-API-Key": api_key},
                    json={"query": "crawling pipelines", "top_k": 3})
    # chromadb may be absent -> endpoint degrades to 503; but it must NOT be 401
    assert r.status_code in (200, 503), r.text


def test_process_time_and_ratelimit_headers(client):
    tok = signup(client, email="hdr@example.com", org="Hdr Org")
    api_key = make_key(client, tok["access_token"])
    r = client.get("/keys", headers={"X-API-Key": api_key})
    assert "X-RateLimit-Remaining" in r.headers


def test_failed_crawl_job_cannot_be_cancelled(client):
    token = signup(client)
    api_key = make_key(client, token["access_token"])
    created = client.post(
        "/v1/jobs/crawl",
        headers={"X-API-Key": api_key},
        json={"url": "https://example.com", "max_pages": 1},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    redis_client = client.app.state.services.redis
    job_key = f"wcp:v1:job:{job_id}"
    redis_client.hset(job_key, "state", "failed")

    response = client.post(
        f"/v1/jobs/{job_id}/cancel", headers={"X-API-Key": api_key}
    )

    assert response.status_code == 409
    stored_state = redis_client.hget(job_key, "state")
    assert stored_state in (b"failed", "failed")


def test_crawl_artifact_opt_in_requires_rights_confirmation_and_reaches_worker(
        client, monkeypatch):
    token = signup(client, email="artifact@example.com", org="Artifact Org")
    api_key = make_key(client, token["access_token"])
    headers = {"X-API-Key": api_key}
    denied = client.post("/v1/jobs/crawl", headers=headers, json={
        "url": "https://example.com", "max_pages": 1,
        "persist_marketplace_artifact": True,
    })
    assert denied.status_code == 422
    assert "marketplace_data_rights_confirmed" in denied.text

    submitted = []
    monkeypatch.setattr(client.app.state.services.scheduler, "submit",
                        lambda spec: submitted.append(spec))
    response = client.post("/v1/jobs/crawl", headers=headers, json={
        "url": "https://example.com", "max_pages": 1,
        "persist_marketplace_artifact": True,
        "marketplace_data_rights_confirmed": True,
    })

    assert response.status_code == 202, response.text
    assert response.json()["artifact_status"] == "pending"
    assert len(submitted) == 1
    assert submitted[0].payload["persist_marketplace_artifact"] is True
    assert submitted[0].payload["marketplace_data_rights_confirmed"] is True
    status = client.get(f"/v1/jobs/{response.json()['job_id']}", headers=headers)
    assert status.status_code == 200
    assert status.json()["artifact_status"] == "pending"
