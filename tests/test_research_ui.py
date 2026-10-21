import json

import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app


def test_research_page_is_authenticated_and_explains_scope():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False), follow_redirects=False)
    assert client.get("/app/research").status_code in (302, 303)
    signup = client.post("/auth/signup", json={"org_name": "Research Org",
                         "email": "research@example.test", "password": "correct-horse-123"})
    assert signup.status_code == 200
    client.cookies.set("wcp_session", signup.json()["access_token"])
    response = client.get("/app/research")
    assert response.status_code == 200
    assert "Ask your indexed sources" in response.text
    assert "does not start a new crawl" in response.text


def test_research_report_console_can_cancel_active_job():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False), follow_redirects=False)
    signup = client.post("/auth/signup", json={"org_name": "Research Org",
                         "email": "research-cancel@example.test",
                         "password": "correct-horse-123"})
    assert signup.status_code == 200
    client.cookies.set("wcp_session", signup.json()["access_token"])

    queued = client.post("/app/research/agent", data={
        "topic": "Cancellable research topic", "sources": 1,
        "max_pages_per_source": 1,
    })
    assert queued.status_code == 303
    research_id = queued.headers["location"].split("research_id=", 1)[1].split("&", 1)[0]

    active_page = client.get(f"/app/research/agent?research_id={research_id}")
    assert active_page.status_code == 200
    assert "Cancel research" in active_page.text

    cancelled = client.post(f"/app/research/agent/{research_id}/cancel", follow_redirects=False)
    assert cancelled.status_code == 303
    assert f"research_id={research_id}" in cancelled.headers["location"]
    stored = client.app.state.services.redis.get(f"wcp:v1:research:{research_id}")
    record = json.loads(stored)
    assert record["status"] == "cancelled"

    terminal_page = client.get(f"/app/research/agent?research_id={research_id}")
    assert terminal_page.status_code == 200
    assert "Research was cancelled" in terminal_page.text
    assert "Cancel research" not in terminal_page.text
