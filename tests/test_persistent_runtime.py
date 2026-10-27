import asyncio
import json
import sqlite3
import subprocess
import sys

import httpx
import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.history import HistoryError
from personal_agent.core.models import (
    AssistantMessage,
    ModelResponse,
    TextMessage,
    ToolCall,
    ToolMessage,
)
from personal_agent.llm.base import ProviderError
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.main import chat
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from personal_agent.tools.registry import Tool, ToolArguments
from tests.fakes import FakeProvider, add_call
from tests.test_provider import call_item, make_client, response_body, text_item


def start(store):
    store.initialize()
    store.recover_incomplete_runs()
    return SessionManager(store)


def runtime(provider, sessions, tools=None, **kwargs):
    return Agent(provider, tools or create_builtin_registry(), context=sessions.context(), **kwargs)


async def test_restart_restores_previous_topic_and_new_conversation_is_empty(tmp_path):
    path = tmp_path / "history.sqlite3"
    topic = "我正在开发 HeyBuddy，准备参考 Letta Code 设计长期记忆。"
    with SQLiteStore(path) as store:
        sessions = start(store)
        first_id = sessions.current.id
        agent_id = sessions.agent.id
        await runtime(FakeProvider(ModelResponse(text="先分离会话历史与长期记忆。")), sessions).run(
            topic
        )
    for restart in range(3):
        with SQLiteStore(path) as store:
            sessions = start(store)
            assert sessions.current.id == first_id
            assert sessions.agent.id == agent_id
            provider = FakeProvider(
                ModelResponse(text="之前准备参考 Letta Code，分离历史与长期记忆。")
            )
            result = await runtime(provider, sessions).run("我们之前准备怎样实现 Memory？")
            assert result.status == "completed"
            assert provider.requests[0][2].text == topic
            assert len(store.list_runs(first_id)) == restart + 2
    with SQLiteStore(path) as store:
        sessions = start(store)
        sessions.new()
        new_id = sessions.current.id
        provider = FakeProvider(ModelResponse(text="这是新会话。"))
        await runtime(provider, sessions).run("hello")
        assert len(provider.requests[0]) == 3
        assert len(store.load_messages(first_id)) == 8
        assert len(store.load_messages(new_id)) == 2
    with SQLiteStore(path) as store:
        sessions = start(store)
        assert sessions.current.id == new_id
        sessions.resume(first_id)
        assert sessions.context().history[0].text == topic


async def test_complete_provider_items_replay_through_sdk_after_restart(tmp_path):
    path = tmp_path / "provider.sqlite3"
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque-test-reasoning",
    }
    with SQLiteStore(path) as store:
        sessions = start(store)
        provider = FakeProvider(
            ModelResponse(
                tool_calls=[add_call("call_1")], provider_data={"openai": [reasoning, call_item()]}
            ),
            ModelResponse(text="5", provider_data={"openai": [text_item("5")]}),
        )
        agent = runtime(provider, sessions)
        await agent.run("sum")
        expected = OpenAIProvider._input(agent.context.build("continue"))
        conversation_id = sessions.current.id
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body([text_item("remembered")]))

    with SQLiteStore(path) as store:
        sessions = start(store)
        assert sessions.current.id == conversation_id
        async with make_client(handle) as client:
            result = await runtime(OpenAIProvider(client, "test-model"), sessions).run("continue")
        assert result.text == "remembered"
        assert requests[0]["input"][:1] + requests[0]["input"][2:] == expected
        assert reasoning in requests[0]["input"]
        outputs = [
            item for item in requests[0]["input"] if item.get("type") == "function_call_output"
        ]
        assert outputs[0]["call_id"] == "call_1"
        assert json.loads(outputs[0]["output"])["data"] == {"sum": 5.0}


async def test_pending_history_is_distinct_from_replayable_and_full_history(tmp_path):
    with SQLiteStore(tmp_path / "pending.sqlite3") as store:
        sessions = start(store)
        context = sessions.context()
        context.begin("input")
        assert context.history == []
        assert context.pending_messages == [TextMessage(role="user", text="input")]
        snapshot = context.pending_messages
        snapshot[0].text = "mutated"
        assert context.pending_messages[0].text == "input"
        assert len(store.load_messages(sessions.current.id)) == 1
        assert store.load_messages(sessions.current.id, replayable_only=True) == []
        assert store.list_runs(sessions.current.id)[0].status == "running"
        context.abort(cancelled=True)
        assert context.pending_messages == []
        assert store.list_runs(sessions.current.id)[0].status == "cancelled"


async def test_cancel_mid_batch_preserves_partial_journal_without_retry(tmp_path):
    effects = []
    blocked = asyncio.Event()

    async def first(_):
        effects.append("first")
        return "written"

    async def second(_):
        blocked.set()
        await asyncio.Event().wait()

    tools = create_builtin_registry()
    tools.register(Tool("first", "first", ToolArguments, first))
    tools.register(Tool("second", "second", ToolArguments, second))
    path = tmp_path / "cancel.sqlite3"
    with SQLiteStore(path) as store:
        sessions = start(store)
        calls = [ToolCall(id=name, name=name, arguments="{}") for name in ["first", "second"]]
        provider = FakeProvider(ModelResponse(tool_calls=calls))
        agent = runtime(provider, sessions, tools)
        task = asyncio.create_task(agent.run("two actions"))
        await asyncio.wait_for(blocked.wait(), timeout=2)
        assert len(agent.context.pending_messages) == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.context.pending_messages == []
        assert store.list_runs(sessions.current.id)[0].status == "cancelled"
        audit = store.load_messages(sessions.current.id)
        assert len(audit) == 3
        assert audit[-1].content.result.call_id == "first"
        assert sessions.context().history == []
    with SQLiteStore(path) as store:
        sessions = start(store)
        provider = FakeProvider(ModelResponse(text="new request only"))
        await runtime(provider, sessions, tools).run("continue")
        assert len(provider.requests[0]) == 3
        assert store.load_messages(sessions.current.id)[:3] == audit
    assert effects == ["first"]


async def test_database_failure_before_call_record_prevents_tool_execution(tmp_path, monkeypatch):
    effects = []

    async def effect(_):
        effects.append("written")

    with SQLiteStore(tmp_path / "before.sqlite3") as store:
        sessions = start(store)
        tools = create_builtin_registry()
        tools.register(Tool("effect", "effect", ToolArguments, effect))
        original = store.append_message

        def fail_before_execution(run_id, message):
            if isinstance(message, AssistantMessage) and message.tool_calls:
                raise HistoryError("private db information")
            original(run_id, message)

        monkeypatch.setattr(store, "append_message", fail_before_execution)
        provider = FakeProvider(
            ModelResponse(tool_calls=[ToolCall(id="effect-1", name="effect", arguments="{}")])
        )
        result = await runtime(provider, sessions, tools).run("write")
        assert result.status == "storage_error"
        assert "private" not in result.model_dump_json()
        assert store.list_runs(sessions.current.id)[0].status == "interrupted"
    assert effects == []


async def test_result_write_failure_stops_batch_and_keeps_prior_history(tmp_path):
    effects = []

    async def effect(_):
        effects.append("written")
        return "written"

    with SQLiteStore(tmp_path / "after.sqlite3") as store:
        sessions = start(store)
        await runtime(FakeProvider(ModelResponse(text="old answer")), sessions).run("old input")
        previous = store.load_messages(sessions.current.id)
        with sqlite3.connect(store.path) as db:
            db.execute(
                "CREATE TRIGGER reject_results BEFORE INSERT ON messages "
                "WHEN json_extract(NEW.payload,'$.role')='tool' "
                "BEGIN SELECT RAISE(ABORT,'private result write failure'); END"
            )
        tools = create_builtin_registry()
        tools.register(Tool("effect", "effect", ToolArguments, effect))
        provider = FakeProvider(
            ModelResponse(
                tool_calls=[ToolCall(id=f"e-{i}", name="effect", arguments="{}") for i in range(2)]
            ),
            ModelResponse(text="next turn"),
        )
        agent = runtime(provider, sessions, tools)
        result = await agent.run("two writes")
        assert result.status == "storage_error"
        assert effects == ["written"]
        assert len(provider.requests) == 1
        assert store.load_messages(sessions.current.id)[:2] == previous
        assert store.list_runs(sessions.current.id)[-1].status == "interrupted"
        assert len(agent.context.history) == 2
        assert (await agent.run("next")).status == "completed"
        assert [item.text for item in provider.requests[1][:1] + provider.requests[1][2:]] == [
            sessions.agent.system_prompt,
            "old input",
            "old answer",
            "next",
        ]
        assert effects == ["written"]


async def test_begin_write_failure_does_not_call_model(tmp_path):
    with SQLiteStore(tmp_path / "begin.sqlite3") as store:
        sessions = start(store)
        with sqlite3.connect(store.path) as db:
            db.execute(
                "CREATE TRIGGER reject_runs BEFORE INSERT ON runs "
                "BEGIN SELECT RAISE(ABORT,'private begin failure'); END"
            )
        provider = FakeProvider()
        result = await runtime(provider, sessions).run("input")
        assert (result.status, result.rounds) == ("storage_error", 0)
        assert provider.requests == []
        assert store.list_runs(sessions.current.id) == []


async def test_provider_failure_is_a_finished_failed_run_and_remains_replayable(tmp_path):
    path = tmp_path / "failed.sqlite3"
    with SQLiteStore(path) as store:
        sessions = start(store)
        result = await runtime(FakeProvider(ProviderError("private")), sessions).run("hello")
        assert result.status == "provider_error"
        run = store.list_runs(sessions.current.id)[0]
        assert run.status == "failed"
        assert run.result == result
        assert run.ended_at is not None
    with SQLiteStore(path) as store:
        sessions = start(store)
        assert len(sessions.context().history) == 2
        assert "Runtime stopped" in sessions.context().history[-1].text


def test_actual_process_exit_releases_lock_and_retains_unfinished_run(tmp_path):
    path = tmp_path / "crash.sqlite3"
    script = """
import os,sys
from pathlib import Path
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.sessions import SessionManager
from personal_agent.core.models import TextMessage,AssistantMessage,ToolCall
store=SQLiteStore(Path(sys.argv[1]))
store.initialize()
sessions=SessionManager(store)
run=store.begin_run(sessions.current.id,TextMessage(role='user',text='interrupted'))
store.append_message(run,AssistantMessage(tool_calls=[ToolCall(id='pending',name='effect',arguments='{}')]))
os._exit(0)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, timeout=10
    )
    assert process.returncode == 0, process.stderr.decode()
    with SQLiteStore(path) as store:
        store.initialize()
        assert store.recover_incomplete_runs() == 1
        sessions = SessionManager(store)
        assert store.list_runs(sessions.current.id)[0].status == "interrupted"
        assert len(store.load_messages(sessions.current.id)) == 2
        assert sessions.context().history == []


async def test_cli_session_commands_switch_and_restore_without_polluting_messages(tmp_path):
    with SQLiteStore(tmp_path / "cli.sqlite3") as store:
        sessions = start(store)
        original = sessions.current.id
        provider = FakeProvider(
            ModelResponse(text="first"),
            ModelResponse(text="fresh"),
            ModelResponse(text="original resumed"),
        )
        agent = runtime(provider, sessions)
        commands = iter(
            [
                "first input",
                "/new",
                "/status",
                "second input",
                "/sessions",
                "/resume missing",
                "/resume",
                "/unknown",
                f"/resume {original}",
                "third input",
                "quit",
            ]
        )
        output = []
        await chat(agent, read=lambda _: next(commands), write=output.append, sessions=sessions)
        assert len(sessions.list()) == 2
        assert sessions.current.id == original
        assert len(provider.requests[1]) == 3
        assert [item.text for item in provider.requests[2]][2:] == [
            "first input",
            "first",
            "third input",
        ]
        assert any("会话错误" in line for line in output)
        assert any("命令用法" in line for line in output)
        assert not any(
            item.content.text.startswith("/")
            for item in store.load_messages(original)
            if isinstance(item.content, TextMessage)
        )


def test_resume_other_agent_or_unknown_id_preserves_active_session(tmp_path):
    with SQLiteStore(tmp_path / "selection.sqlite3") as store:
        sessions = start(store)
        original = sessions.current.id
        other = store.create_agent("other", "other system")
        foreign = store.create_conversation(other.id)
        for conversation_id in ["missing", foreign.id]:
            with pytest.raises(HistoryError):
                sessions.resume(conversation_id)
            assert sessions.current.id == store.last_conversation(sessions.agent.id) == original


def test_new_conversation_and_active_selection_are_atomic(tmp_path):
    with SQLiteStore(tmp_path / "atomic-selection.sqlite3") as store:
        sessions = start(store)
        original = sessions.current.id
        with sqlite3.connect(store.path) as db:
            db.execute(
                "CREATE TRIGGER reject_selection BEFORE INSERT ON client_state "
                "BEGIN SELECT RAISE(ABORT,'selection failure'); END"
            )
        with pytest.raises(HistoryError):
            sessions.new()
        assert sessions.current.id == store.last_conversation(sessions.agent.id) == original
        assert [item.id for item in sessions.list()] == [original]


async def test_final_round_rejections_are_persisted_and_replayable(tmp_path):
    with SQLiteStore(tmp_path / "limit.sqlite3") as store:
        sessions = start(store)
        provider = FakeProvider(ModelResponse(tool_calls=[add_call()]))
        result = await runtime(provider, sessions, max_rounds=1).run("sum")
        assert result.status == "max_rounds"
        rows = store.load_messages(sessions.current.id, replayable_only=True)
        outputs = [row.content.result for row in rows if isinstance(row.content, ToolMessage)]
        assert outputs[0].call_id == "call_add"
        assert outputs[0].error.code == "round_budget_exhausted"
        assert store.list_runs(sessions.current.id)[0].status == "failed"


async def test_finish_write_failure_never_reports_success(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "finish.sqlite3") as store:
        sessions = start(store)

        def fail_finish(run_id, result):
            raise HistoryError("private finalize failure")

        monkeypatch.setattr(store, "finish_run", fail_finish)
        agent = runtime(FakeProvider(ModelResponse(text="uncommitted answer")), sessions)
        result = await agent.run("hello")
        assert result.status == "storage_error"
        assert result.text == ""
        assert store.list_runs(sessions.current.id)[0].status == "interrupted"
        assert len(store.load_messages(sessions.current.id)) == 2
        assert agent.context.history == []


async def test_failure_to_record_interruption_is_reconciled_on_restart(tmp_path, monkeypatch):
    path = tmp_path / "unmarked.sqlite3"
    with SQLiteStore(path) as store:
        sessions = start(store)

        def fail_write(*args, **kwargs):
            raise HistoryError("private storage failure")

        monkeypatch.setattr(store, "append_message", fail_write)
        monkeypatch.setattr(store, "interrupt_run", fail_write)
        result = await runtime(FakeProvider(ModelResponse(text="answer")), sessions).run("hello")
        assert result.status == "storage_error"
        assert store.list_runs(sessions.current.id)[0].status == "running"
    with SQLiteStore(path) as store:
        sessions = start(store)
        assert store.list_runs(sessions.current.id)[0].status == "interrupted"
        assert sessions.context().history == []
        assert "副作用可能已发生" in sessions.status()
