# Data dictionary

Companion to `database_erd.png`. Documents every table and column, with extra depth on columns whose names don't explain themselves. Enum-like string columns list their allowed values — treat these as the source of truth when generating data (M0) and writing tools (M2).

> Tip: when implementing M0, mirror the important notes here into `COMMENT ON TABLE / COMMENT ON COLUMN` statements in the Alembic migration, so the documentation is also queryable from the database itself.

---

## users

IT end users and approvers. Every ticket, order, comment, and memory fact hangs off a user.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | string | |
| email | string | unique; the demo persona is `demo.user@corp.com` |
| org | string | department: `sales / engineering / finance / hr`. Used by the fulfillment agent to autofill cost center, and by the incident agent to scope outages ("Sales team email down") |
| role | string | `employee / manager / it_agent`. `manager` is who the HITL approval flow routes to |

## assets

Hardware assigned to users. The fulfillment agent reads this to resolve which OS variant of a product to order (the "is it a Mac or Windows laptop" question — one join, no graph needed).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid FK → users | owner |
| type | string | what kind of thing: `laptop / desktop / monitor / phone` |
| os | string | `macos / windows / linux`; the column agents filter on. Must use the same enum values as `catalog_items.os_compat` and article OS tags — cross-table consistency rule from the data spec |
| model | string | specific product name for realism, e.g. "MacBook Pro 16 (2024)", "Dell Latitude 5440". Lets the agent say "for your MacBook Pro 16" instead of "for your laptop" |

## knowledge_articles

The knowledge base — source of truth for article content. Retrieval never searches this table directly; it searches `article_chunks` and joins back here for titles and citations.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| title | string | shown in citations |
| body | text | full original article text. Kept so articles can be re-chunked when chunking strategy changes (M1 tuning), displayed in full when a citation is clicked, and re-ingested. Chunks are *derived* from this column |
| category | string | Ticket-category enum (`accounts/software/hardware/network/email/other`) so retrieval can filter/boost by category and a ticket can be matched to same-category articles — this is what makes cross-table invariant 2 actually queryable rather than conceptual. Indexed |
| doc_type | string | Finer bucket within a category: `howto` (default) / `policy` / `release_notes` / `product` / `onboarding`. Enables doc-type-filtered retrieval (e.g. "show me the policy"); note `release_notes` is also inferable from `version`. Indexed |
| version | string | product version for release-notes articles (e.g. "v5.1"); null for ordinary how-tos. Backs the metadata-filtered "compare v5.1 vs v5.2" retrieval case |
| status | string | `published / outdated / draft`. The dataset deliberately contains outdated articles superseded by newer ones, to test that retrieval filters or ranks them down |
| updated_at | timestamp | drives semantic-cache invalidation: updating an article deletes cache entries that cited it (ADR-006) |

## article_chunks

Articles split into ~500-token pieces for retrieval. One row per chunk. This is the table hybrid search actually queries.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| article_id | uuid FK → knowledge_articles | |
| chunk_index | int | 0-based position of the chunk within its article; unique together with `article_id`. Needed to reconstruct reading order for citations, and for neighbor expansion (if chunk 3 matches, also fetch chunks 2 and 4 for context) |
| content | text | the chunk's text — the single source string that BOTH representations below are computed from |
| category, doc_type, status, version | (mirror of article) | Denormalized copies of the parent article's filter fields, so retrieval can PRE-filter (apply the predicate during the vector/FTS index scan) instead of post-filtering after a join — a join-based filter behaves like a post-filter and can silently return fewer than `k` results under a selective filter. The article stays the source of truth; chunks are a derived, rebuildable projection, and the ingest pipeline re-propagates these on any article change (`status` is the one that can change without a re-chunk). Each is indexed |
| embedding | vector(1536) | dense semantic vector of `content` (pgvector, HNSW index, cosine distance). Catches paraphrase: matches "can't log in" to a password-reset article with zero shared words. One half of hybrid search |
| tsv | tsvector | Postgres full-text (lexical) representation of the same `content`: lowercased, stemmed, stop-words removed (GIN index, queried via `ts_rank`). Catches exact tokens the embedding blurs: error codes ("E-505"), version strings ("v5.1"), product names. The other half of hybrid search; the two are fused with reciprocal rank fusion (ADR-011). Implemented as a generated column so it can never drift from `content` |

## catalog_items

Orderable products and services (the "service catalog" in ITSM terms).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | string | |
| price | decimal | drives the HITL rule: > $500 requires manager approval before the order is placed |
| os_compat | string | which OS values this item supports; same enum as `assets.os`. How the fulfillment agent picks the Mac vs Windows variant |
| form_schema | jsonb | self-describing order form: a list of field definitions `{name, label, type, options, required, autofill}`. `autofill` hints tell the agent where a value lives (`asset.os`, `user.org`) so it pre-fills everything knowable and only asks the user for the rest (e.g. business justification). This one column is what makes the fulfillment agent generic across all items instead of hard-coding one form per product |

## orders

A user's request for a catalog item — the "service request" side of ITSM (kept separate from tickets).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| number | string, unique | user-facing order number (`ORD001`, `ORD002`, …) assigned by a DB sequence at insert (migration 0004, ADR-046) — what agents quote to users and what users quote back; the UUID stays internal. Zero-padded to 3 digits, grows naturally past `ORD999`; sequence gaps (rolled-back inserts) are normal |
| user_id | uuid FK → users | requester |
| item_id | uuid FK → catalog_items | |
| status | string | `draft / submitted / fulfilled / cancelled` — the order's lifecycle |
| approval_state | string | `not_required / pending / approved / rejected` — the human-in-the-loop state, deliberately separate from `status`. `pending` means the agent run ended and the order is waiting on a manager; approval triggers actual placement on a fresh run (ADR-005). Persisting this in the DB (not framework memory) is what lets approvals survive restarts and deploys |
| form_values | jsonb | the filled answers to the item's `form_schema`, e.g. `{"license_type": "single-user", "os_variant": "macos", "cost_center": "sales", "business_justification": "..."}`. Written by the fulfillment agent; keys must match the field names declared in `catalog_items.form_schema` |

## tickets

Unified ticket table. Real ITSM (ITIL) splits incidents, requests, changes, and problems into separate entities; this project simplifies to one table with a `type` discriminator, with catalog `orders` covering the service-request flow separately — an informed simplification, not an accidental one.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| number | string, unique | user-facing ticket number (`TKT001`, …) — same mechanism and rules as `orders.number` (migration 0004, ADR-046). Ticket tools accept it anywhere a `ticket_id` argument is expected |
| user_id | uuid FK → users | reporter |
| asset_id | uuid FK → assets, nullable | the affected device, when the issue is about a specific asset ("my laptop is slow"); null for account/service issues |
| type | string | `incident` (something is broken) / `request` (small non-catalog asks). Gives the router agent a cleaner classification target |
| title | string | short summary; part of the embedded text |
| description | text | full issue description; part of the embedded text |
| category | string | routing bucket assigned at creation: `accounts / software / hardware / network / email / other` — should mirror the article taxonomy so tickets and articles line up |
| priority | string | `low / medium / high / critical` |
| status | string | `open / in_progress / resolved / closed`. Dedup searches only `open`/`in_progress` tickets |
| embedding | vector(1536) | semantic vector computed from `title + description` at creation time — NOT from the whole record. Structured fields (priority, status, category) deliberately stay out of the embedded text; they're SQL filter columns. Powers duplicate detection: the incident agent similarity-searches open tickets before creating a new one, and links instead of duplicating above a similarity threshold |

## ticket_comments

The work-notes / conversation thread on a ticket (one-to-many, which is why it's a table and not a column).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| ticket_id | uuid FK → tickets | |
| author_id | uuid FK → users | a human user or the system/agent service account |
| body | text | |
| created_at | timestamp | |

Also the natural mechanism for M8: when the incident agent links a duplicate Slack report to an existing ticket, it appends a comment ("also reported by N users in #it-help") rather than mutating the ticket.

## user_facts

**Not an ITSM entity — this is the AI system's long-term memory** (ADR-007). Stable facts learned about a user across conversations, extracted at session end and injected into agent context at session start, so session 2 remembers what session 1 revealed ("order me an IDE" → pre-selects the macOS variant because a past session established the user has a Mac).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid FK → users | |
| fact_type | string | category key: `device_os / org / contact_preference / software_stack / ...`. Its real job is deduplication: a new fact with an existing `fact_type` *updates* the old one instead of appending, so contradictions replace rather than accumulate |
| fact | text | the content itself, e.g. "owns a MacBook Pro 16", "prefers email over Slack" |
| source | string | which session/conversation the fact was extracted from — for debugging why the system believes something |
| confidence | float | 0–1 extraction confidence ("user explicitly said they have a Mac" ≈ 0.95; "user mentioned Xcode, probably a Mac" ≈ 0.6). Merge rule keeps newer/higher-confidence; injection skips facts below a threshold |
| updated_at | timestamp | tiebreaker in the merge rule |

## cis

**CMDB configuration items (M9, ADR-035)** — shared infrastructure nodes: services, servers, databases, plus *teams as nodes*. Deliberately a separate table from `assets`: assets are user-owned end devices (an owner, an OS, a model); CIs are shared infrastructure (dependents, an owning org, no user). One generalized node table (rather than per-kind tables) keeps the dependency traversal a single self-join and gives edges real FK integrity. Queried only through `query_dependency_graph` (ADR-004).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | string | unique; the stable *public* identity (`auth-service`, `db-server-02`, `team-sales`) — tools and eval ground truth resolve CIs by name, never by UUID |
| ci_type | string | `service / server / database / team`. Teams are nodes on purpose: "server down → which teams/users?" becomes one traversal plus a users-by-org lookup, not a special-cased join path |
| owner_org | string, nullable | for teams: the org whose users the node represents (`sales / engineering / finance / hr`) — this is how an impact set resolves to a user count. For services: the owning department. NULL for servers/databases (platform-owned) |
| description | text, nullable | one-liner for tool payloads and demos |

## dependencies

Directed CMDB edges: **`dependent` DEPENDS ON `dependency`** (`auth-service runs_on app-server-01`, `team-sales uses crm-service`). Impact analysis ("what breaks?") traverses *against* the arrows; root-cause analysis ("what does it rely on?") follows them. Cycle safety lives in the traversal (recursive-CTE path guard / Cypher relationship isomorphism), not the schema — only self-loops are structurally impossible.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| dependent_id | uuid FK → cis (CASCADE) | the thing that relies |
| dependency_id | uuid FK → cis (CASCADE) | the thing relied upon; unique together with `dependent_id`, and `<> dependent_id` (no self-loops). Both columns are indexed — traversal filters on either depending on direction |
| dep_type | string | edge flavor for display: `runs_on` (service/database → server) / `uses` (service → database, team → service) / `calls` (service → service). Traversal ignores it |

Neo4j (Phase 2) holds a *derived projection* of these two tables — `(:CI)-[:DEPENDS_ON]->(:CI)`, written by `graph/sync_neo4j.py`. Postgres remains the source of truth.

---

## Cross-table invariants (enforce in data generation and tools)

1. One OS enum everywhere: `assets.os`, `catalog_items.os_compat`, and article OS tags share the exact values `macos / windows / linux`.
2. `tickets.category` values mirror the knowledge-article taxonomy so agents can suggest articles for a ticket's category.
3. `article_chunks` and `tickets` embeddings must use the same embedding model and dimension (1536) — the semantic cache and dedup both depend on it.
4. Tickets reference real users, and `asset_id` (when set) must belong to that ticket's user.
5. `orders.approval_state` may be `pending` only while `status = submitted`.
