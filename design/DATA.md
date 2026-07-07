# `data/` files

┌─────────────────────────┬────────┬───────────────────────────────────────────────────┐
│ File                    │ Size   │ Holds                                             │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ knowledge_articles.json │ 656 KB │ 200 articles (real bodies)                        │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ article_chunks.json     │ 714 KB │ 400 chunks (content; embeddings NULL until M1)    │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ tickets.json            │ 183 KB │ 300 tickets                                       │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ assets.json             │ 22 KB  │ 114 assets                                        │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ catalog_items.json      │ 18 KB  │ 30 catalog items + form schemas                   │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ ticket_comments.json    │ 11 KB  │ 45 comments                                       │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ users.json              │ 8 KB   │ 50 users                                          │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ orders.json             │ 5 KB   │ 16 orders                                         │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ user_facts.json         │ 0.7 KB │ 3 long-term-memory facts                          │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ taxonomy.json           │ 37 KB  │ Stage-1 plan (not seeded)                         │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ negative_space.json     │ 1.8 KB │ queries with NO match — refusal memo (not seeded) │
├─────────────────────────┼────────┼───────────────────────────────────────────────────┤
│ positive_space.json     │ 7 KB   │ queries WITH a match — positive memo (not seeded) │
└─────────────────────────┴────────┴───────────────────────────────────────────────────┘

Top 9 files seed one Postgres table each (cached LLM output — reseeding is free). The last 3 aren't
seeded: `taxonomy.json` is the Stage-1 plan; `negative_space.json` (hand-authored) and
`positive_space.json` (derived from the taxonomy) are quick-reference memos. Machine-checkable
version of both memos: `evals/datasets/retrieval.jsonl`.
