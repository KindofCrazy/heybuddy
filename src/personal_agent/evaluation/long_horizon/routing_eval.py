"""Adapter invoking the production router on an already running journal Run."""

from time import perf_counter

from personal_agent.evaluation.long_horizon.models import Prediction, Turn
from personal_agent.organization.router import ThreadRouter
from personal_agent.storage.routing import RoutingRepository


class NetworkForbidden:
    async def generate(self, *args, **kwargs):
        raise RuntimeError("Network and model requests are forbidden in this variant")


class RoutingAdapter:
    async def route(
        self,
        repository: RoutingRepository,
        run_id: str,
        turn: Turn,
        scenario_id: str,
    ) -> Prediction:
        previous = repository.active()
        with repository.store.reading() as db:
            known = {r[0] for r in db.execute("SELECT id FROM topic_threads")}
        router = ThreadRouter(repository, model_selection=False)
        start = perf_counter()
        result = await router.route(run_id, turn.user_message, NetworkForbidden())
        elapsed = (perf_counter() - start) * 1000
        with repository.store.reading() as db:
            bound = db.execute(
                "SELECT thread_id FROM run_threads WHERE run_id=?", (run_id,)
            ).fetchone()
            if bound is None or bound[0] != result.id:
                raise ValueError("Router failed to persist binding")
        return Prediction(
            scenario_id=scenario_id,
            turn_id=turn.turn_id,
            actual_thread_id=result.id,
            previous_thread_id=previous.id if previous else None,
            routing_reason=router.metrics["reason"],
            whether_new_thread_created=result.id not in known,
            ambiguity_flag=bool(router.metrics.get("ambiguous")),
            request_count=router.metrics["extra_requests"],
            latency_ms=elapsed,
        )
