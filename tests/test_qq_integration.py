import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from personal_agent import main
from personal_agent.channels.qq import runner
from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.models import ModelResponse
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.runtime import ChatService, assemble_chat
from personal_agent.sessions import SessionManager
from personal_agent.storage.qq import QQInbox
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_character_state import StateProvider
from tests.test_episodes import organizer, seed
from tests.test_learning import learner, state
from tests.test_organization_schedule import FakeClock
from tests.test_qq_adapter import Sender, event, settled
from tests.test_qq_transport import config

__all__ = ["state"]


async def test_production_cli_then_qq_restart_share_history_memory_and_character(tmp_path):
    path = tmp_path / "shared.db"
    root = tmp_path / "memory"
    settings = Settings(
        api_key="synthetic",
        model="synthetic",
        context_window=131072,
        character_id="sherlock_bbc",
        memory_learning_enabled=False,
        memory_automation="off",
        memory_retrieval_llm_preflight=False,
    )
    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db, budget=settings.context_budget())
        memory = MemoryFS.for_agent(root, sessions.agent.id)
        memory.initialize()
        memory.update(
            "human.md",
            "# Human\n用户的项目是 HeyBuddy。\n",
            expected_hash=memory.read().hashes["human.md"],
        )
        sessions.memory = memory
        cli_provider = FakeProvider(ModelResponse(text="先核对证据。"))
        service = assemble_chat(settings, sessions, memory, cli_provider)
        conversation_id = sessions.current.id
        fingerprint = sessions.character.package.fingerprint
        inputs = iter(["这个反例推翻了之前的假设。", "quit"])
        await main.chat(
            service.agent,
            read=lambda _: next(inputs),
            write=lambda _: None,
            sessions=sessions,
            reflection=service.reflection,
            automation=service.automation,
            organization=service.organization,
        )
        state_service = service.organization.character_state
        assert (await organizer(state_service.repository.episodes).organize(conversation_id))[
            "outcome"
        ] == "completed"
        state_service.model.provider = StateProvider()
        assert (await state_service.run(force=True))["outcome"] == "completed"
        assert state_service.active()["mood"] == "curious"
        await service.close()
    with SQLiteStore(path) as db:
        db.initialize()
        db.recover_incomplete_runs()
        sessions = SessionManager(db, budget=settings.context_budget())
        memory = MemoryFS.for_agent(root, sessions.agent.id)
        memory.initialize()
        sessions.memory = memory
        provider = FakeProvider(ModelResponse(text="继续核对反例。"))
        service = assemble_chat(settings, sessions, memory, provider)
        assert sessions.current.id == conversation_id
        assert sessions.character.package.fingerprint == fingerprint
        assert service.agent.context.character is sessions.character
        assert service.organization.character_state.active()["mood"] == "curious"
        assert service.organization.learning.policy.enabled is False
        assert service.organization.learning.reflection is service.reflection
        assert service.organization.curator.learning is service.organization.learning
        assert service.agent.context.retriever is sessions.retriever
        sender = Sender()
        inbox = QQInbox(db, sessions.agent.id, "12345", "test-openid")
        adapter = QQAdapter(config(), service, inbox, sender)
        adapter.start()
        try:
            await adapter.accept(event(text="继续我们之前的 HeyBuddy 讨论"))
            await settled(adapter)
            request = provider.requests[0]
            assert "add_numbers" not in {tool.name for tool in provider.tools[0]}
            assert {tool.name for tool in provider.tools[0]} == {
                "get_current_time",
                "deferred_list",
                "deferred_read",
                "deferred_index_check",
                "recall_search",
                "recall_get",
            }
            assert any("这个反例推翻了之前的假设。" == m.text for m in request if m.role == "user")
            assert any("用户的项目是 HeyBuddy" in m.text for m in request if m.role == "user")
            assert any(
                m.text.startswith(sessions.character.package.profile["身份"])
                for m in request
                if m.role == "system"
            )
            assert any("curious" in m.text for m in request if m.role == "user")
            assert len(provider.requests) == 1
            assert len(db.list_runs(conversation_id)) == 2
            assert sender.messages[0][-1] == "继续核对反例。"
        finally:
            await adapter.close()
            await service.close()


async def test_qq_input_preempts_actual_background_learning(state):
    db, sessions, memory, _ = state
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def generate(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    learning = learner(state, Waiting())
    clock = FakeClock()
    learning.clock = clock
    seed(db, sessions.current.id, "请长期记住：回答先给结论。")
    organization = OrganizationRuntime(
        organizer(learning.repository.episodes), learning=learning, clock=clock
    )
    provider = FakeProvider(ModelResponse(text="先聊你的问题。"))
    service = ChatService(
        Agent(provider, create_builtin_registry(), context=sessions.context()),
        sessions,
        organization=organization,
    )
    inbox = QQInbox(db, sessions.agent.id, "12345", "test-openid")
    adapter = QQAdapter(config(), service, inbox, Sender())
    adapter.start()
    try:
        organization.schedule(sessions.current.id, after_answer=True)
        await asyncio.wait_for(entered.wait(), 3)
        assert organization.current_stage == "learning"
        await adapter.accept(event())
        await settled(adapter)
        assert cancelled.is_set() and len(provider.requests) == 1
        assert memory.read().blocks["human.md"] == "# Human\n"
        assert learning.repository.pending_job("priority") is not None
    finally:
        await adapter.close()
        await service.close()


def test_qq_entry_configuration_fails_closed_before_provider(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent", "qq"])
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_MODEL", "test")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "64000")
    assert main.main() == 2
    assert "qq_configuration" in capsys.readouterr().err


async def test_discovery_uses_no_agent_database_or_llm_configuration(monkeypatch, capsys):
    class Gateway:
        def __init__(self, settings, api):
            pass

        async def run(self, receive):
            await receive(event(text="synthetic-private-text"))
            await asyncio.Event().wait()

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery must not open the application or Provider")

    monkeypatch.setattr(runner, "QQGateway", Gateway)
    monkeypatch.setattr(runner, "open_application", forbidden)
    await runner.discover_openid(config())
    output = capsys.readouterr().out
    assert "test-openid" in output and "synthetic-private-text" not in output
    assert not list(Path.cwd().glob("*.db"))


def test_discovery_cli_needs_only_qq_credentials(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent", "qq", "--discover-openid"])
    for name, value in {
        "AGENT_QQ_ENABLED": "true",
        "AGENT_QQ_APP_ID": "12345",
        "AGENT_QQ_APP_SECRET": "synthetic-private-secret",
    }.items():
        monkeypatch.setenv(name, value)
    called = []

    async def discover(settings):
        assert isinstance(settings, QQSettings)
        called.append(True)

    monkeypatch.setattr(main, "discover_openid", discover)
    assert main.main() == 0 and called
    assert "synthetic-private-secret" not in str(capsys.readouterr())


async def test_discovery_timeout_is_bounded(monkeypatch):
    class Gateway:
        def __init__(self, *args):
            pass

        async def run(self, receive):
            await asyncio.Event().wait()

    monkeypatch.setattr(runner, "QQGateway", Gateway)
    with pytest.raises(QQError, match="qq_discovery_timeout"):
        await runner.discover_openid(config(qq_discovery_seconds=0.01))


async def test_runner_supervises_gateway_and_closes_shared_application(state, monkeypatch):
    db, sessions, memory, _ = state
    provider = FakeProvider(ModelResponse(text="通过完整接线回复。"))
    service = ChatService(
        Agent(provider, create_builtin_registry(), context=sessions.context()), sessions
    )
    delivered = asyncio.Event()
    closed = []

    @asynccontextmanager
    async def application(settings):
        try:
            yield service
        finally:
            closed.append(True)

    class API:
        def __init__(self, *args):
            pass

        async def send_text(self, openid, message_id, sequence, text):
            assert text == "通过完整接线回复。"
            delivered.set()

    class Gateway:
        def __init__(self, *args):
            pass

        async def run(self, receive):
            await receive(event())
            await asyncio.wait_for(delivered.wait(), 2)
            raise QQError("qq_gateway_permission", fatal=True)

    monkeypatch.setattr(runner, "open_application", application)
    monkeypatch.setattr(runner, "QQAPI", API)
    monkeypatch.setattr(runner, "QQGateway", Gateway)
    settings = Settings(
        api_key="synthetic",
        model="synthetic",
        context_window=64000,
        **config().model_dump(),
    )
    with pytest.raises(QQError, match="qq_gateway_permission"):
        await runner.run_qq(settings)
    assert closed and len(provider.requests) == 1
    assert db.list_runs(sessions.current.id)[-1].status == "completed"
