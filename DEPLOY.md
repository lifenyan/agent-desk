# Deploying agentdesk (first deploy, done manually — ADR-009)

Three core services (API + chat UI + manager approvals UI) built from the same Dockerfile,
plus managed Postgres (with pgvector) and managed Redis, plus two optional M8 services off
the same image (MCP server, Slack runner — see "M8 services" below). Railway is the primary
path; Render is the alternative. The CI deploy workflow exists since M4 but ships **inert**
(see "CI deploy" below). (The once-planned M7 AWS migration was dropped — this PaaS runbook
is the deployment story.)

> **M2 note:** the approvals UI (`ui/approval_view.py` — the HITL approve/reject surface) is a
> third service, added below alongside the chat UI. It's optional (API + chat UI deploy fine
> without it), but a full order-approval demo needs it.
>
> **Migrations note (M5/M9):** the schema is now three migrations (0001 initial, 0002 SDK
> sessions, 0003 CMDB graph) — all applied automatically by the image CMD's
> `alembic upgrade head` at boot; nothing extra to run.
>
> **M8 readiness note:** the image now packages `graph/` and `mcp_server/` (the M9 `graph`
> package is imported at API boot; its absence broke image builds between M9 and this fix).

## Prerequisites

- The repo is on GitHub (github.com/lifenyan/agent-desk, public, branch `main`).
- Your `OPENAI_API_KEY`.
- The Docker image already applies migrations at boot (`alembic upgrade head` in the CMD),
  and bundles `scripts/` + `data/` so seeding runs remotely without LLM calls (data is cached
  JSON from M0) and `app/rag/ingest` embeds through the Redis cache.

## Railway (primary)

1. **Create the project** — railway.app → New Project → empty project.
2. **Postgres with pgvector**: add a database → PostgreSQL, then in the service settings set
   the image to `pgvector/pgvector:pg16` and redeploy (Railway's stock postgres image lacks
   the extension; migration 0001 runs `CREATE EXTENSION vector` and will fail without it).
   pg16 keeps prod on the same major as local compose and the CI service containers —
   dev/CI/prod parity is the point. **The volume wipe is mandatory**: Railway's stock
   Postgres (pg18 as of 2026-07) initializes the volume on the service's FIRST boot, and
   pg16 then crash-loops on the newer data dir/config (observed live: `unrecognized
   configuration parameter "autovacuum_worker_slots"`, FATAL on every start — while the
   service badge stays green). After the image swap, wipe the service's volume and
   redeploy so pg16 re-initializes it; harmless before seed/ingest have run. Always check
   the Postgres deploy logs for `database system is ready to accept connections` before
   moving on.
3. **Redis**: add a database → Redis. No config needed (the embedding cache has no TTL and
   tolerates eviction — it just re-embeds).
4. **API service**: New → GitHub Repo → this repo. Railway auto-detects the Dockerfile.
   - Variables:
     - `DATABASE_URL` = `postgresql+psycopg://` + Railway's Postgres connection string with
       the scheme stripped (Railway gives `postgresql://user:pass@host:port/db`; SQLAlchemy
       needs the `+psycopg` driver marker). Build it from service REFERENCES (type `${{` and
       use the autocomplete) — never paste `.env.example`'s localhost value (observed live:
       alembic then dials 127.0.0.1 inside the container and the boot crash-loops). Verify
       the RESOLVED value (eye icon) shows `<service>.railway.internal`, not localhost.
     - `REDIS_URL` = reference the Redis service's `REDIS_URL`.
     - `OPENAI_API_KEY` = your key.
     - Optional (M6 tracing): `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` (+ `LANGFUSE_HOST`
       for a non-EU region). Leave unset and the app runs untraced — empty keys are a clean
       no-op by contract (ADR-042).
   - Settings → Networking: expose the service, target port **8000**.
5. **Seed + ingest (one-time, in this order)** — Railway service → ⋯ → "Shell" (or
   `railway ssh` with the CLI):
   ```bash
   python scripts/seed_db.py      # loads data/*.json (idempotent upsert)
   python -m app.rag.ingest       # chunk + embed articles/tickets (~$0.01, one-time)
   ```
6. **Chat UI service**: New → GitHub Repo → same repo, second service.
   - Settings → Deploy → Custom Start Command — **the `sh -c` wrapper is load-bearing**:
     Railway execs custom start commands without a shell, so a bare `$PORT` reaches
     Streamlit unexpanded (observed live: `'$PORT' is not a valid integer`, crash-loop):
     ```
     sh -c "streamlit run ui/streamlit_app.py --server.port $PORT --server.address 0.0.0.0"
     ```
   - Variables: `API_URL` = `http://<api-service-name>.railway.internal:8000` (private
     networking; the UI talks to the API server-side, so no public URL is needed).
   - Expose the service (Railway sets `$PORT` itself).
7. **Approvals UI service (M2, optional)**: New → GitHub Repo → same repo, third service.
   - Same as the chat UI (including the `sh -c` wrapper), but running `approval_view.py`:
     ```
     sh -c "streamlit run ui/approval_view.py --server.port $PORT --server.address 0.0.0.0"
     ```
   - Same `API_URL` variable; expose the service. This is the manager's approve/reject surface
     — deliberately separate from the chat UI (the approver is not the requester, ADR-005).
     No auth (out of scope for this portfolio project); anyone reaching this URL can approve,
     so keep it private for the demo.
8. **Verify**:
   - `https://<api-domain>/readyz` → `{"status":"ready","checks":{"postgres":"ok","redis":"ok"}}`
   - Chat UI: ask *"how do I reset my password"* → cited answer; *"how do I pair my Bluetooth
     keyboard"* → refusal + ticket offer.
   - HITL (M2): in the chat UI ask *"order me a Tableau license"* → confirm → the agent says
     it's awaiting manager approval. The seeded **$650 Photoshop** order (demo.user@corp.com)
     is already pending, so the **approvals UI** shows something to approve/reject immediately.

## Render (alternative)

1. **Postgres**: New → PostgreSQL (any plan). Render's Postgres ships pgvector — no image
   swap needed. Copy the *internal* connection string; prefix the scheme as
   `postgresql+psycopg://`.
2. **Redis**: New → Key Value (Render's Redis). Copy the internal `redis://` URL.
3. **API**: New → Web Service → this repo, Runtime = Docker. Env vars as in Railway step 4.
   Render routes to the `EXPOSE`d port automatically.
4. **Seed + ingest**: service → Shell tab → same two commands as Railway step 5.
5. **Chat UI**: second Web Service, same repo, Docker command override:
   `streamlit run ui/streamlit_app.py --server.port $PORT --server.address 0.0.0.0`,
   env `API_URL` = the API service's internal address (`http://<api>:8000` on the same
   private network, or the public API URL if not using private networking).
6. **Approvals UI (M2, optional)**: third Web Service, same repo, command override
   `streamlit run ui/approval_view.py --server.port $PORT --server.address 0.0.0.0`,
   same `API_URL`.
7. Verify as above (incl. the M2 HITL check).

## CI deploy (`.github/workflows/deploy.yml` — M4, shipped inert; ADR-029)

The workflow exists but is deliberately **disarmed** until the manual first deploy above has
happened (ADR-009: the first deploy is a learning exercise, done by hand — there is currently
no Railway project, token, or URL for the workflow to talk to). Its behavior today:

- **push to `main`**: the job is skipped unless the repo variable `DEPLOY_ENABLED` is `'true'`.
- **manual `workflow_dispatch`**: always runs, but fails loudly at the first step until the
  secrets/variables below exist — a red run that says exactly what's missing, never a green
  run that deployed nothing.

What an armed run does: validate the Docker image builds → `railway up` the API service →
run `alembic upgrade head` in the deployed environment (explicit release step; the image CMD
also migrates at boot) → poll **`/readyz`** (not `/health` — it doesn't exist) for up to 5
minutes and fail if it never goes ready.

### Arming it (after the manual first deploy)

1. Complete the manual Railway deploy above (project + Postgres + Redis + API service).
2. Repo **secret**: `RAILWAY_TOKEN` — a project token (Railway → project → Settings → Tokens).
3. Repo **variables** (Settings → Secrets and variables → Actions → Variables):
   - `RAILWAY_SERVICE` — the API service name in Railway.
   - `DEPLOY_HEALTH_URL` — the public API base URL, no trailing slash (e.g.
     `https://agentdesk-api.up.railway.app`).
4. Dry-run: trigger **Deploy** via `workflow_dispatch` (Actions tab or
   `gh workflow run deploy.yml`) and watch it go green end-to-end.
5. Flip the repo variable `DEPLOY_ENABLED` to `true` — from then on every push to `main`
   (i.e. every merged PR, which has already passed the CI eval gates) deploys.

Live verification of the workflow is **deferred** until after the manual first deploy
(ADR-009/ADR-029); until then its acceptance is actionlint/dry-run review only. The UI
services redeploy from Railway's own GitHub integration or by re-running their services —
the workflow deploys the API only (the UIs are stateless and rarely change per ADR-005).

## Notes

- **Order matters**: API must boot once (running migrations) before seed/ingest.
- **Both UIs are stateless** and share one image + one `API_URL` — they only differ by start
  command and port, so scaling/redeploy is identical for all three services.
- **Sessions live in Postgres since M5** (ADR-030): conversation history and pending HITL
  state both survive container restarts and redeploys — the M2-era "sessions are ephemeral"
  caveat no longer applies.
- **Costs**: both PaaS free/hobby tiers fit. The only per-request LLM spend is chat + routing
  (gpt-5-mini) — embeddings are one-time + cached.
- **What M4 added**: the inert deploy workflow above, plus the CI eval gates that every PR
  passes before it can be merged (so an armed deploy-on-merge only ever ships gated code).

## M8 services (optional, same image)

Both are optional — the API + UIs deploy and demo fine without them.

- **MCP server** (ADR-040): fourth service off the same repo/image. Custom start command:
  `python -m mcp_server.server`. Variables: `MCP_TOKENS` (e.g. `some-secret=demo.user@corp.com`),
  `MCP_HOST=0.0.0.0` (the default binds loopback), plus the same `DATABASE_URL`/`REDIS_URL`/
  `OPENAI_API_KEY` as the API (its tools hit the DB directly). Expose it with target port
  **8090** (or set `MCP_PORT` to match the platform's assigned port). Clients connect with
  `npx mcp-remote https://<mcp-domain>/mcp --header "Authorization: Bearer <token>"`.
  Remember: tokens are static demo auth (ADR-039) — treat the URL+token pair as a secret.
- **Slack runner** (ADR-038): needs NO public URL (Socket Mode dials out), so the simplest
  setup is running it anywhere — a laptop works — pointed at the deployed API:
  `CHAT_API_URL=https://<api-domain> SLACK_BOT_TOKEN=… SLACK_APP_TOKEN=… python -m app.slack.runner`.
  As a hosted service: same image, start command `python -m app.slack.runner`, those three
  variables, **no exposed port needed**. Slack app setup itself: `SLACK_SETUP.md`.
