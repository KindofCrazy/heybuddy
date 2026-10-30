"""P1 §7: bounded post-answer Episode organization, preempted by console input.

The current P1 request explicitly authorizes this asyncio idle hook and input bridge.
It is not a general background task runtime: no task tools, jobs or resumed effects.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable
from datetime import datetime, timedelta
from uuid import uuid4

from personal_agent.automation.service import MemoryAutomation
from personal_agent.character.state import CharacterStateService
from personal_agent.core.history import HistoryError
from personal_agent.learning.curator import MemoryCurator
from personal_agent.learning.service import MemoryLearningService
from personal_agent.organization.budget import MaintenanceBudget, cycle_providers
from personal_agent.organization.clock import Clock, SystemClock
from personal_agent.organization.cycle import CycleProgress, WorkResult
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationError
from personal_agent.organization.threads import ThreadOrganizer
from personal_agent.proactive.runtime import ProactiveRuntime
from personal_agent.storage.schedule import ScheduleRepository


class OrganizationRuntime:
    def __init__(
        self,
        episodes: EpisodeOrganizer,
        automation: MemoryAutomation | None = None,
        threads: ThreadOrganizer | None = None,
        clock: Clock | None = None,
        learning: MemoryLearningService | None = None,
        curator: MemoryCurator | None = None,
        character_state: CharacterStateService | None = None,
        proactive: ProactiveRuntime | None = None,
    ) -> None:
        self.episodes = episodes
        self.automation = automation
        self.threads = threads
        self.clock = clock or SystemClock()
        self.learning = learning
        self.curator = curator
        self.character_state = character_state
        self.proactive = proactive
        if proactive is not None:
            proactive.clock = self.clock
        self._learning_first = True
        self._dirty_yield_to_learning = False
        self.schedule_repository = ScheduleRepository(episodes.repository)
        self._stream_id: str | None = None
        self._foreground = False
        self._activity_key: str | None = None
        self._idle_base = self.clock.monotonic()
        self._initial_idle = 0.0
        self._thread_due_key: str | None = None
        self._thread_deadline = self.clock.monotonic()
        self._cooldown_key: str | None = None
        self._cooldown_deadline = self.clock.monotonic()
        self.current_stage: str | None = None
        self.task: asyncio.Task | None = None
        self.last_report: dict = {}
        self.events: deque[dict] = deque(maxlen=128)

    def schedule(self, stream_id: str, *, after_answer: bool = False) -> None:
        if self.task is not None and not self.task.done():
            return
        self.schedule_repository.ensure(
            stream_id,
            self.clock.now(),
            self.episodes.policy.thread_interval_seconds,
            activity=self._foreground,
        )
        self._stream_id = stream_id
        self._foreground = False
        self.task = asyncio.create_task(self._background(stream_id, after_answer))

    def _idle_remaining(self, state: dict) -> float:
        stamps = [state["last_activity_at"]]
        if state["last_completed_at"]:
            stamps.append(state["last_completed_at"])
        activity = max(datetime.fromisoformat(stamp) for stamp in stamps)
        key = activity.isoformat()
        if key != self._activity_key:
            self._activity_key = key
            self._idle_base = self.clock.monotonic()
            self._initial_idle = min(
                self.episodes.policy.idle_seconds,
                max(
                    0,
                    (self.clock.now() - activity).total_seconds(),
                ),
            )
        elapsed = self._initial_idle + self.clock.monotonic() - self._idle_base
        return max(0, self.episodes.policy.idle_seconds - elapsed)

    async def _execute(self, stage: str, operation: Awaitable[dict]) -> dict:
        self.current_stage = stage
        self._event(stage, "started")
        try:
            report = await operation
            self._event(stage, report.get("outcome", "completed"))
            return report
        except asyncio.CancelledError:
            self._event(stage, "cancelled")
            raise
        except Exception:
            self._event(stage, "failed")
            raise
        finally:
            self.current_stage = None

    def _event(self, stage: str, status: str) -> None:
        self.events.append(
            {
                "event_id": "live:" + uuid4().hex,
                "timestamp": self.clock.now().isoformat(),
                "event_type": stage + "_" + status,
                "status": status,
                "related_kind": "collection",
                "related_object_id": "jobs"
                if stage.startswith("learning") or stage == "curator"
                else "episodes",
                "origin": "live_runtime",
                "timestamp_basis": "observed_at",
            }
        )

    def _thread_remaining(self, state: dict) -> float:
        key = state["thread_due_at"]
        if key != self._thread_due_key:
            self._thread_due_key = key
            remaining = max(0, (datetime.fromisoformat(key) - self.clock.now()).total_seconds())
            self._thread_deadline = self.clock.monotonic() + min(
                self.episodes.policy.thread_interval_seconds, remaining
            )
        return max(0, self._thread_deadline - self.clock.monotonic())

    def _cooldown_remaining(self, state: dict) -> float:
        key = state["cooldown_until"]
        if key != self._cooldown_key:
            self._cooldown_key = key
            remaining = (
                max(0, (datetime.fromisoformat(key) - self.clock.now()).total_seconds())
                if key
                else 0
            )
            self._cooldown_deadline = self.clock.monotonic() + min(
                self.episodes.policy.cooldown_seconds, remaining
            )
        return max(0, self._cooldown_deadline - self.clock.monotonic())

    async def _cycle(self, stream_id: str, state: dict, idle: bool) -> bool:
        policy = self.episodes.policy
        budget = MaintenanceBudget(policy, self.clock)
        models = [self.episodes.model, *([self.threads.model] if self.threads else [])]
        if self.learning and self.learning.policy.enabled:
            models.append(self.learning.model)
        if self.character_state and self.character_state.policy.enabled:
            models.append(self.character_state.model)
        if self.proactive and self.proactive.policy.enabled:
            models.append(self.proactive.model)
        with cycle_providers(models, budget):
            cycle = CycleProgress(
                learning_first=self._learning_first or self._dirty_yield_to_learning,
                learning_capacity=self._learning_capacity(),
            )
            try:
                async with asyncio.timeout(policy.maintenance_seconds):
                    for _ in range(policy.maintenance_batches):
                        if not await self._step(stream_id, idle, budget, cycle):
                            break
            except TimeoutError:
                budget.exhausted = cycle.time_limited = True
            finally:
                self.last_report["cycle"] = {
                    **budget.report(),
                    "attempted_batches": cycle.attempts,
                    "committed_or_failed_batches": cycle.batches,
                    "time_limited": cycle.time_limited,
                }
                if cycle.attempts:
                    self._learning_first = not cycle.learning_first
                    finished_at = self.clock.now()
                    self.schedule_repository.cycle(
                        finished_at,
                        cycle.batches,
                        budget.requests,
                        cycle.error,
                        frozenset(cycle.successes),
                        policy.cooldown_seconds,
                        self.last_report["cycle"],
                    )
                    self._cooldown_key = (
                        finished_at + timedelta(seconds=policy.cooldown_seconds)
                    ).isoformat()
                    self._cooldown_deadline = self.clock.monotonic() + policy.cooldown_seconds
                else:
                    self._learning_first = True
            return bool(cycle.attempts)

    def _learning_capacity(self) -> bool:
        policy = self.episodes.policy
        return bool(
            self.learning
            and 2 + self.learning.policy.max_files <= policy.maintenance_requests
            and (2 + self.learning.policy.max_files)
            * (self.learning.model.input_limit + self.learning.policy.output_tokens)
            <= policy.maintenance_tokens
        )

    def _observe_cycle(self, cycle: CycleProgress, stage: str, report: dict) -> bool:
        self.last_report[stage] = report
        return cycle.observe(stage, WorkResult.from_report(report))

    async def _step(
        self, stream_id: str, idle: bool, budget: MaintenanceBudget, cycle: CycleProgress
    ) -> bool:
        policy = self.episodes.policy
        # Every unit yields; input cancellation runs before another batch.
        await asyncio.sleep(0)
        current = self.schedule_repository.state(stream_id)
        if self._foreground or current["run_active"]:
            return False
        now = self.clock.now()
        if budget.clock.monotonic() - budget.started >= policy.maintenance_seconds:
            budget.exhausted = True
            return False
        # Proactive claims a fresh cycle only. A waiting Learning pipeline
        # gets its whole reservation on Learning-first cycles.
        if (
            idle
            and self.proactive
            and not cycle.proactive_ran
            and not cycle.attempts
            and self.proactive.ready()
            and not (
                cycle.learning_first
                and cycle.learning_capacity
                and self.learning
                and self.learning.ready(idle=idle)
            )
        ):
            cycle.proactive_ran = True
            cycle.attempts += 1
            if not self._observe_cycle(
                cycle, "proactive", await self._execute("proactive", self.proactive.run())
            ):
                return False
            return True
        if idle and self.learning and not cycle.recovery_ran and self.learning.recovery_ready():
            cycle.recovery_ran = True
            cycle.attempts += 1
            self._observe_cycle(
                cycle,
                "memory_recovery",
                await self._execute("memory_recovery", self.learning.recover_background()),
            )
            return True
        if (
            idle
            and policy.enabled
            and self.threads
            and not (
                self._dirty_yield_to_learning
                and cycle.learning_capacity
                and self.learning
                and self.learning.ready(idle=idle)
            )
            and self.threads.repairs.pending(
                at=now, max_failures=self.threads.policy.repair_max_failures
            )
            is not None
        ):
            cycle.attempts += 1
            self._dirty_yield_to_learning = True
            if not self._observe_cycle(
                cycle,
                "repair",
                await self._execute("repair", self.threads.repair(retry=False, at=now)),
            ):
                return False
            return True
        if (
            self.learning
            and not cycle.learning_ran
            and self.learning.ready(idle=idle)
            and (cycle.learning_first or cycle.attempts > 0)
        ):
            # Reserve room for the complete in-memory pipeline before paying
            # for Extraction; no repeated partial pipelines across budget stops.
            needed = 2 + self.learning.policy.max_files
            tokens = needed * (self.learning.model.input_limit + self.learning.policy.output_tokens)
            if needed > policy.maintenance_requests or tokens > policy.maintenance_tokens:
                self.last_report["learning"] = {
                    "outcome": "blocked",
                    "error_code": "learning_budget_config",
                    "required_requests": needed,
                    "reserved_tokens_required": tokens,
                }
            if (
                budget.requests + needed <= policy.maintenance_requests
                and budget.reserved_tokens + tokens <= policy.maintenance_tokens
            ):
                cycle.learning_ran = True
                self._dirty_yield_to_learning = False
                cycle.attempts += 1
                report = await self._execute(
                    "learning",
                    self.learning.learn_priority()
                    if self.learning.priority_pending()
                    else self.learning.learn_batch(),
                )
                if not self._observe_cycle(cycle, "learning", report):
                    return False
                return True
        if (
            idle
            and self.curator
            and not cycle.curator_ran
            and self.curator.poll()
            and (
                not cycle.learning_first
                or cycle.attempts > 0
                or not (self.learning and self.learning.ready(idle=idle))
            )
            and budget.requests < policy.maintenance_requests
        ):
            cycle.curator_ran = True
            cycle.attempts += 1
            report = await self._execute("curator", self.curator.run())
            if not self._observe_cycle(cycle, "curator", report):
                return False
            return True
        if (
            idle
            and self.character_state
            and not cycle.character_ran
            and self.character_state.ready()
            and budget.requests < policy.maintenance_requests
            and (not cycle.learning_first or not (self.learning and self.learning.ready(idle=idle)))
        ):
            cycle.character_ran = True
            cycle.attempts += 1
            if not self._observe_cycle(
                cycle,
                "character_state",
                await self._execute("character_state", self.character_state.run()),
            ):
                return False
            return True
        return await self._organization_step(stream_id, current, idle, cycle)

    async def _organization_step(
        self, stream_id: str, current: dict, idle: bool, cycle: CycleProgress
    ) -> bool:
        policy = self.episodes.policy
        now = self.clock.now()
        if not policy.enabled:
            return False
        if self.threads:
            status = self.threads.repository.status()
            due = self._thread_remaining(current) <= 0
            if not status["pending_episodes_capped"] and due:
                self.schedule_repository.thread_check(now, policy.thread_interval_seconds)
            elif (
                idle
                and status["state"] not in {"failed", "running"}
                and (due or status["pending_episodes_capped"] >= policy.thread_threshold)
            ):
                cycle.attempts += 1
                report = await self._execute("thread", self.threads.organize(retry=False))
                if not self._observe_cycle(cycle, "thread", report):
                    return False
                if report["outcome"] == "completed":
                    self.schedule_repository.thread_check(
                        self.clock.now(),
                        policy.thread_interval_seconds,
                        success=True,
                    )
                return True
        # Finalization is time driven even if an unrelated/broken
        # pending batch needs explicit retry; it never asks the LLM.
        if idle and self.episodes.repository.finalize(stream_id, policy, at=now):
            cycle.batches += 1
            cycle.attempts += 1
            cycle.successes.add("episode")
            return True
        episode = self.episodes.repository.status(stream_id)
        if (
            episode["pending_runs_capped"]
            and episode["state"] not in {"failed", "running"}
            and (idle or episode["pending_runs_capped"] >= policy.run_threshold)
        ):
            cycle.attempts += 1
            if not self._observe_cycle(
                cycle,
                "episode",
                await self._execute(
                    "episode",
                    self.episodes.organize(stream_id, retry=False, at=now),
                ),
            ):
                return False
        else:
            return False
        return True

    async def _background(self, stream_id: str, after_answer: bool) -> None:
        try:
            p2 = bool(self.learning and self.learning.policy.enabled)
            if after_answer and not p2 and self.automation and self.automation.policy.mode != "off":
                self.last_report["memory"] = await self._execute(
                    "memory", self.automation.run(stream_id)
                )
            policy = self.episodes.policy
            p3 = bool(self.character_state and self.character_state.policy.enabled)
            p4 = bool(self.proactive and self.proactive.policy.enabled)
            if not policy.enabled and not p2 and not p3 and not p4:
                return
            while not self._foreground:
                if p2:
                    self.learning.poll()
                state = self.schedule_repository.state(stream_id)
                remaining = self._idle_remaining(state)
                cooldown = self._cooldown_remaining(state)
                if not state["run_active"] and not cooldown:
                    if await self._cycle(stream_id, state, remaining <= 0):
                        await self.clock.sleep(policy.cooldown_seconds)
                        continue
                delays = [policy.check_seconds]
                if p4:
                    delays.append(self.proactive.wake.delay(self.clock.now()))
                if remaining > 0:
                    delays.append(remaining)
                if cooldown:
                    delays.append(cooldown)
                if state["thread_due_at"]:
                    until_thread = self._thread_remaining(state)
                    if until_thread > 0:
                        delays.append(until_thread)
                await self.clock.sleep(min(delays))
        except asyncio.CancelledError:
            raise
        except (HistoryError, OrganizationError):
            self.last_report["scheduler"] = {"outcome": "storage_or_organization_error"}
        except Exception:
            self.last_report["scheduler"] = {"outcome": "runtime_error"}

    async def foreground(self, *, activity: bool = True) -> None:
        self._foreground = True
        task, self.task = self.task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if activity and self._stream_id:
            self.schedule_repository.activity(self._stream_id, self.clock.now())

    async def close(self) -> None:
        await self.foreground(activity=False)

    def diagnostics(self, stream_id: str, *, read_only: bool = False) -> dict:
        if read_only:
            return self._observe(stream_id)
        state = self.schedule_repository.state(
            stream_id, at=self.clock.now(), interval=self.episodes.policy.thread_interval_seconds
        )
        remaining = self._idle_remaining(state)
        episodes = self.episodes.repository.status(stream_id)
        threads = self.threads.repository.status() if self.threads else {}
        return {
            **state,
            "is_idle": not self._foreground and not state["run_active"] and remaining <= 0,
            "next_idle_at": (self.clock.now() + timedelta(seconds=remaining)).isoformat(),
            "next_thread_check_at": (
                self.clock.now() + timedelta(seconds=self._thread_remaining(state))
            ).isoformat(),
            "background_running": bool(self.task and not self.task.done() and not self._foreground),
            "foreground_active": self._foreground,
            "has_backlog": bool(
                episodes["pending_runs_capped"]
                or threads.get("pending_episodes_capped")
                or threads.get("dirty_threads_capped")
                or (
                    self.learning
                    and self.learning.policy.enabled
                    and (
                        self.learning.priority_pending()
                        or self.learning.repository.pending_job("batch")
                        or self.learning.repository.pending_episodes(1)
                    )
                )
            ),
            "maintenance_running": self.current_stage is not None,
            "current_stage": self.current_stage,
            "learning": self.learning.status() if self.learning else None,
            "learning_last_report": self.last_report.get("learning"),
            "character_state": self.character_state.status() if self.character_state else None,
            "curator": self.curator.status()
            if self.curator and self.learning.policy.enabled
            else None,
            "next_finalize_at": (
                datetime.fromisoformat(state["provisional_ended_at"])
                + timedelta(seconds=self.episodes.policy.revision_seconds)
            ).isoformat()
            if state["provisional_ended_at"]
            else None,
        }

    def _observe(self, stream_id: str) -> dict:
        """Observe existing metadata without ensure(), polling work or model requests."""
        return {
            **self.schedule_repository.observe(stream_id),
            "current_stage": self.current_stage,
            "maintenance_running": self.current_stage is not None,
            "background_task_active": bool(self.task and not self.task.done()),
            "learning_enabled": bool(self.learning and self.learning.policy.enabled),
            "curator_enabled": bool(self.curator and self.curator.policy.enabled),
        }
