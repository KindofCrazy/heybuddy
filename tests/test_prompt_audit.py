import argparse
import json
from types import SimpleNamespace

import pytest

from personal_agent.evaluation import prompt_audit as audit
from personal_agent.llm.base import ProviderError


def arguments(tmp_path, **overrides):
    values = dict(
        output=tmp_path / "audit",
        variant="test",
        live=False,
        prompts=None,
        split="dev",
        stages="main",
        max_requests=100,
    )
    return argparse.Namespace(**(values | overrides))


async def test_snapshot_needs_no_credentials_or_client(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline snapshot must not load credentials or open a client")

    monkeypatch.setattr(audit, "load_settings", forbidden)
    monkeypatch.setattr(audit, "AsyncOpenAI", forbidden)
    await audit.run(arguments(tmp_path))
    prompts = json.loads((tmp_path / "audit/test-prompts.json").read_text(encoding="utf-8"))
    assert set(prompts) == set(audit.stages())
    assert not (tmp_path / "audit/ledger.jsonl").exists()


async def test_attempted_request_cap_stops_before_client(tmp_path, monkeypatch):
    args = arguments(tmp_path, live=True)
    args.output.mkdir()
    (args.output / "ledger.jsonl").write_text("{}\n" * 100, encoding="utf-8")
    monkeypatch.setattr(audit, "load_settings", lambda: SimpleNamespace())

    def forbidden(*args, **kwargs):
        raise AssertionError("Budget exceeded must not open a client")

    monkeypatch.setattr(audit, "AsyncOpenAI", forbidden)
    with pytest.raises(ValueError, match="cap would be exceeded"):
        await audit.run(args)
    assert len((args.output / "ledger.jsonl").read_text().splitlines()) == 100


async def test_failed_api_attempt_is_charged_once_without_retry(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        api_key=SimpleNamespace(get_secret_value=lambda: "synthetic-unused"),
        base_url=None,
        model="fake",
        request_timeout_seconds=10,
    )
    monkeypatch.setattr(audit, "load_settings", lambda: settings)

    class Client:
        def __init__(self, **options):
            assert options["max_retries"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Provider:
        def __init__(self, *args):
            self.calls = 0

        async def generate(self, messages, tools, **options):
            self.calls += 1
            assert self.calls == 1 and tools == []
            raise ProviderError("synthetic diagnostic must not be serialized")

    monkeypatch.setattr(audit, "AsyncOpenAI", Client)
    monkeypatch.setattr(audit, "OpenAIProvider", Provider)
    args = arguments(tmp_path, live=True)
    await audit.run(args)
    ledger = (args.output / "ledger.jsonl").read_text(encoding="utf-8")
    result = (args.output / "test-main.json").read_text(encoding="utf-8")
    assert len(ledger.splitlines()) == 1
    assert json.loads(result)["error_type"] == "ProviderError"
    assert "synthetic diagnostic" not in result
