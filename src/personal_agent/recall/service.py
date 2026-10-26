"""Validation, output budgets and a host-bound identity for conversation evidence."""

import json

from pydantic import ValidationError

from personal_agent.core.history import HistoryError
from personal_agent.core.models import Model
from personal_agent.recall.models import (
    GetArguments,
    GetResult,
    RecalledMessage,
    SearchArguments,
    SearchHit,
    SearchResult,
)
from personal_agent.storage.sqlite import SQLiteStore

OUTPUT_BYTES = 12288
EXPAND_TEXT_BYTES = 8192


class RecallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def clip_text(text: str, budget: int) -> str:
    """Bound both UTF-8 and JSON-escaped text, including pathological control characters."""
    result = []
    used = 0
    for char in text:
        size = len(json.dumps(char, ensure_ascii=False)[1:-1].encode("utf-8"))
        if used + size > budget:
            break
        used += size
        result.append(char)
    return "".join(result)


def bounded[Result: Model](result: Result) -> Result:
    if len(result.model_dump_json().encode("utf-8")) > OUTPUT_BYTES:
        raise RecallError("recall_unavailable", "Recall evidence exceeds its output budget.")
    return result


class Recall:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self._store = store
        self._agent_id = agent_id

    def search(self, query: str, limit: int = 5) -> SearchResult:
        try:
            args = SearchArguments(query=query, limit=limit)
        except ValidationError:
            raise RecallError("invalid_query", "Recall query or limit is invalid.") from None
        try:
            rows = self._store.search_recall(self._agent_id, args.query, args.limit)
            hits = []
            for row in rows[: args.limit]:
                data = dict(row)
                snippet = clip_text(data.pop("text"), 512)
                original_bytes = data.pop("original_bytes")
                hits.append(
                    SearchHit(
                        **data,
                        snippet=snippet,
                        truncated=original_bytes > len(snippet.encode("utf-8")),
                    )
                )
            return bounded(
                SearchResult(
                    mode="fts5_trigram" if len(args.query) >= 3 else "recent_literal",
                    scope_limited=len(args.query) < 3,
                    has_more=len(rows) > args.limit,
                    hits=hits,
                )
            )
        except (HistoryError, ValidationError, UnicodeError):
            raise RecallError(
                "recall_unavailable",
                "Recall could not be read; check storage or explicitly rebuild its index.",
            ) from None

    def get(self, message_id: str, before: int = 1, after: int = 1) -> GetResult:
        try:
            args = GetArguments(message_id=message_id, before=before, after=after)
        except ValidationError:
            raise RecallError("invalid_query", "Recall identifier or window is invalid.") from None
        try:
            rows = self._store.get_recall(self._agent_id, args.message_id, args.before, args.after)
            if not rows:
                # Do not reveal whether an inaccessible ID belongs to another agent/incomplete run.
                raise RecallError(
                    "recall_not_found", "No accessible ended text message with this ID."
                )
            per_message = min(2048, EXPAND_TEXT_BYTES // len(rows))
            messages = []
            for row in rows:
                data = dict(row)
                text = clip_text(data.pop("text"), per_message)
                original_bytes = data.pop("original_bytes")
                messages.append(
                    RecalledMessage(
                        **data, text=text, truncated=original_bytes > len(text.encode("utf-8"))
                    )
                )
            return bounded(
                GetResult(
                    anchor_message_id=args.message_id,
                    truncated=any(message.truncated for message in messages),
                    messages=messages,
                )
            )
        except (HistoryError, ValidationError, UnicodeError):
            raise RecallError("recall_unavailable", "Recall context could not be read.") from None
