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
        "OPENAI_API_KEY=test-local\nOPENAI_MODEL=file-model\n", encoding="utf-8-sig"
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
    ],
)
def test_invalid_settings(field, value):
    data = {"api_key": "test-key", "model": "test-model", field: value}
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


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
        api_key="test-key", model="deepseek-flash", base_url="https://api.deepseek.com"
    )
    observed = {}

    async def chat(agent):
        observed["agent"] = agent
        assert agent.provider.client.max_retries == 0
        assert agent.provider.client.api_key == "test-key"
        assert str(agent.provider.client.base_url) == "https://api.deepseek.com/"
        assert agent.provider.model == "deepseek-flash"

    monkeypatch.setattr(cli, "chat", chat)
    import asyncio

    asyncio.run(cli.run_cli(settings))
    assert observed["agent"].provider.client.is_closed()
