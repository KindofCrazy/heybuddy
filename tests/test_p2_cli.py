import json

from personal_agent import main, runtime
from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.models import ModelResponse
from personal_agent.learning.curator import MemoryCurator
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.runtime import ChatService
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_episodes import organizer
from tests.test_learning import learner, state

__all__ = ["state"]


async def test_production_assembly_has_one_learning_entry_and_shared_retriever(state, monkeypatch):
    db, sessions, memory, run = state

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    provider = FakeProvider()
    monkeypatch.setattr(runtime, "AsyncOpenAI", Client)
    monkeypatch.setattr(runtime, "OpenAIProvider", lambda *_: provider)
    called = []

    async def chat(service):
        agent, sessions = service.agent, service.sessions
        reflection, automation, organization = (
            service.reflection,
            service.automation,
            service.organization,
        )
        called.append(True)
        assert organization.learning.policy.enabled
        assert organization.curator.learning is organization.learning
        assert organization.learning.model.provider is provider
        assert sessions.retriever is agent.context.retriever
        assert sessions.retriever.preflight_model.provider is provider
        assert automation.reflection is reflection
        await organization.close()

    monkeypatch.setattr(main, "chat", chat)
    await main._run_session(
        Settings(api_key="synthetic", model="synthetic", context_window=64000), sessions, memory
    )
    assert called


async def test_actual_cli_debug_statuses_are_readonly_and_safe(state):
    db, sessions, memory, _ = state
    learning = learner(state, FakeProvider())
    curator = MemoryCurator(learning)
    organization = OrganizationRuntime(
        organizer(learning.repository.episodes), learning=learning, curator=curator
    )
    before = memory.read()
    agent = Agent(FakeProvider(), create_builtin_registry(), context=sessions.context())
    commands = iter(
        ["/memory learning status", "/memory curator status", "/context status", "exit"]
    )
    output = []
    await main.chat(
        agent,
        read=lambda _: next(commands),
        write=output.append,
        sessions=sessions,
        organization=organization,
    )
    payload = [json.loads(line) for line in output if line.startswith("{")]
    assert any("activation" in row for row in payload)
    assert any("next_check_at" in row for row in payload)
    assert memory.read() == before
    assert not learning.model.provider.requests


async def test_cli_uses_exact_shared_service_and_leaves_closing_to_owner(state, monkeypatch):
    _, sessions, _, _ = state
    provider = FakeProvider(ModelResponse(text="answer"))
    learning = learner(state, provider)
    organization = OrganizationRuntime(organizer(learning.repository.episodes))
    agent = Agent(provider, create_builtin_registry(), context=sessions.context())
    service = ChatService(agent, sessions=sessions, organization=organization)
    original = service.run
    calls = []

    async def run(*args, **kwargs):
        calls.append(service)
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "run", run)
    commands = iter(["synthetic", "exit"])
    try:
        await main.chat(service, read=lambda _: next(commands), write=lambda _: None)
        assert calls == [service]
        assert organization.task is not None  # open_chat owns final cancellation.
    finally:
        await service.close()
