"""Application-owned P5 composition; channels pass only trusted source identity."""

import json
import os
from pathlib import Path

from pydantic import Field

from personal_agent.core.execution import (
    CURRENT_SCOPE,
    CURRENT_THREAD,
    ROUTING_CLARIFY,
    ROUTING_MODEL_USED,
    ExecutionBudget,
    RunScope,
)
from personal_agent.core.replay import ReplayView
from personal_agent.execution.service import ExecutionService
from personal_agent.organization.router import ThreadRouter
from personal_agent.planning.service import PlanningService
from personal_agent.skills.loader import SkillLoader
from personal_agent.storage.harness import HarnessRepository
from personal_agent.storage.routing import RoutingRepository
from personal_agent.storage.thread_history import SQLiteThreadHistory
from personal_agent.subagents.service import SubagentService
from personal_agent.tools.mcp import MCPManager, MCPServerConfig
from personal_agent.tools.registry import Tool, ToolArguments
from personal_agent.tools.workspace import Workspace, WorkspaceSearch
from personal_agent.workflows.runtime import WorkflowRegistry, register_report


class OutputGet(ToolArguments):
    artifact_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16384, ge=1, le=16384)


class HarnessRuntime:
    def __init__(self, settings, sessions, agent, *, read_only: bool = False) -> None:
        self.sessions, self.agent = sessions, agent
        self.repository = HarnessRepository(sessions.store)
        self.read_only = read_only
        self.routing_enabled = settings.thread_routing_enabled
        self.routing_model_selection = settings.thread_router_model_selection
        self.current_router = None
        self.starting_goal_id = None
        self.last_budget = {}
        self.max_tool_calls = settings.max_tool_calls_per_run
        data_root = os.environ.get("LOCALAPPDATA")
        if data_root:
            base = Path(data_root) / "HeyBuddy"
        elif settings.workspace_root is not None and settings.skills_root is not None:
            base = settings.workspace_root.parent
        else:
            base = Path.home() / ".local/share/HeyBuddy"
        self.workspace = Workspace(
            settings.workspace_root or base / "workspace", git_root=settings.workspace_git_root
        )
        self.skills = SkillLoader(settings.skills_root or base / "skills")
        self.planning = PlanningService(self.repository)

        def file_check(path):
            from personal_agent.tools.registry import ToolExecutionError

            try:
                self.workspace.contents(self.workspace.path(path))
                return True
            except (ToolExecutionError, OSError):
                return False

        self.planning.checkers["file"] = file_check
        if settings.goal_model_review:
            self.planning.evaluator = agent.provider
        self.execution = ExecutionService(self.repository)
        self.subagents = SubagentService(
            self.repository, agent.provider, self.workspace, self.skills, settings.context_budget()
        )
        self.workflows = WorkflowRegistry(self.repository)
        register_report(self.workflows, self.workspace)
        configs = []
        if settings.mcp_config_path is not None:
            path = settings.mcp_config_path
            if path.stat().st_size > 65536:
                raise ValueError("MCP configuration exceeds budget")
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, list) or len(config) > 16:
                raise ValueError("MCP config requires at most 16 servers")
            names = set()
            for item in config:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("name"), str)
                    or item["name"] in names
                ):
                    raise ValueError("Invalid or duplicate MCP server")
                names.add(item["name"])
                for key in ("args", "allowed_tools", "env_names"):
                    if not isinstance(item.get(key, []), list) or any(
                        not isinstance(v, str) for v in item.get(key, [])
                    ):
                        raise ValueError("Invalid MCP server fields")
                configs.append(
                    MCPServerConfig(
                        name=item["name"],
                        command=item.get("command"),
                        args=tuple(item.get("args", [])),
                        url=item.get("url"),
                        allowed_tools=frozenset(item.get("allowed_tools", [])),
                        env={
                            name: os.environ[name]
                            for name in item.get("env_names", [])
                            if name in os.environ
                        },
                    )
                )
        self.mcp = MCPManager(configs)
        if not read_only:
            self.repository.recover()
            self.workspace.register(agent.tools)
            self.skills.register(agent.tools)
            self.planning.register(agent.tools)
            self.subagents.register(agent.tools)
            self.execution.register(agent.tools)
            self.workflows.register_tools(agent.tools)
            self.execution.register_operation("delegate", self.subagents.operation)

            async def search(arguments):
                return self.workspace.search(WorkspaceSearch.model_validate(arguments))

            self.execution.register_operation("workspace_search", search)

            async def output(args):
                return self.repository.artifact(args.artifact_id, args.offset, args.limit)

            agent.tools.register(
                Tool(
                    "tool_output_get",
                    "Read an archived tool result by source-scoped ID.",
                    OutputGet,
                    output,
                )
            )
            agent.goal_gate = self.planning
        self.agent.scope_factory = lambda permissions: self.scope("cli", permissions)
        self.bind_context()

    def routing(self):
        scope = CURRENT_SCOPE.get()
        return RoutingRepository(
            self.sessions.store,
            self.sessions.agent.id,
            self.sessions.current.id,
            scope.source if scope else "cli",
        )

    def bind_context(self) -> None:
        if self.routing_enabled:
            context = self.agent.context
            if not isinstance(context._store, SQLiteThreadHistory):
                context._store = SQLiteThreadHistory(self.sessions.store, self.sessions.current.id)

            async def prepare_scope(provider):
                repository = self.routing()
                router = ThreadRouter(repository, model_selection=self.routing_model_selection)
                thread = await router.route(
                    context.run_id, context.pending_messages[0].text, provider
                )
                context._store.thread_id, context._store.source = thread.id, repository.source
                CURRENT_THREAD.set(thread.id)
                ROUTING_MODEL_USED.set(bool(router.metrics.get("extra_requests")))
                ROUTING_CLARIFY.set(bool(router.metrics.get("ambiguous")))
                context._diagnostics["routing"] = {**router.metrics, "thread_id": thread.id}
                self.current_router = router
                if self.starting_goal_id:
                    goal = self.repository.get(self.starting_goal_id, "goal")
                    for old in self.repository.list("goal"):
                        if (
                            old["id"] != goal["id"]
                            and old["status"] == "active"
                            and old.get("thread_id") == thread.id
                        ):
                            old["status"] = "cancelled"
                            self.repository.put("goal", old, identifier=old["id"])
                    goal["thread_id"] = thread.id
                    self.repository.put("goal", goal, identifier=goal["id"])

            context.prepare_scope = prepare_scope
        self.agent.context.replay_factory = lambda: ReplayView(
            self.repository if CURRENT_SCOPE.get() else None
        )

        class ScopedContributor:
            def __init__(self, contributor):
                self.contributor = contributor

            def context(self):
                return self.contributor.context() if CURRENT_SCOPE.get() else None

        self.agent.context.contributors = [
            ScopedContributor(item) for item in (self.planning, self.skills, self.execution)
        ]

        def on_begin(run_id):
            scope = CURRENT_SCOPE.get()
            if scope is not None:
                self.repository.put("run_scope", dict(run_id=run_id, root_id=scope.root_id))

        self.agent.context.on_begin = on_begin
        if self.routing_enabled:

            class RoutingContext:
                def context(inner):
                    return (
                        self.current_router.context()
                        if self.current_router and CURRENT_SCOPE.get()
                        else None
                    )

            self.agent.context.contributors.append(RoutingContext())

    async def start(self) -> None:
        if not self.read_only:
            await self.mcp.start(self.agent.tools)

    async def close(self) -> None:
        await self.execution.close()
        await self.mcp.close()

    def scope(self, source: str = "cli", permissions=frozenset()) -> RunScope:
        return RunScope(
            self.sessions.agent.id,
            self.sessions.current.id,
            source,
            permissions,
            budget=ExecutionBudget(max_tool_calls=self.max_tool_calls),
        )

    def status(self) -> dict:
        return dict(
            planning=self.planning.context(),
            background=self.execution.context(),
            workflows=self.repository.list("workflow", 20),
            mcp=dict(self.mcp.status),
        )

    async def command(self, text: str, *, trusted: bool) -> dict:
        if self.routing_enabled:
            active = self.routing().active()
            CURRENT_THREAD.set(active.id if active else None)
            if text.startswith("/compact"):
                self.agent.context._store.thread_id = active.id if active else None
                self.agent.context._store.source = self.routing().source
        parts = text.split(maxsplit=2)
        if parts[0] == "/goal":
            if len(parts) == 1 or parts[1] == "status":
                return {"goals": self.repository.list("goal")}
            if parts[1] == "cancel":
                cancelled = []
                for goal in self.repository.list("goal"):
                    if goal["status"] == "active" and goal.get("thread_id") in {
                        None,
                        CURRENT_THREAD.get(),
                    }:
                        cancelled.append(goal["id"])
                        goal["status"] = "cancelled"
                        self.repository.put("goal", goal, identifier=goal["id"])
                from personal_agent.planning.service import Identifier

                for job in self.repository.list("execution"):
                    if job.get("goal_id") in cancelled:
                        await self.execution.cancel(Identifier(id=job["id"]))
                return {"status": "cancelled"}
            # /goal <condition> | <successful receipt IDs, comma-separated>
            condition, _, evidence = text.removeprefix("/goal").strip().partition("|")
            goal_thread = CURRENT_THREAD.set(None) if self.routing_enabled else None
            try:
                goal = self.planning.goal(
                    condition, [s.strip() for s in evidence.split(",") if s.strip()]
                )
            finally:
                if goal_thread is not None:
                    CURRENT_THREAD.reset(goal_thread)
            self.starting_goal_id = goal["id"]
            return goal
        if not trusted:
            return {"error": "host_only_command"}
        if parts[0] == "/harness":
            return self.status()
        if parts[0] == "/workflow" and len(parts) == 3 and parts[1] == "resume":
            return await self.workflows.resume(parts[2])
        if parts[0] == "/compact":
            from personal_agent.core.execution import BudgetProvider

            provider = BudgetProvider(self.agent.provider, self.repository.scope().budget)
            return await self.agent.context.compact(provider, text.removeprefix("/compact").strip())
        return {"error": "unknown_harness_command"}

    async def run(self, text, permissions, images=None, *, source="cli"):
        scope = self.scope(source, permissions)
        token = CURRENT_SCOPE.set(scope)
        thread_token = CURRENT_THREAD.set(None)
        self.starting_goal_id = None
        self.subagents.foreground.clear()
        try:
            self.bind_context()
            if text.split(maxsplit=1) and text.split(maxsplit=1)[0] in {
                "/goal",
                "/compact",
                "/harness",
                "/workflow",
            }:
                command_result = await self.command(text, trusted=source == "cli")
                if text.startswith("/goal ") and command_result.get("status") == "active":
                    text = command_result["condition"]
                else:
                    return command_result
            result = await self.agent.run(text, permissions=permissions, images=images, scope=scope)
            if result.status == "completed":
                self.execution.acknowledge()
            self.last_budget = scope.budget.report()
            self.repository.put("budget", dict(root_id=scope.root_id, **self.last_budget))
            result.execution_id = scope.root_id
            return result
        finally:
            self.subagents.foreground.set()
            self.starting_goal_id = None
            CURRENT_THREAD.reset(thread_token)
            CURRENT_SCOPE.reset(token)
