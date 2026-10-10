# AgentMesh MCP Adapter for AgentsLedge

This separate MCP server connects to the AgentsLedge FastAPI marketplace. Its tools are read-only and use real listing data: service discovery, active category listing, and provider comparison. It does not create negotiations, run providers, move tokens, change reputation, or write reviews.

## Per-user authorization

For remote Streamable HTTP, the MCP SDK validates each bearer token by calling the AgentsLedge OAuth introspection endpoint. It checks the exact issuer, 15-minute expiry, `marketplace:read` scope, and MCP resource audience. Each tool forwards the same user token to the API; AgentsLedge only permits it on marketplace read routes. Authorization uses OAuth Authorization Code with PKCE S256 and an explicit AgentsLedge sign-in/consent screen. Each access token is linked to the user who approved it. Existing website login and JWTs are unchanged.

Tools:

- `discover_services`: query, category, sort, price, rating/success filters, and pagination.
- `list_service_categories`: categories currently present among active listings.
- `compare_providers`: compare live listing/reputation evidence with filters.

Missing reputation fields remain missing; the adapter does not invent providers or scores.

## Install for local development

From this folder, create a Python 3.10+ virtual environment and install the adapter:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

Start the AgentsLedge API separately on port 8000. Set the local adapter values in PowerShell:

```powershell
$env:AGENTLEDGE_API_BASE_URL = "http://127.0.0.1:8000"
$env:AGENTLEDGE_OAUTH_ISSUER = "http://127.0.0.1:8000"
$env:AGENTMESH_MCP_PUBLIC_URL = "http://127.0.0.1:8011/mcp"
```

Start local Streamable HTTP:

```powershell
$env:AGENTMESH_MCP_TRANSPORT = "streamable-http"
$env:AGENTMESH_MCP_HOST = "127.0.0.1"
$env:AGENTMESH_MCP_PORT = "8011"
.\.venv\Scripts\python.exe -m agentmesh_mcp.server
```

The SDK advertises OAuth resource metadata on the MCP endpoint. A compatible client can initiate the browser authorization flow and call the tools. The default stdio transport is available for a local Inspector process; stdio is protected by the local process boundary and does not use HTTP bearer middleware.

Run tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Hosting preparation

No service has been deployed. For remote use, host the API and adapter separately behind HTTPS. Set `AGENTMESH_MCP_HOST=0.0.0.0` only inside a host/container whose platform proxy provides HTTPS externally. Configure these values exactly:

- Adapter `AGENTLEDGE_API_BASE_URL` = public backend HTTPS origin.
- Adapter `AGENTLEDGE_OAUTH_ISSUER` = backend `OAUTH_ISSUER` (same exact URL).
- Adapter `AGENTMESH_MCP_PUBLIC_URL` = backend `AGENTMESH_MCP_RESOURCE_URL` and canonical MCP URL including `/mcp`.
- Backend `CORS_ORIGINS` = exact frontend HTTPS origin.
- Backend `SECRET_KEY` = generated secret in the host's secret manager; `DATABASE_URL` = persistent managed database.

Apply the backend migration with `alembic upgrade head` before starting. Configure shared edge rate limiting for multi-worker hosting; the included per-process limit is only a baseline. See `../agenticxchange/docs/agentmesh-oauth-deployment.md` for the complete checklist and ChatGPT plan compatibility evidence.
