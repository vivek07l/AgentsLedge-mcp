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

    FakeAsyncClient.payload = {**FakeAsyncClient.payload, "scope": "marketplace:read marketplace:write"}
    write_token = asyncio.run(server.AgentLedgeTokenVerifier().verify_token("user-token"))
    assert write_token is not None
    assert write_token.scopes == ["marketplace:read", "marketplace:write"]

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
        self.write_calls = []

    def request_action_approval(self, action, payload):
        return {"approval_id": "pending-approval", "action": action, "payload": payload, "status": "pending"}

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
        return {"agent_id": agent_id, "average_rating": 4.8, "success_rate": 95}

    def get_agent_details(self, agent_id):
        return {"id": agent_id, "name": "Resume Pro"}

    def get_agent_reviews(self, agent_id):
        return [{"id": 9, "agent_id": agent_id, "rating": 5}]

    def get_negotiation_status(self, negotiation_id):
        return {"id": negotiation_id, "status": "awaiting_buyer"}

    def get_invocation_result(self, invocation_id):
        return {"invocation_id": invocation_id, "result": {"output_text": "result"}}

    def start_negotiation(self, agent_id, buyer_budget, initial_offer, approval_id=None):
        self.write_calls.append(("start_negotiation", approval_id))
        return {"id": 100, "agent_id": agent_id, "buyer_budget": buyer_budget, "initial_offer": initial_offer}

    def submit_negotiation_offer(self, negotiation_id, amount, approval_id=None):
        self.write_calls.append(("submit_offer", approval_id))
        return {"id": negotiation_id, "offer": amount}

    def accept_negotiation(self, negotiation_id, approval_id=None):
        self.write_calls.append(("accept_negotiation", approval_id))
        return {"id": negotiation_id, "status": "accepted", "agreed_price": 8}

    def reject_negotiation(self, negotiation_id, approval_id=None):
        self.write_calls.append(("reject_negotiation", approval_id))
        return {"id": negotiation_id, "status": "rejected"}

    def invoke_service_agent(self, agent_id, input_data, negotiation_id, idempotency_key, approval_id=None):
        self.write_calls.append(("invoke_service", approval_id))
        return {"invocation_id": 200, "execution_mode": "live", "charged_tokens": 8, "idempotency_key": idempotency_key}

    def submit_agent_review(self, agent_id, invocation_id, rating, review_text=None, approval_id=None):
        self.write_calls.append(("submit_review", approval_id))
        return {"id": 300, "invocation_id": invocation_id, "rating": rating}

    def close(self):
        self.closed = True


def _run_as_user(monkeypatch, tool, client, *args, scopes=None, **kwargs):
    access = AccessToken(token="user-token", client_id="client", scopes=scopes or ["marketplace:read"],
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


def test_all_additional_read_tools_map_to_authenticated_backend_routes(monkeypatch):
    read_scopes = ["marketplace:read"]
    cases = [
        (server.get_service_details, (12,), "service"),
        (server.get_negotiation_status, (13,), "negotiation"),
        (server.get_invocation_result, (14,), "invocation"),
        (server.get_agent_reputation, (15,), "reputation"),
        (server.get_agent_reviews, (16,), "reviews"),
    ]
    for tool, args, result_key in cases:
        client = FakeMarketplaceClient([])
        response = _run_as_user(monkeypatch, tool, client, *args, scopes=read_scopes)
        assert response["ok"] is True
        assert result_key in response
        assert client.closed is True


def test_all_write_tools_require_write_scope_and_call_existing_api_methods(monkeypatch):
    write_scopes = ["marketplace:read", "marketplace:write"]
    cases = [
        (server.start_negotiation, (2, 12, 8), "negotiation", "start_negotiation"),
        (server.submit_negotiation_offer, (100, 9), "negotiation", "submit_offer"),
        (server.accept_negotiation, (100,), "negotiation", "accept_negotiation"),
        (server.reject_negotiation, (100,), "negotiation", "reject_negotiation"),
        (server.invoke_service_agent, (2, 100, {"input_text": "review"}, "run-once"), "execution", "invoke_service"),
        (server.submit_agent_review, (2, 200, 5, "Helpful"), "review", "submit_review"),
    ]
    for tool, args, result_key, action in cases:
        client = FakeMarketplaceClient([])
        pending = _run_as_user(monkeypatch, tool, client, *args, scopes=write_scopes)
        assert pending["ok"] is True
        assert pending["approval_required"] is True
        assert pending["approval"]["action"] == action
        assert client.write_calls == []
        client.closed = False
        response = _run_as_user(monkeypatch, tool, client, *args, "approved-on-website", scopes=write_scopes)
        assert response["ok"] is True and result_key in response
        assert client.write_calls and client.write_calls[-1][1] == "approved-on-website"
        assert client.closed is True

    read_only_client = FakeMarketplaceClient([])
    denied = _run_as_user(monkeypatch, server.accept_negotiation, read_only_client, 100, scopes=["marketplace:read"])
    assert denied["ok"] is False
    assert "marketplace:write" in denied["error"]
    assert read_only_client.closed is False


def test_remote_resource_metadata_advertises_read_and_write_scopes():
    from starlette.testclient import TestClient

    app = server.create_remote_app()
    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    response = TestClient(app).get(metadata_path)
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == server.MCP_PUBLIC_URL
    assert body["authorization_servers"] == [server.OAUTH_ISSUER]
    assert body["scopes_supported"] == ["marketplace:read", "marketplace:write"]


def test_write_tools_describe_user_approval_requirements():
    for tool in (server.start_negotiation, server.submit_negotiation_offer, server.accept_negotiation,
                 server.reject_negotiation, server.invoke_service_agent, server.submit_agent_review):
        assert "approval_id" in tool.__doc__
        assert "approval" in tool.__doc__.lower()


def test_tool_security_metadata_requires_read_plus_write_for_mutations():
    class Context:
        method = "tools/list"

    async def call_next(_ctx):
        return {"tools": [
            {"name": "discover_services"},
            {"name": "accept_negotiation"},
        ]}

    result = asyncio.run(server.ChatGPTToolSecurityMiddleware()(Context(), call_next))
    assert result["tools"][0]["securitySchemes"][0]["scopes"] == ["marketplace:read"]
    assert result["tools"][1]["securitySchemes"][0]["scopes"] == ["marketplace:read", "marketplace:write"]
