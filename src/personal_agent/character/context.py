"""Bounded character system layer and optional canonical references for the existing Agent."""

import json
from collections.abc import Callable
from typing import Literal

from personal_agent.character.models import CharacterError, CharacterPackage, CharacterPolicy
from personal_agent.character.prompt import compile_system, select_examples
from personal_agent.core.history import HistoryError
from personal_agent.core.models import TextMessage
from personal_agent.memory.retrieval import terms


def size(messages: list[TextMessage]) -> int:
    return len(json.dumps([m.model_dump() for m in messages], ensure_ascii=False).encode())


class CharacterContextCompiler:
    def __init__(
        self,
        package: CharacterPackage,
        policy: CharacterPolicy | None = None,
        *,
        variant: Literal["E0", "E1", "E2"] = "E2",
        state: Callable[[], dict | None] | None = None,
        on_run: Callable[[str], None] | None = None,
        source_scope: Callable[[tuple[str, ...]], dict] | None = None,
    ) -> None:
        self.package = package
        self.policy = policy or CharacterPolicy()
        self.variant = variant
        self.state = state
        self.on_run = on_run
        self.source_scope = source_scope
        self.source_tag_error = False
        self._last = {}
        longest = sorted(package.examples, key=lambda s: len(s.text.encode()), reverse=True)
        worst = self._static(longest[: self.policy.example_count])
        if size([worst]) > self.policy.max_context_bytes:
            raise CharacterError("character_base_budget")

    def _static(self, examples: list) -> TextMessage:
        return TextMessage(
            role="system",
            text=compile_system(self.package, examples, include_behavior=self.variant != "E0"),
        )

    def compile(
        self, user_input: str, *, source_run_ids: tuple[str, ...] = ()
    ) -> list[TextMessage]:
        words = [word for word in terms(user_input) if word not in {"关系", "其他", "解释", "用户"}]
        chosen = select_examples(self.package.examples, user_input, self.policy.example_count)
        messages = [self._static(chosen)]
        if size(messages) > self.policy.max_context_bytes:
            raise CharacterError("character_base_budget")
        data = {
            "kind": "character_context",
            "trust": "reference_data_only",
            "character_id": self.package.character_id,
            "canonical_backstory_not_shared_history": [],
        }
        scope = None
        if source_run_ids and self.source_scope:
            try:
                scope = self.source_scope(source_run_ids[:32])
            except HistoryError:
                scope = {"status": "unavailable_assume_unattributed"}
            data["source_role_participation"] = scope
        state = self.state() if self.state and self.variant == "E2" else None
        if state:
            data["transient_simulated_state"] = state
        ranked = sorted(
            self.package.lore,
            key=lambda section: -sum(word in section.title.lower() for word in words),
        )
        omitted = 0
        for section in ranked:
            if (
                not any(word in section.title.lower() for word in words)
                or len(data["canonical_backstory_not_shared_history"]) >= self.policy.lore_sections
            ):
                continue
            trial = {
                **data,
                "canonical_backstory_not_shared_history": [
                    *data["canonical_backstory_not_shared_history"],
                    {"source": "lore.md", "heading": section.title, "text": section.text},
                ],
            }
            optional = TextMessage(role="user", text=json.dumps(trial, ensure_ascii=False))
            if (
                size([optional]) > self.policy.contextual_bytes
                or size([*messages, optional]) > self.policy.max_context_bytes
            ):
                omitted += 1
                continue
            data = trial
        if state or scope or data["canonical_backstory_not_shared_history"]:
            optional = TextMessage(role="user", text=json.dumps(data, ensure_ascii=False))
            if (
                size([optional]) <= self.policy.contextual_bytes
                and size([*messages, optional]) <= self.policy.max_context_bytes
            ):
                messages.append(optional)
            else:
                omitted += 1
        self._last = {
            "compiled_bytes": size(messages),
            "lore_omitted": omitted,
            "contextual_injected": len(messages) > 1,
            "state_available": bool(state),
            "extra_appraisal_requests": 0,
        }
        return messages

    def diagnostics(self) -> dict:
        return {
            "character_id": self.package.character_id,
            "variant": self.variant,
            "package_hash": self.package.fingerprint,
            "max_bytes": self.policy.max_context_bytes,
            **self._last,
            "source_tag_error": self.source_tag_error,
        }

    def record_run(self, run_id: str) -> None:
        if self.on_run:
            try:
                self.on_run(run_id)
                self.source_tag_error = False
            except HistoryError:
                # Untagged runs never become own-character state evidence.
                self.source_tag_error = True
