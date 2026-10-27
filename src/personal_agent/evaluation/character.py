"""Offline character ablations by default; paid generation requires explicit --live."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.context import ContextBuilder
from personal_agent.core.history import HistoryTurn, InMemoryHistory
from personal_agent.core.models import (
    AssistantMessage,
    ModelResponse,
    TextMessage,
    ToolCall,
    ToolMessage,
)
from personal_agent.core.retrieval import RetrievalItem, RetrievalResult
from personal_agent.evaluation.character_cases import CASES, RUBRIC
from personal_agent.evaluation.runner import Budget, EvaluationLimit
from personal_agent.llm.base import Provider
from personal_agent.memory.privacy import require_private_directory
from personal_agent.tools.builtin import create_builtin_registry


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def turn(index: int, prompt: str, answer: str) -> HistoryTurn:
    messages = [TextMessage(role="user", text=prompt), AssistantMessage(text=answer)]
    return HistoryTurn(
        run_id=f"synthetic-run-{index}",
        first_sequence=index * 2 + 1,
        last_sequence=index * 2 + 2,
        messages=messages,
        source_hash=digest([m.model_dump() for m in messages]),
    )


class FixedRetrieval:
    """Controlled P1/P2 output fixture, not a claim about organizer semantic accuracy."""

    def __init__(self, enabled: bool):
        self.result = RetrievalResult(
            items=[
                RetrievalItem(
                    reference={
                        "kind": "episode_thread_evidence",
                        "episode_id": "synthetic-ep-1",
                        "thread_id": "synthetic-thread-1",
                        "as_of": "2026-01-01T08:00:00Z",
                    },
                    turn=turn(100, "我们调试索引错误，确认参数化查询。", "已核验该合成测试。"),
                ),
                RetrievalItem(
                    reference={
                        "kind": "semantic_memory",
                        "path": "people/preferences.md",
                        "section_hash": "synthetic-preference",
                        "text": "先给结论，不要固定行动清单。",
                    }
                ),
                RetrievalItem(
                    reference={
                        "kind": "episode_thread_evidence",
                        "episode_id": "synthetic-ep-2",
                        "thread_id": "synthetic-thread-1",
                        "as_of": "2026-01-02T08:00:00Z",
                    },
                    turn=turn(101, "更正：索引方案需要额外测试，不能说已经上线。", "确认未上线。"),
                ),
            ]
            if enabled
            else []
        )

    def retrieve(self, user_input, exclude_run_ids):
        return self.result.model_copy(deep=True)


class OfflineProvider:
    """Mechanical script only. Its text must never receive a semantic character score."""

    async def generate(self, messages, tools, *, max_output_tokens=None):
        user = next(
            m.text for m in reversed(messages) if isinstance(m, TextMessage) and m.role == "user"
        )
        if user.startswith("工具核验") and not isinstance(messages[-1], ToolMessage):
            return ModelResponse(
                tool_calls=[
                    ToolCall(id="synthetic-add", name="add_numbers", arguments='{"a":2,"b":3}')
                ]
            )
        return ModelResponse(text="离线脚本占位回复，不代表角色语义品质。")


class Trace:
    def __init__(self, provider: Provider, budget: Budget):
        self.provider, self.budget = provider, budget
        self.calls = []

    async def generate(self, messages, tools, *, max_output_tokens=None):
        self.budget.reserve(messages, tools, max_output_tokens)
        row = {
            "input_estimated": estimate_request(messages, tools),
            "messages": [m.model_dump(mode="json") for m in messages],
            "tools": [t.model_dump(mode="json") for t in tools],
        }
        self.calls.append(row)
        start = perf_counter()
        try:
            response = await self.provider.generate(
                messages, tools, max_output_tokens=max_output_tokens
            )
            self.budget.observe(response.usage)
            row.update(
                text=response.text,
                tool_calls=[t.model_dump() for t in response.tool_calls],
                usage=response.usage.model_dump() if response.usage else None,
            )
            return response
        finally:
            row["seconds"] = perf_counter() - start


async def evaluate(
    variant: str,
    provider: Provider,
    budget: Budget,
    *,
    case_ids=None,
    context_window=131072,
    output_tokens=512,
    model="offline-script",
) -> dict:
    if variant not in {"E0", "E1", "E2"}:
        raise ValueError("Only E0/E1/E2 are implemented")
    package = CharacterLoader().load("sherlock_bbc")
    rows = []
    before_usage = budget.report()
    traced = Trace(provider, budget)
    for case in CASES:
        if case_ids is not None and case.id not in case_ids:
            continue
        fixed_history = (
            [turn(90, "相关性可以证明因果吗？", "能证明因果。")] if case.id == "own_error" else []
        )
        for index, prompt in enumerate(case.prompts):
            history = InMemoryHistory()
            history.turns = [t.model_copy(deep=True) for t in fixed_history]
            history._sequence = fixed_history[-1].last_sequence if fixed_history else 0
            retrieval = FixedRetrieval(case.retrieved)
            # Identical externally controlled evidence/state fixtures for all variants.
            state = (
                {
                    "kind": "simulated_character_state",
                    "mood": "curious" if index < 7 else "focused",
                    "current_focus": "核验反例",
                    "observed_at": "2026-01-02T09:00:00Z",
                    "authority": "transient_reference_not_identity_or_permission",
                }
                if index < 13
                else None
            )
            compiler = CharacterContextCompiler(
                package,
                variant=variant,
                state=lambda value=state: value,
                source_scope=lambda ids: {
                    rid: "same_character" for rid in ids if rid.startswith("synthetic-run-")
                },
            )
            tools = create_builtin_registry()
            context = ContextBuilder(
                history_store=history,
                character=compiler,
                retriever=retrieval,
                budget=ContextBudget(
                    context_window=context_window, reserved_output_tokens=output_tokens
                ),
                temporal=lambda: {"now": "2026-01-02T10:00:00Z"},
            )
            start_calls = len(traced.calls)
            result = await Agent(traced, tools, context=context, max_rounds=3).run(prompt)
            if budget.exhausted:
                raise EvaluationLimit("evaluation_budget_exhausted")
            calls = traced.calls[start_calls:]
            conditions = {
                "prompt": prompt,
                "history": [t.model_dump() for t in fixed_history],
                "retrieval": retrieval.result.model_dump(),
                "model": model,
                "state_fixture": state,
                "context_window": context_window,
                "output_tokens": output_tokens,
                "tools": [t.model_dump() for t in tools.definitions()],
            }
            checks = {
                "completed": result.status == "completed",
                "identity_every_request": bool(calls)
                and all(
                    c["messages"][1]["role"] == "system"
                    and "character_identity" in c["messages"][1]["text"]
                    for c in calls
                ),
                "no_appraisal_requests": len(calls) == result.rounds,
                "no_background_or_proactive_requests": True,
            }
            rows.append(
                {
                    "case": case.id,
                    "turn": index + 1,
                    "condition_hash": digest(conditions),
                    "expectation": case.expectation,
                    "checks": checks,
                    "status": result.status,
                    "character": compiler.diagnostics(),
                    "calls": calls,
                    "answer": result.text,
                }
            )
            # Teacher-forced history: candidate output never changes another variant's input.
            fixed_history.append(turn(index, prompt, "合成固定前文：按证据核验，未执行外部动作。"))
    total_usage = budget.report()
    return {
        "variant": variant,
        "model": model,
        "package_hash": package.fingerprint,
        "corpus_hash": digest([(c.id, c.prompts, c.expectation, c.retrieved) for c in CASES]),
        "history_mode": "teacher_forced_identical_across_variants",
        "contract_pass": bool(rows) and all(all(r["checks"].values()) for r in rows),
        "semantic_review_required": True,
        "axes": {axis: {"score": None, "rubric": rubric} for axis, rubric in RUBRIC.items()},
        "usage": {
            key: total_usage[key] - before_usage[key]
            for key in (
                "requests",
                "reserved_tokens",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
            )
        },
        "shared_budget": total_usage,
        "state_update_requests": 0,
        "state_mode": "controlled_fixture_not_live_state_generation",
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="P3 fixed synthetic character ablations")
    parser.add_argument("--variant", choices=["E0", "E1", "E2", "all"], default="all")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize paid generation")
    parser.add_argument("--cases", default="all")
    parser.add_argument("--output", type=Path, default=Path(".heybuddy/evaluations"))
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--max-tokens", type=int, default=2_000_000)
    args = parser.parse_args()
    selected = None if args.cases == "all" else set(args.cases.split(","))
    if selected is not None and not selected <= {c.id for c in CASES}:
        parser.error("Unknown case ID")

    async def run():
        folder = args.output / ("p3-" + uuid4().hex[:12])
        require_private_directory(folder)
        folder.mkdir(parents=True)
        budget = Budget(args.max_requests, args.max_tokens)
        variants = ["E0", "E1", "E2"] if args.variant == "all" else [args.variant]

        async def execute(provider, model="offline-script", window=131072):
            reports = []
            for variant in variants:
                report = await evaluate(
                    variant, provider, budget, case_ids=selected, context_window=window, model=model
                )
                (folder / f"{variant}.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                reports.append(report)
            print(
                json.dumps(
                    {
                        "output": str(folder.resolve()),
                        "live": args.live,
                        "contracts": {r["variant"]: r["contract_pass"] for r in reports},
                        "usage": budget.report(),
                        "semantic_scores": "unreviewed",
                    }
                )
            )
            return 0 if all(r["contract_pass"] for r in reports) else 1

        if not args.live:
            return await execute(OfflineProvider())
        # Credentials are loaded only in the explicitly paid branch, never serialized.
        from openai import AsyncOpenAI

        from personal_agent.config import load_settings
        from personal_agent.llm.openai_provider import OpenAIProvider

        settings = load_settings()
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url) if settings.base_url else None,
            max_retries=0,
            timeout=settings.request_timeout_seconds,
        ) as client:
            return await execute(
                OpenAIProvider(client, settings.model), settings.model, settings.context_window
            )

    try:
        return asyncio.run(run())
    except EvaluationLimit:
        print(json.dumps({"outcome": "evaluation_budget_exhausted"}))
        return 2
    except Exception as error:
        print(json.dumps({"outcome": "failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
