# One image, all services: API (default CMD), the two Streamlit UIs, the optional MCP
# server, and the optional Slack runner — start command selects the role (DEPLOY.md).
FROM python:3.12-slim

WORKDIR /app

# pyproject + the packaged source first: hatchling needs the packages present to build the
# wheel (app/ + graph/ + mcp_server/ — graph is imported by app.tools.graph_tools, so it is
# load-bearing at API boot, not an optional extra).
COPY pyproject.toml ./
COPY app/ ./app/
COPY graph/ ./graph/
COPY mcp_server/ ./mcp_server/
RUN pip install --no-cache-dir .

# Runtime assets: migrations config, UI, and the seed/eval toolchain (used by one-off
# `docker compose exec` / PaaS shell commands — see DEPLOY.md).
COPY alembic.ini ./
COPY ui/ ./ui/
COPY .streamlit/ ./.streamlit/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY evals/ ./evals/

EXPOSE 8000
# Apply migrations before serving so a fresh database is always at schema head.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
