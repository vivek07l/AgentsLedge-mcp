"""AgentMesh MCP server: per-user authorized marketplace tools.

For local development, launch with the MCP Inspector (see README.md).
For remote use, run Streamable HTTP behind HTTPS with the configured OAuth issuer.
"""

from __future__ import annotations

import os
from typing import Any, Literal
import math
import time
from urllib.parse import urlparse
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import ServerMiddleware, ServerRequestContext

from agentmesh_mcp.client import AgentLedgeAPIError, AgentLedgeClient
from agentmesh_mcp.compare import extract_records, normalise_provider


API_BASE_URL = os.getenv("AGENTLEDGE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
OAUTH_ISSUER = os.getenv("AGENTLEDGE_OAUTH_ISSUER", API_BASE_URL).rstrip("/")
MCP_PUBLIC_URL = os.getenv("AGENTMESH_MCP_PUBLIC_URL", "http://127.0.0.1:8011/mcp").rstrip("/")
READ_SCOPE = "marketplace:read"
WRITE_SCOPE = "marketplace:write"
SUPPORTED_SCOPES = {READ_SCOPE, WRITE_SCOPE}
WRITE_TOOLS = {
    "start_negotiation", "submit_negotiation_offer", "accept_negotiation",
    "reject_negotiation", "invoke_service_agent", "submit_agent_review",
}


class ChatGPTToolSecurityMiddleware(ServerMiddleware[Any]):
    """Expose per-tool OAuth scopes for clients that support OpenAI's MCP extension."""

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next):
        result = await call_next(ctx)
        if ctx.method == "tools/list" and isinstance(result, dict):
            for tool in result.get("tools", []):
                if isinstance(tool, dict):
                    scopes = [READ_SCOPE, WRITE_SCOPE] if tool.get("name") in WRITE_TOOLS else [READ_SCOPE]
                    tool["securitySchemes"] = [{"type": "oauth2", "scopes": scopes}]
        return result


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
                    or not set(scopes).issubset(SUPPORTED_SCOPES) or READ_SCOPE not in scopes
                    or not isinstance(expires_at, int)
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
    "AgentMesh - AgentsLedge Marketplace",
    token_verifier=AgentLedgeTokenVerifier(),
    auth=AuthSettings(
        issuer_url=OAUTH_ISSUER,
        resource_server_url=MCP_PUBLIC_URL,
        # All connections need read for discovery; write tools perform a second,
        # per-tool scope check and the backend independently enforces this scope.
        required_scopes=[READ_SCOPE],
        validate_token_resource=True,
    ),
    middleware=[ChatGPTToolSecurityMiddleware()],
)

ALLOWED_SORTS = {"relevance", "rating", "success_rate", "price"}
DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def _get_client() -> AgentLedgeClient:
    access = get_access_token()
    return AgentLedgeClient(access_token=access.token if access else None)


def _scope_error(required_scope: str) -> str | None:
    access = get_access_token()
    if access is None:
        return "Reconnect AgentsLedge and authorize this request."
    if required_scope not in access.scopes:
        return f"This action needs the {required_scope} permission. Reauthorize AgentsLedge with that permission."
    return None


def _tool_failure(error: Exception) -> dict[str, Any]:
    if isinstance(error, AgentLedgeAPIError):
        return {"ok": False, "error": str(error)}
    return {"ok": False, "error": "The AgentsLedge API could not complete this request."}


def _pending_approval(client: AgentLedgeClient, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    approval = client.request_action_approval(action, payload)
    return {
        "ok": True,
        "approval_required": True,
        "approval": approval,
        "next_step": "Review and approve or reject this exact action in the signed-in AgentsLedge website at /approvals, then call this tool again with approval_id.",
    }


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
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
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
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
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
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            categories = client.list_categories()
            return {"ok": True, "source": "AgentsLedge GET /marketplace/categories", "count": len(categories), "categories": categories}
        except AgentLedgeAPIError as exc:
            return {"ok": False, "error": str(exc)}


@mcp.tool()
def get_service_details(agent_id: int) -> dict[str, Any]:
    """Read one active marketplace listing, including its capabilities and price."""
    if agent_id < 1:
        return {"ok": False, "error": "agent_id must be a positive integer"}
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            return {"ok": True, "service": client.get_agent_details(agent_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def get_negotiation_status(negotiation_id: int) -> dict[str, Any]:
    """Read a negotiation only when the authenticated user is one of its participants."""
    if negotiation_id < 1:
        return {"ok": False, "error": "negotiation_id must be a positive integer"}
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            return {"ok": True, "negotiation": client.get_negotiation_status(negotiation_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def get_invocation_result(invocation_id: int) -> dict[str, Any]:
    """Read an execution result only when it belongs to the authenticated user."""
    if invocation_id < 1:
        return {"ok": False, "error": "invocation_id must be a positive integer"}
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            return {"ok": True, "invocation": client.get_invocation_result(invocation_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def get_agent_reputation(agent_id: int) -> dict[str, Any]:
    """Read verified reputation metrics for a listed service."""
    if agent_id < 1:
        return {"ok": False, "error": "agent_id must be a positive integer"}
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            return {"ok": True, "reputation": client.get_agent_reputation(agent_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def get_agent_reviews(agent_id: int) -> dict[str, Any]:
    """Read approved reviews for a listed service."""
    if agent_id < 1:
        return {"ok": False, "error": "agent_id must be a positive integer"}
    if scope_error := _scope_error(READ_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            reviews = client.get_agent_reviews(agent_id)
            return {"ok": True, "count": len(reviews), "reviews": reviews}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def start_negotiation(agent_id: int, buyer_budget: float, initial_offer: float, approval_id: str = "") -> dict[str, Any]:
    """Create a negotiation only after explicit user approval of the offer and maximum budget. First call without approval_id to request approval; then repeat with the approval_id shown after the user approves it in AgentsLedge /approvals. No tokens are charged here."""
    if not math.isfinite(buyer_budget) or not math.isfinite(initial_offer) or buyer_budget <= 0 or initial_offer <= 0 or initial_offer > buyer_budget:
        return {"ok": False, "error": "buyer_budget and initial_offer must be positive finite amounts, and the offer cannot exceed the budget"}
    if scope_error := _scope_error(WRITE_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            if not approval_id:
                return _pending_approval(client, "start_negotiation", {
                    "agent_id": agent_id, "buyer_budget": buyer_budget, "initial_offer": initial_offer,
                })
            return {"ok": True, "negotiation": client.start_negotiation(agent_id, buyer_budget, initial_offer, approval_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def submit_negotiation_offer(negotiation_id: int, amount: float, approval_id: str = "") -> dict[str, Any]:
    """Submit a buyer offer only after explicit user approval. First call without approval_id, have the user approve the exact amount in AgentsLedge /approvals, then repeat with that approval_id."""
    if negotiation_id < 1 or not math.isfinite(amount) or amount <= 0:
        return {"ok": False, "error": "negotiation_id and amount must be positive; amount must be finite"}
    if scope_error := _scope_error(WRITE_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            if not approval_id:
                return _pending_approval(client, "submit_offer", {"negotiation_id": negotiation_id, "amount": amount})
            return {"ok": True, "negotiation": client.submit_negotiation_offer(negotiation_id, amount, approval_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def accept_negotiation(negotiation_id: int, approval_id: str = "") -> dict[str, Any]:
    """Accept a provider counter-offer only after explicit approval of its displayed price. First call without approval_id, have the user approve it in AgentsLedge /approvals, then repeat with that approval_id. Execution may later charge the agreed price."""
    if negotiation_id < 1:
        return {"ok": False, "error": "negotiation_id must be a positive integer"}
    if scope_error := _scope_error(WRITE_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            if not approval_id:
                return _pending_approval(client, "accept_negotiation", {"negotiation_id": negotiation_id})
            return {"ok": True, "negotiation": client.accept_negotiation(negotiation_id, approval_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def reject_negotiation(negotiation_id: int, approval_id: str = "") -> dict[str, Any]:
    """Reject a negotiation only after the user approves the exact rejection. First call without approval_id to request approval, then repeat with the ID after the website user approves."""
    if negotiation_id < 1:
        return {"ok": False, "error": "negotiation_id must be a positive integer"}
    if scope_error := _scope_error(WRITE_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            if not approval_id:
                return _pending_approval(client, "reject_negotiation", {"negotiation_id": negotiation_id})
            return {"ok": True, "negotiation": client.reject_negotiation(negotiation_id, approval_id)}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def invoke_service_agent(
    agent_id: int, negotiation_id: int, input_data: dict[str, Any], idempotency_key: str,
    approval_id: str = "",
) -> dict[str, Any]:
    """Run an accepted marketplace service only after website approval of the exact input and agreed price. First call without approval_id to request approval, then repeat with the ID after the user approves in AgentsLedge /approvals. This may spend tokens. Reuse the same idempotency_key when retrying the same request."""
    if agent_id < 1 or negotiation_id < 1:
        return {"ok": False, "error": "agent_id and negotiation_id must be positive integers"}
    if not isinstance(input_data, dict):
        return {"ok": False, "error": "input_data must be an object"}
    if not idempotency_key.strip() or len(idempotency_key) > 255:
        return {"ok": False, "error": "idempotency_key must be non-empty and at most 255 characters"}
    if scope_error := _scope_error(WRITE_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            if not approval_id:
                return _pending_approval(client, "invoke_service", {
                    "agent_id": agent_id, "negotiation_id": negotiation_id,
                    "input_data": input_data, "idempotency_key": idempotency_key,
                })
            result = client.invoke_service_agent(agent_id, input_data, negotiation_id, idempotency_key, approval_id)
            return {"ok": True, "execution": result}
        except Exception as exc:
            return _tool_failure(exc)


@mcp.tool()
def submit_agent_review(agent_id: int, invocation_id: int, rating: int, review_text: str = "", approval_id: str = "") -> dict[str, Any]:
    """Submit a review only after the user approves the exact rating and text in AgentsLedge /approvals. First call without approval_id to request approval, then repeat with the ID. The backend requires your own successful, not demo, invocation and prevents duplicates."""
    if agent_id < 1 or invocation_id < 1 or not 1 <= rating <= 5 or len(review_text) > 1000:
        return {"ok": False, "error": "Use positive IDs, a rating from 1 to 5, and review_text no longer than 1000 characters"}
    if scope_error := _scope_error(WRITE_SCOPE):
        return {"ok": False, "error": scope_error}
    with _client_context() as client:
        try:
            if not approval_id:
                return _pending_approval(client, "submit_review", {
                    "agent_id": agent_id, "invocation_id": invocation_id,
                    "rating": rating, "review_text": review_text,
                })
            return {"ok": True, "review": client.submit_agent_review(agent_id, invocation_id, rating, review_text or None, approval_id)}
        except Exception as exc:
            return _tool_failure(exc)


class _client_context:
    """Tiny context manager to ensure each tool closes its HTTP client."""

    def __enter__(self) -> AgentLedgeClient:
        self.client = _get_client()
        return self.client

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.client.close()


def create_remote_app(host: str = "127.0.0.1"):
    """Build the authenticated HTTP app and advertise both supported scopes."""
    app = mcp.streamable_http_app(streamable_http_path="/mcp", host=host)
    metadata_url = build_resource_metadata_url(MCP_PUBLIC_URL)
    metadata_path = urlparse(str(metadata_url)).path

    async def protected_resource_metadata(request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": MCP_PUBLIC_URL,
            "authorization_servers": [OAUTH_ISSUER],
            "scopes_supported": sorted(SUPPORTED_SCOPES),
            "bearer_methods_supported": ["header"],
        })

    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != metadata_path]
    app.router.routes.insert(0, Route(metadata_path, endpoint=protected_resource_metadata, methods=["GET"]))
    return app


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
        import uvicorn
        uvicorn.run(create_remote_app(host), host=host, port=port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
