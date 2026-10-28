"""Memory candidate confirmation policy over shared exact-quote checks."""

from personal_agent.core.evidence import EvidenceError, history_evidence, verify_quotes
from personal_agent.core.history import HistoryTurn
from personal_agent.reflection.models import Extraction, ReflectionError


def memory_evidence(conversation_id: str, turns: list[HistoryTurn]) -> list[dict]:
    try:
        return history_evidence(conversation_id, turns)
    except EvidenceError as error:
        raise ReflectionError(error.code) from None


def verify_candidates(extraction: Extraction, sources: dict[tuple[str, str], HistoryTurn]) -> None:
    for candidate in extraction.candidates:
        try:
            user = verify_quotes(candidate.evidence, sources)
        except EvidenceError as error:
            raise ReflectionError(error.code) from None
        if candidate.confidence == "confirmed" and not user:
            raise ReflectionError("unconfirmed_evidence")
