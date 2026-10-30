"""Typed interpretation of service reports and mutable state for one serial cycle."""

from dataclasses import dataclass, field
from enum import StrEnum


class WorkStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class WorkResult:
    status: WorkStatus
    error_code: str = ""

    @classmethod
    def from_report(cls, report: dict) -> "WorkResult":
        outcome = report.get("outcome")
        if outcome in {"completed", "finalized", "repaired", "repair_page"}:
            return cls(WorkStatus.COMPLETED)
        if outcome in {"failed", "requires_reconciliation"}:
            return cls(WorkStatus.FAILED, report.get("error_code") or str(outcome))
        if outcome == "budget_exhausted":
            return cls(WorkStatus.BUDGET_EXHAUSTED)
        # No work, stale, blocked, pending and future outcomes are never successes.
        return cls(WorkStatus.STOPPED)


@dataclass
class CycleProgress:
    learning_first: bool
    learning_capacity: bool
    batches: int = 0
    attempts: int = 0
    successes: set[str] = field(default_factory=set)
    error: str = ""
    time_limited: bool = False
    learning_ran: bool = False
    curator_ran: bool = False
    character_ran: bool = False
    recovery_ran: bool = False
    proactive_ran: bool = False

    def observe(self, stage: str, result: WorkResult) -> bool:
        if result.status in {WorkStatus.STOPPED, WorkStatus.BUDGET_EXHAUSTED}:
            return False
        self.batches += 1
        if result.status == WorkStatus.FAILED:
            self.error = result.error_code
        else:
            self.successes.add(stage)
        return True
