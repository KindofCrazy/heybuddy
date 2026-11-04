import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from pydantic import Field

from personal_agent.core.execution import CURRENT_RUN, CURRENT_SCOPE
from personal_agent.planning.service import Identifier
from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError


class ExecutionStart(ToolArguments):
    operation: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)
    task_id: str | None = None


class ExecutionService:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.operations: dict[str, Callable[[dict], Awaitable[dict]]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.roots: dict[str, str] = {}
        self.scopes = {}
        self.semaphore = asyncio.Semaphore(2)
        self.closed = False
        self.presented: dict[str, set[str]] = {}

    def register_operation(self, name: str, operation) -> None:
        if name in self.operations:
            raise ValueError("Duplicate background operation")
        self.operations[name] = operation

    async def start(self, args: ExecutionStart) -> dict:
        if self.closed or args.operation not in self.operations:
            raise ToolExecutionError("operation_denied", "Background operation is not registered.")
        if len(self.tasks) >= 18:
            raise ToolExecutionError("execution_limit", "Background queue is full.")
        if args.task_id:
            self.repository.get(args.task_id, "task")
        item = self.repository.put(
            "execution",
            dict(
                operation=args.operation,
                arguments=args.arguments,
                task_id=args.task_id,
                status="queued",
                result=None,
                notified=False,
            ),
        )
        scope = self.repository.scope()
        goals = [g for g in self.repository.list("goal") if g["status"] == "active"]
        item.update(root_id=scope.root_id, goal_id=goals[-1]["id"] if goals else None)
        item = self.repository.put("execution", item, identifier=item["id"])
        self.roots[item["id"]] = scope.root_id
        self.scopes[item["id"]] = scope
        self.tasks[item["id"]] = asyncio.create_task(self._run(item), name="harness-background")
        return item

    async def _run(self, item: dict) -> None:
        CURRENT_RUN.set(None)
        CURRENT_SCOPE.set(replace(self.repository.scope(), background=True))
        try:
            scope = self.repository.scope()
            remaining = scope.budget.max_seconds - (time.monotonic() - scope.budget.started)
            async with asyncio.timeout(max(0, remaining)):
                async with self.semaphore:
                    item["status"] = "running"
                    item = self.repository.put("execution", item, identifier=item["id"])
                    result = await self.operations[item["operation"]](item["arguments"])
                    item.update(status="succeeded", result=result)
        except asyncio.CancelledError:
            item.update(status="cancelled", result=None)
            raise
        except Exception as exc:
            item.update(status="failed", result={"error_type": type(exc).__name__})
        finally:
            try:
                self.repository.put("execution", item, identifier=item["id"])
            finally:
                self.tasks.pop(item["id"], None)
                self.roots.pop(item["id"], None)
                self.scopes.pop(item["id"], None)

    async def cancel(self, args: Identifier) -> dict:
        item = self.repository.get(args.id, "execution")
        task = self.tasks.get(args.id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            item = self.repository.get(args.id, "execution")
            if item["status"] in {"queued", "running"}:
                item["status"] = "cancelled"
                item = self.repository.put("execution", item, identifier=args.id)
            self.tasks.pop(args.id, None)
            self.roots.pop(args.id, None)
            self.scopes.pop(args.id, None)
            return item
        return item

    async def cancel_root(self, root_id: str) -> None:
        targets = [key for key, root in self.roots.items() if root == root_id]
        for identifier in targets:
            scope = self.scopes.get(identifier)
            if scope is not None:
                token = CURRENT_SCOPE.set(scope)
                try:
                    await self.cancel(Identifier(id=identifier))
                finally:
                    CURRENT_SCOPE.reset(token)

    async def close(self) -> None:
        self.closed = True
        for identifier in list(self.tasks):
            scope = self.scopes.get(identifier)
            if scope is None:
                continue
            token = CURRENT_SCOPE.set(scope)
            try:
                await self.cancel(Identifier(id=identifier))
            finally:
                CURRENT_SCOPE.reset(token)

    def context(self) -> dict:
        result = dict(
            kind="background_results",
            trust="execution_data_only",
            results=[
                dict(
                    id=item["id"],
                    status=item["status"],
                    operation=item.get("operation"),
                    result_available=item.get("result") is not None,
                )
                for item in self.repository.notifications()
            ],
        )

        self.presented.setdefault(self.repository.scope().root_id, set()).update(
            item["id"] for item in result["results"]
        )
        return result

    def acknowledge(self) -> None:
        presented = self.presented.pop(self.repository.scope().root_id, set())
        for identifier in presented:
            item = self.repository.get(identifier, "execution")
            if not item["notified"]:
                item["notified"] = True
                self.repository.put("execution", item, identifier=item["id"])

    def register(self, registry) -> None:
        registry.register(
            Tool(
                "execution_start",
                "Start a registered read-only background operation.",
                ExecutionStart,
                self.start,
            )
        )
        for name in ("execution_status", "execution_result"):

            async def get(args):
                return self.repository.get(args.id, "execution")

            registry.register(Tool(name, "Read a source-scoped background job.", Identifier, get))
        registry.register(
            Tool("execution_cancel", "Cancel an existing background job.", Identifier, self.cancel)
        )
