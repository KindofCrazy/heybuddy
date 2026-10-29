from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from openai import AsyncOpenAI

from personal_agent.automation.service import MemoryAutomation
from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.state import CharacterStateService
from personal_agent.character.storage import CharacterStateRepository
from personal_agent.config import Settings, memory_root_for
from personal_agent.core.agent import Agent
from personal_agent.core.models import RunResult
from personal_agent.learning.curator import MemoryCurator
from personal_agent.learning.service import MemoryLearningService
from personal_agent.llm.base import Provider
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.store import MemoryFS
from personal_agent.memory.tools import register_memory_tools
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.organization.threads import ThreadOrganizer
from personal_agent.recall.service import Recall
from personal_agent.recall.tools import register_recall_tools
from personal_agent.reflection.service import ReflectionService
from personal_agent.retrieval import ContextRetriever
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.threads import ThreadRepository
from personal_agent.tools.builtin import create_builtin_registry


@dataclass
class ChatService:
    """Shared pipeline; adapters schedule maintenance after delivering the reply."""

    agent: Agent
    sessions: SessionManager | None = None
    reflection: ReflectionService | None = None
    automation: MemoryAutomation | None = None
    organization: OrganizationRuntime | None = None
    foreground_active: bool = False
    qq_status: Callable[[], dict] | None = None
    observer_only: bool = False

    async def foreground(self) -> None:
        if self.organization is not None:
            await self.organization.foreground()

    async def run(self, text: str, *, permissions: frozenset[str] = frozenset()) -> RunResult:
        if self.observer_only:
            raise RuntimeError("Observer-only service cannot start a Run")
        self.foreground_active = True
        try:
            await self.foreground()
            return await self.agent.run(text, permissions=permissions)
        finally:
            self.foreground_active = False

    def after_reply(self, result: RunResult | None = None) -> None:
        if self.observer_only:
            return
        if self.organization is not None and self.sessions is not None:
            self.organization.schedule(
                self.sessions.current.id,
                after_answer=result is not None and result.status == "completed",
            )

    async def close(self) -> None:
        if self.organization is not None:
            await self.organization.close()


def assemble_chat(
    settings: Settings,
    sessions: SessionManager,
    memory: MemoryFS,
    provider: Provider,
    *,
    observer_only: bool = False,
) -> ChatService:
    if settings.character_id != "none":
        policy = settings.character_policy()
        package = CharacterLoader(settings.character_root, policy).load(settings.character_id)
        sessions.character = CharacterContextCompiler(
            package, policy, variant=settings.character_variant
        )
    else:
        sessions.character = None
    thread_repository = ThreadRepository(sessions.store, sessions.agent.id)
    sessions.retriever = ContextRetriever(
        thread_repository,
        settings.retrieval_policy(),
        memory=memory,
        memory_policy=settings.memory_retrieval_policy(),
    )
    tools = create_builtin_registry(settings.tool_timeout_seconds, include_add_numbers=False)
    register_memory_tools(tools, memory, unified_reads=True)
    register_recall_tools(tools, Recall(sessions.store, sessions.agent.id))
    agent = Agent(
        provider,
        tools,
        context=sessions.context(),
        max_rounds=settings.max_rounds,
        max_tool_calls_per_response=settings.max_tool_calls_per_response,
        max_tool_calls_per_run=settings.max_tool_calls_per_run,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    reflection = ReflectionService(
        ReflectionRepository(sessions.store, sessions.agent.id),
        memory,
        agent.provider,
        settings.context_budget(),
        settings.reflection_policy(),
    )
    retrieval_policy = settings.memory_retrieval_policy()
    sessions.retriever.preflight_model = OrganizerModel(
        agent.provider,
        settings.context_budget(),
        OrganizationPolicy(
            input_tokens=retrieval_policy.input_tokens,
            output_tokens=retrieval_policy.output_tokens,
            timeout_seconds=retrieval_policy.timeout_seconds,
            response_bytes=4096,
        ),
    )
    automation = MemoryAutomation(reflection, settings.automation_policy())
    if not observer_only:
        automation.repository.recover()
    repository = EpisodeRepository(sessions.store, sessions.agent.id)
    if not observer_only:
        repository.recover()
        thread_repository.recover()
    learning = MemoryLearningService(
        LearningRepository(repository),
        reflection,
        settings.learning_policy(),
        initialize=not observer_only,
    )
    character_state = None
    if sessions.character is not None:
        character_repository = CharacterStateRepository(
            repository, settings.character_id, sessions.character.package.fingerprint
        )
        character_state = CharacterStateService(
            character_repository,
            sessions.character.package,
            agent.provider,
            settings.context_budget(),
            settings.character_state_policy(),
            initialize=not observer_only,
        )
        sessions.character.on_run = character_repository.tag_run
        sessions.character.source_scope = character_repository.references
        sessions.character.state = character_state.active
    organization = OrganizationRuntime(
        EpisodeOrganizer(
            repository,
            OrganizerModel(
                agent.provider, settings.context_budget(), settings.organization_policy()
            ),
        ),
        automation=automation,
        learning=learning,
        character_state=character_state,
        curator=MemoryCurator(learning, settings.curator_policy(), initialize=not observer_only),
        threads=ThreadOrganizer(
            thread_repository,
            OrganizerModel(
                agent.provider, settings.context_budget(), settings.organization_policy()
            ),
        ),
    )
    return ChatService(
        agent, sessions, reflection, automation, organization, observer_only=observer_only
    )


@asynccontextmanager
async def open_chat(
    settings: Settings, sessions: SessionManager, memory: MemoryFS, *, observer_only: bool = False
) -> AsyncIterator[ChatService]:
    # One request per round; no retry of a possibly executed tool turn.
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
    ) as client:
        service = assemble_chat(
            settings,
            sessions,
            memory,
            OpenAIProvider(client, settings.model),
            observer_only=observer_only,
        )
        try:
            yield service
        finally:
            await service.close()


@asynccontextmanager
async def open_application(
    settings: Settings, *, observer_only: bool = False
) -> AsyncIterator[ChatService]:
    with SQLiteStore(settings.database_path) as store:
        store.initialize()
        if not observer_only:
            store.recover_incomplete_runs()
        sessions = SessionManager(
            store,
            budget=settings.context_budget(),
            optional_history_tokens=settings.retrieval_optional_tokens,
        )
        memory = MemoryFS.for_agent(
            memory_root_for(settings, sessions.agent.id),
            sessions.agent.id,
            max_bytes=settings.memory_max_bytes,
        )
        if not observer_only:
            memory.initialize()
        sessions.memory = memory
        async with open_chat(settings, sessions, memory, observer_only=observer_only) as service:
            yield service
