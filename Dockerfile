# TODO(M4): finalize for deploy — multi-stage build, non-root user
FROM python:3.12-slim

WORKDIR /app

# pyproject + app/ first: hatchling needs the package present to build the wheel.
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --no-cache-dir .

# Runtime assets: migrations config, UI, and the seed/eval toolchain (used by one-off
# `docker compose exec` / PaaS shell commands — see DEPLOY.md).
COPY alembic.ini ./
COPY ui/ ./ui/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY evals/ ./evals/

EXPOSE 8000
# Apply migrations before serving so a fresh database is always at schema head.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
