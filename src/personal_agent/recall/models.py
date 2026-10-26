from datetime import datetime
from typing import Literal
from unicodedata import category

from pydantic import Field, field_validator

from personal_agent.core.models import Model
from personal_agent.tools.registry import ToolArguments


class SearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=5, ge=1, le=5)

    @field_validator("query")
    @classmethod
    def literal_query(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value.encode("utf-8")) > 512:
            raise ValueError("Query is empty or too large")
        if any(category(char) in {"Cc", "Cs"} for char in value):
            raise ValueError("Query contains control characters")
        return value


class GetArguments(ToolArguments):
    message_id: str = Field(min_length=1, max_length=128)
    before: int = Field(default=1, ge=0, le=3)
    after: int = Field(default=1, ge=0, le=3)


class Evidence(Model):
    conversation_id: str = Field(max_length=128)
    message_id: str = Field(max_length=128)
    run_id: str = Field(max_length=128)
    role: Literal["user", "assistant"]
    run_status: Literal["completed", "failed"]
    created_at: datetime
    sequence: int = Field(ge=1)


class SearchHit(Evidence):
    snippet: str
    truncated: bool


class RecalledMessage(Evidence):
    text: str
    truncated: bool


class SearchResult(Model):
    kind: Literal["conversation_recall"] = "conversation_recall"
    trust: Literal["historical_data_only"] = "historical_data_only"
    mode: Literal["fts5_trigram", "recent_literal"]
    scope_limited: bool
    has_more: bool
    hits: list[SearchHit]


class GetResult(Model):
    kind: Literal["conversation_recall"] = "conversation_recall"
    trust: Literal["historical_data_only"] = "historical_data_only"
    anchor_message_id: str
    truncated: bool
    messages: list[RecalledMessage]
