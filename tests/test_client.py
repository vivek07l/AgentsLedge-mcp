import httpx
import pytest

from agentmesh_mcp.client import AgentLedgeAPIError, AgentLedgeClient


def test_discover_services_uses_existing_route_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"id": 1, "name": "Reviewer"}])

    client = AgentLedgeClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.discover_services(query="review", category="Writing", sort="price")
    finally:
        client.close()

    assert seen["path"] == "/agents"
    assert seen["params"] == {"sort": "price", "query": "review", "category": "Writing", "limit": "50", "offset": "0"}
    assert result[0]["name"] == "Reviewer"


def test_api_errors_do_not_forward_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal secret details")

    client = AgentLedgeClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AgentLedgeAPIError) as caught:
            client.discover_services()
    finally:
        client.close()

    assert "HTTP 500" in str(caught.value)
    assert "internal secret details" not in str(caught.value)


def test_bad_base_url_is_rejected():
    with pytest.raises(ValueError):
        AgentLedgeClient(base_url="testserver")


def test_user_bearer_is_forwarded():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer user-access-token"
        return httpx.Response(200, json=[])

    client = AgentLedgeClient(base_url="http://testserver", access_token="user-access-token", transport=httpx.MockTransport(handler))
    try:
        assert client.discover_services() == []
    finally:
        client.close()
