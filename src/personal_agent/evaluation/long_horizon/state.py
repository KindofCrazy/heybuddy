"""Controlled production Episode/Thread maintenance on completed synthetic history only."""

import json
from collections import defaultdict
from datetime import timedelta

from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.models import ModelResponse
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.threads import ThreadOrganizer
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.threads import ThreadRepository


class ExtractiveContractProvider:
    """Deterministic contract fixture, not a semantic state summarizer or quality claim."""

    async def generate(self, messages, tools, **options):
        if tools:
            raise ValueError("Maintenance cannot use tools")
        data = json.loads(messages[1].text)
        if "runs" in data:
            episodes = []
            for run in data["runs"]:
                user = " ".join(m["text"] for m in run["messages"] if m["role"] == "user")
                episodes.append(
                    {
                        "run_ids": [run["run_id"]],
                        "title": user[:80] or "用户原话",
                        "summary": f"{run['started_at']}: 用户原话：{user}"[:1500],
                        "highlights": [],
                    }
                )
            result = {"episodes": episodes}
        elif "previous_rebuild" in data:
            values = [data["previous_rebuild"], *[e["summary"] for e in data["episodes"]]]
            result = {"current_state": compact_quotes(values), "status": "active"}
        else:
            grouped = defaultdict(list)
            for episode in data["episodes"]:
                grouped[data["fixed_thread_assignments"][episode["id"]]].append(episode)
            existing = {t["id"]: t for t in data["existing_threads"]}
            result = {
                "groups": [
                    {
                        "thread_id": tid,
                        "episode_ids": [e["id"] for e in episodes],
                        "title": existing[tid]["title"],
                        "current_state": compact_quotes(
                            [
                                existing[tid].get("current_state") or "",
                                *[e["summary"] for e in episodes],
                            ]
                        ),
                        "status": "active",
                    }
                    for tid, episodes in grouped.items()
                ],
                "no_thread": [],
            }
        return ModelResponse(text=json.dumps(result, ensure_ascii=False))


def compact_quotes(values: list[str]) -> str:
    lines = list(dict.fromkeys(line for value in values for line in value.splitlines() if line))
    # Retain identity anchor and recent quotes. Dates prevent old quotes posing as new facts.
    selected = [*lines[:1], *lines[-3:]]
    return "\n".join(dict.fromkeys(selected))[:1800] or "无用户原话"


class ObservedMaintenanceProvider:
    def __init__(self, provider, stage: str, calls: list[dict]):
        self.provider, self.stage, self.calls = provider, stage, calls

    async def generate(self, messages, tools, *, max_output_tokens=None, **options):
        record = {
            "stage": self.stage,
            "estimated_input_tokens": estimate_request(messages, tools),
            "output_limit": max_output_tokens,
            "status": "running",
        }
        self.calls.append(record)
        try:
            response = await self.provider.generate(
                messages, tools, max_output_tokens=max_output_tokens, **options
            )
            record["status"] = "completed"
            return response
        except BaseException:
            record["status"] = "failed"
            raise


class ProductionMaintenance:
    """Explicit replay schedule, not production idle scheduler timing.

    Pass a metered real Provider for a future authorized semantic-summary experiment;
    offline callers use ExtractiveContractProvider and report it as such.
    """

    def __init__(self, provider, *, interval: int = 3):
        if not 1 <= interval <= 16:
            raise ValueError("Invalid maintenance interval")
        self.provider, self.interval = provider, interval
        self.calls: list[dict] = []
        self.policy = OrganizationPolicy(
            page_runs=8, revision_seconds=1, idle_seconds=1, output_tokens=4096, input_tokens=32768
        )

    async def after_completed(self, repository, turn, count: int):
        if count % self.interval:
            return None
        start = len(self.calls)
        budget = ContextBudget(context_window=131072)
        episodes = EpisodeRepository(repository.store, repository.agent_id)
        threads = ThreadRepository(repository.store, repository.agent_id)
        episode_model = OrganizerModel(
            ObservedMaintenanceProvider(self.provider, "episode", self.calls), budget, self.policy
        )
        thread_model = OrganizerModel(
            ObservedMaintenanceProvider(self.provider, "thread", self.calls), budget, self.policy
        )
        episode_service = EpisodeOrganizer(episodes, episode_model)
        thread_service = ThreadOrganizer(threads, thread_model)
        at = turn.timestamp + timedelta(seconds=2)
        episode_report = await episode_service.organize(repository.conversation_id, at=at)
        if episode_report["outcome"] not in {"completed", "finalized", "no_history"}:
            raise ValueError("Episode maintenance failed")
        # Bounded draining; preserve production CAS, fixed ownership and dirty-state repair.
        for _ in range(4):
            report = await thread_service.organize()
            if report["outcome"] == "no_episodes":
                break
            if report["outcome"] != "completed":
                raise ValueError("Thread maintenance failed")
        else:
            raise ValueError("Thread maintenance backlog exceeds bound")
        for _ in range(32):
            report = await thread_service.repair(at=at)
            if report["outcome"] == "no_dirty_work":
                break
            if report["outcome"] not in {"repaired", "repair_page"}:
                raise ValueError("Thread repair failed")
        else:
            raise ValueError("Thread repair backlog exceeds bound")
        candidates = threads.list()
        return {
            "after_turn_id": turn.turn_id,
            "completed_turn_count": count,
            "interval": self.interval,
            "schedule": "completed-boundary virtual idle, +2 seconds",
            "calls": self.calls[start:],
            "threads": [
                {
                    "id": t.id,
                    "title": t.title,
                    "current_state": t.current_state,
                    "dirty": t.dirty,
                    "revision": t.revision,
                }
                for t in candidates
            ],
        }
