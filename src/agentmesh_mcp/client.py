"""HTTP client for the existing AgentsLedge REST API.

The client forwards only the authenticated user's OAuth bearer token and calls
existing API routes. It does not access the marketplace database directly.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class AgentLedgeAPIError(RuntimeError):
    """Raised when AgentLedge returns an HTTP error or invalid JSON."""


def _normalise_base_url(value: str | None) -> str:
    raw = (value or "http://127.0.0.1:8000").strip()
    if not raw:
        raise ValueError("AGENTLEDGE_API_BASE_URL cannot be empty")
    if not raw.startswith(("http://", "https://")):
        raise ValueError("AGENTLEDGE_API_BASE_URL must start with http:// or https://")
    return raw.rstrip("/") + "/"


class AgentLedgeClient:
    """HTTP client that delegates policy and state changes to the backend."""

    _SAFE_AUTHORIZATION_DETAILS = {
        "This API operation is not available to MCP clients",
        "This action approval is missing, expired, or already used",
        "Approve this exact action in the AgentsLedge website first",
        "An authorized negotiation for this service is required",
        "You are not a participant in this negotiation",
        "Only the buyer can request this action approval",
        "Only a negotiation participant can request rejection",
    }

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
        access_token: str | None = None,
    ) -> None:
        configured_timeout = timeout_seconds
        if configured_timeout is None:
            configured_timeout = float(os.getenv("AGENTLEDGE_API_TIMEOUT_SECONDS", "10"))
        if configured_timeout <= 0:
            raise ValueError("AGENTLEDGE_API_TIMEOUT_SECONDS must be greater than zero")

        self.base_url = _normalise_base_url(
            base_url if base_url is not None else os.getenv("AGENTLEDGE_API_BASE_URL")
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(configured_timeout),
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "agentmesh-mcp/0.2.0", **({"Authorization": f"Bearer {access_token}"} if access_token else {})},
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON endpoint and return its response without rewriting fields."""
        return self._request_json("GET", path, params=params)

    @staticmethod
    def _error_for_status(status_code: int, method: str, path: str, api_detail: str | None = None) -> str:
        messages = {
            401: "Authorization is missing or expired. Reconnect your AgentsLedge account.",
            402: "The account does not have enough marketplace tokens for this service.",
            403: "This action is not authorized for this account or OAuth permission set.",
            404: "The requested service or user-owned record was not found.",
            409: "The requested state transition is not currently allowed, or this idempotency key was already used.",
            422: "The request failed marketplace validation. Check the price, budget, rating, or required input fields.",
            502: "The service provider could not complete execution. No successful result was recorded.",
        }
        detail = messages.get(status_code, "The AgentsLedge API could not complete this request.")
        if status_code == 403 and (
            api_detail in AgentLedgeClient._SAFE_AUTHORIZATION_DETAILS
            or api_detail in {"OAuth token requires marketplace:read", "OAuth token requires marketplace:write"}
        ):
            detail = f"{detail} Backend reason: {api_detail}."
        return f"{detail} (HTTP {status_code} for {method} /{path})"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        safe_path = path.lstrip("/")
        try:
            response = self._client.request(method, safe_path, params=params, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AgentLedgeAPIError("AgentLedge API timed out") from exc
        except httpx.HTTPError as exc:
            raise AgentLedgeAPIError("Could not reach the AgentLedge API") from exc

        if response.is_redirect:
            raise AgentLedgeAPIError(
                f"AgentLedge returned an unexpected redirect (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            # Do not relay arbitrary response bodies or internal exception data.
            api_detail = None
            if response.status_code == 403:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    api_detail = body["detail"]
            raise AgentLedgeAPIError(self._error_for_status(
                response.status_code, method, safe_path, api_detail=api_detail,
            ))
        try:
            return response.json()
        except ValueError as exc:
            raise AgentLedgeAPIError("AgentLedge API returned a non-JSON response") from exc

    def discover_services(
        self, query: str = "", category: str = "", sort: str = "relevance",
        max_price: float | None = None, minimum_rating: float | None = None,
        minimum_success_rate: float | None = None, limit: int = 50, offset: int = 0,
    ) -> Any:
        params: dict[str, Any] = {"sort": sort, "limit": limit, "offset": offset}
        if query.strip():
            params["query"] = query.strip()
        if category.strip():
            params["category"] = category.strip()
        if max_price is not None:
            params["max_price"] = max_price
        if minimum_rating is not None:
            params["minimum_rating"] = minimum_rating
        if minimum_success_rate is not None:
            params["minimum_success_rate"] = minimum_success_rate
        return self.get_json("/agents", params=params)

    def list_categories(self) -> Any:
        return self.get_json("/marketplace/categories")

    def get_agent_details(self, agent_id: str | int) -> Any:
        return self.get_json(f"/agents/{agent_id}")

    def get_agent_reputation(self, agent_id: str | int) -> Any:
        return self.get_json(f"/agents/{agent_id}/reputation")

    def get_agent_reviews(self, agent_id: int) -> Any:
        return self.get_json(f"/agents/{agent_id}/reviews")

    def get_negotiation_status(self, negotiation_id: int) -> Any:
        return self.get_json(f"/negotiations/{negotiation_id}")

    def list_my_negotiations(self) -> Any:
        """List only negotiations attached to the authenticated marketplace user."""
        return self.get_json("/users/me/negotiations")

    def get_invocation_result(self, invocation_id: int) -> Any:
        return self.get_json(f"/users/me/invocations/{invocation_id}")

    def request_action_approval(self, action: str, payload: dict[str, Any]) -> Any:
        """Queue an exact write request for approval in the signed-in website UI."""
        return self._request_json("POST", "/mcp/action-approvals", payload={"action": action, "payload": payload})

    def start_negotiation(self, agent_id: int, buyer_budget: float, initial_offer: float, approval_id: str | None = None) -> Any:
        return self._request_json("POST", "/negotiations", payload={
            "agent_id": agent_id, "buyer_budget": buyer_budget, "initial_offer": initial_offer,
        }, headers={"X-User-Approval": approval_id} if approval_id else None)

    def submit_negotiation_offer(self, negotiation_id: int, amount: float, approval_id: str | None = None) -> Any:
        return self._request_json("POST", f"/negotiations/{negotiation_id}/offers", payload={"amount": amount},
                                  headers={"X-User-Approval": approval_id} if approval_id else None)

    def accept_negotiation(self, negotiation_id: int, approval_id: str | None = None) -> Any:
        return self._request_json("POST", f"/negotiations/{negotiation_id}/accept", payload={},
                                  headers={"X-User-Approval": approval_id} if approval_id else None)

    def reject_negotiation(self, negotiation_id: int, approval_id: str | None = None) -> Any:
        return self._request_json("POST", f"/negotiations/{negotiation_id}/reject", payload={},
                                  headers={"X-User-Approval": approval_id} if approval_id else None)

    def invoke_service_agent(
        self, agent_id: int, input_data: dict[str, Any], negotiation_id: int, idempotency_key: str,
        approval_id: str | None = None,
    ) -> Any:
        return self._request_json(
            "POST", f"/agents/invoke/{agent_id}",
            payload={**input_data, "negotiation_id": negotiation_id},
            headers={"Idempotency-Key": idempotency_key, **({"X-User-Approval": approval_id} if approval_id else {})},
        )

    def submit_agent_review(
        self, agent_id: int, invocation_id: int, rating: int, review_text: str | None = None,
        approval_id: str | None = None,
    ) -> Any:
        return self._request_json("POST", f"/agents/{agent_id}/reviews", payload={
            "invocation_id": invocation_id, "rating": rating, "review_text": review_text,
        }, headers={"X-User-Approval": approval_id} if approval_id else None)
