import asyncio
import json

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse, TokenUsage
from personal_agent.llm.base import ProviderError, ProviderTimeout
from personal_agent.storage import sqlite as storage
from personal_agent.storage.tracing import SQLiteRunTrace
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider, add_call
from tests.test_dashboard import observatory
from tests.test_episodes import seed

__all__ = ["observatory"]


async def payload(client, run_id, number, part="prompt"):
    parts = []
    offset = 0
    while True:
        response = await client.get(
            f"/api/runs/{run_id}/rounds/{number}/payload",
            params={"part": part, "offset": offset},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "saved"
        parts.append(data["content"])
        if not data["has_more"]:
            return json.loads("".join(parts))
        offset = data["next_offset"]


async def test_each_actual_request_keeps_time_specific_prompt_and_tools_after_memory_edit(
    observatory,
):
    client, store, service, _, app = observatory
    provider = FakeProvider(
        ModelResponse(
            tool_calls=[add_call("trace-add")],
            usage=TokenUsage(input_tokens=123, output_tokens=30, reasoning_tokens=10),
            provider_data={"opaque": [{"reasoning": "must-not-be-copied"}]},
        ),
        ModelResponse(text="5", usage=TokenUsage(input_tokens=160, output_tokens=2)),
    )
    context = service.sessions.context()
    agent = Agent(provider, create_builtin_registry(), context, max_rounds=2)
    result = await agent.run("2+3")
    assert result.status == "completed" and result.rounds == 2
    rid = (await client.get("/api/runs")).json()["items"][0]["id"]
    first = (await client.get(f"/api/runs/{rid}/rounds?limit=1")).json()
    second = (await client.get(f"/api/runs/{rid}/rounds?after={first['next_cursor']}")).json()
    assert [r["number"] for r in first["items"] + second["items"]] == [1, 2]
    assert first["items"][0]["input_tokens"] == 123
    assert first["items"][0]["reasoning_tokens"] == 10
    assert first["items"][0]["elapsed_ms"] >= 0
    assert first["items"][0]["state"] == "responded"
    before = store._connection.total_changes
    for number in (1, 2):
        saved = await payload(client, rid, number)
        assert saved["messages"] == [
            m.model_dump(exclude={"provider_data"}) for m in provider.requests[number - 1]
        ]
        assert saved["tools"] == [t.model_dump() for t in provider.tools[number - 1]]
    assert (await payload(client, rid, 2))["messages"][-1]["result"]["call_id"] == "trace-add"
    assert (await payload(client, rid, 2))["tools"] == []
    saved_response = await payload(client, rid, 1, "response")
    assert "provider_data" not in saved_response
    assert "must-not-be-copied" not in json.dumps(saved_response)
    assert store._connection.total_changes == before
    # A new repository reads the original snapshot even after today's Core is edited.
    service.sessions.memory.update(
        "human.md",
        "Changed after run",
        expected_hash=service.sessions.memory.read().hashes["human.md"],
    )
    restored = type(app.state.repository)(store, service.sessions.agent.id)
    assert "Changed after run" not in restored.round_payload(rid, 1, "prompt", 0)["content"]


@pytest.mark.parametrize(
    "error,state",
    [
        (ProviderError("private failure body"), "provider_error"),
        (ProviderTimeout("private failure body"), "timeout"),
        (TimeoutError(), "timeout"),
        (RuntimeError("private failure body"), "runtime_error"),
    ],
)
async def test_failed_model_attempt_is_recorded_without_exception_body(observatory, error, state):
    client, _, service, _, _ = observatory
    agent = Agent(FakeProvider(error), create_builtin_registry(), service.sessions.context())
    assert (await agent.run("synthetic failure")).status == state
    rid = (await client.get("/api/runs")).json()["items"][0]["id"]
    response = await client.get(f"/api/runs/{rid}/rounds")
    assert response.json()["items"][0]["state"] == state
    assert "private failure body" not in response.text


async def test_cancelled_request_snapshot_remains_audit_only(observatory):
    client, _, service, _, _ = observatory
    entered = asyncio.Event()

    class Waiting:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    agent = Agent(Waiting(), create_builtin_registry(), service.sessions.context())
    task = asyncio.create_task(agent.run("cancel this request"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rid = (await client.get("/api/runs")).json()["items"][0]["id"]
    data = (await client.get(f"/api/runs/{rid}/rounds")).json()
    assert data["items"][0]["state"] == "cancelled"
    assert (await payload(client, rid, 1))["messages"][-1]["text"] == "cancel this request"


async def test_audit_start_failure_prevents_model_call(observatory, monkeypatch):
    _, _, service, _, _ = observatory
    context = service.sessions.context()

    def fail(*args, **kwargs):
        raise HistoryError("synthetic audit failure")

    monkeypatch.setattr(context.trace, "start", fail)
    provider = FakeProvider(ModelResponse(text="must not be used"))
    result = await Agent(provider, create_builtin_registry(), context).run("start failure")
    assert result.status == "storage_error" and not provider.requests


async def test_audit_agent_scope_legacy_and_interrupted_attempts(observatory):
    client, store, service, provider, _ = observatory
    rid = seed(store, service.sessions.current.id)
    assert (await client.get(f"/api/runs/{rid}/rounds")).json()["items"] == []
    other = store.create_agent("Other", "unused", agent_id="other-rounds")
    foreign = seed(store, store.create_conversation(other.id).id)
    for route in (f"/api/runs/{foreign}/rounds", f"/api/runs/{foreign}/rounds/1/payload"):
        assert (await client.get(route)).status_code == 404
    assert (await client.get(f"/api/runs/{rid}/rounds/1/payload")).status_code == 404
    assert (
        await client.get(f"/api/runs/{rid}/rounds/1/payload?part=provider_data")
    ).status_code == 422
    assert (await client.get(f"/api/runs/{rid}/rounds?limit=999")).status_code == 422
    from personal_agent.core.models import TextMessage

    unfinished = store.begin_run(service.sessions.current.id, TextMessage(role="user", text="stop"))
    SQLiteRunTrace(store).start(unfinished, 1, [], [], None)
    store.recover_incomplete_runs()
    assert (await client.get(f"/api/runs/{unfinished}/rounds")).json()["items"][0][
        "state"
    ] == "interrupted"
    assert not provider.requests


async def test_snapshot_pages_preserve_long_inputs_and_redact_before_splitting(observatory):
    client, _, service, _, _ = observatory
    text = "界" * 11990 + " API_KEY=synthetic-credential-to-redact " + "末" * 18000
    provider = FakeProvider(ModelResponse(text="ok"))
    result = await Agent(provider, create_builtin_registry(), service.sessions.context()).run(text)
    assert result.status == "completed"
    rid = (await client.get("/api/runs")).json()["items"][0]["id"]
    saved = await payload(client, rid, 1)
    assert saved["messages"][-1]["text"] == text.replace(
        "API_KEY=synthetic-credential-to-redact", "[REDACTED]"
    )


async def test_oversized_snapshot_is_explicit_without_changing_sent_prompt(
    observatory, monkeypatch
):
    from personal_agent.storage import tracing

    client, _, service, _, _ = observatory
    monkeypatch.setattr(tracing, "MAX_SNAPSHOT_BYTES", 64)
    provider = FakeProvider(ModelResponse(text="ok"))
    assert (
        await Agent(provider, create_builtin_registry(), service.sessions.context()).run(
            "unaltered"
        )
    ).status == "completed"
    assert provider.requests[0][-1].text == "unaltered"
    rid = (await client.get("/api/runs")).json()["items"][0]["id"]
    data = (await client.get(f"/api/runs/{rid}/rounds/1/payload")).json()
    assert data["state"] == "too_large" and data["content"] == ""


def test_schema14_migration_is_atomic_and_keeps_old_journal(tmp_path, monkeypatch):
    path = tmp_path / "trace-migration.db"
    with storage.SQLiteStore(path) as store:
        with monkeypatch.context() as patch:
            patch.setattr(storage, "SCHEMA_VERSION", 13)
            store.initialize()
        from personal_agent.sessions import SessionManager

        sessions = SessionManager(store)
        rid = seed(store, sessions.current.id)
        original = store.load_messages(sessions.current.id)
        migration = storage.MIGRATIONS[14]
        with monkeypatch.context() as patch:
            patch.setitem(storage.MIGRATIONS, 14, (*migration, "INVALID SQL"))
            with pytest.raises(HistoryError):
                store.initialize()
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert not store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='model_rounds'"
        ).fetchone()
        store.initialize()
        assert store.load_messages(sessions.current.id) == original
        assert store.list_runs(sessions.current.id)[0].id == rid
        assert store._connection.execute("SELECT count(*) FROM model_rounds").fetchone()[0] == 0


def test_request_audit_survives_owner_restart_without_replaying(tmp_path):
    from personal_agent.core.models import TextMessage
    from personal_agent.dashboard.repository import ObservatoryRepository
    from personal_agent.sessions import SessionManager

    path = tmp_path / "restart-trace.db"
    with storage.SQLiteStore(path) as store:
        store.initialize()
        sessions = SessionManager(store)
        rid = store.begin_run(sessions.current.id, TextMessage(role="user", text="old input"))
        SQLiteRunTrace(store).start(rid, 1, [TextMessage(role="system", text="old base")], [], 64)
    with storage.SQLiteStore(path) as store:
        store.initialize()
        before = store._connection.total_changes
        repo = ObservatoryRepository(store, "heybuddy")
        assert "old base" in repo.round_payload(rid, 1, "prompt", 0)["content"]
        assert store._connection.total_changes == before
        assert store.recover_incomplete_runs() == 1
        assert repo.rounds(rid, 6, 0)["items"][0]["state"] == "interrupted"


async def test_response_audit_failure_stops_before_tools(observatory, monkeypatch):
    _, store, service, _, _ = observatory
    context = service.sessions.context()

    def fail(*args, **kwargs):
        raise HistoryError("synthetic finish failure")

    monkeypatch.setattr(context.trace, "finish", fail)
    provider = FakeProvider(ModelResponse(tool_calls=[add_call("never-run")]))
    result = await Agent(provider, create_builtin_registry(), context).run("finish failure")
    assert result.status == "storage_error"
    assert len(provider.requests) == 1
    messages = store.load_messages(service.sessions.current.id)
    assert len(messages) == 1 and messages[0].content.role == "user"
