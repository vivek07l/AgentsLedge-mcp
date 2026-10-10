"""AgentMesh MCP server: read-only, per-user authorized marketplace tools.

For local development, launch with the MCP Inspector (see README.md).
For remote use, run Streamable HTTP behind HTTPS with the configured OAuth issuer.
"""

from __future__ import annotations

import os
from typing import Any, Literal
import math
import time
import httpx

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token

from agentmesh_mcp.client import AgentLedgeAPIError, AgentLedgeClient
from agentmesh_mcp.compare import extract_records, normalise_provider


API_BASE_URL = os.getenv("AGENTLEDGE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
OAUTH_ISSUER = os.getenv("AGENTLEDGE_OAUTH_ISSUER", API_BASE_URL).rstrip("/")
MCP_PUBLIC_URL = os.getenv("AGENTMESH_MCP_PUBLIC_URL", "http://127.0.0.1:8011/mcp").rstrip("/")


class AgentLedgeTokenVerifier(TokenVerifier):
    """Validate the user's opaque token by online introspection at AgentsLedge."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.post(
                    f"{OAUTH_ISSUER}/oauth/introspect",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            if response.status_code != 200:
                return None
            claims = response.json()
            scopes = claims.get("scope", "").split()
            expires_at = claims.get("exp")
            if (claims.get("active") is not True or claims.get("iss") != OAUTH_ISSUER
                    or claims.get("aud") != MCP_PUBLIC_URL or claims.get("resource") != MCP_PUBLIC_URL
                    or "marketplace:read" not in scopes or not isinstance(expires_at, int)
                    or expires_at <= int(time.time()) or not claims.get("sub") or not claims.get("client_id")):
                return None
            return AccessToken(
                token=token,
                client_id=str(claims["client_id"]),
                scopes=scopes,
                expires_at=expires_at,
                resource=claims["resource"],
                subject=str(claims["sub"]),
                claims={"iss": claims["iss"], "aud": claims["aud"]},
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None


mcp = MCPServer(
    "AgentMesh - AgentsLedge Marketplace (Read Only)",
    token_verifier=AgentLedgeTokenVerifier(),
    auth=AuthSettings(
        issuer_url=OAUTH_ISSUER,
        resource_server_url=MCP_PUBLIC_URL,
        required_scopes=["marketplace:read"],
        validate_token_resource=True,
    ),
)

ALLOWED_SORTS = {"relevance", "rating", "success_rate", "price"}
DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def _get_client() -> AgentLedgeClient:
    access = get_access_token()
    return AgentLedgeClient(access_token=access.token if access else None)


@mcp.tool()
def discover_services(
    query: str = "",
    category: str = "",
    sort: Literal["relevance", "rating", "success_rate", "price"] = "relevance",
    max_price: float | None = None,
    minimum_rating: float | None = None,
    minimum_success_rate: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search real AgentsLedge marketplace listings with filters and pagination. Read-only.

    Args:
        query: Optional text to search for in service listings.
        category: Optional marketplace category filter.
        sort: Marketplace sort order: relevance, rating, success_rate, or price.
        max_price: Optional maximum listed price in marketplace tokens.
        minimum_rating: Optional minimum average rating from 0 to 5.
        minimum_success_rate: Optional minimum completed-run success rate from 0 to 100.
        limit: Maximum number of returned listings, from 1 to 100.
        offset: Number of matching marketplace listings to skip for the next page.
    """
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        return {"ok": False, "error": "limit must be 1-100 and offset must be 0-100000"}
    if max_price is not None and (not math.isfinite(max_price) or max_price < 0):
        return {"ok": False, "error": "max_price must be a finite non-negative number"}
    if minimum_rating is not None and (not math.isfinite(minimum_rating) or not 0 <= minimum_rating <= 5):
        return {"ok": False, "error": "minimum_rating must be between 0 and 5"}
    if minimum_success_rate is not None and (not math.isfinite(minimum_success_rate) or not 0 <= minimum_success_rate <= 100):
        return {"ok": False, "error": "minimum_success_rate must be between 0 and 100"}
    with _client_context() as client:
        try:
            payload = client.discover_services(query=query, category=category, sort=sort, max_price=max_price,
                                               minimum_rating=minimum_rating, minimum_success_rate=minimum_success_rate,
                                               limit=limit, offset=offset)
            records, total = extract_records(payload)
            visible = records[:limit]
            # A missing total does not prove the result set is complete.
            complete = (total is not None and offset + len(records) >= total) or len(records) < limit
            return {
                "ok": True,
                "source": "AgentsLedge GET /agents",
                "query": query,
                "category": category,
                "sort": sort,
                "filters": {"max_price": max_price, "minimum_rating": minimum_rating, "minimum_success_rate": minimum_success_rate},
                "offset": offset,
                "count_returned": len(visible),
                "total_reported_by_api": total,
                "results_complete": complete,
                "results": visible,
            }
        except AgentLedgeAPIError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "Unexpected error while reading AgentsLedge listings"}


@mcp.tool()
def compare_providers(
    service_query: str,
    max_price: float | None = None,
    minimum_rating: float | None = None,
    minimum_success_rate: float | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Compare matching listings using real AgentsLedge listing/reputation data.

    Comparisons only cover results returned by the current public discovery
    endpoint. If the backend's response does not establish completeness, the
    result explicitly says results_complete=false. The adapter does not create
    a separate reputation score or infer missing ratings.

    Args:
        service_query: Service name, capability, or text to search for.
        max_price: Optional maximum listing price in marketplace tokens.
        minimum_rating: Optional minimum rating from 0 to 5.
        minimum_success_rate: Optional minimum completed-run success rate from 0 to 100.
        limit: Maximum number of listings to compare, from 1 to 100.
    """
    if not service_query.strip():
        return {"ok": False, "error": "service_query is required"}
    if not 1 <= limit <= 100:
        return {"ok": False, "error": "limit must be between 1 and 100"}
    if max_price is not None and (not math.isfinite(max_price) or max_price < 0):
        return {"ok": False, "error": "max_price must be a finite non-negative number"}
    if minimum_rating is not None and (not math.isfinite(minimum_rating) or not 0 <= minimum_rating <= 5):
        return {"ok": False, "error": "minimum_rating must be between 0 and 5"}
    if minimum_success_rate is not None and (not math.isfinite(minimum_success_rate) or not 0 <= minimum_success_rate <= 100):
        return {"ok": False, "error": "minimum_success_rate must be between 0 and 100"}

    with _client_context() as client:
        try:
            payload = client.discover_services(
                query=service_query,
                category="",
                sort="rating",
                max_price=max_price,
                minimum_rating=minimum_rating,
                minimum_success_rate=minimum_success_rate,
                limit=100,
                offset=0,
            )
            records, total = extract_records(payload)
            provider_rows: list[dict[str, Any]] = []
            for record in records[:MAX_LIMIT]:
                agent_id = record.get("agent_id", record.get("id"))
                reputation: Any = None
                # Only request reputation when listing data does not already contain it.
                rating_missing = (
                    record.get("rating") is None
                    and record.get("average_rating") is None
                    and record.get("average_review_rating") is None
                )
                success_rate_missing = record.get("success_rate") is None
                if agent_id is not None and (rating_missing or success_rate_missing):
                    try:
                        reputation = client.get_agent_reputation(agent_id)
                    except AgentLedgeAPIError:
                        # Missing reputation should not hide an otherwise real listing.
                        reputation = None

                item = normalise_provider(record, reputation=reputation)
                provider_rows.append(item)

            provider_rows = provider_rows[:limit]
            return {
                "ok": True,
                "source": ["AgentsLedge GET /agents", "AgentsLedge GET /agents/{agent_id}/reputation"],
                "search": service_query,
                "filters": {"max_price": max_price, "minimum_rating": minimum_rating, "minimum_success_rate": minimum_success_rate},
                "currency": "marketplace_tokens",
                "count_returned": len(provider_rows),
                "total_reported_by_api": total,
                "results_complete": len(records) < 100,
                "note": "Comparison includes the first page of matching real listings. Missing ratings, success rates, or capabilities are not invented.",
                "providers": provider_rows,
            }
        except AgentLedgeAPIError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "Unexpected error while comparing AgentsLedge providers"}


@mcp.tool()
def list_service_categories() -> dict[str, Any]:
    """List categories currently present among active AgentsLedge listings. Read-only."""
    with _client_context() as client:
        try:
            categories = client.list_categories()
            return {"ok": True, "source": "AgentsLedge GET /marketplace/categories", "count": len(categories), "categories": categories}
        except AgentLedgeAPIError as exc:
            return {"ok": False, "error": str(exc)}


class _client_context:
    """Tiny context manager to ensure each tool closes its HTTP client."""

    def __enter__(self) -> AgentLedgeClient:
        self.client = _get_client()
        return self.client

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.client.close()


def main() -> None:
    """Run a local stdio server by default, or remote Streamable HTTP explicitly."""
    transport = os.getenv("AGENTMESH_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in {"stdio", "streamable-http"}:
        raise SystemExit("AGENTMESH_MCP_TRANSPORT must be 'stdio' or 'streamable-http'")
    if transport == "streamable-http":
        # Authentication is enforced by the MCP SDK using OAuth introspection.
        port = int(os.getenv("AGENTMESH_MCP_PORT", "8011"))
        if not 1 <= port <= 65535:
            raise SystemExit("AGENTMESH_MCP_PORT must be between 1 and 65535")
        host = os.getenv("AGENTMESH_MCP_HOST", "127.0.0.1").strip()
        if not host:
            raise SystemExit("AGENTMESH_MCP_HOST cannot be empty")
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
