# ITSM AI Service Desk

An AI-powered IT service management desk where a multi-agent system answers knowledge questions, fulfills software orders, and manages incident tickets end to end.

Built as a portfolio project to demonstrate applied AI engineering: multi-agent orchestration with LLM-driven handoffs, hybrid RAG, three-layer caching, hand-rolled long-term memory, human-in-the-loop approvals, evaluation harnesses in CI, observability, MCP integration, and Graph-RAG over a CMDB dependency graph.

> **Status:** in development. See the [milestones](#milestones) table for current progress.

---

## What it does

A user chats with the service desk. Behind the scenes:

1. **Session layer** — user facts are loaded from long-term memory, input guardrails screen the query, and a semantic cache answers repeated read-only questions instantly (~50ms instead of a full agent run).
2. **Router agent** — a tool-less router that classifies intent and hands off to one specialist.
3. **Specialist agents** —
   - **Knowledge agent**: expands the query, runs hybrid RAG (vector + full-text) over knowledge articles, answers with citations, and refuses to improvise when no article covers the question.
   - **Fulfillment agent**: reads the user's assets to resolve OS, pre-fills catalog order forms from profile + conversation, and pauses for human approval on orders over $500.
   - **Incident agent**: summarizes issues (including Slack threads via MCP), checks for duplicate open tickets by embedding similarity, and links or creates tickets accordingly.
4. **Response** — traced in Langfuse, written to the semantic cache, and new stable user facts extracted into long-term memory.

## Architecture

```
        Chat UI (Streamlit)          Slack (MCP, M8)
                 │                         │
                 ▼                         │
        FastAPI backend  ◄─────────────────┘
   (sessions · guardrails · semantic cache)
                 │
                 ▼
  ┌──────────  Agent layer — OpenAI Agents SDK  ──────────┐
  │                    Router agent                        │
  │            (tool-less, routes via handoffs)            │
  │         ┌──────────────┼──────────────┐                │
  │         ▼              ▼              ▼                │
  │   Knowledge      Fulfillment      Incident             │
  │     agent           agent           agent              │
  └─────────┼──────────────┼──────────────┼────────────────┘
            ▼              ▼              ▼
              Deterministic tools (only DB path)
            │              │              │
            ▼              ▼              ▼
   Postgres + pgvector    Redis        Neo4j (M9)
   (relational · vectors  (3-layer     (CMDB graph)
    · full-text search)    cache ·
                           sessions)

   Cross-cutting: Langfuse (traces, cost, latency) · GitHub Actions (tests, evals, deploy)
```

Design invariants:

- **Tools are the only database access path.** Agents never touch the DB directly; every side effect goes through a typed, unit-tested Python function.
- **The router agent has no tools.** Its only job is classification and handoff, so it can't half-answer things it should route.
- **The semantic cache runs before any agent** and serves read-only intents only — a cached answer can never place an order or create a ticket.
- **Human-in-the-loop**: orders above $500 write a pending approval and end the run; a manager approves or rejects before the order is placed.

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Agent framework | OpenAI Agents SDK (+ LiteLLM) | agents, handoffs, guardrails, sessions; model-agnostic via LiteLLM |
| Backend | FastAPI | REST API, session middleware, cache check |
| Frontend | Streamlit | chat UI + manager approval view |
| Database | Postgres 16 + pgvector | relational data, vectors (HNSW), and full-text search in one engine |
| Cache | Redis | embedding cache · semantic cache · response cache |
| Graph (M9) | Neo4j | CMDB dependency graph; Postgres recursive CTEs first, then Cypher |
| Observability | Langfuse | per-call traces, tokens, cost, latency; cache hit rates |
| Evals | Custom harness | recall@k, MRR, routing accuracy, e2e side effects, LLM-as-judge |
| CI/CD | GitHub Actions | lint + tests + eval subset on PR; nightly full evals; deploy on merge |
| Deployment | Railway/Render → AWS (M7) | ECS Fargate, RDS, ElastiCache via Terraform |
| Integrations | MCP (official Python SDK) | Slack ingest; ITSM tools exposed as an MCP server |

## Data model

Core tables: `users`, `assets`, `knowledge_articles`, `article_chunks`, `catalog_items`, `orders`, `tickets`, `user_facts`.

Notable columns:

- `article_chunks.embedding vector(1536)` (HNSW index) **and** `article_chunks.tsv tsvector` (GIN index) — the pair that makes hybrid search a single SQL query with reciprocal rank fusion.
- `tickets.embedding` — powers duplicate-incident detection at creation time.
- `catalog_items.form_schema` (JSONB) — describes each item's order form so the fulfillment agent fills forms generically.
- `orders.approval_state` — `not_required / pending / approved / rejected`; the persistence behind the HITL pause.
- `user_facts` — hand-rolled long-term memory: `{fact_type, fact, confidence, updated_at}` per user, injected at session start, extracted and deduped at session end.

M9 adds `services`, `servers`, and a `dependencies` edge table for the CMDB graph.

## Repository layout

```
app/
  agents/         router, knowledge, fulfillment, incident, guardrails
  tools/          deterministic tools — the only DB access path
  rag/            chunking, embeddings, hybrid search (vector + FTS + RRF), ingest
  cache/          embedding cache, semantic cache, response cache
  memory/         SDK sessions (Postgres), user_facts long-term memory
  db/             SQLAlchemy models, Alembic migrations
  api/            /chat, /approvals, /health
  observability/  Langfuse tracing
ui/               Streamlit chat + approval view
scripts/          synthetic data generation, DB seeding
evals/            datasets (retrieval, routing, e2e, quality), runner, metrics
tests/            unit tests for tools, chunking, cache, memory
mcp_server/       ITSM tools exposed over MCP + Slack flow (M8)
graph/            CMDB Graph-RAG (M9)
infra/            Terraform for AWS (M7)
```

## Milestones

| # | Milestone | Highlights | Status |
|---|---|---|---|
| 0 | Data generation | schema, migrations, LLM-generated synthetic dataset with deliberate near-duplicates | ☐ |
| 1 | RAG + chat UI + first deploy | hybrid search (pgvector + FTS + RRF), citations, Langfuse tracing, Railway/Render deploy | ☐ |
| 2 | Multi-agent + tools + HITL | router handoffs to 3 specialists, guardrails, >$500 approval flow | ☐ |
| 3 | Memory + caching | 3-layer cache with invalidation, sessions in Postgres, hand-rolled user facts | ☐ |
| 4 | CI/CD | lint + tests + cost-capped eval subset on PR, nightly full evals, auto-deploy | ☐ |
| 5 | Full evaluation | ~100 cases: retrieval, routing, e2e side effects, LLM-as-judge; baseline table | ☐ |
| 6 | Observability | p50/p95 latency, cost per resolved request, caching on/off comparison | ☐ |
| 7 | AWS migration | Terraform: ECS Fargate, RDS, ElastiCache; PaaS → AWS writeup | ☐ |
| 8 | MCP + Slack | Slack thread → dedup → ticket → reply; ITSM tools as an MCP server | ☐ |
| 9 | Graph-RAG | CMDB graph, recursive CTEs → Neo4j, plain-RAG vs Graph-RAG benchmark | ☐ |

## Getting started

```bash
cp .env.example .env        # fill in LLM + Langfuse keys
make db-up                  # postgres (pgvector) + redis via docker compose
make seed                   # generate + load synthetic data
make dev                    # FastAPI + Streamlit
make test                   # unit tests
make eval                   # full eval suite with metrics table
```

## Evaluation results

Baseline results land here after Milestone 5.

| Suite | Metric | Baseline |
|---|---|---|
| Retrieval (40 cases) | recall@5 / MRR / refusal rate | — |
| Routing (30 cases) | accuracy / ping-pong rate | — |
| End-to-end (20 cases) | side-effect success | — |
| Quality (10 cases) | LLM-judge faithfulness / helpfulness | — |
| Graph (15 cases, M9) | plain RAG vs Graph-RAG accuracy | — |

## Performance & cost

Populated after Milestone 6: p50/p95 latency (cache hit vs miss, per agent), tokens and cost per conversation, cache hit rates, cost per resolved request, and the caching on/off comparison.

## Design decisions

Every non-obvious choice — pgvector over Pinecone, Agents SDK over LangGraph, cache invalidation strategy, CTEs vs Neo4j — is logged with context and tradeoffs in [DECISIONS.md](DECISIONS.md).