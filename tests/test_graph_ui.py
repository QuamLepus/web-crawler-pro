import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app


def test_graph_console_renders_canvas_and_tenant_entities():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False), follow_redirects=False)
    signup = client.post("/auth/signup", json={"org_name": "Graph UI",
                         "email": "graph-ui@example.test", "password": "correct-horse-123"})
    token = signup.json()["access_token"]
    client.cookies.set("wcp_session", token)
    headers = {"Authorization": f"Bearer {token}"}
    first = client.post("/api/v1/graph/entities", headers=headers,
                        json={"name": "Northwind Labs", "entity_type": "Company"}).json()
    client.post("/api/v1/graph/entities", headers=headers,
                json={"name": "Atlas Sensor", "entity_type": "Product"})
    client.post("/api/v1/graph/relations", headers=headers,
                json={"source_id": first["entity_id"],
                      "target_id": client.get("/api/v1/graph", headers=headers).json()["nodes"][1]["entity_id"],
                      "relation": "builds"})
    response = client.get("/app/graph")
    assert response.status_code == 200
    assert "graph-canvas" in response.text
    assert "Northwind Labs" in response.text
    assert "builds" in response.text
