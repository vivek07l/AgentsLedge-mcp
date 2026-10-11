# AgentMesh MCP Adapter for AgentsLedge

This separate MCP server connects ChatGPT to the AgentsLedge FastAPI marketplace. It keeps the existing real-data discovery tools and adds user-authorized negotiation, invocation, and verified-review workflows. It calls the existing backend routes and never reads or writes the database directly.

## Per-user authorization

For remote Streamable HTTP, the MCP SDK validates each bearer token by calling the AgentsLedge OAuth introspection endpoint. It checks issuer, 15-minute expiry, supported scopes, and the exact MCP resource audience. Each tool forwards the same user token to the API. Read tools require `marketplace:read`; write tools require both `marketplace:read` and `marketplace:write`, and the backend enforces those scopes again. Authorization uses OAuth Authorization Code with PKCE S256 and an explicit AgentsLedge sign-in/consent screen. Each token is tied to the user who approved it. Existing website login and JWTs are unchanged.

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

The remote server publishes OAuth protected-resource metadata advertising both supported scopes. A compatible client can initiate the browser authorization flow and call the tools. The default stdio transport is available for a local Inspector process; stdio is protected by the local process boundary and does not use HTTP bearer middleware.

Write tools are: `start_negotiation`, `submit_negotiation_offer`, `accept_negotiation`, `reject_negotiation`, `invoke_service_agent`, and `submit_agent_review`. A write tool call without `approval_id` only creates a pending approval. The user signs into the AgentsLedge website, reviews the exact service, price/budget, input or review at `/approvals`, and approves or rejects it. Only then can the caller repeat the same action with the one-time approval ID. The backend binds it to the user and action, checks expiry and current state, and consumes it once; tool instructions are not the security control. Acceptance itself records an agreement; token settlement occurs only through the existing invocation endpoint after successful live execution. Invocation requires a stable idempotency key for safe retries.

The read tools are: `list_service_categories`, `discover_services`, `compare_providers`, `get_service_details`, `get_negotiation_status`, `get_invocation_result`, `get_agent_reputation`, and `get_agent_reviews`. Negotiations and invocation results are checked against the authenticated account by the backend. No caller-supplied user ID is accepted. Admin routes, token purchases, account details, and arbitrary database writes are not exposed.

Gemini execution remains controlled by backend settings through the existing OpenAI-compatible endpoint. Configure `AI_PROVIDER=gemini`, `GEMINI_API_KEY`, a Gemini-supported `AI_MODEL`, and `AI_MODE=live`; a key alone does not enable live mode. With live mode enabled but no key, the backend returns a configuration error before charging. When a live provider fails, it reports a 502 and releases any reserved tokens rather than returning a demo response under a live label. Unsupported listings stay discoverable but are marked non-executable and fail before token reservation. Without live mode/key, supported handlers may return labeled demo results, which are not reviewable.

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

Apply the backend migration with `alembic upgrade head` before starting. Configure shared edge rate limiting for multi-worker hosting; the included per-process limit is only a baseline. See `../agenticxchange/docs/agentmesh-oauth-deployment.md` for the deployment checklist and ChatGPT plan compatibility evidence. Do not change Render or deploy these changes without review and authorization.
