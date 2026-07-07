# Architecture decision log

Lightweight ADRs for every non-obvious choice in this project. Each entry records the context, the decision, the alternatives considered, and the tradeoffs accepted — written so a future interviewer (or future me) can follow the reasoning without the original conversation.

**Template for new entries:**

```
## ADR-NNN: Title
Date: YYYY-MM-DD · Status: accepted | superseded by ADR-XXX
Context: what problem forced a choice
Decision: what was chosen
Alternatives: what else was considered and why it lost
Tradeoffs: what this costs us, and when we'd revisit
```

---

## ADR-001: Postgres + pgvector instead of Pinecone + MySQL

Date: 2026-07-03 · Status: accepted

**Context:** The system needs relational data (users, assets, tickets, orders), vector search over article chunks and tickets, and keyword search — and many queries join across them (e.g. "release notes for v5.1 vs v5.2" is vector similarity filtered by a metadata column; dedup joins ticket embeddings against ticket rows).

**Decision:** One Postgres 16 instance with the pgvector extension (HNSW index) plus built-in full-text search (`tsvector` + GIN). No dedicated vector database.

**Alternatives:** Pinecone + MySQL. Rejected because (a) vector–relational joins would require duplicating metadata into Pinecone and keeping two stores in sync — a classic two-sources-of-truth bug factory; (b) MySQL has no mainstream vector extension and weaker FTS, so the stack would grow to three systems; (c) a managed vector DB is a black box, while tuning pgvector (HNSW parameters, query plans) is itself the infra learning this project is for.

**Tradeoffs:** pgvector is the right call at this scale (~thousands of documents). At tens of millions of vectors, or with no ops capacity, a dedicated vector DB earns its extra network hop and sync cost — that's the revisit trigger.

---

## ADR-002: OpenAI Agents SDK instead of LangGraph

Date: 2026-07-03 · Status: accepted

**Context:** The orchestration need is modest — one router and three specialists — and my employer is actively migrating to the OpenAI Agents SDK after a heavily engineered custom multi-agent system proved brittle. Skills built here should transfer to work.

**Decision:** OpenAI Agents SDK (agents, handoffs, guardrails, sessions), with models accessed via LiteLLM to avoid provider lock-in.

**Alternatives:** LangGraph — more mindshare and stronger explicit control flow (cycles, parallel fan-out, mature `interrupt` for HITL). Rejected because the architecture here is literally the SDK's canonical pattern (router + handoffs), LangGraph's power would be spent hand-wiring what the SDK gives in a few lines, and company alignment outweighs framework generality. Also considered: CrewAI (fast but less control), AutoGen (research-flavored), Pydantic AI.

**Tradeoffs:** Weaker built-in human-in-the-loop than LangGraph's `interrupt` — budgeted extra time in M2 to use or hand-roll tool approval (see ADR-005). Optional hedge: after M2, reimplement the supervisor + one specialist in LangGraph and record the comparison here.

---

## ADR-003: LLM-driven handoffs with a tool-less router agent

Date: 2026-07-03 · Status: accepted

**Context:** The prior-art failure mode (observed at my company) is rigid, over-engineered orchestration where routing logic lives in code and grows unmaintainable.

**Decision:** A router agent with zero tools whose only job is intent classification and handoff; each specialist (knowledge, fulfillment, incident) gets a narrow toolset and a one-line `handoff_description` that the router LLM reads when routing. Handoffs are mostly one-directional; specialists hand back to router only on a genuine mid-conversation intent change. Conversation history transfers with the handoff.

**Alternatives:** (a) Giving router its own tools — rejected: it starts half-answering instead of routing. (b) One mega-agent with all tools — kept only as an M2 intermediate step to validate tool-calling before refactoring. (c) Code-based routing — the thing being deliberately avoided.

**Tradeoffs:** Routing quality now depends on prompt/description quality rather than deterministic code, so it must be measured: the eval suite tracks routing accuracy and router→A→router→B ping-pong rate. If accuracy can't be held ≥ 0.9, revisit with a structured-output classifier in front of the handoff.

---

## ADR-004: Deterministic tools are the only database access path

Date: 2026-07-03 · Status: accepted

**Context:** Agents need to read and mutate state (tickets, orders, assets) safely, testably, and traceably.

**Decision:** All DB access goes through plain, typed Python functions (`search_knowledge_articles`, `create_ticket`, `place_catalog_order`, …). Agents never issue SQL or touch ORM sessions. Tool = deterministic function; agent = LLM loop that decides which tools to call. Anything of the form "do X" is a tool; "figure out how to accomplish X" is an agent.

**Alternatives:** Letting agents generate SQL (flexible but unauditable and unsafe for writes); embedding business logic inside agent prompts (untestable).

**Tradeoffs:** Some flexibility lost — new capabilities require writing a tool. Accepted: every side effect is unit-testable, traceable in Langfuse, and reusable (the same tools back the MCP server in M8).

---

## ADR-005: Human-in-the-loop approval via persisted order state

Date: 2026-07-03 · Status: accepted

**Context:** Catalog orders above $500 must pause for a manager's approval. The Agents SDK's built-in approval support is less mature than LangGraph's `interrupt`.

**Decision:** The fulfillment agent calls a `request_approval` tool that writes the order with `approval_state='pending'` and ends the run. A separate approvals API/UI lists pending orders; approval triggers `place_catalog_order` on a fresh run. State lives in the `orders` table, not in framework internals. (M2 will first check the SDK's current tool-approval support; if sufficient, use it — same persisted-state design either way.)

**Alternatives:** Framework-native interrupts (LangGraph) — ruled out with ADR-002; blocking the run while waiting (fragile across restarts and deploys).

**Tradeoffs:** Slightly more plumbing than a native interrupt, but the approval survives restarts, is visible in plain SQL, and hand-rolling it teaches the underlying pattern.

---

## ADR-006: Semantic cache runs before agents, read-only intents only

Date: 2026-07-03 · Status: accepted

**Context:** Repeated questions ("how do I reset my password" phrased 100 ways) shouldn't pay full agent latency and cost — but a cached answer must never perform an action.

**Decision:** Three cache layers in Redis: (1) embedding cache, `sha256(model+text) → vector`, no TTL; (2) semantic cache checked in the FastAPI layer before any agent runs — embed the query, similarity-search past queries, serve the cached answer above ~0.95 similarity, 24h TTL, and only for intents classified read-only; (3) response cache (short TTL) on read tools like `list_catalog_items`. Invalidation: each semantic-cache entry stores the article IDs it cited; updating an article via ingest deletes entries that cited it.

**Alternatives:** Caching inside the agent loop (still pays agent overhead); caching everything including action intents (unsafe — a cache hit could "place an order" without doing anything, or worse); no invalidation (serves stale answers after KB updates).

**Tradeoffs:** The 0.95 threshold trades hit rate against wrong-answer risk and needs tuning against evals. Citation-based invalidation adds bookkeeping but is the difference between a demo cache and a defensible one.

---

## ADR-007: Hand-rolled long-term memory instead of Mem0/Letta

Date: 2026-07-03 · Status: accepted

**Context:** The system should remember stable user facts across sessions ("has a MacBook Pro", "in Sales org") so, e.g., a later "order me an IDE" pre-selects the macOS option.

**Decision:** A `user_facts` table (`user_id, fact_type, fact, source, confidence, updated_at`). At session end, an LLM extraction step pulls stable facts; a merge step dedupes by `fact_type` (keep newer / higher-confidence) so contradictions update rather than accumulate. Facts are injected into the router agent's context at session start. Short-term memory is separate: Agents SDK Sessions persisted in Postgres.

**Alternatives:** Mem0 or Letta — mature open-source memory layers. Rejected for the primary build because rolling a simple version teaches the mechanics (extraction, dedup, injection) this project exists to demonstrate; they remain the answer at production scale.

**Tradeoffs:** Simpler than dedicated memory systems — no graph memory, no relevance-ranked recall. Revisit if fact volume per user grows beyond what fits in a system prompt.

---

## ADR-008: Streamlit for the UI

Date: 2026-07-03 · Status: accepted

**Context:** The project's value is in the AI/infra layers; the UI needs a chat window, citations, and an approval card — fast.

**Decision:** Streamlit for the chat UI and manager approval view.

**Alternatives:** Next.js/React — better polish, meaningfully more time. Deferred; swap later only if frontend presentation becomes a goal.

**Tradeoffs:** Streamlit's interaction model is limiting (reruns, state quirks). Accepted for speed; the FastAPI boundary means the UI is replaceable without touching the system.

---

## ADR-009: Deploy early on a PaaS, migrate to AWS as a documented milestone

Date: 2026-07-03 · Status: accepted

**Context:** I have no deployment experience. Deploying last would mean learning the scariest unknown under maximum system complexity.

**Decision:** Deploy the minimal M1 system to Railway or Render (managed Postgres + Redis) and auto-deploy on merge from M4. Migrate to AWS (Terraform: ECS Fargate, RDS, ElastiCache, Secrets Manager, CloudWatch) as milestone 7, executing the runbook manually to learn, and write up "PaaS → AWS: what actually changed".

**Alternatives:** AWS from day one (steep learning curve at the worst time); PaaS forever (weaker infra-learning story).

**Tradeoffs:** One migration's worth of duplicated deployment work — deliberately accepted, because the migration writeup converts a stated weakness (infra) into a demonstrated-growth narrative.

---

## ADR-010: Evals as CI regression gates, not perfection gates

Date: 2026-07-03 · Status: accepted

**Context:** Agent behavior regresses silently when prompts, tools, or models change. Portfolio credibility also depends on measured results, not claimed ones.

**Decision:** Eval suites live in the repo (retrieval recall@5/MRR + refusal rate, routing accuracy + ping-pong rate, end-to-end side-effect assertions against a scratch DB, small LLM-as-judge quality set). CI runs a cost-capped ~30-case subset on every PR with thresholds derived from the committed baseline minus a small margin; the full suite runs nightly. Thresholds fail builds on regression — they are not aspirational targets.

**Alternatives:** Manual spot-checking (doesn't scale, drifts); full suite on every PR (slow and expensive); hosted-only evals without in-repo datasets (results not reproducible by a reader).

**Tradeoffs:** LLM-as-judge and even retrieval metrics are noisy — thresholds need margins to avoid flaky CI, which means small real regressions can slip through until the nightly run.

---

## ADR-011: Hybrid retrieval — pgvector + Postgres FTS fused with RRF

Date: 2026-07-03 · Status: accepted

**Context:** Pure vector search misses exact identifiers (error codes, version strings, product names); pure keyword search misses paraphrase. ITSM queries contain both.

**Decision:** Every chunk stores an embedding (HNSW) and a `tsvector` (GIN). Retrieval runs both searches and fuses with reciprocal rank fusion (k=60), with metadata filters (version, status) applied in SQL. A relevance threshold gates answering: below it, the knowledge agent says no article covers the question and offers a ticket — it never improvises IT instructions.

**Alternatives:** Vector-only (simplest, fails on identifiers); adding Elasticsearch (a third system for marginal gain at this scale); learned rerankers (worth trying later as an eval-measured experiment).

**Tradeoffs:** RRF's k and the refusal threshold are tunable constants that must be justified by eval numbers, not vibes.

---

## ADR-012: Graph-RAG deferred to M9, CTE-first then Neo4j, framed as a comparison study

Date: 2026-07-03 · Status: accepted

**Context:** Early design over-reached: "what OS is the user's laptop" was slated for Graph-RAG but is a single SQL join (now plain tool use in the fulfillment agent). The genuine graph problem is the CMDB — multi-hop questions like "server X is down, which services and users are impacted?", change blast radius, and shared root cause across tickets.

**Decision:** Build the CMDB dependency graph in milestone 9, implementing `query_dependency_graph` twice behind one interface: first with Postgres recursive CTEs, then Neo4j/Cypher. Benchmark plain RAG vs both graph variants on 15 multi-hop cases and publish the table. If the graph shows no advantage, publish the honest null result with analysis rather than tuning until it "wins".

**Alternatives:** Graph from day one (complexity before the simple system works); Neo4j-only (loses the CTE-vs-Cypher learning comparison); forcing graph search onto single-join queries (the original mistake — reverted).

**Tradeoffs:** The dual implementation costs a few extra days and buys the project's flagship artifact: a measured answer to "when does Graph-RAG actually beat plain RAG?"

---

## ADR-013: Retrieval metadata denormalized onto chunks for pre-filtering

Date: 2026-07-04 · Status: accepted

**Context:** Retrieval searches `article_chunks`, but the fields we filter on (`category`, `doc_type`, `status`, `version`) naturally describe the parent article. ITSM queries lean on metadata filters: exclude `outdated` articles by default, restrict to a category, compare specific `version`s. Where those fields live decides whether the filter is a pre-filter or a post-filter.

**Decision:** Add `category` (ticket-category enum, so invariant 2 is queryable) and `doc_type` to `knowledge_articles`, then **denormalize the full filter set (`category`, `doc_type`, `status`, `version`) onto `article_chunks`** so the predicate is applied on the same row as the vector — a true pre-filter during the index scan, no join. `article_chunks` is treated as a rebuildable search projection (it already holds a generated `tsv`); the article stays the source of truth. Drift is handled in layers: the retrieval tool applies a default `status='published'` visibility filter; the transactional ingest tool (single write path, ADR-004) re-propagates metadata on article change; and a DB trigger (`trg_sync_chunk_metadata`) is a belt-and-suspenders guarantee for `status`, the one field that can change without a re-chunk.

**Alternatives:** Keep the fields only on the article and filter via a JOIN — rejected because it degrades to post-filtering, which under a selective filter can return fewer than `k` results (silent recall loss). No metadata columns at all — rejected: makes invariant 2 and version-filtered retrieval impossible. A separate search store with CDC/outbox re-indexing — the right answer if search ever leaves Postgres, overkill while it's one DB.

**Tradeoffs:** Denormalization duplicates four fields per chunk and needs a sync path; accepted because chunks are derived and a single write path owns them. The trigger adds "invisible" logic, mitigated by keeping the tool the primary writer and the trigger a pure safety net.

---

## ADR-014: Enum-like columns as CHECK constraints + Python StrEnum, not native PG ENUM

Date: 2026-07-04 · Status: accepted

**Context:** Many columns are closed value sets (OS, org, role, statuses, priorities, approval states). They need DB-level integrity and type-safety in application code and data generation.

**Decision:** Store as plain strings guarded by `CHECK ... IN (...)` constraints, with the allowed values defined once in Python `StrEnum` classes reused by data generation and (later) tools. No native Postgres `ENUM` types.

**Alternatives:** Native PG `ENUM` types — rejected because adding a value requires `ALTER TYPE` (awkward and lock-prone in migrations), whereas a CHECK is a trivial migration and the values stay visible inline in the schema. App-only validation (no DB constraint) — rejected: the DB must reject bad rows regardless of the writer.

**Tradeoffs:** The value lists are duplicated between the Python enums and the CHECK strings in the migration; acceptable for rarely-changing sets, and both are covered by tests/seeding.

---

## ADR-015: Cross-table invariants enforced in the schema, not application code

Date: 2026-07-04 · Status: accepted

**Context:** Several data-integrity rules span rows/tables (data spec invariants 4 and 5, plus the long-term-memory dedup rule). Enforcing them only in tools means any future write path can violate them.

**Decision:** Push them into the schema. "A ticket's `asset_id` (when set) must belong to the ticket's owner" (invariant 4) is a **composite FK** `tickets(asset_id, user_id) → assets(id, user_id)` with a supporting `UNIQUE(id, user_id)` on assets — MATCH SIMPLE skips it when `asset_id` is NULL and enforces it otherwise, with zero application logic. "`approval_state` may be `pending` only while `status='submitted'`" (invariant 5) is a table CHECK. The user_facts dedup rule ("a new fact of an existing `fact_type` updates rather than appends") is a `UNIQUE(user_id, fact_type)` that makes the merge a clean upsert. The lexical `tsv` is a generated column so it can never drift from `content`.

**Alternatives:** Enforce in tools only — rejected: relies on every writer being disciplined; the DB is the last line of defense. Triggers for the asset-owner rule — rejected: a declarative composite FK is simpler and self-documenting.

**Tradeoffs:** Schema-level rules are less flexible than app checks (changing them needs a migration) and the composite-FK trick is non-obvious to readers — mitigated by `COMMENT ON` documentation in the migration. Accepted because correctness invariants belong where they can't be bypassed.
---

## ADR-016: Heading-aware chunking with contextual continuation headers; ingest as atomic rebuild

Date: 2026-07-05 · Status: accepted

**Context:** M1 needs a chunking strategy for ~3 KB markdown KB articles (500-token target, 50-token overlap per the plan) and an ingest pipeline that is safe to re-run during the eval-tuning loop. Retrieval quality on continuation chunks was the open question: an article's key terms usually live only in its H1, so chunk 1+ of "How to reset your login password" contains neither "reset" nor "password" for the lexical branch, and embeds without its topic anchor.

**Decision:** Three-layer chunker in `app/rag/chunking.py`: (1) split the body on markdown headings and pack whole sections greedily into ≤500-token chunks (headings are never severed from their text); (2) token-window fallback with 50-token overlap for a single oversized section; (3) every continuation chunk is prefixed with `# <title> (continued)` plus the last 50 tokens of the previous chunk — the title header keeps the article's key terms visible to BOTH halves of hybrid search on every chunk. Tokens are counted with cl100k_base — the embedding model's own tokenizer, so size is measured in the units the model sees. Ingest (`app/rag/ingest.py`) is the single write path for chunks (ADR-013): each run re-chunks all articles from source-of-truth and swaps the whole table atomically in one transaction (delete + insert). Idempotency is layered: deterministic uuid5 chunk ids make re-runs byte-identical, and the embedding cache (ADR-006, pulled forward to M1) makes them free — a second `make ingest` reports 440 cache hits, 0 embeddings.

**Alternatives:** Fixed token windows only (simplest; severs headings from their steps and cuts mid-procedure); embedding article-level (200 articles fit, but a 750-token article dilutes into one vector and per-chunk metadata filtering is lost); LLM-generated contextual summaries per chunk (Anthropic-style contextual retrieval — strictly better anchoring but adds an LLM pass per chunk; unnecessary at recall@5 = 1.0); incremental upsert instead of full rebuild (avoids table churn but must diff chunk counts per article and handle shrinkage; the atomic swap is simpler and 440 rows make churn irrelevant).

**Tradeoffs:** The continuation header + overlap add ~60 tokens of duplicated text per chunk (storage + embedding cost, trivial here). The full-rebuild ingest is O(corpus) per run and would need the incremental path at ~100k chunks. Measured result on the M1 eval set: recall@5 = 1.000, MRR = 0.980 (25 answerable queries).

---

## ADR-017: Refusal is a two-stage cascade; the deterministic gate uses raw cosine, not RRF

Date: 2026-07-05 · Status: accepted

**Context:** ADR-011 requires the knowledge agent to refuse rather than improvise when no article covers the question. Two problems surfaced in M1. First, the fused RRF score is rank-based — the best hit scores ≈1/61 whether it is a great match or merely the least-bad one — so it carries no absolute relevance signal to threshold on. Second, and measured: NO retrieval-level signal separates the eval set's near-miss negative space. "Set up email on my smartwatch" scores 0.611 cosine against the email-on-a-PHONE article while the answerable "are we allowed to use AI tools like ChatGPT" scores 0.466 against its correct policy article. A threshold sweep showed 5/5 refusals requires t=0.65, which falsely refuses 8 of 25 answerable queries; five signal families (cosine gates, lexical-support gates, out-of-vocabulary term detection, text-embedding-3-large at dims=1536, query-vs-title similarity) all fail on the same two near-miss cases — several in the wrong direction (the smartwatch query's title similarity is HIGHER than most answerable ones). Distinguishing "an adjacent article exists" from "an article covers this" is entailment, not similarity.

**Decision:** Two stages. Stage 1 (deterministic, in the search tools): `sufficient_evidence = max(cosine_sim of fused results) >= 0.45`, tuned by sweep to catch gross negative space (3/5 refusal cases: parking badge 0.26, bluetooth 0.39, whiteboard 0.45) with ZERO false refusals; the agent may never answer when it is false. Stage 2 (the knowledge agent): instructions require verifying that retrieved chunks cover the user's SPECIFIC device/product/version/error-code before answering, with an explicit output contract — answers end with a "Sources:" list; refusals never carry one and always offer a ticket. The eval harness mirrors the cascade: answerable metrics stay LLM-free against hybrid_search directly, while the 5 refusal cases run through the knowledge agent (~5 small LLM calls per eval run) and are scored structurally against the output contract. Result: recall@5 = 1.000, refusals 5/5, false refusals 0.

**Alternatives:** Pure retrieval-level threshold (mathematically cannot reach 5/5 without 8 false refusals — see sweep); overfit threshold t=0.463 splitting a 0.006 gap between an answerable and a refusal case (indefensible, one re-embedding away from flipping); cross-encoder reranker (the principled non-LLM fix for entailment-ish relevance — deferred as an eval-measured experiment per ADR-011, since the agent already reads the chunks anyway); scoring refusals by keyword-matching "ticket" alone (brittle; the structural Sources-list contract is anchored in the instructions).

**Tradeoffs:** `make eval` is no longer fully deterministic or free — 5 LLM calls (~$0.01) and the stage-2 outcome depends on model behavior; acceptable because it measures the ACTUAL refusal mechanism instead of a proxy that provably cannot work. The output contract couples instructions to the eval detector (documented in both files). The 0.45 gate and the contract need re-validation when the specialist model changes (M4's eval-in-CI catches this).

---

## ADR-018: Deterministic handoff/action via RECOMMENDED_PROMPT_PREFIX + tool_choice="required"

Date: 2026-07-05 · Status: accepted

**Context:** End-to-end testing of the `/chat` router path (a multi-intent query, "reset my password + show v5.1 release notes") surfaced an intermittent failure invisible to the M1 evals: after the router hands off, the destination agent's first turn was sometimes a narration message ("You're being transferred to the knowledge specialist…") with NO tool call. The Agents SDK Runner treats any agent message without a tool call as the final output, so the run ended before any search ran — a non-answer. Reproduced ~3/5 on gpt-5-mini, on single-intent too; the router itself sometimes emitted "Routing to knowledge…" instead of a handoff. Crucially the eval suite missed it entirely because it calls `Runner.run(knowledge_agent, …)` directly and never exercises the router→specialist handoff (the knowledge agent in isolation always calls its tools correctly — so this is a coordination/prompting defect, not a model-capability one; a larger model was not warranted, and gpt-5-series reasoning models don't expose a tunable temperature anyway).

**Decision:** Two layered guards on both the router and the knowledge agent. (1) Prepend the SDK's `RECOMMENDED_PROMPT_PREFIX` (from `agents.extensions.handoff_prompt`) to their instructions — it frames the model as part of a multi-agent system and instructs it that transfers are seamless and must not be announced to the user. (2) Set `model_settings=ModelSettings(tool_choice="required")` so the model MUST emit a tool call on its acting turn: for the tool-less router the only "tools" are its handoffs (forcing a handoff instead of narration); for the knowledge agent it forces the first search. `reset_tool_choice` (Agent default True) flips tool choice back to "auto" after the first tool call, so the agent is still free to write the final answer, refuse, or make additional tool calls — no infinite loop, and multi-tool/refusal paths keep working. Verified: 10/10 direct-runner runs and 4/4 live-compose runs now hand off, call tools, and answer; `make eval` unchanged (recall@5=1.0, refusals 5/5).

**Alternatives:** A more powerful model (rejected — the knowledge agent already succeeds in isolation on gpt-5-mini; the defect is narration, not reasoning; also more cost/latency). Lower temperature (not available on gpt-5 reasoning models, and doesn't address the root framing issue). Prompt wording alone without `tool_choice="required"` (helps but stays probabilistic — the forced tool call is the deterministic guard). Post-hoc: detect an empty/no-tool final output and re-run (papers over the bug, adds latency).

**Tradeoffs:** `tool_choice="required"` forces a tool call on every *acting* turn, so the router can no longer emit its own "ordering/incidents are coming soon" text — that copy moved to the knowledge agent's refusal/ticket offer (acceptable in M1 where knowledge is the only specialist; revisit when M2 adds real fulfillment/incident handoffs). The handoff path still has ZERO automated eval coverage — this bug was found by hand; the routing-accuracy + handoff-ping-pong suite promised in ADR-003/ADR-010 (M4) is what should have caught it, and now has a concrete regression case to encode.

---

## ADR-019: File-backed SQLiteSession as the pre-M5 conversation-continuity stopgap

Date: 2026-07-06 · Status: accepted

**Context:** M2's fulfillment flow is inherently multi-turn — pre-fill the form, ask for missing fields, confirm, then place or request approval — but M1's `/chat` was one-shot: every POST started from zero history, so a "yes, go ahead" turn had nothing to refer to. The real session backend (SDK sessions persisted in Postgres) is scheduled with the memory milestone (M5).

**Decision:** `ChatRequest` gains an optional `session_id` (client-generated; the Streamlit UI mints a uuid4 per browser session and offers a "New conversation" reset). `routes_chat` runs the router with the SDK's `SQLiteSession` keyed by it, file-backed under the git-ignored `ignore/` dir. The session factory (`_load_session`) is the single swap point: M5 replaces only its body with the Postgres store (`app/memory/session_store.py` stays `TODO(M5)`); request schema, Runner call, and UI don't change. Omitting `session_id` keeps the M1 one-shot behavior (used by the evals). Note the interaction with routing: every turn re-enters the ROUTER with the replayed history — the router re-classifies per turn (follow-ups reach the right specialist because the history shows the flow in progress), rather than resuming `last_agent`.

**Alternatives:** Round-tripping `result.to_input_list()` through the client (ships the whole transcript to the browser every turn — unbounded payloads and a tampering surface); pulling the M5 Postgres store forward (delays M2's actual deliverables for infrastructure M5 will redo properly); an in-process dict (dies on restart, saves no code over SQLiteSession).

**Tradeoffs:** A second, temporary persistence surface (a SQLite file) that M5 deletes; sessions are unauthenticated and per-browser-tab (the user picker stands in for login until M7); one extra small LLM call per turn for router re-classification. Accepted because the swap point is one function.

---

## ADR-020: HITL approval is hand-rolled in orders.approval_state; native SDK interruptions evaluated and rejected

Date: 2026-07-06 · Status: accepted

**Context:** Orders > $500 (config.hitl_approval_threshold_usd) need a human decision that must survive process restarts and arrive on a different surface (the manager's approval view), possibly days later. ADR-005 already fixed the persistence point (orders.approval_state). SDK 0.17.7 also ships native HITL — `function_tool(needs_approval=True)` + `RunResult.interruptions` + a serializable `RunState` — which had to be evaluated before hand-rolling (measured in `ignore/tem/m2_throwaway_react.py`: interruption raised correctly; `RunState.to_json()` ≈ 7 KB; resume via `RunState.from_string` + `state.approve()` + `Runner.run(state)` works, in-process).

**Decision:** Hand-rolled, per ADR-005, as a plain order state machine that respects the M0 CHECK ("pending only while submitted"): `place_catalog_order` self-gates on the DB price — at/under threshold it places directly (`submitted`/`not_required`); above it it saves a `draft`. `request_approval` flips draft → `submitted`+`pending` (the single legal transition) and the agent run ENDS after telling the user. The approvals API + `approval_view` UI later approve (pending → `approved`; the still-`submitted` order is thereby placed) or reject (`rejected` + `cancelled`) from a fresh process, through the same `catalog_tools` DB path the agent tools use (ADR-004). `approve_order`/`reject_order` are deliberately NOT agent tools: approval authority is human-only — an LLM that can approve its own orders has no HITL at all.

**Alternatives:** Native interruptions — rejected on measured grounds: (a) resuming needs the opaque RunState blob persisted somewhere (no schema column, and M2 adds no migrations) and rebuilt against the live agent graph; (b) the SDK warns the dataclass ChatContext needs a custom serializer/deserializer; (c) the blob becomes a second source of truth beside orders.approval_state; (d) it cannot represent approval requests that never came from a run — the two SEEDED pending orders would need the hand-rolled path anyway, so choosing native means maintaining both. Also considered: approval as a synchronous wait in the run (violates "run must end"; dies with the process).

**Tradeoffs:** No exact-position resume — approval does not continue the original conversation; placement is completed by trusted code and the user sees the outcome in the UI or next time they ask. A run dying between `place_catalog_order` and `request_approval` strands an inert `draft` row (harmless; a reaper job could clean them). Revisit if a future SDK persists RunState server-side natively.

---

## ADR-021: Ticket dedup = deterministic 0.80 auto-link gate + an agent-judgment gray band (threshold measured — and measurably insufficient alone)

Date: 2026-07-06 · Status: accepted

**Context:** The incident agent must link "me too" reports to existing tickets instead of filing duplicates. The dedup threshold is a tunable constant, so ADR-017 discipline applies: justify it with numbers against the seeded data, and say so honestly if a threshold cannot do the job alone.

**Decision + evidence** (`ignore/tem/m2_dedup_sweep.py`, over the 300 seeded embedded tickets): same-issue pairs (shared title, differently-worded description; 2 786 pairs) sit at p5 = 0.798 / median = 0.921 cosine; cross-issue nearest neighbors at median = 0.667 / p95 = 0.754 / max = 0.797. At **0.80**: 95% of same-issue pairs flagged, **0%** cross-issue false flags. BUT fresh formally-drafted reports of those same issues (8 hand-written probes) score only **0.59–0.77** against their true group — inside the cross-issue range — so *no single threshold separates "new report of the same issue" from "similar but different issue"*. Raw colloquial phrasing is worse still (0.52–0.72 — register mismatch), which is why the agent searches with its formal draft ("title\n\ndescription", the exact format ticket embeddings were ingested with). Dedup is therefore a two-stage cascade mirroring ADR-017: **stage 1** — `search_similar_tickets` computes `likely_duplicate = cosine ≥ 0.80` deterministically in the tool (auto-link; the agent never eyeballs raw scores for this call); **stage 2** — the agent reads the 0.60–0.80 gray band and links only when it is clearly the same failure of the same thing, preferring a duplicate ticket over a lost report when unsure.

**Alternatives:** A single lower threshold (0.75 flags 5.3% of cross-issue neighbors — and still misses most fresh-report probes); embedding the raw user text (measured register mismatch above); a cross-encoder or LLM pair-judge for the gray band (the principled fix — deferred to M4's formal dedup eval rather than added unmeasured).

**Tradeoffs:** Gray-band judgment is non-deterministic and unevaluated until M4 — honest accounting: the 0.80 flag alone would have auto-linked ~none of the 8 realistic probes; today those linkings depend on the agent reading candidates. The threshold binds to the current embedding model and must be re-measured if it changes.

---

## ADR-022: Graph assembly in router.py; restricted back-edges; the router never resets tool choice

Date: 2026-07-06 · Status: accepted

**Context:** M2 grows the graph to router→{knowledge, fulfillment, incident}, knowledge→incident (a refusal's ticket offer the user accepted), and specialist→router back-edges for genuine mid-conversation intent changes. Three wiring problems: (1) specialists importing each other or the router is an import cycle (the router already imports every specialist); (2) the first routing-eval run caught fulfillment using the back-edge for "item not found in catalog" — a dead end the router cannot fix; (3) the same run surfaced ADR-018's failure class one layer up: the SDK's reset-tool-choice tracking counts a HANDOFF as tool use (`HandoffCallItem` in `_TOOL_USE_RESET_TRACKING_ITEM_TYPES`), so after the router's first handoff the default `reset_tool_choice=True` flipped it to `"auto"` — and when a specialist handed BACK mid-run, the "auto" router emitted an empty message, ending the run with a non-answer.

**Decision:** (1) All cross-agent edges are wired post-construction in `router.py`, the graph assembly point; everything that runs agents imports it (routes_chat, evals, tests), so the graph is always fully materialized, and `tests/test_agents.py` pins the exact edge set. (2) Back-edge policy in every specialist's instructions: hand back to the router ONLY when the user's request changes domain — never because the specialist's own answer is inconvenient ("no such catalog item" is fulfillment's news to deliver). (3) The router sets `reset_tool_choice=False`: it never speaks (ADR-003), so its `tool_choice="required"` must survive every acting turn, including back-edge re-entries. Specialists keep the default True — they must be free to write their final message after tools. Loop safety = restricted back-edges + `max_turns`, measured by the routing suite's ping-pong metric (mean 0.00 post-fix).

**Alternatives:** A lazy/registry indirection for agent references (machinery for a 4-node graph); letting specialists carry all sibling edges (N² edges and the router stops being the single triage point, ADR-003); handling the empty-final with a runtime retry (papers over the mechanism instead of removing it); dropping back-edges entirely (forces users to restart conversations on topic change).

**Tradeoffs:** Post-construction wiring means an agent's full edge set isn't visible at its definition site (mitigated by module docstrings + the pinned test). `reset_tool_choice=False` on the router hard-codes that the router can never answer directly — if a future milestone wants router-level clarifying questions, that turn has to become a (clarify) specialist or the setting revisited. The empty-final burp remains possible INSIDE a specialist's turn after its reset (observed once in 30 runs pre-fix); instructions now demand a substantive final message, and the routing suite's integrity metric watches the rate.
