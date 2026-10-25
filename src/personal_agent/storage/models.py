from datetime import datetime
from typing import Literal

from personal_agent.core.models import Message, Model, RunResult


class AgentRecord(Model):
    id: str
    name: str
    system_prompt: str
    created_at: datetime


class ConversationRecord(Model):
    id: str
    agent_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class StoredMessage(Model):
    id: str
    conversation_id: str
    run_id: str
    sequence: int
    content: Message
    created_at: datetime


class RunRecord(Model):
    id: str
    conversation_id: str
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    started_at: datetime
    ended_at: datetime | None
    result: RunResult | None
