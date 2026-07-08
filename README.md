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
│   │   ├── catalog_tools.py          # list_catalog_items, place_catalog_order, request_approval
│   │   └── graph_tools.py            # M9: query_dependency_graph (CMDB impact / root cause)
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
│   │   └── migrations/               # env.py + versions/0001_initial, 0002_sessions, 0003_cmdb_graph
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
│   │   ├── quality.jsonl             # 10 LLM-as-judge cases (ADR-033)
│   │   └── graph.jsonl               # 15 multi-hop impact/root-cause cases + ground truth (ADR-036)
│   ├── run_evals.py                  # CLI: full suite, --subset for CI, --out for JSON results
│   ├── suite_e2e.py                  # side-effect assertions through a live uvicorn (ADR-027)
│   ├── suite_dedup.py                # incident-agent gray-band judgment (ADR-028)
│   ├── suite_quality.py              # faithfulness + helpfulness, judged by gpt-5 (ADR-033)
│   ├── suite_graph.py                # M9: plain RAG vs Graph-RAG three-way comparison (ADR-036)
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
└── graph/                            # M9: dependency traversal — postgres_graph.py (recursive CTE),
                                      #     neo4j_graph.py (Cypher), sync_neo4j.py (Postgres -> Neo4j)
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
| **graph** | 15 × 3 arms | plain RAG F1 **0.44–0.64** vs Graph-RAG (CTE and Neo4j) **1.000**, 45/45 exact sets — see the comparison section below² | $0.15 | 10.3 s / 21.5 s |

¹ e2e conversations bill inside the suite-spawned server, invisible to the HTTP client — that
cost gap is closed by the M6 Langfuse wiring.
² graph numbers are the M9 three-run baseline (2026-07-07), not part of the committed M5
`baseline.json` full run. All agentic metrics vary run to run (that's
LLMs); the floors gate regressions, not perfection.

## Plain RAG vs Graph-RAG on multi-hop questions

The flagship experiment (M9, ADR-035/036/037). An IT outage question like *"db-server-01 is
down — which services and teams are impacted?"* is a **multi-hop join**: db-server-01 hosts
auth-db and ldap-db → auth-service and ldap-directory use those → six more services call
auth-service → four teams use those services. The CMDB dependency graph (59 CIs, 82 edges,
`cis` + `dependencies` tables) makes that one recursive traversal; plain RAG has to assemble
it from prose.

**The comparison is deliberately fair to RAG**: seven runbook articles in the KB document
*every one-hop fact in the graph* (each service's servers, databases, callers, teams, plus a
database-hosting map — deterministic templates, so no garbled facts). Plain RAG has all the
information; what it lacks is the join. 15 committed questions
([`evals/datasets/graph.jsonl`](evals/datasets/graph.jsonl), ground-truth impact sets computed
from the seeded graph and hand-checked) are asked identically to three arms, and answers are
scored by closed-universe CI-name extraction → set precision/recall/F1. Three same-day runs
(gpt-5-mini, 2026-07-07):

| Arm | Retrieval mechanism | F1 (3 runs) | Exact-set rate | Refusals |
|---|---|---|---|---|
| Plain RAG | knowledge agent, hybrid search over articles | 0.44 · 0.64 · 0.47 | 0.07–0.40 | 3–5 of 15 |
| **Graph-RAG (CTE)** | incident agent + `query_dependency_graph`, Postgres recursive CTE | **1.00 · 1.00 · 1.00** | **1.00** | 0 |
| **Graph-RAG (Neo4j)** | same tool over Cypher (`GRAPH_BACKEND=neo4j`) | **1.00 · 1.00 · 1.00** | **1.00** | 0 |

**Where the gap is:** deep, wide cases. The db-server-01 question (18 impacted CIs, 4 hops)
scored F1 0.20 for plain RAG on *every* run — retrieval surfaces the hosting map and the auth
runbook, but assembling 18 names across four articles inside a top-k=5 chunk budget doesn't
happen. And in 3–5 of 15 cases per run the knowledge agent **refused outright**: its grounding
contract (ADR-017) judges chained runbook evidence as insufficient coverage — which makes
plain RAG's multi-hop score not just lower but *high-variance*, since that refusal is a coin
flip. **Where it isn't:** shallow questions answerable from one runbook (crm-db, 2 hops:
0.75–1.00) — if your questions are one-hop, you don't need a graph.

**CTE vs Neo4j:** identical answers (LLM-free parity check on every case, plus a
synthetic-cycle test that caught a real divergence — Cypher's relationship isomorphism
re-emits the *start node* when a cycle closes; the CTE's path guard never does). Identical
tool-level latency at this scale (~2–3 ms p50 both, measured LLM-free, 20 reps × every case).
The traversal is 16 lines of SQL vs 5 of Cypher — Cypher wins ergonomics — but the Neo4j path
costs a compose service, a sync script, credentials, and a staleness failure mode, so
**Postgres is the default** and Neo4j stays an optional, parity-tested backend
(ADR-037 details when a graph DB would earn that cost: unbounded depth, varied graph-shaped
queries, graph-as-the-product). The suite runs nightly (`SUITES["graph"]`); the Neo4j arm
self-skips where the server is absent (e.g. CI).

## Milestones

| # | Milestone | Scope | Status |
|---|-----------|-------|--------|
| M0 | Data & schema | Synthetic dataset generation, DB models + migration, seed script | ✅ done |
| M1 | Core loop | Router + knowledge agent, RAG pipeline (chunk/embed/hybrid search), chat API, Streamlit UI, embedding cache, retrieval evals, deploy prep (`DEPLOY.md`) | ✅ done |
| M2 | Action agents | Fulfillment + incident agents, deterministic tools, HITL approvals (> $500) | ✅ done |
| M3 | Caching | Semantic cache + response cache (embedding cache landed in M1) | ✅ done |
| M4 | CI & evals | Routing + e2e + dedup eval suites, floors in `thresholds.toml`, CI subset gate, nightly workflow | ✅ done |
| M5 | Memory + full eval harness | SDK sessions in Postgres, `user_facts` inject/extract, quality suite (LLM-as-judge), per-case cost/latency, committed baseline | ✅ done |
| M6 | Observability | Langfuse traces, dashboards; cross-check harness cost/latency | |
| M7 | AWS migration | Move off the Railway plan onto AWS (first deploy still manual per ADR-009) | |
| M8 | MCP + Slack + guardrails | Expose ITSM tools over MCP, Slack flow, input guardrails | ← next |
| M9 | Graph-RAG | CMDB dependency graph (Postgres CTE + optional Neo4j), graph tool, three-way RAG comparison | ✅ done |
