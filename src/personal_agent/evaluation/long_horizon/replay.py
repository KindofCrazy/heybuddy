"""Isolated teacher-forced replies with streaming production routing decisions."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from personal_agent.core.models import AssistantMessage, RunResult, TextMessage
from personal_agent.evaluation.long_horizon.models import ReplayResult, Scenario, Snapshot
from personal_agent.evaluation.long_horizon.routing_eval import RoutingAdapter
from personal_agent.memory.store import MemoryFS
from personal_agent.storage.routing import RoutingRepository
from personal_agent.storage.sqlite import SQLiteStore


class OracleHistoryAdapter(Protocol):
    """Future host-only standard history constructor; never receives evaluation gold.

    Not accepted by streaming replay. A future runner must report oracle-history separately.
    """

    def prepare_history(self, store: SQLiteStore, scenario: Scenario, turn_index: int) -> None: ...


async def replay(scenario: Scenario, *, adapter: RoutingAdapter | None = None) -> ReplayResult:
    predictions, snapshots = [], []
    stage, run_id = "setup", None
    adapter = adapter or RoutingAdapter()
    try:
        with TemporaryDirectory(prefix="heybuddy-lh-") as temporary:
            root = Path(temporary)
            with SQLiteStore(root / "synthetic.sqlite3") as store:
                store.initialize()
                agent = store.create_agent("benchmark", "", agent_id="long-horizon-synthetic")
                memory = MemoryFS.for_agent(root / "memory", agent.id)
                memory.initialize()
                conversations = {}
                try:
                    for turn in scenario.turns:
                        await asyncio.sleep(
                            0
                        )  # Allow cancellation even with all-local rule routing.
                        stage = "journal"
                        if turn.conversation_key not in conversations:
                            conversations[turn.conversation_key] = store.create_conversation(
                                agent.id
                            )
                        conversation = conversations[turn.conversation_key]
                        run_id = store.begin_run(
                            conversation.id, TextMessage(role="user", text=turn.user_message)
                        )
                        # Evaluation clock adapter: only this fresh synthetic journal is updated.
                        with store._transaction() as db:
                            db.execute(
                                "UPDATE runs SET started_at=? WHERE id=?",
                                (turn.timestamp.isoformat(), run_id),
                            )
                            db.execute(
                                "UPDATE messages SET created_at=? WHERE run_id=?",
                                (turn.timestamp.isoformat(), run_id),
                            )
                        stage = "routing"
                        prediction = await adapter.route(
                            RoutingRepository(store, agent.id, conversation.id, turn.source),
                            run_id,
                            turn,
                            scenario.scenario_id,
                        )
                        stage = "journal"
                        store.append_message(
                            run_id, AssistantMessage(text=turn.frozen_assistant_message)
                        )
                        store.finish_run(run_id, RunResult(status="completed", rounds=1))
                        with store._transaction() as db:
                            db.execute(
                                "UPDATE runs SET ended_at=? WHERE id=?",
                                (turn.timestamp.isoformat(), run_id),
                            )
                            db.execute(
                                "UPDATE messages SET created_at=? WHERE run_id=?",
                                (turn.timestamp.isoformat(), run_id),
                            )
                        run_id = None
                        predictions.append(prediction)
                        stage = "checkpoint"
                        for checkpoint in scenario.checkpoints:
                            if checkpoint.after_turn_id == turn.turn_id:
                                snapshots.append(
                                    Snapshot(
                                        checkpoint_id=checkpoint.checkpoint_id,
                                        after_turn_id=turn.turn_id,
                                        completed_turn_ids=[p.turn_id for p in predictions],
                                        bindings={
                                            p.turn_id: p.actual_thread_id for p in predictions
                                        },
                                        memory_blocks=memory.read().blocks,
                                    )
                                )
                except BaseException as error:
                    if run_id:
                        store.interrupt_run(
                            run_id, cancelled=isinstance(error, asyncio.CancelledError)
                        )
                    raise
    except asyncio.CancelledError:
        raise  # Caller persists campaign cancellation; never converts it into success.
    except Exception as error:
        return ReplayResult(
            scenario_id=scenario.scenario_id,
            status="failed",
            predictions=predictions,
            snapshots=snapshots,
            error_stage=stage,
            error_code=type(error).__name__,
        )
    result = ReplayResult(
        scenario_id=scenario.scenario_id,
        status="completed",
        predictions=predictions,
        snapshots=snapshots,
    )
    result.validate_for(scenario)
    return result
