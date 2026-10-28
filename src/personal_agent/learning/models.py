"""Bounded learning policy and target-grouped semantic proposals."""

from pydantic import Field

from personal_agent.core.models import Model
from personal_agent.reflection.models import Decision, ReflectionPolicy


class LearningPolicy(ReflectionPolicy):
    enabled: bool = True
    batch_episodes: int = Field(default=3, ge=1, le=3)
    max_wait_seconds: float = Field(default=21600, gt=0, le=604800)
    priority_enabled: bool = True
    source_bytes: int = Field(default=131072, ge=4096, le=524288)
    max_source_runs: int = Field(default=48, ge=1, le=48)
    max_files: int = Field(default=2, ge=1, le=3)
    recovery_attempts: int = Field(default=3, ge=1, le=8)
    recovery_interval_seconds: float = Field(default=300, gt=0, le=86400)
    recovery_seconds: float = Field(default=2, gt=0, le=10)


class Target(Model):
    path: str = Field(min_length=1, max_length=260)
    candidate_ids: list[int] = Field(min_length=1, max_length=8)


class Targets(Model):
    groups: list[Target] = Field(max_length=8)
    paths: list[str] = Field(default_factory=list, max_length=6)


class FileDecision(Model):
    action: str = Field(pattern="^(create|update|no_op|reject|review)$")
    old_text: str = Field(default="", max_length=8192)
    new_text: str = Field(default="", max_length=16384)
    reason: str = Field(min_length=1, max_length=1000)

    def decision(self, path: str, candidate_id: int) -> Decision:
        return Decision(
            candidate_id=candidate_id,
            action=self.action,
            path=path if self.action in {"create", "update"} else "",
            old_text=self.old_text,
            new_text=self.new_text,
            reason=self.reason,
        )
