# agentdesk

**An AI-powered ITSM service desk: a router agent routes each query to specialist agents for knowledge search, catalog fulfillment, and incident management — with hybrid RAG, semantic caching, long-term memory, and human-in-the-loop approvals.**

## Tech Stack

- **Agent framework:** OpenAI Agents SDK (agents, handoffs, guardrails, sessions), models via LiteLLM
- **Backend:** FastAPI · **Frontend:** Streamlit
- **Database:** Postgres + pgvector (relational + vector + full-text search in one DB)
- **Cache:** Redis (embedding cache, semantic cache, response cache)
- **Observability:** Langfuse · **Evals:** custom harness run in CI · **CI/CD:** GitHub Actions
- **Integrations:** MCP server (official MCP Python SDK, M8) · Slack Socket Mode ingestion (M8) · optional Neo4j Graph-RAG backend (M9)

## Architecture

A tool-less **router agent** classifies each query and hands off to one of three specialists:

- **Knowledge agent** — query expansion, hybrid RAG (pgvector + FTS with reciprocal rank fusion), citations
- **Fulfillment agent** — reads user assets, pre-fills catalog orders, human-in-the-loop approval for orders > $500
- **Incident agent** — summarize, dedup via ticket embedding similarity, create/link tickets

Agents call deterministic tools; **tools are the only DB access path**. A semantic cache is checked before any agent runs (read-only intents only). Short-term memory = SDK sessions in Postgres; long-term memory = a hand-rolled `user_facts` table (injected at session start, extracted at session end).

Two external surfaces reuse those same layers (M8): a **Slack Socket Mode runner** feeds thread reports through the normal pipeline (router → incident agent → dedup → in-thread reply) behind an **injection guardrail** that treats thread text as report content, never as commands; and an **MCP server** exposes the same plain tool functions to external clients (e.g. Claude Desktop) behind bearer-token → acting-user auth.

## File Tree

```
agentdesk/
├── README.md
├── CLAUDE.md                         # working brief + live status (auto-loaded by Claude Code)
├── DECISIONS.md                      # architecture decision log (45 ADRs)
├── DEPLOY.md                         # M1: manual Railway/Render deploy runbook (ADR-009)
├── SLACK_SETUP.md                    # M8: Slack app manifest, scopes, token setup (manual steps)
├── .gitignore
├── .env.example                      # DATABASE_URL, REDIS_URL, LLM keys, LANGFUSE keys
├── pyproject.toml
├── alembic.ini                       # Alembic config (DB URL injected from $DATABASE_URL)
├── Makefile                          # db-up · seed · reset · migrate · generate · test · lint
├── Dockerfile
├── docker-compose.yml                # app + ui + approvals + postgres(pgvector) + redis
├── docker-compose.langfuse.yml       # M6: OPTIONAL local Langfuse stack (Cloud is the default, ADR-042)
├── .github/workflows/
│   ├── ci.yml                        # M4: lint, tests, eval subset on PR
│   ├── nightly.yml                   # M4: every eval suite (7 as of M8), nightly + on dispatch
│   └── deploy.yml                    # M4: deploy on merge to main (inert until armed, ADR-029)
├── app/
│   ├── main.py                       # FastAPI entrypoint
│   ├── config.py                     # settings via pydantic-settings
│   ├── api/
│   │   ├── routes_chat.py            # POST /chat — session load, cache check, run router; /identity/resolve (M8)
│   │   ├── routes_approvals.py       # list/approve/reject pending orders (HITL)
│   │   └── routes_health.py
│   ├── agents/
│   │   ├── router.py                 # tool-less agent: classify intent, hand off to 3 specialists
│   │   ├── knowledge.py
│   │   ├── fulfillment.py
│   │   ├── incident.py
│   │   ├── guardrails.py             # M8: Slack-gated injection screen (SDK input guardrail, ADR-041)
│   │   └── context.py                # per-run context object (user_id, facts, session)
│   ├── tools/
│   │   ├── knowledge_tools.py        # search_knowledge_articles, get_release_notes
│   │   ├── user_tools.py             # get_user_profile, get_user_assets
│   │   ├── ticket_tools.py           # create_ticket, update_ticket, get_ticket_status, search_similar_tickets
│   │   ├── catalog_tools.py          # list_catalog_items, place_catalog_order, request_approval
│   │   ├── slack_tools.py            # M8: post_slack_message (destination locked to the run's thread)
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
│   ├── slack/
│   │   └── runner.py                 # M8: Socket Mode runner — pure HTTP client of the chat API (ADR-038)
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
│       ├── tracing.py                # M6: Langfuse bridge — SDK trace processor, tags, cost budget (ADR-042/043/045)
│       └── costs.py                  # M6: THE price table (evals + tracing import the same dict)
├── ui/
│   ├── streamlit_app.py              # chat UI
│   └── approval_view.py              # manager approval card for HITL
├── scripts/
│   ├── generate_data.py              # M0: two-stage LLM dataset generator (cached to data/)
│   ├── seed_db.py                    # M0: load data/ into Postgres (idempotent upsert)
│   ├── export_metrics.py             # M6: headline numbers from Langfuse traces + cache counters
│   └── ab_caching_report.py          # M6: caches ON-vs-OFF comparison from two --out JSONs (ADR-044)
├── evals/
│   ├── datasets/
│   │   ├── retrieval.jsonl           # 40 cases: query -> expected article ids (+ refusal probes)
│   │   ├── routing.jsonl             # 30 cases: query -> expected specialist
│   │   ├── e2e.jsonl                 # 18 flows: conversation -> expected DB side effects
│   │   ├── dedup.jsonl               # 12 gray-band link/trap probes (ADR-028)
│   │   ├── quality.jsonl             # 10 LLM-as-judge cases (ADR-033)
│   │   ├── graph.jsonl               # 15 multi-hop impact/root-cause cases + ground truth (ADR-036)
│   │   └── slack.jsonl               # M8: 5 recorded thread fixtures incl. the injection trap (ADR-039/041)
│   ├── run_evals.py                  # CLI: full suite, --subset for CI, --out for JSON results
│   ├── suite_e2e.py                  # side-effect assertions through a live uvicorn (ADR-027)
│   ├── suite_dedup.py                # incident-agent gray-band judgment (ADR-028)
│   ├── suite_quality.py              # faithfulness + helpfulness, judged by gpt-5 (ADR-033)
│   ├── suite_graph.py                # M9: plain RAG vs Graph-RAG three-way comparison (ADR-036)
│   ├── suite_slack.py                # M8: fixtures through the real runner code + live API (ADR-039)
│   ├── judge_prompt.md               # the committed judge rubric (verbatim instructions)
│   ├── thresholds.toml               # eval floors — single source of truth (ADR-026)
│   ├── results/baseline.json         # committed full-run baseline (per-case cost/latency)
│   ├── common.py                     # dataset loading, floors, cost/latency helpers, trace-id hook (M6)
│   ├── langfuse_datasets.py          # M6: eval cases -> Langfuse datasets, runs linked to traces
│   └── metrics.py                    # recall@k, MRR, percentile
├── tests/
│   ├── conftest.py
│   ├── test_agents.py
│   ├── test_tools.py
│   ├── test_chunking.py
│   ├── test_retrieval.py
│   ├── test_cache.py
│   ├── test_memory.py                # M5: session store, fact merge rule, injection plumbing
│   ├── test_slack.py                 # M8: runner — triggers, envelope, fail-closed identity, re-submit
│   ├── test_guardrails.py            # M8: injection-screen gating + tripwire plumbing (LLM-free)
│   ├── test_mcp.py                   # M8: MCP tool surface, token map, identity threading
│   └── test_observability.py         # M6: no-op contract, tag/cost aggregation, cost budget, A/B seam
├── data/                             # M0: generated dataset (cached JSON) + taxonomy — rebuild via `make seed`
├── design/                           # DATA_DICTIONARY.md · database_erd.png · architecture diagrams
├── ignore/                           # git-ignored local scratch / notes
├── mcp_server/
│   └── server.py                     # M8: the same plain tools over MCP, bearer-token auth (ADR-040)
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
| **slack** | 5 fixtures | recorded Slack threads through the real ingestion path: **5/5 · 4/5 · 5/5** across the three M8 baseline runs (floor 0.75); the injection-trap and identity-fallback cases passed **every** run³ | n/a¹ | 32.5 s / 47.5 s |

¹ e2e conversations bill inside the suite-spawned server, invisible to the HTTP client —
closed in M6: per-flow cost is read from the flows' Langfuse traces (see "Observability &
metrics" below).
² graph numbers are the M9 three-run baseline (2026-07-07), not part of the committed M5
`baseline.json` full run. All agentic metrics vary run to run (that's
LLMs); the floors gate regressions, not perfection.
³ slack numbers are the M8 final-code baseline (2026-07-07; evidence in `ignore/tem/`): the
two observed misses are the system's known variable modes (the ADR-028 dedup gray band and
the residual ADR-022 empty-final burp), not Slack-specific failures.

## Observability & metrics (M6)

Every agent run is traced to **Langfuse** via a custom Agents SDK trace processor
(`app/observability/tracing.py`, ADR-042/043): router handoffs, specialist spans, tool calls,
guardrail verdicts, per-call tokens and cost (priced from the one committed table in
`app/observability/costs.py` — the same dict the eval harness uses, so the two can never
disagree by drift). Traces carry population-separating tags (`source`, `intent`, `agent`,
`cache_hit`, `flagged`); conversations group by session id; semantic-cache hits emit their own
cheap trace instead of vanishing from the stats. With no Langfuse keys configured the whole
layer is a verified no-op (CI runs keyless and green). Langfuse Cloud is the recommended
backend; `docker compose -f docker-compose.langfuse.yml up -d` is the keyless local
alternative (UI on :3000). The retrieval and routing eval suites also publish to Langfuse
datasets on every run, each case linked to its exact trace.

### Headline numbers

Measured 2026-07-08 against a local Langfuse over one PR-gate eval run + one caching A/B
(2× retrieval+e2e) + manual chat traffic — 61 `chat` traces
(`make metrics` regenerates this table live):

| metric | value |
|---|---|
| chat latency p50/p95 — semantic-cache **hit** | **0.07 s / 0.08 s** (n=3) |
| chat latency p50/p95 — cache miss (agents ran) | 23.6 s / 36.8 s (n=58) |
| per specialist p50/p95 — knowledge | 22.9 s / 33.2 s (n=26) |
| per specialist p50/p95 — fulfillment | 20.3 s / 32.2 s (n=14) |
| per specialist p50/p95 — incident | 31.8 s / 42.2 s (n=17) |
| tokens per conversation p50/p95 | 14.6k / 73.9k (49 conversations) |
| cost per conversation p50/p95 | **$0.0076 / $0.0254** |
| cost per resolved request (mean) | $0.0079 (flagged runs excluded) |
| handoff-count distribution | 1 hop × 56 · 0 hops × 4 (cache hits + a guardrail flag) · 3 hops × 1 |
| cache hit rates (lifetime counters, `GET /cache/stats`) | embedding **84.7%** · semantic 14.7% · response 76.7% |

Note the units: a *trace* is one chat turn; the e2e suite's per-flow latencies below are
whole multi-turn conversations — the numbers are consistent, not contradictory. The
harness-vs-Langfuse **cross-check** (the ADR-034 debt): all 20 billed cases of a subset run
joined by trace id — harness **$0.0974** vs Langfuse **$0.0974**, worst per-case delta
$0.0000005 (pure rounding; both sides are SDK tokens × the same table, so agreement is by
construction — a disagreement would have meant a plumbing bug). Per-case cost also matches
the committed M5 `baseline.json` magnitudes (routing ≈ $0.0066/case vs baseline $0.0065).
The cache-miss latencies in this table predate the M10 reasoning-effort change below —
post-M10, the same probes run 1.3–3× faster.

### Latency: where the time goes (M10, ADR-047)

Methodology in two sentences: 6 representative conversations (a knowledge refusal, an
answerable question, a semantic-cache hit, a ≤$500 auto-placed order, an incident
dedup-link, a multi-intent report) were driven through `POST /chat` 3× each, and every
turn's Langfuse trace was decomposed into its spans — router generation, specialist
generations, tool calls, handoffs, app overhead (script + raw data:
`ignore/tem/m10_latency_baseline.py` / `.json`).

**The span-attribution finding: 97–99% of every cache-miss conversation is LLM generation
time.** All tool calls together (SQL, embeddings, RRF search) cost 0.02–0.7 s; handoffs and
app overhead are milliseconds. The surprise inside that number: the *tool-less router* —
one forced classification call — was burning 6–18 s per conversation, and the answer's
token bill was mostly invisible: a Bluetooth-refusal turn billed ~1,800 output tokens of
which only ~60 are the visible refusal — the rest were **reasoning tokens** at the
gpt-5-family default effort (`medium` when the request doesn't specify, which this app
never did before M10).

The fix is configuration, not architecture — three settings in `config.py`, env-overridable,
wired into each agent's `ModelSettings(reasoning=…)`: `ROUTER_REASONING_EFFORT=minimal`
(a single-label classification needs no deliberation — 0 reasoning tokens, routing suite
30/30 three consecutive runs), `SPECIALIST_REASONING_EFFORT=low` for fulfillment + incident
(keeps a thinking budget for order forms and dedup gray-band judgment; every gate green),
and `KNOWLEDGE_REASONING_EFFORT=medium` — knowledge is deliberately **carved out**: at
`low` it kept refusing out-of-KB questions but decorated 5/13 refusals on the
facts-injected chat path with the forbidden "Sources:" list, violating the ADR-017 output
contract *and* leaking refusals into the semantic cache through the ADR-023 write-time
gate (0/6 + zero historical occurrences at `medium`; caught by the e2e refusal flow's 1.0
floor, isolated with `ignore/tem/m10_smartwatch_http_probe.py`). Passthrough was verified
against the logged OpenAI request (`'reasoning': {'effort': …}` in the `/responses`
payload) and the billed reasoning-token counts — not assumed
(`ignore/tem/m10_reasoning_passthrough.py`).

Before/after on the same 6 probes, 3 reps each (p50 wall of the whole conversation; span
means; cost per conversation from the priced generation spans):

| probe | wall p50 | router gen | specialist gen | cost |
|---|---|---|---|---|
| knowledge refusal (Bluetooth) | 24.2 s → **12.3 s (−49%)** | 10.7 → 1.4 s | 12.2 → 10.6 s | $0.0055 → $0.0031 |
| knowledge answerable (MFA) | 19.4 s → **15.0 s (−23%)** | 6.9 → 1.1 s | 11.4 → 12.7 s | $0.0048 → $0.0036 |
| semantic-cache hit | 0.07 s → 0.09 s | — | — | $0 |
| order auto-place (2 turns) | 33.1 s → **11.0 s (−67%)** | 13.3 → 2.1 s | 20.1 → 8.8 s | $0.0164 → $0.0081 |
| incident dedup-link | 27.3 s → **13.0 s (−52%)** | 6.0 → 1.6 s | 20.2 → 10.6 s | $0.0108 → $0.0051 |
| multi-intent (2 turns) | 64.4 s → **42.5 s (−34%)** | 17.9 → 3.7 s | 45.8 → 37.5 s | $0.0263 → $0.0160 |

Knowledge-heavy flows keep only the router saving (their specialist stays at `medium` per
the carve-out); fulfillment/incident flows keep the full effort win. Cost dropped 24–53%
alongside latency (reasoning tokens bill as output tokens), and the PR-gate eval subset
itself got cheaper ($0.10 → $0.07 measured). Eval floors are untouched and green at the
final defaults: e2e **18/18**, full routing 30/30 (accuracy 1.000, hard 6/6, zero
ping-pong/integrity), retrieval recall@5 1.000 / refusals 10/10 / false refusals 0, dedup
0.917 vs floor 0.65, slack 5/5.

A `gpt-5-nano` router was also measured — and rejected (ADR-047): full routing suite twice
at 0.933 / **0.900 (exactly the floor, zero margin**, vs mini's 1.000/1.000, with a
repeated misroute and ping-pong appearing), for a latency win that no longer exists — the
mini router at `minimal` effort is already 1–1.4 s.

**The honest structural floor:** a cache-miss turn is still ≥2 sequential gpt-5-mini calls
(router classification → specialist tool round-trips → final composition), so ~5–9 s of
serial generation remains at any effort — that floor is why perceived latency is M11's
(streaming) job, not another knob here.

### Caching A/B (ADR-044)

Same command, two arms: caches on vs `CACHES_DISABLED=1` (a deliberate flag in all three
caches — not a simulated Redis outage). Slice: retrieval + e2e, the only suites that can hit
the caches (the others call agents directly, bypassing routes_chat by design). One run per
arm, 2026-07-08; per-flow table via `scripts/ab_caching_report.py`.

| measurement | caches ON | caches OFF | delta |
|---|---|---|---|
| `knowledge_cache` flow (a question, then a fresh-session **paraphrase**) | 21.7 s | 48.9 s | **-56% latency** — the paraphrase is served from the semantic cache in ~0.1 s instead of re-running the agent (~20 s) |
| retrieval answerable-case p50 (pure embedding path) | 0.01 s | 0.25 s | **~25× — the embedding cache** |
| e2e case latency p50 / p95 | 34.2 s / 57.3 s | 41.9 s / 72.0 s | -18% / -20% blended |
| chat-trace LLM spend over the e2e window (from Langfuse — the harness can't see inside the spawned server) | $0.224 | $0.259 | -13% |

**The honest read:** the caching win is real but *concentrated*. The semantic cache only
fires on paraphrase repeats, so the flow built around a repeat shows the dramatic number
(-56% latency; the avoided second knowledge run is the directly attributable ~$0.005 of the
cost delta) — while flows that never repeat a question sit inside the ±30% run-to-run LLM
latency noise band (several OFF flows were *faster*; caches cannot cause that). The blended
-18%/-13% numbers lean on the knowledge flows and should be quoted with that caveat, which is
why the report is per-flow. As predicted, the OFF arm **fails** the `knowledge_cache` flow's
`cached=true` assertion — that assertion is the mechanism under test, and its failure under
the flag is the flag working.

**Flagged for investigation, not smoothed over:** (1) `multi_intent` failed in *both* arms
this day ("incident half acted" — the incident agent handled the second intent without
creating the row); it was 18/18 in the M5 baseline and the post-merge nightly, so this is
either the known gray-band variance or a drift worth watching in the next nightlies.
(2) The OFF arm also dropped `incident_create` (0 tickets — the "lost report" mode from the
first nightly dry-run) and `refusal_to_ticket`; neither touches a cache, and the OFF arm ran
at visibly higher API latency (p95 72 s), consistent with provider-side variance rather than
a caching effect — but 3 non-cache failures in one run is above the historical rate.

There is deliberately **no fourth dashboard service**: Langfuse's own UI is the dashboard
(traces, sessions, costs, datasets); `make metrics` prints the table above for terminals and
this README. The per-conversation **cost budget alert** (ADR-045, `COST_ALERT_THRESHOLD_USD`,
default $0.10) accumulates spend per session in Redis and, on crossing, logs a loud warning
and attaches a WARNING event to the crossing trace.

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

## MCP server — connect Claude Desktop and create a ticket (M8)

The MCP server (`mcp_server/server.py`, ADR-040) exposes four ITSM tools —
`search_knowledge_articles`, `list_catalog_items`, `create_ticket`, `get_ticket_status` —
over streamable HTTP with bearer-token auth. They are the **same plain functions the chat
agents use** (one tool surface, two adapters), so every identity/ownership guard applies to
MCP clients too: the token maps to one acting user, and tickets land under that user.

**1. Configure a token** in `.env` (any secret string, mapped to a seeded user's email):

```bash
MCP_TOKENS=my-secret-token=demo.user@corp.com
```

**2. Start the stack** (Postgres/Redis must be up, as usual):

```bash
make db-up
make mcp        # serves http://localhost:8090/mcp
```

**3. Connect Claude Desktop** via `mcp-remote` (static bearer tokens aren't a native Desktop
connector flow, so the standard proxy carries the header). Add to
`claude_desktop_config.json` (Settings → Developer → Edit Config), then fully restart
Claude Desktop:

```json
{
  "mcpServers": {
    "agentdesk": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://localhost:8090/mcp",
        "--header",
        "Authorization: Bearer my-secret-token"
      ]
    }
  }
}
```

**4. Create a ticket end-to-end.** In a new Claude Desktop chat (the `agentdesk` tools show
under the tools icon), ask:

> My laptop dock stopped detecting external displays this morning — please open an IT ticket
> for it, then check its status.

Claude calls `create_ticket` (the row lands in Postgres owned by `demo.user@corp.com`,
embedded for dedup like every ticket) and `get_ticket_status` reads it back. A wrong or
missing token gets `401` before any tool is reachable, and another user's ticket id gets an
ownership refusal — the same guards the in-process agents live behind (verified with a real
MCP client in `ignore/tem/m8_mcp_smoke.py`).

## Slack ingestion (M8)

React with :ticket: 🎫 (or @mention the bot) in any Slack thread and the incident agent
files or dedup-links a ticket and replies in-thread with the ticket id + one suggested KB
article. Setup (app manifest, scopes, tokens) is manual and documented in
[`SLACK_SETUP.md`](SLACK_SETUP.md); design in ADR-038/039/041. Everything runs Slack-less by
default — CI and the eval suite use recorded thread fixtures, never a live workspace.

## Milestones

| # | Milestone | Scope | Status |
|---|-----------|-------|--------|
| M0 | Data & schema | Synthetic dataset generation, DB models + migration, seed script | ✅ done |
| M1 | Core loop | Router + knowledge agent, RAG pipeline (chunk/embed/hybrid search), chat API, Streamlit UI, embedding cache, retrieval evals, deploy prep (`DEPLOY.md`) | ✅ done |
| M2 | Action agents | Fulfillment + incident agents, deterministic tools, HITL approvals (> $500) | ✅ done |
| M3 | Caching | Semantic cache + response cache (embedding cache landed in M1) | ✅ done |
| M4 | CI & evals | Routing + e2e + dedup eval suites, floors in `thresholds.toml`, CI subset gate, nightly workflow | ✅ done |
| M5 | Memory + full eval harness | SDK sessions in Postgres, `user_facts` inject/extract, quality suite (LLM-as-judge), per-case cost/latency, committed baseline | ✅ done |
| M6 | Observability | Langfuse tracing (Agents SDK trace processor), tagged traces + eval datasets, caching A/B, cost budget alert | ✅ done |
| ~~M7~~ | ~~AWS migration~~ | ~~Move off the Railway plan onto AWS~~ — **dropped** (cut for time; the PaaS runbook in [`DEPLOY.md`](DEPLOY.md) is the deployment story) | ✂️ dropped |
| M8 | MCP + Slack + guardrails | MCP server (bearer-token auth, Claude Desktop), Slack Socket Mode ingestion with in-thread replies, injection guardrail + adversarial eval | ✅ done |
| M9 | Graph-RAG | CMDB dependency graph (Postgres CTE + optional Neo4j), graph tool, three-way RAG comparison | ✅ done |

**Status: complete.** M6 was the final milestone. A finished project states its cut lines —
deliberately NOT done: the AWS migration (M7, dropped — Railway/Render per `DEPLOY.md` is the
deployment story and the deploy itself is user-executed, ADR-009/029); auth on the approvals
UI (anyone who reaches `:8502` is a "manager" — fine for a local demo, stated so nobody
mistakes it for a product decision); multi-user MCP auth beyond the static token map
(ADR-039/040); tracing on the MCP server (a separate agent-less process — ADR-042); and a
live public deployment unless/until the owner runs the `DEPLOY.md` runbook.
