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


def test_sdk_redacts_transport_errors_and_surfaces_http_status():
    def handler(_request):
        return httpx.Response(401, json={"error": {"message": "authentication required"}})
    client = Client("top-secret", transport=httpx.MockTransport(handler))
    with pytest.raises(WebIntelligenceError) as exc:
        client.ask("query")
    assert exc.value.status_code == 401
    assert "top-secret" not in str(exc.value)
    client.close()
