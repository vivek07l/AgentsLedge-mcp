import asyncio
import time

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

import agentmesh_mcp.server as server


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeAsyncClient:
    payload = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, headers):
        assert url.endswith("/oauth/introspect")
        assert headers["Authorization"] == "Bearer user-token"
        return FakeResponse(self.payload)


def test_oauth_verifier_accepts_only_live_scoped_token_for_expected_issuer_and_resource(monkeypatch):
    monkeypatch.setattr(server.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(server, "OAUTH_ISSUER", "https://api.example.test")
    monkeypatch.setattr(server, "MCP_PUBLIC_URL", "https://mcp.example.test/mcp")
    FakeAsyncClient.payload = {"active": True, "sub": "42", "client_id": "chat-client",
        "scope": "marketplace:read", "exp": int(time.time()) + 90,
        "iss": "https://api.example.test", "aud": "https://mcp.example.test/mcp",
        "resource": "https://mcp.example.test/mcp"}
    accepted = asyncio.run(server.AgentLedgeTokenVerifier().verify_token("user-token"))
    assert accepted is not None
    assert accepted.subject == "42"
    assert accepted.resource == "https://mcp.example.test/mcp"
    assert accepted.scopes == ["marketplace:read"]

    for field, invalid in (("iss", "https://wrong.example"), ("aud", "https://wrong.example/mcp"),
                           ("scope", "marketplace:write"), ("exp", int(time.time()) - 1),
                           ("active", False)):
        FakeAsyncClient.payload = {**FakeAsyncClient.payload, field: invalid}
        assert asyncio.run(server.AgentLedgeTokenVerifier().verify_token("user-token")) is None


class FakeMarketplaceClient:
    def __init__(self, records):
        self.records = records
        self.params = None
        self.closed = False

    def discover_services(self, **params):
        self.params = params
        matches = self.records
        if params.get("max_price") is not None:
            matches = [row for row in matches if row.get("price", 10**9) <= params["max_price"]]
        if params.get("minimum_rating") is not None:
            matches = [row for row in matches if row.get("average_rating", -1) >= params["minimum_rating"]]
        if params.get("minimum_success_rate") is not None:
            matches = [row for row in matches if row.get("success_rate", -1) >= params["minimum_success_rate"]]
        start = params.get("offset", 0)
        return matches[start:start + params.get("limit", 100)]

    def list_categories(self):
        return ["Career", "Writing"]

    def get_agent_reputation(self, agent_id):
        raise AssertionError("Complete listing metrics should not trigger a reputation request")

    def close(self):
        self.closed = True


def _run_as_user(monkeypatch, tool, client, *args, **kwargs):
    access = AccessToken(token="user-token", client_id="client", scopes=["marketplace:read"],
                         expires_at=int(time.time()) + 60, resource="http://127.0.0.1:8011/mcp", subject="7")
    token = auth_context_var.set(AuthenticatedUser(access))
    monkeypatch.setattr(server, "AgentLedgeClient", lambda **kw: (assert_forwarded(kw, client)))
    try:
        return tool(*args, **kwargs)
    finally:
        auth_context_var.reset(token)


def assert_forwarded(kwargs, client):
    assert kwargs["access_token"] == "user-token"
    return client


def test_discovery_filters_and_comparison_tools_use_real_results_and_user_token(monkeypatch):
    records = [
        {"id": 1, "name": "Resume Pro", "price": 8, "average_rating": 4.8, "success_rate": 95, "capabilities": "ATS"},
        {"id": 2, "name": "Resume Helper", "price": 5, "average_rating": 4.5, "success_rate": 91, "capabilities": "editing"},
    ]
    client = FakeMarketplaceClient(records)
    result = _run_as_user(monkeypatch, server.discover_services, client, query="resume", category="Career",
        sort="rating", max_price=9, minimum_rating=4, minimum_success_rate=90, limit=10, offset=0)
    assert result["ok"] is True
    assert result["count_returned"] == 2
    assert client.params["minimum_success_rate"] == 90
    assert client.closed is True

    client = FakeMarketplaceClient(records)
    compared = _run_as_user(monkeypatch, server.compare_providers, client, "resume", max_price=7,
                            minimum_rating=4, minimum_success_rate=90, limit=10)
    assert compared["ok"] is True
    assert [provider["agent_id"] for provider in compared["providers"]] == [2]
    assert client.params["limit"] == 100


def test_categories_tool_reads_categories_from_api(monkeypatch):
    client = FakeMarketplaceClient([])
    result = _run_as_user(monkeypatch, server.list_service_categories, client)
    assert result == {"ok": True, "source": "AgentsLedge GET /marketplace/categories", "count": 2, "categories": ["Career", "Writing"]}


def test_discovery_validates_price_rating_success_and_page_inputs(monkeypatch):
    client = FakeMarketplaceClient([])
    assert not server.discover_services(max_price=float("nan"))["ok"]
    assert not server.discover_services(minimum_rating=6)["ok"]
    assert not server.discover_services(minimum_success_rate=-1)["ok"]
    assert not server.discover_services(limit=101)["ok"]
    assert client.closed is False
