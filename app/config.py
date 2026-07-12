"""Application settings via pydantic-settings (DB/Redis URLs, model names, Langfuse keys, HITL threshold)."""
# Implemented in M1. Langfuse keys are loaded here but unused until M6; the HITL threshold until M2.

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config. Every value can come from the environment / .env; defaults suit local dev.

    Note: `app/db/database.py` deliberately reads DATABASE_URL from os.environ itself (kept
    independent of this module so M0 scripts and Alembic run standalone) — the field here exists
    for health checks and docs, and must stay in sync with that default.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- infrastructure ---
    database_url: str = "postgresql+psycopg://itsm:itsm@localhost:5432/itsm"
    redis_url: str = "redis://localhost:6379/0"

    # --- models (LiteLLM names; a bare name goes to OpenAI, "litellm/<provider>/<model>" elsewhere) ---
    triage_model: str = "gpt-5-mini"
    specialist_model: str = "gpt-5-mini"
    embedding_model: str = "text-embedding-3-small"  # must produce EMBED_DIM=1536 vectors
    # Eval judge (M5, ADR-033): deliberately STRONGER than the models it judges — gpt-5-mini
    # scoring its own outputs is a known self-preference bias. gpt-5 is the same-generation
    # full-size sibling of the pinned workhorse (verified served, account model list
    # 2026-07-07); same OPENAI_API_KEY, no new credentials.
    judge_model: str = "gpt-5"

    # --- reasoning effort (M10, ADR-047): where the latency actually lives -----------------
    # gpt-5-family models spend "reasoning tokens" before every visible output, at the API's
    # default effort ("medium") when the request doesn't say otherwise — which this app never
    # did before M10. Span-attributed measurement (ignore/tem/m10_latency_baseline.json)
    # showed 97-99% of chat wall time is LLM generations, dominated by those hidden tokens
    # (tools/handoffs/app overhead are all sub-second), so effort is THE latency knob this
    # architecture leaves free (hop count and instructions are pinned by ADR-003/018/022).
    # Defaults below are measured, not guessed; both are BEHAVIOR changes gated by the eval
    # floors (subset + full routing green at these values — the runs are in ADR-047).
    # The router is pure single-label classification with forced tool choice — "minimal"
    # measured 0 reasoning tokens, and its 6-18s of generation time per conversation fell
    # to 1-3s with routing accuracy unchanged.
    router_reasoning_effort: str = "minimal"
    # Fulfillment + incident: "low" keeps a thinking budget for order forms and the ADR-021
    # dedup gray band while cutting most of the default's reasoning tokens — every gate green
    # at "low" (routing 30/30 x3, dedup 11/12 in historical range, slack 5/5, all e2e
    # order/incident flows). "minimal" was NOT adopted (untested against the gray bands,
    # savings over "low" marginal). Set to "medium" to restore the exact pre-M10 default.
    specialist_reasoning_effort: str = "low"
    # Knowledge is deliberately SEPARATE and stays at the API default: its ADR-017 stage-2
    # coverage judgment measurably DEGRADES at "low" on the facts-injected chat path — the
    # model keeps refusing but decorates the refusal with the forbidden "Sources:" list
    # (5/13 runs at low vs 0 ever observed at medium), which violates the output contract
    # AND leaks the refusal into the semantic cache via the write-time gate. Measured in
    # ignore/tem/m10_smartwatch_http_probe.py; the e2e refusal flow (floor 1.0) is the gate.
    knowledge_reasoning_effort: str = "medium"

    # --- retrieval knobs (ADR-011 / ADR-016; tuned against `make eval`, not vibes) ---
    retrieval_top_k: int = 5
    rrf_k: int = 60
    # Refusal gate = best cosine similarity among retrieved chunks (NOT the rank-based RRF score).
    retrieval_refusal_threshold: float = 0.45

    # --- caching (M3; the embedding cache has no knobs — exact-match, no TTL) ---
    # Semantic-cache similarity gate (ADR-023), measured not vibed (ADR-017/021 discipline;
    # evidence in ignore/tem/m3_semantic_cache_demo.py): tight paraphrases of a stored question
    # score >= 0.816, near-miss neighbors ("change my wifi password") <= 0.672, and LOOSE
    # paraphrases OVERLAP the near-miss band — so 0.75 = midpoint of the only separable gap.
    # A false HIT serves the wrong answer; a false miss just re-runs the agent.
    semantic_cache_threshold: float = 0.75
    semantic_cache_ttl_seconds: int = (
        86_400  # 24 h — KB answers go stale slowly (+ ingest invalidation)
    )
    response_cache_ttl_seconds: int = 300  # 5 min — catalog/asset reads (ADR-025)

    # --- CMDB graph (M9, ADR-037) ---
    # Traversal backend for query_dependency_graph: "postgres" (recursive CTE — the default;
    # no extra infrastructure) or "neo4j" (requires the compose neo4j service + a sync run:
    # `python -m graph.sync_neo4j`). Neo4j absent => the postgres path is unaffected and the
    # neo4j path fails loudly, never silently.
    graph_backend: str = "postgres"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "agentdesk"  # dev default; mirrors docker-compose NEO4J_AUTH

    # --- Slack integration (M8, ADR-038/039) ---
    # Both tokens empty => Slack is OFF: the runner refuses to start and post_slack_message
    # no-ops with a logged error dict (CI and local dev run Slack-less by design).
    slack_bot_token: str = ""  # xoxb-… — WebClient auth (thread reads + posting replies)
    slack_app_token: str = ""  # xapp-… — Socket Mode connection (runner only)
    slack_trigger_emoji: str = "ticket"  # reaction name that triggers ingestion (:ticket: 🎫)
    # Test seam: when set, post_slack_message appends JSON lines here instead of calling Slack —
    # how the slack eval suite asserts reply content with no live workspace (ADR-039).
    slack_sink_file: str = ""
    # The chat API the Slack runner posts to — the runner is a pure HTTP client of the normal
    # pipeline (router → incident), exactly like the Streamlit UI. No second identity path.
    chat_api_url: str = "http://localhost:8000"

    # --- MCP server (M8) ---
    mcp_host: str = "127.0.0.1"  # loopback for local dev; a deployed service sets 0.0.0.0
    mcp_port: int = 8090
    # Static bearer-token → acting-user map: "token=email[,token=email…]". Full multi-user
    # auth (OAuth, token issuance/rotation) is deliberately out of scope — see ADR-039.
    mcp_tokens: str = ""

    # --- observability (M6, ADR-042/045) ---
    # Both keys empty => tracing is a verified NO-OP (nothing registered, langfuse never even
    # imported) — the CI contract: no secrets, still green, no network calls added anywhere.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # Per-CONVERSATION (session) spend alert: crossing this logs a loud warning + attaches a
    # WARNING event to the Langfuse trace that crossed it. 0 disables. Default is ~10x a
    # typical multi-turn gpt-5-mini conversation (baseline.json: most flows bill < $0.01).
    cost_alert_threshold_usd: float = 0.10
    # A/B seam (ADR-044): deliberately turn OFF all three caches (embedding/semantic/response)
    # — reads miss, writes skip, and stats counters are NOT bumped (a chosen OFF is not a
    # miss). Distinct from Redis-down degradation on purpose: the caching A/B's OFF arm must
    # be a clean decision, not a simulated outage full of warning logs.
    caches_disabled: bool = False

    # --- app ---
    app_env: str = "dev"
    log_level: str = "INFO"
    hitl_approval_threshold_usd: float = 500.0  # M2

    @field_validator(
        "triage_model",
        "specialist_model",
        "embedding_model",
        "judge_model",
        "router_reasoning_effort",
        "specialist_reasoning_effort",
        "knowledge_reasoning_effort",
        "hitl_approval_threshold_usd",
        "retrieval_refusal_threshold",
        "graph_backend",
        "slack_trigger_emoji",
        "chat_api_url",
        "mcp_port",
        mode="before",
    )
    @classmethod
    def _empty_env_means_default(cls, v: object, info) -> object:
        """`.env` ships `TRIAGE_MODEL=` (blank) placeholders; treat blank as 'use the default'."""
        if isinstance(v, str) and v.strip() == "":
            return cls.model_fields[info.field_name].default
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
