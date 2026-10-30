"""Isolated, bounded real/Fake Provider evaluations. No access to real conversation data."""

import hashlib
import json
import math
from pathlib import Path
from time import perf_counter

from personal_agent.automation.models import AutomationPolicy
from personal_agent.automation.service import MemoryAutomation
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.models import (
    AssistantMessage,
    RunResult,
    TextMessage,
    ToolCall,
    ToolMessage,
    ToolResult,
)
from personal_agent.evaluation.cases import Case
from personal_agent.evaluation.prompts import install, tool_variant
from personal_agent.evaluation.scoring import score
from personal_agent.llm.base import Provider, ProviderError, ReasoningEffort
from personal_agent.memory import index
from personal_agent.memory.store import MemoryFS
from personal_agent.memory.tools import register_memory_tools
from personal_agent.recall.service import Recall
from personal_agent.recall.tools import register_recall_tools
from personal_agent.reflection.models import ReflectionPolicy
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError


class EvaluationLimit(Exception):
    pass


class Budget:
    def __init__(
        self,
        requests: int = 256,
        tokens: int = 4_000_000,
        *,
        input_price: float | None = None,
        output_price: float | None = None,
        max_cost: float | None = None,
    ) -> None:
        if (
            requests < 1
            or tokens < 1
            or any(
                value is not None and (not math.isfinite(value) or value < 0)
                for value in (input_price, output_price, max_cost)
            )
        ):
            raise ValueError("Evaluation limits and supplied prices must be finite and nonnegative")
        if max_cost is not None and max_cost <= 0:
            raise ValueError("Cost cap must be positive")
        if max_cost is not None and (input_price is None or output_price is None):
            raise ValueError("A cost cap requires explicit reliable input/output prices")
        self.max_requests = requests
        self.max_tokens = tokens
        self.input_price = input_price
        self.output_price = output_price
        self.max_cost = max_cost
        self.requests = 0
        self.reserved_tokens = 0
        self.reserved_cost = 0.0
        self.exhausted = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.usage_complete = True

    def reserve(self, messages, tools, output_limit):
        estimated = estimate_request(messages, tools)
        total = estimated + (output_limit or 0)
        cost = (
            estimated * (self.input_price or 0) + (output_limit or 0) * (self.output_price or 0)
        ) / 1_000_000
        if (
            self.requests >= self.max_requests
            or self.reserved_tokens + total > self.max_tokens
            or (self.max_cost is not None and self.reserved_cost + cost > self.max_cost)
        ):
            self.exhausted = True
            raise EvaluationLimit("evaluation_budget_exhausted")
        self.requests += 1
        self.reserved_tokens += total
        self.reserved_cost += cost

    def observe(self, usage):
        if usage:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.reasoning_tokens += usage.reasoning_tokens or 0
        else:
            self.usage_complete = False

    def report(self):
        return dict(
            requests=self.requests,
            reserved_tokens=self.reserved_tokens,
            reserved_cost_usd=self.reserved_cost,
            exhausted=self.exhausted,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reasoning_tokens=self.reasoning_tokens,
            usage_complete=self.usage_complete,
            estimated_cost_usd=(
                (self.input_tokens * self.input_price + self.output_tokens * self.output_price)
                / 1_000_000
                if self.input_price is not None and self.output_price is not None
                else None
            ),
        )


class TraceProvider:
    def __init__(self, provider: Provider, budget: Budget, prompts: dict):
        self.provider = provider
        self.budget = budget
        self.prompts = prompts
        self.calls = []
        self.old_history_visible = False

    async def generate(
        self,
        messages,
        tools,
        *,
        max_output_tokens=None,
        reasoning_effort: ReasoningEffort | None = None,
    ):
        self.budget.reserve(messages, tools, max_output_tokens)
        prompt = messages[0].text
        stage = "main"
        if prompt == self.prompts["summary"]:
            stage = "compaction"
        elif prompt == self.prompts["worthiness"]:
            stage = "worthiness"
        else:
            for name in ("EXTRACT", "SELECT", "CONSOLIDATE"):
                if prompt == self.prompts["reflection"][name]:
                    stage = name.lower()
        self.old_history_visible |= any("OLD-CHAT-ONLY" in getattr(m, "text", "") for m in messages)
        record = {"stage": stage, "tool_definitions": [tool.name for tool in tools]}
        self.calls.append(record)
        started = perf_counter()
        usage = None
        try:
            response = await self.provider.generate(
                messages,
                tools,
                max_output_tokens=max_output_tokens,
                **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
            )
            usage = response.usage
            record.update(
                text=response.text, tool_calls=[call.model_dump() for call in response.tool_calls]
            )
            return response
        except ProviderError as error:
            record["error_code"] = error.code
            record["diagnostics"] = (
                error.diagnostics.model_dump(exclude_none=True) if error.diagnostics else None
            )
            usage = error.diagnostics.usage if error.diagnostics else None
            raise
        finally:
            self.budget.observe(usage)
            record["seconds"] = round(perf_counter() - started, 3)
            record["usage"] = usage.model_dump(exclude_none=True) if usage else None


def seed(db, conversation, user, assistant, *, failed_tool=False):
    run = db.begin_run(conversation, TextMessage(role="user", text=user))
    if failed_tool:
        db.append_message(
            run,
            AssistantMessage(
                tool_calls=[ToolCall(id="old-deploy", name="demo_status", arguments="{}")]
            ),
        )
        db.append_message(
            run,
            ToolMessage(
                result=ToolResult(
                    call_id="old-deploy",
                    name="demo_status",
                    ok=False,
                    error={
                        "code": "unavailable",
                        "message": "Deployment failed; no success established.",
                    },
                )
            ),
        )
    db.append_message(run, AssistantMessage(text=assistant))
    db.finish_run(run, RunResult(status="completed", text=assistant, rounds=1))


def files(memory):
    return {
        str(path.relative_to(memory.root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in memory.root.rglob("*.md")
    }


def make_case_directory(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=False)


async def evaluate_case(
    case: Case,
    folder: Path,
    provider: Provider,
    prompts: dict,
    budget: Budget,
    context_window: int = 1048576,
    reflection_policy: ReflectionPolicy | None = None,
    request_output_tokens: int = 4096,
) -> dict:
    make_case_directory(folder)
    install(prompts)
    trace = TraceProvider(provider, budget, prompts)
    started = perf_counter()
    with SQLiteStore(folder / "history.sqlite3") as db:
        db.initialize()
        policy = ContextBudget(
            context_window=context_window,
            reserved_output_tokens=request_output_tokens,
            page_runs=8,
            recent_runs=2,
        )
        sessions = SessionManager(db, budget=policy)
        memory = MemoryFS.for_agent(folder / "memory", "heybuddy")
        memory.initialize()
        sessions.memory = memory
        if case.topic:
            index.create(memory, case.path, case.topic)
        for user, assistant in case.history:
            seed(db, sessions.current.id, user, assistant)
        if case.kind == "cross_session":
            seed(db, sessions.current.id, "OLD-CHAT-ONLY 不得复制到新会话。", "历史示例。")
            sessions.new()
        if case.kind == "compaction":
            for n in range(16):
                user = (
                    "最初采用方案 A。"
                    if n == 0
                    else "release-check 发布验证失败，仍待人工验证，不要重试部署。"
                    if n == 1
                    else "明确纠正：废弃方案 A，当前采用方案 B，release-check 仍待人工验证。"
                    if n == 6
                    else f"背景事件 {n}，没有新的长期决策。"
                )
                seed(db, sessions.current.id, user, "已记录这一历史状态。", failed_tool=n == 1)
        before = files(memory)
        original = [
            record.content.model_dump_json() for record in db.load_messages(sessions.current.id)
        ]
        registry = create_builtin_registry()
        register_memory_tools(registry, memory)
        register_recall_tools(registry, Recall(db, "heybuddy"))
        if case.kind == "failure":

            async def unavailable(args):
                raise ToolExecutionError(
                    "unavailable", "Synthetic status unavailable; do not retry."
                )

            registry.register(
                Tool(
                    "demo_status",
                    "Query demo service status. On unavailable report the error; do not retry.",
                    ToolArguments,
                    unavailable,
                )
            )
        tool_variant(registry, prompts)
        context = sessions.context()
        context.system_prompt = prompts["main"]
        agent = Agent(trace, registry, context=context, max_rounds=5)
        reflection = ReflectionService(
            ReflectionRepository(db, "heybuddy"), memory, trace, policy, reflection_policy
        )
        automation = MemoryAutomation(
            reflection, AutomationPolicy(mode="suggest" if case.automation else "off")
        )
        result = await agent.run(case.user)
        auto = await automation.run(sessions.current.id, completed=result.status == "completed")
        records = db.load_messages(sessions.current.id)
        tools = []
        for record in records[len(original) :]:
            if isinstance(record.content, AssistantMessage):
                for call in record.content.tool_calls:
                    try:
                        arguments = json.loads(call.arguments)
                    except ValueError:
                        arguments = {}
                    tools.append({"name": call.name, "arguments": arguments})
        plan = reflection.review(sessions.current.id)
        items = []
        if plan:
            for item in plan.items:
                change = item.decision
                after = ""
                if change.action == "update":
                    current = memory.raw(
                        memory.path(change.path),
                        memory.max_bytes if "/" not in change.path else 65536,
                    ).decode()
                    after = current.replace(change.old_text, change.new_text, 1)
                elif change.action == "create":
                    after = change.new_text
                items.append(
                    {
                        "action": change.action,
                        "path": change.path,
                        "after": after,
                        "evidence_verified": bool(item.candidate.evidence),
                        "category": item.candidate.category,
                    }
                )
        summary = db.context_summary(sessions.current.id)
        observation = {
            "case": case.id,
            "split": case.split,
            "category": case.category,
            "run_status": result.status,
            "answer": result.text,
            "tools": tools,
            "items": items,
            "summary": summary.text if summary else "",
            "automation_outcome": auto["outcome"],
            "automation": auto,
            "memory_unchanged": files(memory) == before,
            "journal_preserved": [
                record.content.model_dump_json() for record in records[: len(original)]
            ]
            == original,
            "old_history_visible": trace.old_history_visible,
            "calls": trace.calls,
            "seconds": round(perf_counter() - started, 3),
        }
        observation["grading"] = score(case, observation)
    (folder / "result.json").write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return observation
