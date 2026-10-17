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
