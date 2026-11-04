"""Deterministic offline harness probes; no live-model or remote-server mode."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from time import perf_counter

from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.execution import CURRENT_SCOPE
from personal_agent.core.models import ModelResponse, TokenUsage, ToolCall, ToolMessage
from personal_agent.harness import HarnessRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from personal_agent.tools.mcp import SchemaArguments
from personal_agent.tools.registry import Tool
from personal_agent.tools.workspace import WorkspaceWrite
from personal_agent.workflows.runtime import WorkflowDefinition, WorkflowStart, WorkflowStep


class ScriptedWorker:
    """Fixed behavior measures harness contracts, not language-model intelligence."""

    def __init__(self, variant: str) -> None:
        self.variant = variant
        self.requests = 0
        self.tool_calls = 0

    async def generate(self, messages, tools, **options):
        self.requests += 1
        if messages[0].text.startswith("Complete the supplied read-only subtask"):
            return ModelResponse(
                text="Source: source.md, fixed source material.",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        results = {m.result.name: m.result for m in messages if isinstance(m, ToolMessage)}
        names = {tool.name for tool in tools}
        call = None
        if self.variant == "planning" and "task_create" not in results:
            call = ("task_create", {"title": "Write evidence report"})
        elif self.variant == "planning" and "task_claim" not in results:
            call = ("task_claim", {"id": results["task_create"].data["id"]})
        elif self.variant == "skills_mcp" and "skill_load" not in results:
            call = ("skill_load", {"name": "report"})
        elif self.variant == "skills_mcp" and "mcp_fixture_search" not in results:
            call = ("mcp_fixture_search", {"query": "source"})
        elif self.variant == "workflow" and "delegate" not in results:
            call = ("delegate", {"task": "Inspect source.md", "sources": ["source.md"]})
        elif self.variant == "workflow" and "workflow_start" not in results:
            call = (
                "workflow_start",
                {
                    "name": "working_report",
                    "arguments": {"sources": ["source.md"], "output": "report.md"},
                },
            )
        elif self.variant != "workflow" and "workspace_write" not in results:
            call = ("workspace_write", {"path": "report.md", "content": "Source: source.md"})
        elif self.variant == "planning" and "task_complete" not in results:
            call = (
                "task_complete",
                {
                    "id": results["task_create"].data["id"],
                    "evidence_call_id": results["workspace_write"].call_id,
                },
            )
        if call:
            name, arguments = call
            if name not in names:
                raise ValueError("Fixed scenario tool is missing")
            self.tool_calls += 1
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=f"probe-{self.tool_calls}", name=name, arguments=json.dumps(arguments)
                    )
                ],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            text="Report prepared.", usage=TokenUsage(input_tokens=1, output_tokens=1)
        )


async def probe(variant: str, root: Path) -> dict:
    folder = root / variant
    folder.mkdir()
    skills = folder / "skills" / "report"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: report\ndescription: Prepare a source-linked report\n---\nInclude sources.\n",
        encoding="utf-8",
    )
    started = perf_counter()
    with SQLiteStore(folder / "journal.db") as store:
        store.initialize()
        sessions = SessionManager(store, budget=ContextBudget(context_window=131072))
        worker = ScriptedWorker(variant)
        agent = Agent(worker, create_builtin_registry(), sessions.context())
        settings = Settings(
            api_key="offline-placeholder",
            model="offline-scripted",
            context_window=131072,
            workspace_root=folder / "work",
            skills_root=folder / "skills",
        )
        runtime = HarnessRuntime(settings, sessions, agent)
        await runtime.workspace.write(
            WorkspaceWrite(path="source.md", content="Fixed source material.")
        )

        async def fixture(arguments):
            return {"matches": [{"source": "source.md", "text": "Fixed source material."}]}

        agent.tools.register(
            Tool(
                "mcp_fixture_search",
                "Offline MCP-schema fixture (no transport).",
                SchemaArguments(
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    }
                ),
                fixture,
            )
        )
        try:
            result = await runtime.run(
                "/goal prepare source-linked report | file:report.md", frozenset()
            )
            recovery = None
            if variant == "workflow":
                attempts = {"first": 0, "second": 0}

                async def first(arguments, outputs):
                    attempts["first"] += 1
                    return {"verified": True}

                async def second(arguments, outputs):
                    attempts["second"] += 1
                    if attempts["second"] == 1:
                        raise RuntimeError("synthetic interruption")
                    return {"verified": True}

                runtime.workflows.register(
                    WorkflowDefinition(
                        "recovery_probe",
                        "1",
                        (WorkflowStep("first", first), WorkflowStep("second", second, ("first",))),
                        {"type": "object"},
                        {"type": "object"},
                    )
                )
                token = CURRENT_SCOPE.set(runtime.scope())
                try:
                    stopped = await runtime.workflows.start(WorkflowStart(name="recovery_probe"))
                    resumed = await runtime.workflows.resume(stopped["id"])
                    recovery = {
                        "initial_status": stopped["status"],
                        "resumed_status": resumed["status"],
                        "attempts": attempts,
                        "completed_step_repeated": attempts["first"] != 1,
                    }
                finally:
                    CURRENT_SCOPE.reset(token)
            with store.reading() as db:
                rounds = db.execute(
                    "SELECT sum(input_tokens),sum(output_tokens) FROM model_rounds"
                ).fetchone()
            return dict(
                variant=variant,
                recovery=recovery,
                task_success=result.status == "completed",
                goal_verified=result.status == "completed",
                model_requests=worker.requests,
                tool_calls=worker.tool_calls,
                synthetic_input_tokens=rounds[0] or 0,
                synthetic_output_tokens=rounds[1] or 0,
                elapsed_ms=int((perf_counter() - started) * 1000),
                failures=0 if result.status == "completed" else 1,
                reserved_tokens=runtime.last_budget.get("reserved_tokens", 0),
                usage_kind="scripted_not_model_quality",
                mcp_transport="fixture_only",
            )
        finally:
            await runtime.close()


async def evaluate() -> dict:
    with tempfile.TemporaryDirectory(prefix="heybuddy-harness-") as directory:
        results = [
            await probe(variant, Path(directory))
            for variant in ("tools", "planning", "skills_mcp", "workflow")
        ]
    return dict(
        mode="offline", scenarios=results, passed=all(item["task_success"] for item in results)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = asyncio.run(evaluate())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
