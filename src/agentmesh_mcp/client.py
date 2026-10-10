"""Small HTTP client for the existing AgentLedge REST API.

This adapter deliberately calls only public GET endpoints in the first phase.
It does not read or write the marketplace database directly.
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
    """HTTP client that calls only documented public read endpoints."""

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
        safe_path = path.lstrip("/")
        try:
            response = self._client.get(safe_path, params=params)
        except httpx.TimeoutException as exc:
            raise AgentLedgeAPIError("AgentLedge API timed out") from exc
        except httpx.HTTPError as exc:
            raise AgentLedgeAPIError("Could not reach the AgentLedge API") from exc

        if response.is_redirect:
            raise AgentLedgeAPIError(
                f"AgentLedge returned an unexpected redirect (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            # Do not return arbitrary backend response bodies that could contain internals.
            raise AgentLedgeAPIError(
                f"AgentLedge API returned HTTP {response.status_code} for GET /{safe_path}"
            )
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
