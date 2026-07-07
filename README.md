# agentdesk

**An AI-powered ITSM service desk (a mini ServiceNow): a router agent routes each query to specialist agents for knowledge search, catalog fulfillment, and incident management — with hybrid RAG, semantic caching, long-term memory, and human-in-the-loop approvals.**

## Tech Stack

- **Agent framework:** OpenAI Agents SDK (agents, handoffs, guardrails, sessions), models via LiteLLM
- **Backend:** FastAPI · **Frontend:** Streamlit
- **Database:** Postgres + pgvector (relational + vector + full-text search in one DB)
- **Cache:** Redis (embedding cache, semantic cache, response cache)
- **Observability:** Langfuse · **Evals:** custom harness run in CI · **CI/CD:** GitHub Actions
- **Later milestones:** MCP server, Neo4j Graph-RAG

## Architecture

A tool-less **router agent** classifies each query and hands off to one of three specialists:

- **Knowledge agent** — query expansion, hybrid RAG (pgvector + FTS with reciprocal rank fusion), citations
- **Fulfillment agent** — reads user assets, pre-fills catalog orders, human-in-the-loop approval for orders > $500
- **Incident agent** — summarize, dedup via ticket embedding similarity, create/link tickets

Agents call deterministic tools; **tools are the only DB access path**. A semantic cache is checked before any agent runs (read-only intents only). Short-term memory = SDK sessions in Postgres; long-term memory = a hand-rolled `user_facts` table (injected at session start, extracted at session end).

## File Tree

```
agentdesk/
├── README.md
├── CLAUDE.md                         # working brief + live status (auto-loaded by Claude Code)
├── DECISIONS.md                      # architecture decision log (34 ADRs)
├── DEPLOY.md                         # M1: manual Railway/Render deploy runbook (ADR-009)
├── .gitignore
├── .env.example                      # DATABASE_URL, REDIS_URL, LLM keys, LANGFUSE keys
├── pyproject.toml
├── alembic.ini                       # Alembic config (DB URL injected from $DATABASE_URL)
├── Makefile                          # db-up · seed · reset · migrate · generate · test · lint
├── Dockerfile
├── docker-compose.yml                # app + ui + approvals + postgres(pgvector) + redis
├── .github/workflows/
│   ├── ci.yml                        # M4: lint, tests, eval subset on PR
│   ├── nightly.yml                   # M4: all five eval suites, nightly + on dispatch
│   └── deploy.yml                    # M4: deploy on merge to main (inert until armed, ADR-029)
├── app/
│   ├── main.py                       # FastAPI entrypoint
│   ├── config.py                     # settings via pydantic-settings
│   ├── api/
│   │   ├── routes_chat.py            # POST /chat — session load, cache check, run router
│   │   ├── routes_approvals.py       # list/approve/reject pending orders (HITL)
│   │   └── routes_health.py
│   ├── agents/
│   │   ├── router.py                 # tool-less agent: classify intent, hand off to 3 specialists
│   │   ├── knowledge.py
│   │   ├── fulfillment.py
│   │   ├── incident.py
│   │   ├── guardrails.py             # input guardrails (prompt-injection screen)
│   │   └── context.py                # per-run context object (user_id, facts, session)
│   ├── tools/
│   │   ├── knowledge_tools.py        # search_knowledge_articles, get_release_notes
│   │   ├── user_tools.py             # get_user_profile, get_user_assets
│   │   ├── ticket_tools.py           # create_ticket, update_ticket, search_similar_tickets
│   │   └── catalog_tools.py          # list_catalog_items, place_catalog_order, request_approval
│   ├── rag/
│   │   ├── chunking.py
│   │   ├── embeddings.py             # embedding client, wrapped by embedding cache
│   │   ├── hybrid_search.py          # pgvector + FTS with reciprocal rank fusion
│   │   └── ingest.py                 # article -> chunks -> embeddings pipeline
│   ├── cache/
│   │   ├── redis_client.py
│   │   ├── embedding_cache.py        # M3: hash(text) -> vector
│   │   ├── semantic_cache.py         # M3: similarity-matched query cache, TTL + invalidation
│   │   └── response_cache.py         # M3: TTL cache for catalog/asset lookups
│   ├── memory/
│   │   ├── session_store.py          # M5: SDK SQLAlchemySession on Postgres (ADR-030)
│   │   ├── user_facts.py             # M5: long-term memory CRUD + deterministic merge rule
│   │   └── extraction.py             # M5: post-response fact extraction (ADR-031)
│   ├── db/
│   │   ├── database.py               # engine/session factory
│   │   ├── models.py                 # 9 tables: users, assets, knowledge_articles, article_chunks,
│   │   │                             # catalog_items, orders, tickets, ticket_comments, user_facts
│   │   └── migrations/               # env.py + versions/0001_initial_schema, 0002_agent_sessions
│   └── observability/
│       └── tracing.py                # Langfuse setup, cost/latency logging
├── ui/
│   ├── streamlit_app.py              # chat UI
│   └── approval_view.py              # manager approval card for HITL
├── scripts/
│   ├── generate_data.py              # M0: two-stage LLM dataset generator (cached to data/)
│   └── seed_db.py                    # M0: load data/ into Postgres (idempotent upsert)
├── evals/
│   ├── datasets/
│   │   ├── retrieval.jsonl           # 40 cases: query -> expected article ids (+ refusal probes)
│   │   ├── routing.jsonl             # 30 cases: query -> expected specialist
│   │   ├── e2e.jsonl                 # 18 flows: conversation -> expected DB side effects
│   │   ├── dedup.jsonl               # 12 gray-band link/trap probes (ADR-028)
│   │   └── quality.jsonl             # 10 LLM-as-judge cases (ADR-033)
│   ├── run_evals.py                  # CLI: full suite, --subset for CI, --out for JSON results
│   ├── suite_e2e.py                  # side-effect assertions through a live uvicorn (ADR-027)
│   ├── suite_dedup.py                # incident-agent gray-band judgment (ADR-028)
│   ├── suite_quality.py              # faithfulness + helpfulness, judged by gpt-5 (ADR-033)
│   ├── judge_prompt.md               # the committed judge rubric (verbatim instructions)
│   ├── thresholds.toml               # eval floors — single source of truth (ADR-026)
│   ├── results/baseline.json         # committed full-run baseline (per-case cost/latency)
│   ├── common.py                     # dataset loading, floors, price table, cost/latency helpers
│   └── metrics.py                    # recall@k, MRR, percentile
├── tests/
│   ├── conftest.py
│   ├── test_agents.py
│   ├── test_tools.py
│   ├── test_chunking.py
│   ├── test_retrieval.py
│   ├── test_cache.py
│   └── test_memory.py                # M5: session store, fact merge rule, injection plumbing
├── data/                             # M0: generated dataset (cached JSON) + taxonomy — rebuild via `make seed`
├── design/                           # DATA_DICTIONARY.md · database_erd.png · architecture diagrams
├── ignore/                           # git-ignored local scratch / notes
├── mcp_server/
│   └── server.py                     # M8: expose ITSM tools over MCP + Slack flow
└── graph/                            # M9: CMDB Graph-RAG (Neo4j)
```

## Eval Results

Committed baseline (2026-07-07, one full `make eval` run: **33.5 min wall, $0.42 metered LLM
spend**, all floors green). Models: gpt-5-mini (agents) · text-embedding-3-small ·
gpt-5 (judge). Per-case detail lives in [`evals/results/baseline.json`](evals/results/baseline.json);
the pass/fail floors — regression gates set below observed run-to-run variance — in
[`evals/thresholds.toml`](evals/thresholds.toml).

| Suite | Cases | Headline metrics | Metered cost | Case latency p50 / p95 |
|---|---|---|---|---|
| **retrieval** | 40 | recall@5 **1.000** · MRR **0.983** · refusals **10/10** · false refusals **0** | $0.03 | 0.01 s / 11.2 s |
| **routing** | 30 | accuracy **1.000** (hard cases 6/6) · ping-pong 0 · integrity failures 0 · wrong-handoff matrix fully diagonal | $0.20 | 23.8 s / 33.7 s |
| **e2e** | 18 flows | **18/18** side-effect contracts through the live HTTP API — HITL order approve/reject, ≤$500 auto-place + form validation, dedup link-vs-create, ticket update, refusal→ticket edge, multi-intent, memory carryover across sessions, chat-history survival across a server restart | n/a¹ | 34.1 s / 49.7 s |
| **dedup** | 12 | gray-band judgment **9/12** (observed range 8–12 across runs — genuinely variable, tracked as a trend) | $0.07 | 21.2 s / 31.2 s |
| **quality** | 10 | faithfulness **4.5/5** · helpfulness **4.8/5** (LLM-as-judge: gpt-5; report-only until variance data supports a floor) | $0.13 | 21.6 s / 28.9 s |

¹ e2e conversations bill inside the suite-spawned server, invisible to the HTTP client — that
cost gap is closed by the M6 Langfuse wiring. All agentic metrics vary run to run (that's
LLMs); the floors gate regressions, not perfection.

## Milestones

| # | Milestone | Scope | Status |
|---|-----------|-------|--------|
| M0 | Data & schema | Synthetic dataset generation, DB models + migration, seed script | ✅ done |
| M1 | Core loop | Router + knowledge agent, RAG pipeline (chunk/embed/hybrid search), chat API, Streamlit UI, embedding cache, retrieval evals, deploy prep (`DEPLOY.md`) | ✅ done |
| M2 | Action agents | Fulfillment + incident agents, deterministic tools, HITL approvals (> $500) | ✅ done |
| M3 | Caching | Semantic cache + response cache (embedding cache landed in M1) | ✅ done |
| M4 | CI & evals | Routing + e2e + dedup eval suites, floors in `thresholds.toml`, CI subset gate, nightly workflow | ✅ done |
| M5 | Memory + full eval harness | SDK sessions in Postgres, `user_facts` inject/extract, quality suite (LLM-as-judge), per-case cost/latency, committed baseline | ✅ done |
| M6 | Observability | Langfuse traces, dashboards; cross-check harness cost/latency | ← next |
| M7 | AWS migration | Move off the Railway plan onto AWS (first deploy still manual per ADR-009) | |
| M8 | MCP + Slack + guardrails | Expose ITSM tools over MCP, Slack flow, input guardrails | |
| M9 | Graph-RAG | CMDB graph in Neo4j, graph-augmented retrieval | |
