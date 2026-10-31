"""Covered check-ins are persistent work state, not completed tasks or reminder cancellation."""

from datetime import datetime, timedelta

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.proactive.models import FollowupReview, ProposalChange, ScoutResult
from personal_agent.scheduler.models import ReminderRequest, SchedulerPolicy
from personal_agent.scheduler.runtime import SchedulerRuntime
from personal_agent.storage import sqlite as storage
from personal_agent.storage.reminders import ReminderRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_episodes import seed, state
from tests.test_outbound import PipelineProvider, permission, pipeline
from tests.test_proactive import proactive

__all__ = ["state"]


class ProgressProvider(PipelineProvider):
    def __init__(self):
        super().__init__()
        self.mode = "keep"
        self.mutate = self.review

    def review(self, result, data):
        if "users" not in data or not data["existing"]:
            return
        source = data["users"][0]
        ref = {k: source[k] for k in ("conversation_id", "run_id", "sequence")}
        ref["quote"] = source["text"][:512]
        if self.mode in {"update", "new_plan"}:
            result["changes"][0]["followup_intent"] = self.mode
            if self.mode == "update":
                result["changes"][0]["followup_intent"] = "update"
        else:
            result["changes"] = []
        if self.mode == "keep":
            return
        if self.mode == "omit":
            result["followups"] = []
            return
        if self.mode in {"update", "new_plan"}:
            return
        for review in result["followups"]:
            review.update(outcome="covered", sources=[ref])
        if self.mode == "foreign":
            result["followups"][0]["proposal_id"] = "foreign-proposal"
        elif self.mode == "revision":
            result["followups"][0]["proposal_revision"] += 1
        elif self.mode == "quote":
            result["followups"][0]["sources"][0]["quote"] = "助手自己想出来的进展"
        elif self.mode == "source":
            result["followups"][0]["sources"][0]["run_id"] = "foreign-run"
        elif self.mode == "duplicate":
            result["followups"].append(result["followups"][0])


async def scene(state, *, social=False):
    store, conversation, _ = state
    service, outbound = pipeline(store, conversation)
    provider = ProgressProvider()
    service.model.provider = provider
    service.policy.social_followup_enabled = social
    seed(store, conversation, "我明天准备跑实验。")
    assert (await service.run())["stage"] == "scout"
    return service, outbound, provider


async def share(state, service, provider, text="实验结果出来了。"):
    store, conversation, _ = state
    provider.mode = "covered"
    seed(store, conversation, text)
    return await service.run()


async def test_progress_before_evening_covers_only_this_checkin_and_survives_restart(state):
    store, conversation, _ = state
    service, _, provider = await scene(state, social=True)
    before = service.repository.candidates()[0]
    assert datetime.fromisoformat(before["next_review_at"]) > service.clock.now()
    assert (await share(state, service, provider))["outcome"] == "completed"
    covered = service.repository.candidates()[0]
    assert covered["status"] == "open"  # Progress sharing does not claim the experiment finished.
    assert covered["followup_state"] == "covered" and covered["last_action"] == "covered"
    assert covered["next_review_at"] is None
    assert covered["covered_sources"][0]["quote"] == "实验结果出来了。"
    assert covered["covered_position"] > before["source_position"]
    restarted = proactive(store, provider, social_followup_enabled=True)
    restarted.clock.time = datetime.fromisoformat(before["next_review_at"])
    assert (await restarted.run())["outcome"] == "no_proactive_work"
    assert restarted.repository.candidates()[0]["followup_state"] == "covered"
    assert len(provider.requests) == 2 and len(store.list_runs(conversation)) == 2


@pytest.mark.parametrize(
    "progress",
    [
        "结果出来了",
        "跑完了",
        "已经开始跑了，先等结果",
        "刚跑了一半，今天有些新发现",
        "还没开始，今天太忙了",
        "结果还没出来，先等着",
    ],
)
async def test_actual_progress_does_not_need_to_be_a_completed_task(state, progress):
    service, _, provider = await scene(state)
    await share(state, service, provider, progress)
    current = service.repository.candidates()[0]
    assert current["followup_state"] == "covered" and current["status"] == "open"


async def test_unrelated_or_unclear_message_keeps_original_window(state):
    store, conversation, _ = state
    service, _, _ = await scene(state, social=True)
    before = service.repository.candidates()[0]
    seed(store, conversation, "今天午饭挺好吃的。")
    assert (await service.run())["outcome"] == "completed"
    after = service.repository.candidates()[0]
    assert after == before


async def test_progress_covers_matching_plan_without_cancelling_another_topic(state):
    store, conversation, _ = state
    service, _, provider = await scene(state, social=True)
    experiment = service.repository.candidates()[0]

    def movie(result, data):
        if "users" in data:
            result["changes"][0].update(topic_key="movie", title="看电影")

    provider.mutate = movie
    seed(store, conversation, "我明天打算去看电影。")
    await service.run()
    movie_before = next(p for p in service.repository.candidates() if p["topic_key"] == "movie")

    def progress(result, data):
        if "users" not in data:
            return
        result["changes"] = []
        user = data["users"][0]
        evidence = {k: user[k] for k in ("conversation_id", "run_id", "sequence")}
        evidence["quote"] = user["text"]
        for review in result["followups"]:
            if review["proposal_id"] == experiment["id"]:
                review.update(outcome="covered", sources=[evidence])

    provider.mutate = progress
    seed(store, conversation, "实验结果出来了。")
    await service.run()
    current = {p["topic_key"]: p for p in service.repository.candidates()}
    assert current["movie"] == movie_before
    assert current[experiment["topic_key"]]["followup_state"] == "covered"


async def test_changed_target_during_scout_cannot_be_covered_with_old_revision(state):
    store, conversation, _ = state
    service, _, provider = await scene(state)
    before = service.repository.state()["run_cursor"]

    def race(result, data):
        provider.mode = "covered"
        provider.review(result, data)
        with store._transaction() as db:
            db.execute("UPDATE proactive_proposals SET revision=revision+1")

    provider.mutate = race
    seed(store, conversation, "实验结果出来了。")
    assert (await service.run())["outcome"] == "failed"
    assert service.repository.state()["run_cursor"] == before
    assert service.repository.candidates()[0]["followup_state"] == "pending"


async def test_coverage_cancels_prepared_draft_without_running_composer_again(state):
    service, outbound, provider = await scene(state)
    await service.run()  # Attention notify.
    result = await service.run()  # Composer.
    item = outbound.repository.get(result["outbox_id"])
    assert item["status"] == "prepared"
    await share(state, service, provider)
    current = outbound.repository.get(item["id"])
    assert current["status"] == "cancelled" and current["error_code"] == "followup_covered"
    assert not service.repository.due(service.clock.now(), 4)
    assert (await service.run())["outcome"] == "no_proactive_work"


async def test_composer_keeps_full_role_but_omits_duplicate_passive_chat_instructions(state):
    import json

    from personal_agent.core.prompts import CONVERSATION_PROMPT

    service, outbound, provider = await scene(state)
    await service.run()
    assert (await service.run())["outcome"] == "completed"
    payload = json.loads(provider.requests[-1][1].text)
    _, original_style = outbound.persona()
    assert (
        payload["persona_style"][0]["text"] + "\n\n" + CONVERSATION_PROMPT
        == original_style[0]["text"]
    )
    assert payload["proposal"]["sources"][0]["quote"] == "我明天准备跑实验。"


async def test_coverage_before_composer_consumes_old_decision_without_model_call(state):
    service, outbound, provider = await scene(state)
    await service.run()  # notify audit exists, no draft yet.
    await share(state, service, provider)
    count = len(provider.requests)
    assert (await service.run())["outcome"] == "stale"
    assert len(provider.requests) == count
    assert outbound.repository.listing("outbox")["items"][0]["status"] == "cancelled"


async def test_accepted_simulation_remains_a_historical_receipt(state):
    from personal_agent.proactive.delivery import FakeSender

    _, conversation, _ = state
    service, outbound, provider = await scene(state)
    await service.run()
    item = (await service.run())["outbox_id"]
    result = await outbound.simulate(item, FakeSender(), lambda: permission(conversation))
    assert result["outcome"] == "accepted"
    # A new progress update must not rewrite a historical accepted attempt as retracted.
    await share(state, service, provider)
    assert outbound.repository.get(item)["status"] == "accepted"


@pytest.mark.parametrize("action", ["silent", "propose_task", "invalid-action"])
async def test_later_progress_covers_paused_or_silent_checkin_before_retry(state, action):
    service, _, provider = await scene(state)
    provider.action = action
    await service.run()
    assert service.repository.candidates()[0]["next_review_at"] is None
    await share(state, service, provider)
    assert service.repository.candidates()[0]["followup_state"] == "covered"
    service.retry()
    assert not service.repository.due(service.clock.now(), 4)


async def test_explicit_reminder_is_unchanged_by_social_coverage(state):
    store, conversation, _ = state
    service, _, provider = await scene(state)
    scheduler = SchedulerRuntime(
        ReminderRepository(store, service.repository.agent_id), SchedulerPolicy()
    )
    reminder = scheduler.create(
        ReminderRequest(
            request_id="explicit",
            conversation_id=conversation,
            due_at=scheduler.clock.now() + timedelta(hours=1),
            text="检查实验结果",
        )
    )["reminder"]
    await share(state, service, provider)
    assert scheduler.repository.get(reminder["id"]) == reminder


async def test_ordinary_update_cannot_reopen_but_new_plan_can(state):
    store, conversation, _ = state
    service, _, provider = await scene(state, social=True)
    await share(state, service, provider)
    covered = service.repository.candidates()[0]
    provider.mode = "update"
    seed(store, conversation, "我再补充一下这次实验的数据，数值还需要检查。")
    await service.run()
    assert service.repository.candidates()[0]["followup_state"] == "covered"
    provider.mode = "new_plan"
    seed(store, conversation, "我明天准备再跑一组实验。")
    await service.run()
    new = service.repository.candidates()[0]
    assert new["id"] == covered["id"] and new["followup_state"] == "pending"
    assert new["source_position"] > covered["covered_position"]
    assert new["next_review_at"] is not None


async def test_reopening_covered_plan_obeys_pending_capacity(state):
    import json

    store, conversation, _ = state
    service, _, provider = await scene(state)
    await share(state, service, provider)
    covered = service.repository.candidates()[0]
    with store._transaction() as db:
        for index in range(48):
            db.execute(
                "INSERT INTO proactive_proposals "
                "(id,agent_id,topic_key,title,context,sources,source_position,status,revision,"
                "next_review_at,expires_at,updated_at) VALUES (?,?,?,?,?,?,?,'open',1,?,?,?)",
                (
                    f"capacity-{index}",
                    service.repository.agent_id,
                    f"capacity-{index}",
                    covered["title"],
                    covered["context"],
                    json.dumps(covered["sources"]),
                    covered["source_position"],
                    covered["expires_at"],
                    covered["expires_at"],
                    covered["updated_at"],
                ),
            )
    provider.mode = "new_plan"
    seed(store, conversation, "我明天准备再跑一次实验。")
    assert (await service.run())["outcome"] == "completed"
    with store.reading() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM proactive_proposals WHERE followup_state='pending'"
        ).fetchone()[0]
    assert count == 48
    current = next(p for p in service.repository.candidates() if p["id"] == covered["id"])
    assert current["followup_state"] == "covered"


async def test_late_raw_history_cannot_reopen_coverage_or_suppress_a_new_plan(state):
    store, conversation, _ = state
    service, _, provider = await scene(state, social=True)
    provider.mode = "covered"
    seed(store, conversation, "实验结果出来了。")
    old_sources = service.repository.snapshot(service.policy)["users"]
    await service.run()
    covered = service.repository.candidates()[0]
    # A Thread-style disclosure can contain older raw evidence after the live Run lane advanced.
    seed(store, conversation, "另一段不相关的聊天")
    snapshot = service.repository.snapshot(service.policy)
    snapshot["users"] = old_sources
    ref = old_sources[0]
    evidence = dict(
        conversation_id=ref.conversation_id,
        run_id=ref.run_id,
        sequence=ref.sequence,
        quote=ref.text,
    )
    result = ScoutResult(
        changes=[
            ProposalChange(
                topic_key=covered["topic_key"],
                title=covered["title"],
                context=covered["context"],
                sources=[evidence],
                followup_intent="new_plan",
            )
        ]
    )
    service.repository.publish(snapshot, result, service.policy, service.clock.now())
    assert service.repository.candidates()[0]["followup_state"] == "covered"
    provider.mode = "new_plan"
    seed(store, conversation, "我明天再跑一次实验。")
    await service.run()
    target = service.repository.candidates()[0]
    seed(store, conversation, "Thread 风格的旧证据核对")
    snapshot = service.repository.snapshot(service.policy)
    snapshot["users"], snapshot["review_targets"] = old_sources, [target]
    service.repository.publish(
        snapshot,
        ScoutResult(
            followups=[
                FollowupReview(
                    proposal_id=target["id"],
                    proposal_revision=target["revision"],
                    outcome="covered",
                    sources=[evidence],
                )
            ]
        ),
        service.policy,
        service.clock.now(),
    )
    assert service.repository.candidates()[0] == target


@pytest.mark.parametrize("mode", ["omit", "foreign", "revision", "quote", "source", "duplicate"])
async def test_missing_forged_or_wrong_revision_review_stops_without_advancing(state, mode):
    store, conversation, _ = state
    service, _, provider = await scene(state)
    before = service.repository.state()["run_cursor"]
    provider.mode = mode
    seed(store, conversation, "实验结果出来了。")
    assert (await service.run())["outcome"] == "failed"
    assert service.repository.state()["run_cursor"] == before
    assert service.repository.candidates()[0]["followup_state"] == "pending"
    assert service.gate() == "failed"


async def test_coverage_publication_rolls_back_draft_and_progress_together(state):
    store, _, _ = state
    service, outbound, provider = await scene(state)
    await service.run()
    item = (await service.run())["outbox_id"]
    before = service.repository.candidates()[0]
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_coverage BEFORE INSERT ON proactive_decisions "
            "WHEN NEW.action='covered' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    assert (await share(state, service, provider))["outcome"] == "failed"
    assert service.repository.candidates()[0] == before
    assert outbound.repository.get(item)["status"] == "prepared"
    assert service.repository.state()["error_code"] == "publication_failed"


def test_schema_21_to_22_atomic_upgrade_and_failure_rollback(tmp_path, monkeypatch):
    path = tmp_path / "coverage.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 21)
        with SQLiteStore(path) as db:
            db.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 22, (*storage.MIGRATIONS[22], "INVALID SQL"))
        with SQLiteStore(path) as db:
            with pytest.raises(HistoryError):
                db.initialize()
            assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 21
            assert "followup_state" not in {
                row[1] for row in db._connection.execute("PRAGMA table_info(proactive_proposals)")
            }
    with SQLiteStore(path) as db:
        db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 22
