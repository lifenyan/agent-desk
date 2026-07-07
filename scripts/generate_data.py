"""Two-stage synthetic-data generator for the ITSM dataset.

Why two stages: Stage 1 emits a cheap *taxonomy* (every record's title/name + tags, plus the ticket
cluster plan and user/asset distribution) for human review BEFORE any expensive body generation.
Stage 2 turns the approved taxonomy into full records.

Determinism & cost: a fixed RANDOM_SEED plus uuid5-derived stable IDs make regeneration reproducible,
and every output file is cached — a file that already exists is skipped unless --force. seed_db.py
then loads the cached JSON, so seeding never re-pays for LLM calls (ADR-010 wants reproducible data).

Division of labor: structured records (users, assets, catalog metadata, orders, facts, ticket/comment
scaffolding) are built deterministically in Python from the taxonomy; only natural-language prose
(article bodies, ticket descriptions) is produced by the LLM. Embeddings stay NULL — M1 populates them.

Run:  python scripts/generate_data.py --stage 1                 # taxonomy only (review it, then:)
      python scripts/generate_data.py --stage 2                 # full records (LLM prose; needs a key)
      python scripts/generate_data.py --stage 2 --dry-run       # templated prose, no LLM, no cost
"""
# Implemented in M0.

from __future__ import annotations

import argparse
import json
import os
import random
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
EVAL_DIR = ROOT / "evals" / "datasets"

RANDOM_SEED = 42
NAMESPACE = uuid.UUID("00000000-0000-0000-0000-a9e7de5c0000")  # fixed → stable IDs across runs
GEN_MODEL = os.environ.get("GEN_MODEL", "gpt-4o-mini")
CHUNK_WORDS = 350  # ~500 tokens

FIRST_NAMES = [
    "Alex",
    "Jordan",
    "Taylor",
    "Casey",
    "Riley",
    "Morgan",
    "Jamie",
    "Avery",
    "Quinn",
    "Sam",
    "Dana",
    "Priya",
    "Wei",
    "Diego",
    "Fatima",
    "Noah",
    "Mia",
    "Liam",
    "Sofia",
    "Omar",
    "Hana",
    "Leo",
    "Nina",
    "Raj",
    "Elena",
    "Kofi",
    "Yuki",
    "Ivan",
    "Grace",
    "Tom",
]
LAST_NAMES = [
    "Reyes",
    "Chen",
    "Patel",
    "Kim",
    "Nguyen",
    "Garcia",
    "Okafor",
    "Rossi",
    "Haddad",
    "Silva",
    "Novak",
    "Ali",
    "Brown",
    "Ivanov",
    "Suzuki",
    "Meyer",
    "Costa",
    "Khan",
    "Park",
    "Diaz",
]
MODELS = {
    ("macos", "laptop"): [
        "MacBook Pro 16 (2024)",
        "MacBook Air 13 (2023)",
        "MacBook Pro 14 (2023)",
    ],
    ("windows", "laptop"): ["Dell Latitude 5450", "Lenovo ThinkPad X1 Carbon", "HP EliteBook 840"],
    ("linux", "laptop"): ["Dell XPS 13 (Ubuntu)", "System76 Lemur Pro"],
    ("macos", "desktop"): ["Mac Studio (2023)", "iMac 24 (2023)"],
    ("windows", "desktop"): ["Dell OptiPlex 7010", "HP Z2 Tower"],
    ("linux", "desktop"): ["System76 Thelio"],
    ("macos", "monitor"): ["Apple Studio Display", "LG UltraFine 27"],
    ("windows", "monitor"): ["Dell UltraSharp U2723", "HP E27"],
    ("linux", "monitor"): ["Dell UltraSharp U2723"],
    ("macos", "phone"): ["iPhone 15", "iPhone 14"],
    ("windows", "phone"): ["Samsung Galaxy S24", "Google Pixel 8"],
    ("linux", "phone"): ["Google Pixel 8"],
}


def stable_id(kind: str, slug: str) -> str:
    """Deterministic UUID for a record, so eval files can hard-code expected IDs."""
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{slug}"))


def _write(name: str, payload, *, force: bool, subdir: Path = DATA_DIR) -> bool:
    path = subdir / name
    if path.exists() and not force:
        print(f"  skip {path.relative_to(ROOT)} (exists; --force to regenerate)")
        return False
    subdir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    path.write_text(text + ("\n" if not text.endswith("\n") else ""))
    print(
        f"  wrote {path.relative_to(ROOT)} ({len(payload) if isinstance(payload, list) else '-'} rows)"
    )
    return True


def _llm_text(prompt: str, *, dry: bool) -> str:
    """Prose via LiteLLM; in --dry-run return deterministic templated text (no LLM, no cost)."""
    if dry:
        return (
            f"[placeholder body]\n\n{prompt.splitlines()[0]}\n\n"
            "Step 1: Open the relevant application or portal.\n\n"
            "Step 2: Follow the on-screen prompts and confirm your changes.\n\n"
            "If the issue persists, contact the IT service desk and reference this article."
        )
    from litellm import completion

    resp = completion(
        model=GEN_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        seed=RANDOM_SEED,
    )
    return resp["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------------------------------------
# Stage 1 — taxonomy (human-curated; committed at data/taxonomy.json)
# --------------------------------------------------------------------------------------------------
def stage1(force: bool) -> None:
    print("Stage 1: taxonomy")
    tax_path = DATA_DIR / "taxonomy.json"
    if tax_path.exists() and not force:
        tax = json.loads(tax_path.read_text())
        print(
            f"  using existing taxonomy.json "
            f"({len(tax['articles'])} articles, {len(tax['catalog_items'])} catalog items)"
        )
        print("  -> review it, then run Stage 2.")
        return
    raise SystemExit(
        "data/taxonomy.json is missing. It is human-authored/reviewed before Stage 2; "
        "restore it from version control rather than regenerating blindly."
    )


# --------------------------------------------------------------------------------------------------
# Stage 2 — full records
# --------------------------------------------------------------------------------------------------
def _chunk(body: str) -> list[str]:
    """Pack paragraphs into ~CHUNK_WORDS-word chunks; deterministic, no external tokenizer."""
    chunks, buf, count = [], [], 0
    for para in (p.strip() for p in body.split("\n\n") if p.strip()):
        words = len(para.split())
        if count + words > CHUNK_WORDS and buf:
            chunks.append("\n\n".join(buf))
            buf, count = [], 0
        buf.append(para)
        count += words
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks or [body.strip()]


def _gen_articles(tax: dict, *, force: bool, dry: bool) -> None:
    articles, chunks = [], []
    total = len(tax["articles"])
    for n, a in enumerate(tax["articles"], 1):
        slug = a["slug"]
        body = _llm_text(
            f"Write a realistic internal corporate IT knowledge-base article.\n"
            f"Title: {a['title']}\nCategory: {a['category']}"
            + (f"\nProduct version: {a['version']}" if a.get("version") else "")
            + (f"\nOS: {a['os_tag']}" if a.get("os_tag") else "")
            + "\n300-500 words, practical numbered steps, plain markdown, no preamble.",
            dry=dry,
        )
        aid = stable_id("article", slug)
        articles.append(
            {
                "id": aid,
                "title": a["title"],
                "body": body,
                "category": a["category"],
                "doc_type": a.get("doc_type", "howto"),
                "version": a.get("version"),
                "status": a.get("status", "published"),
            }
        )
        for i, content in enumerate(_chunk(body)):
            chunks.append(
                {
                    "id": stable_id("chunk", f"{slug}:{i}"),
                    "article_id": aid,
                    "chunk_index": i,
                    "content": content,
                    "category": a["category"],
                    "doc_type": a.get("doc_type", "howto"),
                    "status": a.get("status", "published"),
                    "version": a.get("version"),
                }
            )
        if not dry and n % 25 == 0:
            print(f"    ...{n}/{total} article bodies")
    _write("knowledge_articles.json", articles, force=force)
    _write("article_chunks.json", chunks, force=force)


def _default_form_schema(item: dict) -> list[dict]:
    """Generic form from autofillable profile/asset fields + one free-text justification."""
    fields = [
        {
            "name": "cost_center",
            "label": "Cost center",
            "type": "select",
            "options": ["sales", "engineering", "finance", "hr"],
            "required": True,
            "autofill": "user.org",
        }
    ]
    if item.get("os_compat") and len(item["os_compat"]) > 1:
        fields.append(
            {
                "name": "os_variant",
                "label": "OS variant",
                "type": "select",
                "options": item["os_compat"],
                "required": True,
                "autofill": "asset.os",
            }
        )
    if float(item["price"]) > 500:
        fields.append(
            {
                "name": "business_justification",
                "label": "Business justification",
                "type": "text",
                "required": True,
                "autofill": None,
            }
        )
    return fields


def _gen_catalog(tax: dict, *, force: bool) -> None:
    items = [
        {
            "id": stable_id("catalog", c["slug"]),
            "name": c["name"],
            "price": c["price"],
            "os_compat": c.get("os_compat"),
            "form_schema": _default_form_schema(c),
        }
        for c in tax["catalog_items"]
    ]
    _write("catalog_items.json", items, force=force)


def _gen_people(tax: dict, *, force: bool) -> tuple[list[dict], list[dict]]:
    """Deterministically materialize users + assets from anchors + distribution. Returns them so the
    ticket/order/fact generators can reference real ids and enforce asset-ownership."""
    rnd = random.Random(RANDOM_SEED)
    orgs = ["sales", "engineering", "finance", "hr"]
    users: list[dict] = []
    emails: set[str] = set()

    def add_user(name, email, org, role):
        emails.add(email)
        u = {"id": stable_id("user", email), "name": name, "email": email, "org": org, "role": role}
        users.append(u)
        return u

    # Anchors (exact, from taxonomy)
    add_user("Demo User", "demo.user@corp.com", "sales", "employee")
    add_user("Morgan Reyes", "morgan.reyes@corp.com", "sales", "manager")
    add_user("IT Service Account", "it.bot@corp.com", "engineering", "it_agent")
    # One manager per remaining org + one more it_agent
    add_user("Dana Novak", "dana.novak@corp.com", "engineering", "manager")
    add_user("Priya Patel", "priya.patel@corp.com", "finance", "manager")
    add_user("Leo Meyer", "leo.meyer@corp.com", "hr", "manager")
    add_user("Sam Brown", "sam.brown@corp.com", "engineering", "it_agent")

    while len(users) < 50:
        fn, ln = rnd.choice(FIRST_NAMES), rnd.choice(LAST_NAMES)
        email = f"{fn}.{ln}{rnd.randint(1, 99)}@corp.com".lower()
        if email in emails:
            continue
        add_user(f"{fn} {ln}", email, rnd.choice(orgs), "employee")

    # Assets: primary OS per user (40/50/10), 1 laptop of that OS + 0-3 extras of same OS.
    assets: list[dict] = []

    def add_asset(user, atype, os_):
        idx = len([a for a in assets if a["user_id"] == user["id"]])
        aid = stable_id("asset", f"{user['email']}:{idx}")
        model = rnd.choice(MODELS[(os_, atype)])
        assets.append({"id": aid, "user_id": user["id"], "type": atype, "os": os_, "model": model})

    demo = users[0]
    add_asset(demo, "laptop", "macos")  # demo.user: exactly one MacBook Pro
    for u in users[1:]:
        primary = rnd.choices(["macos", "windows", "linux"], weights=[40, 50, 10])[0]
        add_asset(u, "laptop", primary)
        for atype in rnd.sample(["desktop", "monitor", "phone"], k=rnd.randint(0, 3)):
            if len(assets) >= 150:
                break
            add_asset(u, atype, primary)
        if len(assets) >= 150:
            break

    _write("users.json", users, force=force)
    _write("assets.json", assets, force=force)
    return users, assets


def _gen_tickets(
    tax: dict, users: list[dict], assets: list[dict], *, force: bool, dry: bool
) -> None:
    rnd = random.Random(RANDOM_SEED + 1)
    by_user_laptop = {}
    for a in assets:
        if a["type"] == "laptop":
            by_user_laptop.setdefault(a["user_id"], a["id"])
    sales_users = [u for u in users if u["org"] == "sales" and u["role"] == "employee"]
    all_emp = [u for u in users if u["role"] == "employee"]
    it_bot = next(u for u in users if u["role"] == "it_agent")

    tickets: list[dict] = []
    comments: list[dict] = []

    def desc(title, category, ttype):
        return _llm_text(
            f"Write a first-person IT {ttype} ticket description (2-4 sentences) for: '{title}' "
            f"(category: {category}). Realistic employee voice, no salutation.",
            dry=dry,
        )

    def add_ticket(slug, title, ttype, category, priority, status, reporter, set_asset=False):
        tid = stable_id("ticket", slug)
        asset_id = by_user_laptop.get(reporter["id"]) if set_asset else None
        tickets.append(
            {
                "id": tid,
                "user_id": reporter["id"],
                "asset_id": asset_id,
                "type": ttype,
                "title": title,
                "description": desc(title, category, ttype),
                "category": category,
                "priority": priority,
                "status": status,
            }
        )
        return tid

    # Clusters (dedup demos)
    for cl in tax["ticket_clusters"]:
        titles = [cl["canonical_title"], *cl.get("variant_titles", [])]
        statuses = cl.get("status_mix") or [cl.get("status", "open")]
        pool = sales_users if cl["slug"] == "exchange-outage" else all_emp
        canonical_id = None
        for i, title in enumerate(titles):
            reporter = rnd.choice(pool)
            status = cl.get("status") or rnd.choice(statuses)
            tid = add_ticket(
                f"{cl['slug']}:{i}",
                title,
                cl["type"],
                cl["category"],
                cl["priority"],
                status,
                reporter,
                set_asset=(cl["slug"] == "slow-laptop"),
            )
            if i == 0:
                canonical_id = tid
        # it_agent dedup-link comment on the canonical
        comments.append(
            {
                "id": stable_id("comment", f"{cl['slug']}:dedup"),
                "ticket_id": canonical_id,
                "author_id": it_bot["id"],
                "body": f"Linked {len(titles) - 1} duplicate report(s) to this ticket (dedup).",
            }
        )

    # Remainder spread across categories
    categories = ["accounts", "software", "hardware", "network", "email", "other"]
    cat_weights = [20, 20, 15, 15, 15, 15]
    statuses = ["open", "in_progress", "resolved", "closed"]
    stat_weights = [25, 20, 35, 20]
    priorities = ["low", "medium", "high", "critical"]
    prio_weights = [35, 40, 20, 5]
    target = tax["meta"]["counts"]["tickets"]
    remainder = target - len(tickets)
    for i in range(remainder):
        cat = rnd.choices(categories, weights=cat_weights)[0]
        ttype = rnd.choices(["incident", "request"], weights=[65, 35])[0]
        status = rnd.choices(statuses, weights=stat_weights)[0]
        priority = rnd.choices(priorities, weights=prio_weights)[0]
        reporter = rnd.choice(all_emp)
        set_asset = cat == "hardware" and rnd.random() < 0.6
        title = f"{cat.title()} issue: {rnd.choice(_TITLE_SNIPPETS[cat])}"
        tid = add_ticket(f"filler:{i}", title, ttype, cat, priority, status, reporter, set_asset)
        if status in ("resolved", "closed") and rnd.random() < 0.25:
            comments.append(
                {
                    "id": stable_id("comment", f"filler:{i}"),
                    "ticket_id": tid,
                    "author_id": it_bot["id"],
                    "body": "Resolved — see linked KB article for steps.",
                }
            )

    _write("tickets.json", tickets, force=force)
    _write("ticket_comments.json", comments, force=force)


_TITLE_SNIPPETS = {
    "accounts": [
        "can't access shared drive",
        "MFA prompt loop",
        "locked out after PTO",
        "need group access",
        "SSO redirect fails",
    ],
    "software": [
        "app won't launch",
        "license expired",
        "update stuck",
        "add-in disabled",
        "install request",
    ],
    "hardware": [
        "docking station not detected",
        "keyboard keys sticking",
        "battery drains fast",
        "monitor no signal",
        "webcam not working",
    ],
    "network": [
        "Wi-Fi keeps dropping",
        "can't reach internal site",
        "slow connection",
        "VPN certificate error",
        "DNS not resolving",
    ],
    "email": [
        "not receiving external mail",
        "calendar not syncing",
        "quota exceeded",
        "distribution list request",
        "signature not applying",
    ],
    "other": [
        "onboarding setup",
        "desk move IT request",
        "accessibility tool request",
        "general how-to question",
        "feedback on service",
    ],
}


def _gen_orders(tax: dict, users: list[dict], assets: list[dict], *, force: bool) -> None:
    """A small realistic order set, anchored by a PENDING >$500 HITL order for the demo user."""
    cat = {c["slug"]: c for c in tax["catalog_items"]}
    demo = next(u for u in users if u["email"] == "demo.user@corp.com")

    def order(slug, user, item_slug, status, approval, values):
        return {
            "id": stable_id("order", slug),
            "user_id": user["id"],
            "item_id": stable_id("catalog", item_slug),
            "status": status,
            "approval_state": approval,
            "form_values": values,
        }

    orders = [
        # HITL anchor: Photoshop ($650) awaiting Morgan's approval.
        order(
            "demo-photoshop",
            demo,
            "photoshop",
            "submitted",
            "pending",
            {
                "cost_center": "sales",
                "os_variant": "macos",
                "business_justification": "Editing marketing collateral for Q3 launch.",
            },
        ),
        # A cheap, already-fulfilled order (no approval needed).
        order(
            "demo-onepassword",
            demo,
            "onepassword",
            "fulfilled",
            "not_required",
            {"cost_center": "sales"},
        ),
    ]
    # A few more across employees (mix of states), deterministic.
    rnd = random.Random(RANDOM_SEED + 2)
    emps = [u for u in users if u["role"] == "employee"][1:15]
    cheap = [s for s, c in cat.items() if float(c["price"]) <= 500]
    pricey = [s for s, c in cat.items() if float(c["price"]) > 500]
    for i, u in enumerate(emps):
        if i % 3 == 0:
            item = rnd.choice(pricey)
            st, appr = rnd.choice(
                [("submitted", "pending"), ("fulfilled", "approved"), ("cancelled", "rejected")]
            )
        else:
            item, st, appr = rnd.choice(cheap), rnd.choice(["fulfilled", "draft"]), "not_required"
        orders.append(order(f"emp:{i}", u, item, st, appr, {"cost_center": u["org"]}))
    _write("orders.json", orders, force=force)


def _gen_facts(users: list[dict], *, force: bool) -> None:
    """Seed long-term memory, anchored by demo.user's device_os fact (the memory demo)."""
    demo = next(u for u in users if u["email"] == "demo.user@corp.com")
    facts = [
        {
            "id": stable_id("fact", "demo:device_os"),
            "user_id": demo["id"],
            "fact_type": "device_os",
            "fact": "Owns a MacBook Pro 16 (macOS).",
            "source": "seed",
            "confidence": 0.95,
        },
        {
            "id": stable_id("fact", "demo:org"),
            "user_id": demo["id"],
            "fact_type": "org",
            "fact": "Works in the Sales organization.",
            "source": "seed",
            "confidence": 0.9,
        },
        {
            "id": stable_id("fact", "demo:contact"),
            "user_id": demo["id"],
            "fact_type": "contact_preference",
            "fact": "Prefers email over Slack.",
            "source": "seed",
            "confidence": 0.6,
        },
    ]
    _write("user_facts.json", facts, force=force)


def _gen_evalset(tax: dict, *, force: bool) -> None:
    """Emit evals/datasets/retrieval.jsonl from the taxonomy plan, resolving slugs -> stable ids."""
    plan = tax["retrieval_eval_plan"]
    lines = []
    for r in plan["answerable"]:
        lines.append(
            json.dumps(
                {
                    "query": r["query"],
                    "expected_article_ids": [
                        stable_id("article", s) for s in r["expected_article_slugs"]
                    ],
                }
            )
        )
    for r in plan["refusal"]:
        lines.append(
            json.dumps(
                {
                    "query": r["query"],
                    "expected_article_ids": [],
                    "refusal": True,
                    "negative_space": r["negative_space"],
                }
            )
        )
    _write("retrieval.jsonl", "\n".join(lines), force=force, subdir=EVAL_DIR)


def _gen_positive_space(tax: dict, *, force: bool) -> None:
    """Human-readable memo (mirror of negative_space.json): each answerable query + the article
    title(s) it should retrieve. Derived from the taxonomy so it can't drift; the machine-checkable
    version is evals/datasets/retrieval.jsonl."""
    by_slug = {a["slug"]: a for a in tax["articles"]}
    cases = []
    for r in tax["retrieval_eval_plan"]["answerable"]:
        slugs = r["expected_article_slugs"]
        case = {
            "query": r["query"],
            "expected_articles": [
                {"slug": s, "title": by_slug[s]["title"], "category": by_slug[s]["category"]}
                for s in slugs
            ],
        }
        note = r.get("note") or by_slug[slugs[0]].get("anchor")
        if note:
            case["note"] = note
        cases.append(case)
    payload = {
        "_comment": "Positive-space memo (mirror of negative_space.json): queries that SHOULD "
        "retrieve a specific article, with the expected match. Human reference so the "
        "demo/eval intent isn't forgotten. Derived from taxonomy.json's "
        "retrieval_eval_plan; machine-checkable version = evals/datasets/retrieval.jsonl.",
        "matched_cases": cases,
    }
    _write("positive_space.json", payload, force=force)


def stage2(force: bool, dry: bool) -> None:
    print(f"Stage 2: full records{' (dry-run: templated prose, no LLM)' if dry else ''}")
    tax = json.loads((DATA_DIR / "taxonomy.json").read_text())
    _gen_catalog(tax, force=force)
    users, assets = _gen_people(tax, force=force)
    _gen_orders(tax, users, assets, force=force)
    _gen_facts(users, force=force)
    _gen_tickets(tax, users, assets, force=force, dry=dry)
    _gen_articles(tax, force=force, dry=dry)  # last: the expensive LLM step
    _gen_evalset(tax, force=force)
    _gen_positive_space(tax, force=force)
    print("Stage 2 complete. Next: make seed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["1", "2"], default="1")
    parser.add_argument("--force", action="store_true", help="regenerate even if output exists")
    parser.add_argument("--dry-run", action="store_true", help="templated prose, no LLM calls")
    args = parser.parse_args()
    if args.stage == "1":
        stage1(args.force)
    else:
        stage2(args.force, args.dry_run)


if __name__ == "__main__":
    main()
