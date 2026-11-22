"""Read-only views distinguish background follow-ups from explicit reminder receipts."""

from datetime import UTC, datetime, timedelta

from personal_agent.scheduler.models import ReminderGrant, ReminderRequest
from personal_agent.storage.reminders import ReminderRepository
from tests.test_dashboard import observatory

__all__ = ["observatory"]


async def test_proactive_status_reports_actual_timing_without_advancing_work(observatory):
    client, store, service, provider, _ = observatory
    policy = service.organization.episodes.policy
    policy.idle_seconds, policy.check_seconds, policy.cooldown_seconds = 120, 15, 5
    before = store._connection.total_changes
    status = (await client.get("/api/proactive/status")).json()
    assert status["maintenance"] == dict(idle_seconds=120, check_seconds=15, cooldown_seconds=5)
    assert status["reminders"]["delivery_available"] is False
    assert status["reminders"]["running"] is False
    assert store._connection.total_changes == before and not provider.requests


async def test_reminder_history_shows_real_and_simulated_receipts_with_scoped_pagination(
    observatory,
):
    client, store, service, provider, _ = observatory
    runtime = service.scheduler
    now = datetime.now(UTC)
    conversation = service.sessions.current.id
    items = []
    for index, mode in enumerate([None, "napcat", "simulation"]):
        item = runtime.create(
            ReminderRequest(
                request_id=f"preview-{index}",
                conversation_id=conversation,
                source_scope="chat:synthetic",
                due_at=now + timedelta(minutes=3),
                text="晾衣服" if mode == "napcat" else "合成提醒",
            )
        )["reminder"]
        if mode:
            at = now + timedelta(minutes=3)
            grant = ReminderGrant(
                enabled=True,
                channel=mode,
                conversation_id=conversation,
                source_scope="chat:synthetic",
                recipient="synthetic-peer",
            )
            assert runtime.repository.claim(item, grant, at)
            runtime.repository.finish(item["id"], "accepted", at, receipt_id=f"receipt-{mode}")
        items.append(item)
    other = store.create_agent("Other", "prompt", agent_id="other-reminders")
    other_conversation = store.create_conversation(other.id)
    ReminderRepository(store, other.id).create(
        ReminderRequest(
            request_id="foreign",
            conversation_id=other_conversation.id,
            due_at=now + timedelta(minutes=3),
            text="foreign private reminder",
        ),
        runtime.policy,
        now,
    )
    before = store._connection.total_changes
    rows = []
    cursor = None
    for _ in range(3):
        response = await client.get(
            "/api/reminders", params={"limit": 1, **({"cursor": cursor} if cursor else {})}
        )
        assert response.status_code == 200 and "foreign" not in response.text
        page = response.json()
        rows.extend(page["items"])
        cursor = page["next_cursor"]
    assert [row["id"] for row in rows] == [item["id"] for item in reversed(items)]
    assert rows[0]["mode"] == "simulation" and rows[0]["receipt_id"] == "receipt-simulation"
    assert rows[1]["mode"] == "napcat" and rows[1]["receipt_id"] == "receipt-napcat"
    assert rows[1]["text"] == "晾衣服" and rows[1]["ended_at"] is not None
    assert rows[2]["status"] == "scheduled" and rows[2]["receipt_id"] is None
    assert not page["has_more"] and cursor is None
    assert store._connection.total_changes == before and not provider.requests
    assert not any("recipient" in row or "request_id" in row for row in rows)


async def test_reminder_view_is_read_only_bounded_and_available_in_chinese_page(observatory):
    client, store, _, provider, _ = observatory
    before = store._connection.total_changes
    for params in ({"limit": 21}, {"limit": 0}, {"cursor": 0}, {"cursor": "x"}):
        assert (await client.get("/api/reminders", params=params)).status_code == 422
    assert (await client.post("/api/reminders", json={})).status_code == 405
    assert (await client.get("/api/reminders", headers={"host": "evil.example"})).status_code == 403
    response = await client.get("/proactive")
    assert "明确提醒" in response.text and "关心消息投递" in response.text
    assert "扫描与判断" in response.text and "关注与提醒" in response.text
    assert store._connection.total_changes == before and not provider.requests
