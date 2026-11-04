"""Offline P5 contracts, including interruption and real local MCP transports."""

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, ContextError
from personal_agent.core.context import ContextBuilder
from personal_agent.core.execution import CURRENT_RUN, CURRENT_SCOPE, ExecutionBudget, RunScope
from personal_agent.core.lifecycle import HookRegistry, LifecycleEvent
from personal_agent.core.models import (
    AssistantMessage,
    ModelResponse,
    TextMessage,
    ToolCall,
    ToolMessage,
    ToolResult,
)
from personal_agent.core.replay import ReplayView
from personal_agent.execution.service import ExecutionService, ExecutionStart
from personal_agent.llm.base import ProviderError
from personal_agent.planning.service import PlanningService, TaskCreate, TaskDependencies, TodoWrite
from personal_agent.skills.loader import SkillLoader, SkillName, SkillResource
from personal_agent.storage.harness import HarnessRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.subagents.service import Delegate, SubagentService
from personal_agent.tools.builtin import create_builtin_registry
from personal_agent.tools.mcp import MCPManager, MCPServerConfig, SchemaArguments
from personal_agent.tools.registry import ToolExecutionError, ToolRegistry
from personal_agent.tools.workspace import (
    CommandRun,
    Workspace,
    WorkspaceEdit,
    WorkspaceRead,
    WorkspaceWrite,
)
from personal_agent.workflows.runtime import (
    WorkflowDefinition,
    WorkflowRegistry,
    WorkflowStart,
    WorkflowStep,
    register_report,
)
from tests.fakes import FakeProvider, add_call
from tests.test_dashboard import observatory

__all__ = ["observatory"]


@pytest.fixture
def harness_state(tmp_path):
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        store.initialize()
        agent = store.create_agent("Test", "", agent_id="harness-test")
        conversation = store.create_conversation(agent.id)
        scope = RunScope(agent.id, conversation.id)
        token = CURRENT_SCOPE.set(scope)
        run_id = store.begin_run(conversation.id, TextMessage(role="user", text="Do the task"))
        run_token = CURRENT_RUN.set(run_id)
        try:
            yield HarnessRepository(store), scope
        finally:
            CURRENT_RUN.reset(run_token)
            CURRENT_SCOPE.reset(token)


async def test_hooks_cannot_bypass_permission_and_cancel_propagates():
    tools = create_builtin_registry()
    events = []

    async def deny(event):
        events.append(event.name)
        return "private reason"

    tools.hooks.register("PreToolUse", deny)
    result = await tools.execute(add_call("one"))
    assert result.error.code == "hook_denied"
    assert result.call_id == "one" and events == ["PreToolUse"]
    result = await tools.execute(ToolCall(id="two", name="unknown", arguments="{}"))
    assert result.error.code == "unknown_tool" and len(events) == 1

    async def cancel(event):
        raise asyncio.CancelledError

    hooks = HookRegistry()
    hooks.register("PreToolUse", cancel)
    with pytest.raises(asyncio.CancelledError):
        await hooks.emit(LifecycleEvent("PreToolUse"))


async def test_hook_failure_is_bounded_and_observers_do_not_change_results():
    hooks = HookRegistry(timeout=0.01, event_timeout=0.02)

    async def slow(event):
        await asyncio.Event().wait()

    hooks.register("PreToolUse", slow)
    assert await hooks.emit(LifecycleEvent("PreToolUse")) == "hook_timeout"
    hooks.register("Stop", slow)
    assert await hooks.emit(LifecycleEvent("Stop")) is None


async def test_post_tool_runs_after_journal_result():
    context = ContextBuilder()
    registry = create_builtin_registry()

    async def observe(event):
        assert context.pending_messages[-1].result.call_id == event.call_id

    registry.hooks.register("PostToolUse", observe)
    result = await Agent(
        FakeProvider(ModelResponse(tool_calls=[add_call("one")]), ModelResponse(text="5")),
        registry,
        context,
    ).run("sum")
    assert result.status == "completed"


def test_artifacts_are_bounded_and_source_scoped(harness_state):
    repository, scope = harness_state
    item = repository.save_artifact("large", b"x" * 1048580)
    assert not item["complete"] and item["size_bytes"] == 1048576
    assert repository.artifact(item["artifact_id"], 16384)["next_offset"] == 32768
    token = CURRENT_SCOPE.set(replace(scope, source="chat:other"))
    try:
        with pytest.raises(ToolExecutionError, match="unavailable"):
            repository.artifact(item["artifact_id"])
    finally:
        CURRENT_SCOPE.reset(token)


def test_micro_keeps_unconsumed_and_recent_results_and_original_journal(harness_state):
    repository, _ = harness_state
    replay = ReplayView(repository)
    messages = [TextMessage(role="user", text="original")]
    for i in range(6):
        messages.append(
            ToolMessage(
                result=ToolResult(
                    call_id=str(i), name="read", ok=True, data={"text": "x" * 1000, "exit_code": 0}
                )
            )
        )
    original = [message.model_dump_json() for message in messages]
    assert replay.project(messages, micro=True)[1].result.data["text"]
    replay.mark_consumed(messages[:-1])
    projected = replay.project(messages, micro=True)
    assert "artifact" in projected[1].result.data
    assert projected[1].result.data["exit_code"] == 0
    assert projected[-1].result.data["text"]
    assert [message.model_dump_json() for message in messages] == original


async def test_run_summary_retains_request_tail_and_persists_only_derived_state(harness_state):
    repository, _ = harness_state
    replay = ReplayView(repository)
    messages = [TextMessage(role="user", text="keep this exact request")]
    for i in range(5):
        messages += [
            AssistantMessage(tool_calls=[add_call(str(i))]),
            ToolMessage(
                result=ToolResult(call_id=str(i), name="add_numbers", ok=True, data={"sum": i})
            ),
        ]

    class Safe(FakeProvider):
        def safe_compaction(self, messages):
            return True

    provider = Safe(ModelResponse(text="Completed sums 0, 1, 2; latest work remains."))
    budget = ContextBudget(context_window=32000)
    assert await replay.compact_current(messages, provider, budget)
    view = replay.project(messages)
    assert view[0] == messages[0] and view[-4:] == messages[-4:]
    assert len(repository.list("run_summary")) == 1
    assert repository.store.context_summary(repository.scope().conversation_id) is None
    assert len(messages) == 11


async def test_opaque_unknown_provider_data_prevents_compaction(harness_state):
    from personal_agent.llm.openai_provider import OpenAIProvider

    messages = [AssistantMessage(provider_data={"unknown": [{"type": "reasoning"}]})]
    assert not OpenAIProvider.safe_compaction(messages)


async def test_reactive_retry_is_once_and_never_reexecutes_tools():
    provider = FakeProvider(
        ModelResponse(tool_calls=[add_call("one")]),
        ProviderError("too long", code="context_length_exceeded"),
        ModelResponse(text="5"),
    )
    result = await Agent(provider, create_builtin_registry()).run("sum")
    assert result.status == "completed" and len(provider.requests) == 3
    results = [m for m in provider.requests[-1] if isinstance(m, ToolMessage)]
    assert len(results) == 1
    provider = FakeProvider(
        ProviderError("too long", code="context_length_exceeded"),
        ProviderError("too long again", code="context_length_exceeded"),
    )
    result = await Agent(provider, create_builtin_registry()).run("sum")
    assert result.status == "provider_error" and len(provider.requests) == 2


def test_shared_budget_and_summary_alias():
    budget = ExecutionBudget(max_requests=1)
    budget.reserve([], [], 10)
    budget.observe(None)
    assert not budget.usage_complete
    with pytest.raises(ContextError):
        budget.reserve([], [], 10)
    assert ContextBudget(context_window=32000, summary_max_bytes=1000).summary_tokens == 1000
    with pytest.raises(ValueError):
        ContextBudget(context_window=32000, summary_tokens=1000, summary_max_bytes=2000)


async def test_workspace_exact_edits_and_path_policy(tmp_path):
    workspace = Workspace(tmp_path / "documents")
    first = await workspace.write(WorkspaceWrite(path="note.md", content="original"))
    with pytest.raises(ToolExecutionError):
        await workspace.write(WorkspaceWrite(path="note.md", content="overwrite"))
    edited = await workspace.write(
        WorkspaceEdit(
            path="note.md", old_text="original", new_text="changed", expected_hash=first["hash"]
        )
    )
    assert workspace.read(WorkspaceRead(path="note.md"))["hash"] == edited["hash"]
    for path in (
        "../private.txt",
        "C:/secret.txt",
        ".env",
        "skills/SKILL.md",
        "file.py",
        "NUL.txt",
        "secret.txt:stream",
    ):
        with pytest.raises(ToolExecutionError):
            workspace.path(path)
    with pytest.raises(ToolExecutionError):
        await workspace.command(CommandRun(command_id="git_status"))


def test_task_graph_claim_verification_and_restart(harness_state):
    repository, _ = harness_state
    planning = PlanningService(repository)
    a = planning.create(TaskCreate(title="first"))
    b = planning.create(TaskCreate(title="second"))
    planning.dependencies(TaskDependencies(id=b["id"], dependencies=[a["id"]]))
    with pytest.raises(ToolExecutionError):
        planning.dependencies(TaskDependencies(id=a["id"], dependencies=[b["id"]]))
    with pytest.raises(ToolExecutionError):
        planning.transition(b["id"], "in_progress")
    planning.transition(a["id"], "in_progress")
    with pytest.raises(ToolExecutionError):
        planning.transition(a["id"], "completed", "invented")
    repository.recover()
    assert repository.get(a["id"], "task")["status"] == "interrupted"
    assert repository.get(b["id"], "task")["status"] == "pending"
    with pytest.raises(ValueError):
        TodoWrite(
            items=[{"text": "a", "status": "in_progress"}, {"text": "b", "status": "in_progress"}]
        )


async def test_goal_does_not_accept_worker_claim(harness_state):
    repository, _ = harness_state
    service = PlanningService(repository)
    goal = service.goal("produce report", ["not-real"])
    assert await service.evaluate("all done", ContextBuilder()) == "continue"
    assert repository.get(goal["id"], "goal")["status"] == "active"


def test_skill_loading_does_not_grant_execution(tmp_path):
    root = tmp_path / "skills"
    path = root / "report"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: report\ndescription: Write reports\n---\nUse sources.\n", encoding="utf-8"
    )
    loader = SkillLoader(root)
    assert "Use sources" not in json.dumps(loader.context())
    assert "Use sources" in loader.load(SkillName(name="report"))["text"]
    with pytest.raises(ToolExecutionError):
        loader.resource(SkillResource(name="report", path="../private.txt"))


def test_mcp_preserves_schema_and_disallows_external_resolution():
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    args = SchemaArguments(schema)
    assert args.model_json_schema() == schema
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError):
        args.model_validate_json('{"x":"1"}')
    with pytest.raises(ValueError):
        SchemaArguments({"$ref": "https://example.invalid/schema"})


async def test_real_mcp_stdio_transport_and_permission_filter(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text(
        'from mcp.server.mcpserver import MCPServer\ns=MCPServer("test")\n'
        "@s.tool()\ndef add(x: int, y: int) -> int:\n return x+y\n"
        '@s.tool()\ndef secret() -> str:\n return "hidden"\ns.run()\n',
        encoding="utf-8",
    )
    registry = ToolRegistry()
    manager = MCPManager(
        [
            MCPServerConfig(
                "test",
                command=sys.executable,
                args=("-u", str(server)),
                allowed_tools=frozenset({"add"}),
                env={key: os.environ[key] for key in ("SystemRoot", "PATH") if key in os.environ},
            )
        ]
    )
    try:
        await manager.start(registry)
        assert manager.status == {"test": "ready"}
        definitions = registry.definitions()
        assert len(definitions) == 1 and "secret" not in definitions[0].name
        result = await registry.execute(
            ToolCall(id="mcp-one", name=definitions[0].name, arguments='{"x":2,"y":3}')
        )
        assert result.ok and "5" in str(result.data)
        invalid = await registry.execute(
            ToolCall(id="mcp-two", name=definitions[0].name, arguments='{"x":"2","y":3}')
        )
        assert invalid.error.code == "invalid_arguments"
    finally:
        await manager.close()


async def test_background_cancel_and_no_restart(harness_state):
    repository, _ = harness_state
    service = ExecutionService(repository)
    entered = asyncio.Event()

    async def wait(args):
        entered.set()
        await asyncio.Event().wait()

    service.register_operation("wait", wait)
    item = await service.start(ExecutionStart(operation="wait"))
    await entered.wait()
    from personal_agent.planning.service import Identifier

    result = await service.cancel(Identifier(id=item["id"]))
    assert result["status"] == "cancelled"
    repository.recover()
    assert not service.tasks
    await service.close()


async def test_subagent_isolated_from_parent_recall_and_budget(harness_state, tmp_path):
    repository, scope = harness_state
    provider = FakeProvider(ModelResponse(text="Read-only finding"))
    service = SubagentService(
        repository,
        provider,
        Workspace(tmp_path / "work"),
        SkillLoader(tmp_path / "skills"),
        ContextBudget(context_window=64000),
    )
    result = await service.delegate(Delegate(task="inspect supplied evidence"))
    assert result["status"] == "completed"
    conversation = repository.store.get_conversation(result["conversation_id"])
    assert conversation.agent_id != scope.agent_id
    assert scope.budget.requests == 1
    assert CURRENT_SCOPE.get() == scope


async def test_workflow_report_and_explicit_safe_resume(harness_state, tmp_path):
    repository, _ = harness_state
    workspace = Workspace(tmp_path / "work")
    await workspace.write(WorkspaceWrite(path="input.md", content="Verified source material"))
    workflows = WorkflowRegistry(repository)
    register_report(workflows, workspace)
    result = await workflows.start(
        WorkflowStart(
            name="working_report", arguments={"sources": ["input.md"], "output": "report.md"}
        )
    )
    assert result["status"] == "completed"
    assert result["result"]["sources"] == ["input.md"]
    attempts = []

    async def first(args, outputs):
        attempts.append("first")
        return {"value": 1}

    async def second(args, outputs):
        attempts.append("second")
        if attempts.count("second") == 1:
            raise RuntimeError("interruption")
        return {"value": 2}

    workflows.register(
        WorkflowDefinition(
            "resume",
            "1",
            (WorkflowStep("first", first), WorkflowStep("second", second, ("first",))),
            {"type": "object"},
            {"type": "object"},
        )
    )
    stopped = await workflows.start(WorkflowStart(name="resume"))
    assert stopped["status"] == "blocked"
    resumed = await workflows.resume(stopped["id"])
    assert resumed["status"] == "completed"
    assert attempts == ["first", "second", "second"]


async def test_real_mcp_loopback_http_transport():
    import socket

    import uvicorn
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("loopback")

    @server.tool()
    def echo(text: str) -> str:
        return text

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen()
    application = server.streamable_http_app()
    ready = asyncio.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    http = ReadyServer(uvicorn.Config(application, log_level="error", lifespan="on"))
    task = asyncio.create_task(http.serve(sockets=[listener]))
    manager = MCPManager(
        [
            MCPServerConfig(
                "http", url=f"http://127.0.0.1:{port}/mcp", allowed_tools=frozenset({"echo"})
            )
        ]
    )
    try:
        await asyncio.wait_for(ready.wait(), 5)
        registry = ToolRegistry()
        await manager.start(registry)
        assert manager.status["http"] == "ready"
        result = await registry.execute(
            ToolCall(
                id="http", name=registry.definitions()[0].name, arguments='{"text":"local only"}'
            )
        )
        assert result.ok and "local only" in str(result.data)
    finally:
        await manager.close()
        http.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def test_workspace_command_owns_process_tree(tmp_path):
    import subprocess

    await asyncio.to_thread(
        subprocess.run, ["git", "init", "-q", str(tmp_path / "repo")], check=True
    )
    workspace = Workspace(tmp_path / "work", git_root=tmp_path / "repo")
    result = await workspace.command(CommandRun(command_id="git_status"))
    assert result["exit_code"] == 0 and not result["truncated"]


async def test_failed_artifact_storage_does_not_return_false_reference(harness_state, monkeypatch):
    repository, _ = harness_state
    replay = ReplayView(repository)

    def fail(*args):
        from personal_agent.core.history import HistoryError

        raise HistoryError("synthetic failure")

    monkeypatch.setattr(repository, "save_artifact", fail)
    messages = [
        TextMessage(role="user", text="read"),
        ToolMessage(result=ToolResult(call_id="large", name="read", ok=True, data="x" * 20000)),
    ]
    from personal_agent.core.history import HistoryError

    with pytest.raises(HistoryError):
        replay.project(messages)
    assert replay.artifacts == {} and len(messages[-1].result.data) == 20000


async def test_workflow_unknown_side_effect_never_reexecutes(harness_state):
    repository, _ = harness_state
    workflows = WorkflowRegistry(repository)
    calls = []

    async def uncertain(args, outputs):
        calls.append("effect")
        raise RuntimeError("result unknown")

    workflows.register(
        WorkflowDefinition(
            "unsafe",
            "1",
            (WorkflowStep("effect", uncertain, recovery="unsafe"),),
            {"type": "object"},
            {"type": "object"},
        )
    )
    item = await workflows.start(WorkflowStart(name="unsafe"))
    assert item["status"] == "blocked"
    resumed = await workflows.resume(item["id"])
    assert resumed["status"] == "blocked" and calls == ["effect"]


async def test_background_notification_only_acknowledges_presented_result(harness_state):
    repository, _ = harness_state
    service = ExecutionService(repository)
    first = repository.put("execution", dict(status="succeeded", notified=False, result={"x": 1}))
    assert service.context()["results"][0]["id"] == first["id"]
    second = repository.put("execution", dict(status="succeeded", notified=False, result={"x": 2}))
    service.acknowledge()
    assert repository.get(first["id"], "execution")["notified"]
    assert not repository.get(second["id"], "execution")["notified"]


async def test_shared_runtime_goal_file_check_and_channel_isolation(tmp_path):
    from personal_agent.config import Settings
    from personal_agent.harness import HarnessRuntime
    from personal_agent.sessions import SessionManager

    with SQLiteStore(tmp_path / "integrated.db") as store:
        store.initialize()
        sessions = SessionManager(store, budget=ContextBudget(context_window=131072))
        provider = FakeProvider(
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write-report",
                        name="workspace_write",
                        arguments='{"path":"report.md","content":"report"}',
                    )
                ]
            ),
            ModelResponse(text="Report created"),
        )
        agent = Agent(provider, create_builtin_registry(), sessions.context())
        settings = Settings(
            api_key="fake",
            model="fake",
            context_window=131072,
            workspace_root=tmp_path / "work",
            skills_root=tmp_path / "skills",
        )
        runtime = HarnessRuntime(settings, sessions, agent)
        result = await runtime.run(
            "/goal produce report | file:report.md", frozenset(), source="chat:one"
        )
        assert result.status == "completed" and len(provider.requests) == 2
        first = await runtime.run("/goal", frozenset(), source="chat:one")
        second = await runtime.run("/goal", frozenset(), source="chat:two")
        assert first["goals"][0]["status"] == "completed" and second["goals"] == []
        await runtime.close()


async def test_immediate_background_cancellation_persists_terminal_state(harness_state):
    repository, _ = harness_state
    service = ExecutionService(repository)

    async def operation(args):
        pytest.fail("Cancelled queued operation must not start")

    service.register_operation("queued", operation)
    item = await service.start(ExecutionStart(operation="queued"))
    from personal_agent.planning.service import Identifier

    result = await service.cancel(Identifier(id=item["id"]))
    assert result["status"] == "cancelled" and not service.tasks
    await service.close()


def test_task_success_receipts_are_source_scoped(harness_state):
    repository, scope = harness_state
    run_id = CURRENT_RUN.get()
    repository.put("run_scope", {"run_id": run_id})
    repository.store.append_message(run_id, AssistantMessage(tool_calls=[add_call("verified")]))
    repository.store.append_message(
        run_id,
        ToolMessage(
            result=ToolResult(call_id="verified", name="add_numbers", ok=True, data={"sum": 5})
        ),
    )
    planning = PlanningService(repository)
    assert planning.receipt("verified")
    token = CURRENT_SCOPE.set(replace(scope, source="another-chat"))
    try:
        assert not planning.receipt("verified")
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text=""),
        ModelResponse(text="x" * 3000),
        ModelResponse(text="unsafe", tool_calls=[add_call("unwanted")]),
        ProviderError("private message"),
        asyncio.CancelledError(),
    ],
)
async def test_failed_run_summary_never_advances_checkpoint(harness_state, response):
    repository, _ = harness_state
    replay = ReplayView(repository)
    messages = [TextMessage(role="user", text="original")]
    for i in range(4):
        messages.extend(
            [
                AssistantMessage(tool_calls=[add_call(str(i))]),
                ToolMessage(
                    result=ToolResult(call_id=str(i), name="add_numbers", ok=True, data={"sum": 5})
                ),
            ]
        )

    class Safe(FakeProvider):
        def safe_compaction(self, messages):
            return True

        async def generate(self, *args, **options):
            if isinstance(self.responses[0], BaseException):
                raise self.responses.pop(0)
            return await super().generate(*args, **options)

    expected = (
        asyncio.CancelledError if isinstance(response, asyncio.CancelledError) else ContextError
    )
    with pytest.raises(expected):
        await replay.compact_current(messages, Safe(response), ContextBudget(context_window=32000))
    assert replay.through == 0 and not replay.summary
    assert repository.list("run_summary") == []


async def test_harness_observatory_is_read_only(observatory):
    client, store, service, provider, _ = observatory
    before = store._connection.total_changes
    response = await client.get("/api/harness")
    assert response.status_code == 200 and response.json()["read_only"]
    assert not provider.requests and store._connection.total_changes == before


async def test_utf8_pagination_is_lossless(harness_state, tmp_path):
    repository, _ = harness_state
    text = "资料报告😀后续"
    item = repository.save_artifact("unicode", text.encode())
    parts, offset = [], 0
    while offset is not None:
        page = repository.artifact(item["artifact_id"], offset, 5)
        parts.append(page["text"])
        offset = page["next_offset"]
    assert "".join(parts) == text

    workspace = Workspace(tmp_path / "work")
    await workspace.write(WorkspaceWrite(path="unicode.md", content=text))
    parts, offset = [], 0
    while offset is not None:
        page = workspace.read(WorkspaceRead(path="unicode.md", offset=offset, limit=5))
        parts.append(page["text"])
        offset = page["next_offset"]
    assert "".join(parts) == text


async def test_shared_tool_budget_stops_after_recording_batch_rejections(harness_state):
    _, scope = harness_state
    scope.budget.max_tool_calls = 1
    context = ContextBuilder()
    provider = FakeProvider(ModelResponse(tool_calls=[add_call("allowed"), add_call("denied")]))
    result = await Agent(provider, create_builtin_registry(), context).run("sum twice")
    assert result.status == "budget_exhausted" and len(provider.requests) == 1
    receipts = [m.result for m in context.history if isinstance(m, ToolMessage)]
    assert receipts[0].ok and receipts[1].error.code == "execution_budget"


def test_notifications_do_not_starve_older_completed_jobs(harness_state):
    repository, _ = harness_state
    service = ExecutionService(repository)
    for number in range(20):
        repository.put(
            "execution", dict(status="succeeded", notified=False, result={"number": number})
        )
    assert len(service.context()["results"]) == 16
    service.acknowledge()
    assert len(service.context()["results"]) == 4


def test_invalid_goal_does_not_replace_active_goal(harness_state):
    repository, _ = harness_state
    service = PlanningService(repository)
    goal = service.goal("retain this goal")
    with pytest.raises(ToolExecutionError):
        service.goal("")
    assert repository.get(goal["id"], "goal")["status"] == "active"
