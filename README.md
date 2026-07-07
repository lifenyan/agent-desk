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
├── DECISIONS.md                      # architecture decision log (18 ADRs)
├── DEPLOY.md                         # M1: manual Railway/Render deploy runbook (ADR-009)
├── .gitignore
├── .env.example                      # DATABASE_URL, REDIS_URL, LLM keys, LANGFUSE keys
├── pyproject.toml
├── alembic.ini                       # Alembic config (DB URL injected from $DATABASE_URL)
├── Makefile                          # db-up · seed · reset · migrate · generate · test · lint
├── Dockerfile
├── docker-compose.yml                # app + postgres(pgvector) + redis
├── .github/workflows/
│   ├── ci.yml                        # M4: lint, tests, eval subset on PR
│   └── deploy.yml                    # M4: deploy on merge to main
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
│   │   ├── session_store.py          # SDK sessions backed by Postgres
│   │   ├── user_facts.py             # long-term memory CRUD
│   │   └── extraction.py             # end-of-session fact extraction + dedup
│   ├── db/
│   │   ├── database.py               # engine/session factory
│   │   ├── models.py                 # 9 tables: users, assets, knowledge_articles, article_chunks,
│   │   │                             # catalog_items, orders, tickets, ticket_comments, user_facts
│   │   └── migrations/               # env.py + versions/0001_initial_schema.py
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
│   │   ├── retrieval.jsonl           # query -> expected article ids
│   │   ├── routing.jsonl             # query -> expected specialist
│   │   └── e2e.jsonl                 # query -> expected side effects
│   ├── run_evals.py                  # CLI: full suite or --subset for CI
│   └── metrics.py                    # recall@k, MRR, routing accuracy, handoff ping-pong rate
├── tests/
│   ├── conftest.py
│   ├── test_tools.py
│   ├── test_chunking.py
│   ├── test_cache.py
│   └── test_memory.py
├── data/                             # M0: generated dataset (cached JSON) + taxonomy — rebuild via `make seed`
├── design/                           # DATA_DICTIONARY.md · database_erd.png · architecture diagrams
├── ignore/                           # git-ignored local scratch / notes
├── mcp_server/
│   └── server.py                     # M8: expose ITSM tools over MCP + Slack flow
└── graph/                            # M9: CMDB Graph-RAG (Neo4j)
```

## Milestones

| # | Milestone | Scope | Status |
|---|-----------|-------|--------|
| M0 | Data & schema | Synthetic dataset generation, DB models + migration, seed script | ✅ done |
| M1 | Core loop | Router + knowledge agent, RAG pipeline (chunk/embed/hybrid search), chat API, Streamlit UI, embedding cache, retrieval evals, deploy prep (`DEPLOY.md`) | ✅ done |
| M2 | Action agents | Fulfillment + incident agents, deterministic tools, HITL approvals (> $500) | ← next |
| M3 | Caching | Semantic cache + response cache (embedding cache landed in M1) | |
| M4 | CI & evals | Routing + e2e eval suites, CI subset, GitHub Actions lint/test/eval-subset + deploy | |
| M5 | Memory | SDK sessions in Postgres, `user_facts` long-term memory (inject/extract) | |
| M6 | Observability | Langfuse traces, cost + latency logging | |
| M7 | Guardrails | Input guardrails, prompt-injection screening | |
| M8 | MCP | Expose ITSM tools over MCP + Slack flow | |
| M9 | Graph-RAG | CMDB graph in Neo4j, graph-augmented retrieval | |
