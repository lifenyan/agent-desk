"""Quality suite (M5, ADR-033): LLM-as-judge scoring of knowledge answers, nightly-only.

The deterministic suites check binary contracts (right articles retrieved, right row written,
refusal shape). What they cannot see is GRADED quality: an answer can cite the right article
and still misquote it, or bury the fix in noise. This suite runs ~10 answerable questions
through the knowledge agent and has a judge score each answer 1–5 on two dimensions:

- faithfulness-to-citations: every claim supported by the retrieved chunks the agent saw —
  judged against the SOURCES text, not world knowledge;
- helpfulness: does the answer actually resolve the user's question.

Judge design (ADR-033): the judge prompt lives in evals/judge_prompt.md (verbatim
instructions, diff-reviewed like code); the judge model is settings.judge_model — default
gpt-5, deliberately STRONGER than the gpt-5-mini under test, because a model grading its own
family's outputs shows self-preference bias. Structured output (integer scores + one-line
justifications), so scoring is parse-free.

Floors: none yet, on purpose (ADR-026 discipline — a floor needs a baseline plus observed
run-to-run variance; one run provides neither). The suite REPORTS mean + distribution; a
floor lands in thresholds.toml once nightly runs show what normal looks like.
"""
# Implemented in M5.

from __future__ import annotations

import asyncio
import json
import time
from functools import lru_cache
from pathlib import Path

from agents import Agent, Runner
from pydantic import BaseModel, Field

from app.agents.context import ChatContext
from app.agents.knowledge import knowledge_agent, resolve_model
from app.config import get_settings
from evals.common import DATASET_DIR, FLOORS, cost_latency_aggregates, load_jsonl, usage_fields

JUDGE_PROMPT_PATH = Path(__file__).parent / "judge_prompt.md"
SCORE_MIN, SCORE_MAX = 1, 5


class JudgeScores(BaseModel):
    faithfulness: int = Field(ge=SCORE_MIN, le=SCORE_MAX)
    faithfulness_justification: str
    helpfulness: int = Field(ge=SCORE_MIN, le=SCORE_MAX)
    helpfulness_justification: str


@lru_cache
def _judge() -> Agent:
    # Instructions are everything after the "---" separator: the part above it is
    # human-facing documentation, the part below is the verbatim judge prompt.
    prompt = JUDGE_PROMPT_PATH.read_text().split("---", 1)[1].strip()
    return Agent(
        name="quality_judge",
        instructions=prompt,
        output_type=JudgeScores,
        model=resolve_model(get_settings().judge_model),
    )


def _retrieved_sources(result) -> str:
    """The chunk texts the agent's search tools actually returned — the ground the judge
    scores faithfulness against (same tool-output walk as routes_chat._collect_citations)."""
    blocks: list[str] = []
    for item in result.new_items:
        if getattr(item, "type", None) != "tool_call_output_item":
            continue
        output = item.output
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                continue
        if not isinstance(output, dict):
            continue
        payloads = [p for p in (output, *output.values()) if isinstance(p, dict) and "results" in p]
        for payload in payloads:
            for r in payload["results"]:
                blocks.append(f"[{r['article_title']}] {r['content']}")
    # Dedup identical chunks across retries while preserving retrieval order.
    return "\n\n".join(dict.fromkeys(blocks)) or "(no sources retrieved)"


async def _quality_case(case: dict) -> dict:
    settings = get_settings()
    t0 = time.perf_counter()
    result = await Runner.run(knowledge_agent, case["query"], context=ChatContext())
    answer_latency = time.perf_counter() - t0
    answer = str(result.final_output)
    answer_usage = usage_fields(result, settings.specialist_model)

    judge_input = (
        f"QUESTION:\n{case['query']}\n\nANSWER:\n{answer}\n\nSOURCES:\n{_retrieved_sources(result)}"
    )
    t0 = time.perf_counter()
    judge_result = await Runner.run(_judge(), judge_input)
    judge_latency = time.perf_counter() - t0
    scores: JudgeScores = judge_result.final_output
    judge_usage = usage_fields(judge_result, settings.judge_model)

    cost_parts = [c for c in (answer_usage["cost_usd"], judge_usage["cost_usd"]) if c is not None]
    return {
        "query": case["query"],
        "answer": answer,
        "faithfulness": scores.faithfulness,
        "faithfulness_justification": scores.faithfulness_justification,
        "helpfulness": scores.helpfulness,
        "helpfulness_justification": scores.helpfulness_justification,
        "latency_s": round(answer_latency + judge_latency, 2),
        "answer_latency_s": round(answer_latency, 2),
        "input_tokens": answer_usage["input_tokens"] + judge_usage["input_tokens"],
        "output_tokens": answer_usage["output_tokens"] + judge_usage["output_tokens"],
        "cost_usd": round(sum(cost_parts), 6) if cost_parts else None,
        "judge_cost_usd": judge_usage["cost_usd"],
    }


def _distribution(rows: list[dict], key: str) -> dict[str, int]:
    return {str(s): sum(1 for r in rows if r[key] == s) for s in range(SCORE_MIN, SCORE_MAX + 1)}


def run_quality(**_ignored) -> dict:
    """Run the quality suite; retrieval-suite kwargs (k/threshold/refusal_mode) don't apply."""
    cases = load_jsonl(DATASET_DIR / "quality.jsonl")

    async def _all() -> list[dict]:
        return [await _quality_case(case) for case in cases]

    rows = asyncio.run(_all())

    n = len(rows)
    report = {
        "suite": "quality",
        "judge_model": get_settings().judge_model,
        "rows": rows,
        "aggregates": {
            "faithfulness_mean": round(sum(r["faithfulness"] for r in rows) / n, 2),
            "faithfulness_distribution": _distribution(rows, "faithfulness"),
            "helpfulness_mean": round(sum(r["helpfulness"] for r in rows) / n, 2),
            "helpfulness_distribution": _distribution(rows, "helpfulness"),
            "n": n,
        },
    }
    report["cost_latency"] = cost_latency_aggregates(rows)
    # Report-only until a baseline + variance exist (module docstring); if floors are later
    # added to thresholds.toml under [quality], they gate here with no code change.
    quality_floors = FLOORS.get("quality", {})
    agg = report["aggregates"]
    report["passed"] = all(agg[metric] >= floor for metric, floor in quality_floors.items())
    return report
