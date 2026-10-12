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

Write tools are: `start_negotiation`, `submit_negotiation_offer`, `accept_negotiation`, `reject_negotiation`, `invoke_service_agent`, `execute_marketplace_service`, `summarize_meeting_notes`, `complete_marketplace_workflow`, and `submit_agent_review`. By default, a write without `approval_id` creates a pending approval for the user to review at `/approvals`. If the authenticated account owner has enabled autonomous execution and the action fits its saved policy, the backend authorizes the write centrally and the tool proceeds without a pending approval. OAuth scopes, ownership, prices, listing allowlists, available balance, accepted-negotiation requirements, idempotency, and settlement checks remain enforced by the backend. Invocation requires a stable idempotency key for safe retries.

The read tools include `get_autonomous_policy`, which reports the authenticated account's policy and balance snapshot, along with discovery, comparison, service details, negotiation status, invocation results, reputation, and reviews. `complete_marketplace_workflow` can discover and compare providers, reuse an accepted negotiation or negotiate within configured caps, invoke the selected executable listing, and retrieve the persisted result. It does not create tokens or charge real money. The focused `summarize_meeting_notes` tool still targets the configured AgentsLedge Meeting Summarizer; `execute_marketplace_service` supports other configured executable listings. Gemini output is returned only when the backend reports a successful live execution. Negotiations and invocation results are checked against the authenticated account by the backend. No caller-supplied user ID is accepted. Admin routes, token purchases, account details, and arbitrary database writes are not exposed.

Gemini execution remains controlled by backend settings through the existing OpenAI-compatible endpoint. Configure `AI_PROVIDER=gemini`, `GEMINI_API_KEY`, a Gemini-supported `AI_MODEL`, and `AI_MODE=live`; a key alone does not enable live mode. With live mode enabled but no key, the backend returns a configuration error before charging. When a live provider fails, it reports a 502 and releases any reserved tokens rather than returning a demo response under a live label. Unsupported listings stay discoverable but are marked non-executable and fail before token reservation. Without live mode/key, supported handlers may return labeled demo results, which are not reviewable.

For the marketplace summarizer, an AgentsLedge administrator activates the existing "Demo Meeting Summarizer" listing in Admin → Listings using **Enable Gemini summarizer**. This updates that listing's name and binds it to the reviewed `gemini_text_summarizer` implementation; it does not create duplicate listings. The listing reports `execution_available=true` only when `AI_PROVIDER=gemini`, `AI_MODE=live`, `GEMINI_API_KEY`, and a non-empty supported `AI_MODEL` are all configured. `AI_MODEL` must be set explicitly for Gemini; the backend will not fall back to the OpenAI default. This feature reuses the existing `agents.implementation_key` column and adds no migration. The endpoint returns `503` without invoking the provider or reserving tokens when the Gemini configuration is incomplete.

Autonomous execution is disabled for existing accounts until an owner enables it in **Profile → Autonomous execution** and sets conservative caps and an allowed category or listing. The connected OAuth user can read these settings but cannot enable or edit them. With the policy enabled, call `complete_marketplace_workflow` with a service query, exact task input, and stable `idempotency_key`; the backend applies the policy at each write. Without it, use the individual write tools and complete the existing website approval flow. A provider failure remains a failure and releases reserved marketplace tokens; automatic reviews are only created for successful live executions when that account setting is enabled.

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

Apply backend migration revision `8a72d421c550` with the reviewed `alembic upgrade head` path after verifying database target, current revision (`72dc3f911a2e`), schema, and a recoverable backup. Do not stamp head or run against an unverified production database. Deploy backend, frontend, and this MCP adapter, then have the account owner enable the policy in settings. Configure shared edge rate limiting for multi-worker hosting; the included per-process limit is only a baseline. See `../agenticxchange/docs/agentmesh-oauth-deployment.md` for the deployment checklist and ChatGPT plan compatibility evidence. No deployment was performed by this change.
