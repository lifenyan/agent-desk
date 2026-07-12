.PHONY: dev api ui approvals slack mcp ingest test eval eval-subset ci-local lint db-up db-down migrate generate seed reset metrics langfuse-up langfuse-down

# --- M0: database + data ---------------------------------------------------------------------------

db-up:  ## start Postgres + Redis; data persists in the named volume (never wipes)
	docker compose up -d --wait postgres redis

db-down:  ## stop containers, KEEP the data volume
	docker compose down

migrate:  ## apply Alembic migrations up to head
	alembic upgrade head

generate:  ## (re)generate synthetic data into data/ — LLM calls, cached (see scripts/generate_data.py)
	python scripts/generate_data.py

seed: db-up  ## run migrations if needed, then load data/ into the tables (prints row counts)
	alembic upgrade head
	python scripts/seed_db.py

reset:  ## DESTRUCTIVE: wipe the data volume, restart clean, and reseed (NOT part of normal startup)
	docker compose down -v
	$(MAKE) db-up
	$(MAKE) seed

# --- M1: RAG + app ---------------------------------------------------------------------------------

ingest:  ## rebuild article_chunks (chunk -> embed -> upsert); idempotent, embeds only cache misses
	python -m app.rag.ingest

dev:  ## run FastAPI backend + Streamlit UI together (ctrl-c stops both)
	@trap 'kill 0' EXIT; \
	uvicorn app.main:app --reload --port 8000 & \
	streamlit run ui/streamlit_app.py --server.port 8501; wait

api:  ## backend only
	uvicorn app.main:app --reload --port 8000

ui:  ## Streamlit only (expects the API on :8000, or API_URL set)
	streamlit run ui/streamlit_app.py --server.port 8501

approvals:  ## manager approval view (M2 HITL; expects the API on :8000, or API_URL set)
	streamlit run ui/approval_view.py --server.port 8502

# --- M8: Slack + MCP ---------------------------------------------------------------------------

slack:  ## Socket Mode Slack runner (M8; needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN — see SLACK_SETUP.md — and the API up)
	python -m app.slack.runner

mcp:  ## MCP server on :8090 (M8; needs MCP_TOKENS="token=email" — see README "MCP server")
	python -m mcp_server.server

test:  ## run unit tests
	pytest tests/

eval:  ## FULL eval run: retrieval (40) + routing (30) + e2e (18 flows) + dedup + quality + graph — nightly-sized, use sparingly
	python -m evals.run_evals

eval-subset:  ## the cost-capped PR gate (ADR-026): full retrieval + 10 routing cases, ~$0.07/run measured 2026-07-12 (was $0.10 pre-ADR-047)
	python -m evals.run_evals --subset

ci-local:  ## what ci.yml gates on, runnable locally: lint, format check, tests, eval subset
	ruff check .
	ruff format --check .
	pytest -ra tests/
	python -m evals.run_evals --subset

lint:
	ruff check .

# --- M6: observability -------------------------------------------------------------------------

metrics:  ## headline numbers from Langfuse traces + the M3 cache counters (needs LANGFUSE_* keys)
	python scripts/export_metrics.py

langfuse-up:  ## OPTIONAL local Langfuse UI on :3000 (ADR-042 recommends Cloud; this is the keyless alternative)
	docker compose -f docker-compose.langfuse.yml up -d --wait

langfuse-down:  ## stop the local Langfuse stack, KEEP its volumes
	docker compose -f docker-compose.langfuse.yml down
