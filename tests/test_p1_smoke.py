import json

import pytest

from personal_agent.config import Settings
from personal_agent.core.models import ModelResponse
from personal_agent.evaluation import p1
from personal_agent.evaluation.runner import Budget
from personal_agent.organization.episodes import EPISODE_PROMPT
from personal_agent.organization.threads import THREAD_PROMPT
from tests.fakes import FakeProvider


class SmokeFake(FakeProvider):
    async def generate(self, messages, tools, *, max_output_tokens=None):
        data = (
            json.loads(messages[1].text)
            if messages[0].text in {EPISODE_PROMPT, THREAD_PROMPT}
            else None
        )
        if messages[0].text == EPISODE_PROMPT:
            ids = [r["run_id"] for r in data["runs"]]
            output = {
                "episodes": [
                    {"run_ids": group, "title": title, "summary": "合成状态", "highlights": []}
                    for group, title in [
                        (ids[:2], "HeyBuddy"),
                        ([ids[2]], "晚饭"),
                        ([ids[3]], "HeyBuddy"),
                    ]
                ]
            }
            response = ModelResponse(text=json.dumps(output, ensure_ascii=False))
        elif messages[0].text == THREAD_PROMPT:
            relevant = [e["id"] for e in data["episodes"] if e["title"] == "HeyBuddy"]
            casual = [e["id"] for e in data["episodes"] if e["title"] != "HeyBuddy"]
            response = ModelResponse(
                text=json.dumps(
                    {
                        "groups": [
                            {
                                "episode_ids": relevant,
                                "thread_id": None,
                                "title": "HeyBuddy",
                                "current_state": "先完成 Working Context，然后讨论 P2。",
                                "status": "active",
                            }
                        ],
                        "no_thread": casual,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            response = ModelResponse(text="先完成 Working Context。")
        self.responses.append(response)
        return await super().generate(messages, tools, max_output_tokens=max_output_tokens)


async def test_p1_smoke_uses_isolated_synthetic_data_and_never_applies_memory(tmp_path):
    provider = SmokeFake()
    settings = Settings(api_key="test-key", model="test-model", context_window=64000)
    folder = tmp_path / "p1-smoke"
    result = await p1.evaluate(folder, provider, settings, Budget(requests=6, tokens=196608))
    assert result["contract_pass"] and result["semantic_review_required"]
    assert result["stage_requests"] == {"episode": 1, "thread": 1, "main": 1, "compaction": 0}
    assert result["memory_automation_requests"] == 0
    assert result["usage"]["requests"] == 3 and not result["usage"]["usage_complete"]
    assert "合成状态" not in json.dumps(result, ensure_ascii=False)
    assert (folder / "private-review.json").exists()
    assert provider.tools[0] == provider.tools[1] == []


def test_p1_smoke_requires_live_before_reading_settings(monkeypatch):
    monkeypatch.setattr("sys.argv", ["p1"])
    monkeypatch.setattr(p1, "load_settings", lambda: pytest.fail("must not read settings"))
    with pytest.raises(SystemExit) as error:
        p1.main()
    assert error.value.code == 2
