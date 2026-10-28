"""Pure disclosed history projections and exact source-quote validation."""

import re
from collections.abc import Mapping, Sequence

from pydantic import Field

from personal_agent.core.history import HistoryTurn
from personal_agent.core.models import AssistantMessage, Model, TextMessage, ToolMessage


class EvidenceError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Evidence(Model):
    conversation_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    quote: str = Field(min_length=4, max_length=512)


SECRET = re.compile(
    r"(?i)(?:\b(?:[A-Z_]*API[_ -]?KEY|ACCESS[_ -]?TOKEN|PASSWORD|SECRET)"
    r"[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])[^\s\"',}]+"
    r"|\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9_.-]{12,}"
)


def redact(text: str) -> str:
    return SECRET.sub("[REDACTED]", text)


def projection(turn: HistoryTurn) -> dict:
    evidence = []
    for message in turn.messages:
        if isinstance(message, ToolMessage):
            raw = redact(message.result.model_dump_json())
            evidence.append(
                {
                    "role": "tool",
                    "call_id": message.result.call_id,
                    "evidence": raw[:1024],
                    "truncated": len(raw) > 1024,
                }
            )
        else:
            item = {"role": message.role, "text": redact(message.text)}
            if isinstance(message, AssistantMessage):
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": redact(call.arguments)[:512],
                        "truncated": len(call.arguments) > 512,
                    }
                    for call in message.tool_calls
                ]
            evidence.append(item)
    return {
        "run_id": turn.run_id,
        "first_sequence": turn.first_sequence,
        "last_sequence": turn.last_sequence,
        "messages": evidence,
    }


def history_evidence(conversation_id: str, turns: list[HistoryTurn]) -> list[dict]:
    data = []
    for turn in turns:
        if turn.last_sequence - turn.first_sequence + 1 != len(turn.messages):
            raise EvidenceError("noncontiguous_source")
        item = projection(turn)
        item["conversation_id"] = conversation_id
        for sequence, message in enumerate(item["messages"], turn.first_sequence):
            message["sequence"] = sequence
        data.append(item)
    return data


def verify_quotes(refs: Sequence[Evidence], sources: Mapping[tuple[str, str], HistoryTurn]) -> bool:
    """Check disclosed quotes; return whether any citation is actual user evidence.

    Confirmation, approval, character attribution and time policies belong to callers.
    """
    user = False
    for ref in refs:
        turn = sources.get((ref.conversation_id, ref.run_id))
        if turn is None:
            raise EvidenceError("invalid_evidence")
        index = ref.sequence - turn.first_sequence
        if not 0 <= index < len(turn.messages):
            raise EvidenceError("invalid_evidence")
        disclosed = history_evidence(ref.conversation_id, [turn])[0]["messages"][index]
        if (
            ref.quote not in disclosed.get("text", disclosed.get("evidence", ""))
            or "[REDACTED]" in ref.quote
        ):
            raise EvidenceError("invalid_evidence")
        message = turn.messages[index]
        user |= isinstance(message, TextMessage) and message.role == "user"
    return user
