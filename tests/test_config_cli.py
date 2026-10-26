from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent import main as cli
from personal_agent.config import Settings, load_settings
from personal_agent.core.agent import Agent
from personal_agent.core.models import ModelResponse
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider, add_call


def test_settings_file_and_environment_precedence(monkeypatch):
    Path(".env").write_text(
        "OPENAI_API_KEY=test-local\nOPENAI_MODEL=file-model\nAGENT_CONTEXT_WINDOW=64000\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setenv("OPENAI_MODEL", " env-model ")
    settings = load_settings()
    assert settings.model == "env-model"
    assert settings.api_key.get_secret_value() == "test-local"
    assert "test-local" not in repr(settings)
    assert "test-local" not in settings.model_dump_json()


def test_no_parent_directory_secret_search(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test-parent\nOPENAI_MODEL=parent\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ValidationError):
        load_settings()


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", "  "),
        ("model", " "),
        ("max_rounds", 0),
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", "NaN"),
        ("tool_timeout_seconds", float("inf")),
        ("log_level", "TRACE"),
        ("base_url", "not-a-url"),
        ("memory_max_bytes", 511),
        ("memory_max_bytes", 1_048_577),
    ],
)
def test_invalid_settings(field, value):
    data = {"api_key": "test-key", "model": "test-model", "context_window": 64000, field: value}
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


@pytest.mark.parametrize(
    "base_url",
    [
        None,
        "https://api.example.com/v1",
        "https://192.168.1.10:8443/v1",
        "http://localhost:8000/v1",
        "http://LOCALHOST.:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.20.30.40:8000/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_base_url_allows_https_and_explicit_loopback_http(base_url):
    settings = Settings(
        context_window=64000, api_key="test-key", model="test-model", base_url=base_url
    )
    assert (settings.base_url is None) == (base_url is None)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com/v1",
        "http://8.8.8.8/v1",
        "http://192.168.1.10/v1",
        "http://0.0.0.0:8000/v1",
        "http://[::]:8000/v1",
        "http://[2001:db8::1]/v1",
        "http://localhost.example.com/v1",
        "http://127.0.0.1.example.com/v1",
        "http://localhost@api.example.com/v1",
        "http://api.example.com/localhost",
        "http://[::ffff:192.168.1.10]/v1",
    ],
)
def test_base_url_rejects_non_loopback_http_and_hostname_spoofs(base_url):
    with pytest.raises(ValidationError, match="requires HTTPS"):
        Settings(context_window=64000, api_key="test-key", model="test-model", base_url=base_url)


def test_environment_base_url_cannot_bypass_https_policy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "64000")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://api.example.com/v1")
    with pytest.raises(ValidationError, match="requires HTTPS"):
        load_settings()


def test_memory_configuration_environment_overrides_file(monkeypatch):
    Path(".env").write_text(
        "OPENAI_API_KEY=test-key\nOPENAI_MODEL=test-model\nAGENT_CONTEXT_WINDOW=64000\n"
        "AGENT_MEMORY_ROOT=file-memory\nAGENT_CORE_MEMORY_MAX_BYTES=1024\n"
    )
    monkeypatch.setenv("AGENT_MEMORY_ROOT", "custom-memory")
    monkeypatch.setenv("AGENT_CORE_MEMORY_MAX_BYTES", "2048")
    settings = load_settings()
    assert settings.memory_root == Path("custom-memory")
    assert settings.memory_max_bytes == 2048


async def test_cli_multiple_turns_with_tools_and_quit():
    provider = FakeProvider(
        ModelResponse(text="hello"), ModelResponse(tool_calls=[add_call()]), ModelResponse(text="5")
    )
    agent = Agent(provider, create_builtin_registry())
    inputs = iter(["", "hi", "2+3", "QUIT"])
    outputs = []
    await cli.chat(agent, read=lambda _: next(inputs), write=outputs.append)
    assert "Agent > hello" in outputs
    assert "Agent > 5" in outputs
    assert len(provider.requests) == 3
    assert outputs[-1] == "再见。"


@pytest.mark.parametrize("end", ["exit", "quit", "eof"])
async def test_cli_exits_without_model_request(end):
    def read(_):
        if end == "eof":
            raise EOFError
        return end

    provider = FakeProvider()
    await cli.chat(Agent(provider, create_builtin_registry()), read=read, write=lambda _: None)
    assert not provider.requests


def test_cli_missing_config_returns_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent"])
    assert cli.main() == 2
    assert "model" in capsys.readouterr().err


def test_cli_config_error_hides_input_values(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent"])
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-value")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "64000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "test-secret-value")
    assert cli.main() == 2
    assert "test-secret-value" not in str(capsys.readouterr())


def test_cli_help_needs_no_config(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent", "--help"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0
    assert "Personal Agent" in capsys.readouterr().out


def test_run_cli_constructs_sdk_and_runtime_and_closes_client(monkeypatch):
    settings = Settings(
        context_window=64000,
        api_key="test-key",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
    )
    observed = {}
    memory_root = Path("user-data/heybuddy").absolute()
    monkeypatch.setattr("personal_agent.config.default_memory_root", lambda: Path("user-data"))

    async def chat(agent, *, sessions, reflection):
        assert reflection.memory is sessions.memory
        assert reflection.provider is agent.provider
        observed["agent"] = agent
        assert sessions.current.id
        assert agent.provider.client.max_retries == 0
        assert agent.provider.client.api_key == "test-key"
        assert str(agent.provider.client.base_url) == "https://api.deepseek.com/"
        assert agent.provider.model == "deepseek-flash"
        assert sessions.memory is agent.context.memory
        assert sessions.memory.root == memory_root
        assert sessions.memory.read().blocks["human.md"] == "# Human\n"
        assert {tool.name for tool in agent.tools.definitions()} >= {"recall_search", "recall_get"}

    monkeypatch.setattr(cli, "chat", chat)
    import asyncio

    asyncio.run(cli.run_cli(settings))
    assert observed["agent"].provider.client.is_closed()


def test_context_window_is_explicit_even_for_custom_models():
    with pytest.raises(ValidationError) as caught:
        Settings(api_key="test-key", model="custom-provider-model")
    assert any(error["loc"] == ("context_window",) for error in caught.value.errors())


def test_invalid_combined_context_budget_is_safe_cli_configuration_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent"])
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-key")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "4096")
    assert cli.main() == 2
    output = capsys.readouterr().err
    assert "context_budget" in output and "test-private-key" not in output


async def test_context_status_is_local_and_does_not_create_a_run(tmp_path):
    from personal_agent.core.budget import ContextBudget
    from personal_agent.sessions import SessionManager
    from personal_agent.storage.sqlite import SQLiteStore

    provider = FakeProvider()
    with SQLiteStore(tmp_path / "status.sqlite3") as db:
        db.initialize()
        sessions = SessionManager(db, budget=ContextBudget(context_window=64000))
        agent = Agent(provider, create_builtin_registry(), context=sessions.context())
        inputs = iter(["/context status", "quit"])
        output = []
        await cli.chat(agent, read=lambda _: next(inputs), write=output.append, sessions=sessions)
        assert not provider.requests and db.list_runs(sessions.current.id) == []
        assert any('"context_window": 64000' in line for line in output)
