.PHONY: dev api ui approvals ingest test eval lint db-up db-down migrate generate seed reset

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

test:  ## run unit tests
	pytest tests/

eval:  ## run eval suites (M1: retrieval; the CI --subset flag arrives in M4)
	python -m evals.run_evals

lint:
	ruff check .
