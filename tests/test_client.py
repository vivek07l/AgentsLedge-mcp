import json
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


@pytest.mark.parametrize("detail", [
    "An authorized negotiation for this service is required",
    "This action approval is missing, expired, or already used",
    "OAuth token requires marketplace:write",
])
def test_known_authorization_failures_explain_backend_reason(detail):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": detail})

    client = AgentLedgeClient(base_url="http://testserver", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AgentLedgeAPIError) as caught:
            client.invoke_service_agent(17, {"input_text": "Test"}, 12, "fixed-key", "approved-id")
    finally:
        client.close()

    assert detail in str(caught.value)


def test_unknown_authorization_response_body_remains_hidden():
    secret = "internal account details should not be returned"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": secret})

    client = AgentLedgeClient(base_url="http://testserver", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AgentLedgeAPIError) as caught:
            client.invoke_service_agent(17, {"input_text": "Test"}, 12, "fixed-key", "approved-id")
    finally:
        client.close()

    assert secret not in str(caught.value)


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


def test_negotiation_execution_result_and_review_methods_reuse_backend_routes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer current-user-token"
        seen.append((request.method, request.url.path, request.headers.get("Idempotency-Key"),
                     request.headers.get("X-User-Approval"), request.read()))
        return httpx.Response(200, json={"ok": True})

    client = AgentLedgeClient(base_url="http://testserver", access_token="current-user-token", transport=httpx.MockTransport(handler))
    try:
        client.start_negotiation(4, 10, 8, "approved-start")
        client.get_negotiation_status(5)
        client.list_my_negotiations()
        client.get_autonomous_policy()
        client.get_marketplace_balance()
        client.submit_negotiation_offer(5, 9, "approved-offer")
        client.accept_negotiation(5, "approved-accept")
        client.reject_negotiation(5, "approved-reject")
        client.invoke_service_agent(4, {"input_text": "review"}, 5, "stable-key", "approved-run")
        client.get_invocation_result(6)
        client.get_agent_reputation(4)
        client.get_agent_reviews(4)
        client.submit_agent_review(4, 6, 5, "Helpful", "approved-review")
    finally:
        client.close()

    assert [(method, path) for method, path, _, _, _ in seen] == [
        ("POST", "/negotiations"),
        ("GET", "/negotiations/5"),
        ("GET", "/users/me/negotiations"),
        ("GET", "/users/me/autonomous-policy"),
        ("GET", "/users/me/marketplace-balance"),
        ("POST", "/negotiations/5/offers"),
        ("POST", "/negotiations/5/accept"),
        ("POST", "/negotiations/5/reject"),
        ("POST", "/agents/invoke/4"),
        ("GET", "/users/me/invocations/6"),
        ("GET", "/agents/4/reputation"),
        ("GET", "/agents/4/reviews"),
        ("POST", "/agents/4/reviews"),
    ]
    invoke = seen[8]
    assert invoke[2] == "stable-key"
    assert json.loads(invoke[4])["negotiation_id"] == 5
    assert [approval for _, _, _, approval, _ in seen if approval] == [
        "approved-start", "approved-offer", "approved-accept", "approved-reject", "approved-run", "approved-review",
    ]


def test_request_action_approval_uses_existing_authenticated_api():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer user-token"
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json={"approval_id": "pending-id", "status": "pending"})

    client = AgentLedgeClient(base_url="http://testserver", access_token="user-token", transport=httpx.MockTransport(handler))
    try:
        response = client.request_action_approval("accept_negotiation", {"negotiation_id": 12})
    finally:
        client.close()

    assert response["approval_id"] == "pending-id"
    assert seen[0][0:2] == ("POST", "/mcp/action-approvals")
    assert json.loads(seen[0][2]) == {"action": "accept_negotiation", "payload": {"negotiation_id": 12}}
