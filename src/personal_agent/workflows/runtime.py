import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import Field

from personal_agent.planning.service import Identifier
from personal_agent.tools.mcp import reject_remote_refs
from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError

Operation = Callable[[dict, dict], Awaitable[dict]]


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    operation: Operation
    dependencies: tuple[str, ...] = ()
    recovery: Literal["readonly", "idempotent", "unsafe"] = "readonly"
    reconcile: Operation | None = None
    timeout: float = 30
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: str
    steps: tuple[WorkflowStep, ...]
    input_schema: dict
    output_schema: dict


class WorkflowStart(ToolArguments):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)


class WorkflowRegistry:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.definitions: dict[str, WorkflowDefinition] = {}
        self.running: dict[str, asyncio.Task] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        if definition.name in self.definitions or not 1 <= len(definition.steps) <= 100:
            raise ValueError("Invalid/duplicate workflow")
        for schema in (definition.input_schema, definition.output_schema):
            reject_remote_refs(schema)
            Draft202012Validator.check_schema(schema)
        names = {step.name for step in definition.steps}
        if len(names) != len(definition.steps):
            raise ValueError("Duplicate workflow step")
        done = set()
        while len(done) < len(names):
            ready = {s.name for s in definition.steps if set(s.dependencies) <= done}
            if not ready - done:
                raise ValueError("Workflow dependencies are cyclic or missing")
            done |= ready
        self.definitions[definition.name] = definition

    async def start(self, args: WorkflowStart) -> dict:
        definition = self.definitions.get(args.name)
        if definition is None:
            raise ToolExecutionError("workflow_not_found", "Workflow is not registered.")
        try:
            Draft202012Validator(definition.input_schema).validate(args.arguments)
        except Exception:
            raise ToolExecutionError(
                "invalid_arguments", "Workflow input schema rejected arguments."
            ) from None
        item = self.repository.put(
            "workflow",
            dict(
                name=definition.name,
                version=definition.version,
                arguments=args.arguments,
                input_hash=self.digest(args.arguments),
                status="running",
                steps={},
                result=None,
            ),
        )
        return await self._run(item, definition)

    @staticmethod
    def digest(value) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    async def resume(self, identifier: str) -> dict:
        item = self.repository.get(identifier, "workflow")
        definition = self.definitions.get(item["name"])
        if (
            definition is None
            or definition.version != item["version"]
            or self.digest(item["arguments"]) != item["input_hash"]
        ):
            raise ToolExecutionError("workflow_conflict", "Workflow version or input changed.")
        if identifier in self.running or item["status"] == "completed":
            raise ToolExecutionError("invalid_state", "Workflow is active or already completed.")
        return await self._run(item, definition)

    async def _run(self, item: dict, definition: WorkflowDefinition) -> dict:
        self.running[item["id"]] = asyncio.current_task()
        outputs = {
            name: state["result"]
            for name, state in item["steps"].items()
            if state["status"] == "completed"
        }
        lock = asyncio.Lock()

        async def save():
            nonlocal item
            item = self.repository.put("workflow", item, identifier=item["id"])

        async def execute(step):
            if not step.permissions <= self.repository.scope().permissions:
                raise ToolExecutionError(
                    "permission_denied", "Workflow step requires current Host permissions."
                )
            previous = item["steps"].get(step.name)
            if previous and previous["status"] != "completed" and step.recovery != "readonly":
                if step.reconcile is None:
                    raise ToolExecutionError(
                        "workflow_blocked", "Unknown side effect requires reconciliation."
                    )
                reconciled = await step.reconcile(item["arguments"], outputs)
                if reconciled.get("status") == "completed":
                    outputs[step.name] = reconciled["result"]
                    item["steps"][step.name] = dict(status="completed", result=reconciled["result"])
                    async with lock:
                        await save()
                    return
                if reconciled.get("status") != "not_started" or step.recovery == "unsafe":
                    raise ToolExecutionError(
                        "workflow_blocked", "Side-effect outcome remains unknown."
                    )
            item["steps"][step.name] = dict(
                status="running", attempt=(previous or {}).get("attempt", 0) + 1
            )
            async with lock:
                await save()
            async with asyncio.timeout(step.timeout):
                result = await step.operation(item["arguments"], dict(outputs))
            # Prove serializability before storing a successful checkpoint.
            json.dumps(result, allow_nan=False)
            outputs[step.name] = result
            item["steps"][step.name].update(status="completed", result=result)
            async with lock:
                await save()

        try:
            item["status"] = "running"
            await save()
            while len(outputs) < len(definition.steps):
                ready = [
                    s
                    for s in definition.steps
                    if s.name not in outputs and set(s.dependencies) <= outputs.keys()
                ]
                # Two steps per wave; TaskGroup joins cancellation before persisting terminal state.
                async with asyncio.TaskGroup() as group:
                    for step in ready[:2]:
                        group.create_task(execute(step))
            result = outputs[definition.steps[-1].name]
            Draft202012Validator(definition.output_schema).validate(result)
            item.update(status="completed", result=result)
        except asyncio.CancelledError:
            item["status"] = "interrupted"
            raise
        except Exception:
            item["status"] = "blocked"
        finally:
            try:
                await save()
            finally:
                self.running.pop(item["id"], None)
        return item

    async def cancel(self, args: Identifier) -> dict:
        item = self.repository.get(args.id, "workflow")
        task = self.running.get(args.id)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.repository.get(item["id"], "workflow")

    def register_tools(self, registry) -> None:
        async def invoke(args):
            item = await self.start(args)
            if item["status"] != "completed":
                raise ToolExecutionError("workflow_blocked", "Workflow is not complete.", data=item)
            return item

        registry.register(
            Tool(
                "workflow_start",
                "Execute a fixed Host-registered workflow.",
                WorkflowStart,
                invoke,
                timeout_seconds=180,
            )
        )

        async def status(args):
            return self.repository.get(args.id, "workflow")

        registry.register(Tool("workflow_status", "Read workflow progress.", Identifier, status))
        registry.register(
            Tool("workflow_cancel", "Cancel workflow execution.", Identifier, self.cancel)
        )


def register_report(registry: WorkflowRegistry, workspace) -> None:
    from personal_agent.tools.workspace import WorkspaceRead, WorkspaceWrite

    async def read(args, outputs):
        return {
            path: workspace.read(WorkspaceRead(path=path, limit=16384)) for path in args["sources"]
        }

    async def topics(args, outputs):
        # Deterministic extractive first workflow: no hidden model calls or invented facts.
        return {
            "sections": [
                {"source": path, "excerpt": value["text"][:2000], "hash": value["hash"]}
                for path, value in outputs["read"].items()
            ]
        }

    async def topics_a(args, outputs):
        value = await topics(args, outputs)
        return {"sections": value["sections"][::2]}

    async def topics_b(args, outputs):
        value = await topics(args, outputs)
        return {"sections": value["sections"][1::2]}

    async def merge(args, outputs):
        return {"sections": [*outputs["topics_a"]["sections"], *outputs["topics_b"]["sections"]]}

    async def report(args, outputs):
        content = "# Working report\n\n" + "\n\n".join(
            f"## {item['source']}\n\n{item['excerpt']}\n\nSource hash: {item['hash']}"
            for item in outputs["topics"]["sections"]
        )
        return await workspace.write(WorkspaceWrite(path=args["output"], content=content))

    async def reconcile(args, outputs):
        try:
            current = workspace.read(WorkspaceRead(path=args["output"]))
        except ToolExecutionError:
            return {"status": "not_started"}
        expected_content = "# Working report\n\n" + "\n\n".join(
            f"## {item['source']}\n\n{item['excerpt']}\n\nSource hash: {item['hash']}"
            for item in outputs["topics"]["sections"]
        )
        if current["hash"] == hashlib.sha256(expected_content.encode()).hexdigest():
            return {"status": "completed", "result": current}
        return {"status": "unknown"}

    async def verify(args, outputs):
        report = workspace.read(WorkspaceRead(path=args["output"]))
        if report["hash"] != outputs["report"]["hash"]:
            raise ToolExecutionError("report_changed", "Report changed before validation.")
        return dict(path=args["output"], hash=report["hash"], sources=list(outputs["read"]))

    registry.register(
        WorkflowDefinition(
            "working_report",
            "1",
            (
                WorkflowStep("read", read),
                WorkflowStep("topics_a", topics_a, ("read",)),
                WorkflowStep("topics_b", topics_b, ("read",)),
                WorkflowStep("topics", merge, ("topics_a", "topics_b")),
                WorkflowStep("report", report, ("topics",), "idempotent", reconcile),
                WorkflowStep("verify", verify, ("report",)),
            ),
            {
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "items": {"type": "string"},
                    },
                    "output": {"type": "string"},
                },
                "required": ["sources", "output"],
                "additionalProperties": False,
            },
            {"type": "object", "required": ["path", "hash", "sources"]},
        )
    )
