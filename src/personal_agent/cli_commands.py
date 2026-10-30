"""Trusted local maintenance commands; never exposed as model or HTTP tools."""

import asyncio
import json
from collections.abc import Callable

from personal_agent.organization.budget import MaintenanceBudget, cycle_providers
from personal_agent.organization.runtime import OrganizationRuntime


class MaintenanceCommands:
    def __init__(self, runtime: OrganizationRuntime) -> None:
        self.runtime = runtime

    async def proactive_command(self, text: str) -> dict:
        service = self.runtime.proactive
        if service is None:
            return {"enabled": False}
        if text == "status":
            return service.status()
        if text in {"proposals", "decisions"}:
            return service.repository.listing(text)
        if text == "retry":
            return service.retry()
        if text != "run":
            return {"usage": "/proactive status|proposals|decisions|run|retry"}
        budget = MaintenanceBudget(self.runtime.episodes.policy, self.runtime.clock)
        with cycle_providers([service.model], budget):
            try:
                async with asyncio.timeout(self.runtime.episodes.policy.maintenance_seconds):
                    return {**await service.run(), "budget": budget.report()}
            except TimeoutError:
                return {"outcome": "budget_exhausted", "error_code": "maintenance_timeout"}

    async def command(self, stream_id: str, text: str, write: Callable[[str], None]) -> None:
        parts = text.split()
        if parts and parts[0] == "/thread":
            if self.runtime.threads is None:
                write("Thread > 当前入口未配置 Thread Organizer。")
                return
            if parts == ["/thread", "status"]:
                result = {
                    **self.runtime.threads.repository.status(),
                    "schedule": self.runtime.diagnostics(stream_id),
                }
            elif parts == ["/thread", "organize"]:
                result = await self.runtime.threads.organize()
            elif parts == ["/thread", "repair"]:
                result = await self.runtime.threads.repair()
            elif parts == ["/thread", "list"]:
                result = [t.model_dump(mode="json") for t in self.runtime.threads.repository.list()]
            elif len(parts) == 3 and parts[:2] == ["/thread", "show"]:
                result = self.runtime.threads.repository.get(parts[2]).model_dump(mode="json")
            else:
                write("用法：/thread status|organize|repair|list|show <id>")
                return
        elif parts == ["/episode", "status"]:
            result = {
                **self.runtime.episodes.repository.status(stream_id),
                "last_report": self.runtime.last_report,
                "schedule": self.runtime.diagnostics(stream_id),
            }
        elif parts == ["/episode", "organize"]:
            result = await self.runtime.episodes.organize(stream_id)
        elif parts == ["/episode", "list"]:
            result = [
                episode.model_dump(mode="json")
                for episode in self.runtime.episodes.repository.list(stream_id)
            ]
        elif len(parts) == 3 and parts[:2] == ["/episode", "show"]:
            result = self.runtime.episodes.repository.get(parts[2]).model_dump(mode="json")
        else:
            write("用法：/episode status|organize|list|show <id>")
            return
        write(json.dumps(result, ensure_ascii=False))

    async def learning_command(self, text: str, *, curator=False) -> dict:
        if self.runtime.learning is None:
            return {"outcome": "disabled"}
        if curator and self.runtime.curator is None:
            return {"outcome": "disabled"}
        if text == "status":
            return (
                self.runtime.curator.status()
                if curator and self.runtime.curator
                else self.runtime.learning.status()
            )
        parts = text.split()
        if len(parts) == 2 and parts[0] == "reconcile":
            return self.runtime.learning.reconcile(parts[1])
        if len(parts) == 2 and parts[0] == "retry":
            return self.runtime.learning.retry_job(parts[1])
        if text not in {"run", "retry"}:
            return {"usage": "/memory learning status|run|retry|reconcile <job_id>"}
        model = self.runtime.learning.model
        budget = MaintenanceBudget(self.runtime.episodes.policy, self.runtime.clock)
        with cycle_providers([model], budget):
            try:
                async with asyncio.timeout(self.runtime.episodes.policy.maintenance_seconds):
                    self.runtime.learning.poll()
                    if curator:
                        return await self.runtime.curator.run(force=True, retry=text == "retry")
                    report = await self.runtime.learning.learn_priority(retry=text == "retry")
                    if report["outcome"] == "no_learning_work":
                        report = await self.runtime.learning.learn_batch(
                            force=True, retry=text == "retry"
                        )
                    return {**report, "budget": budget.report()}
            except TimeoutError:
                return {
                    "outcome": "budget_exhausted",
                    "error_code": "maintenance_timeout",
                    "budget": budget.report(),
                }

    async def character_command(self, text: str) -> dict:
        service = self.runtime.character_state
        if service is None:
            return {"enabled": False}
        if text == "status":
            return service.status()
        if text not in {"run", "retry"}:
            return {"usage": "/character status|run|retry"}
        model = service.model
        budget = MaintenanceBudget(self.runtime.episodes.policy, self.runtime.clock)
        with cycle_providers([model], budget):
            try:
                async with asyncio.timeout(self.runtime.episodes.policy.maintenance_seconds):
                    report = await service.run(force=True, retry=text == "retry")
                    return {**report, "budget": budget.report()}
            except TimeoutError:
                return {"outcome": "budget_exhausted", "error_code": "maintenance_timeout"}
