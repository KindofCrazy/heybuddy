"""UUID-independent pairwise scoring; no inferred action equivalences."""

from collections import defaultdict
from itertools import combinations
from statistics import mean

from personal_agent.evaluation.long_horizon.models import Prediction, ScenarioGold

SCORER_VERSION = "routing-identity-v2"


def rate(numerator: int, support: int) -> dict:
    return {
        "numerator": numerator,
        "support": support,
        "rate": numerator / support if support else None,
    }


def relation_metrics(predictions: list[Prediction], gold: ScenarioGold) -> dict:
    """Check explicit past evidence edges, never align UUIDs using future gold.

    Consecutive failures measure observed persistence, not causal error attribution.
    Ambiguous/abstained/unreferenced turns break streaks and remain separately counted.
    """
    labels = {g.turn_id: g for g in gold.routing}
    seen = {}
    correct = edges = failures = evaluated = streak = max_streak = streaks = 0
    after_failure = opportunities = 0
    previous_failed = None
    for prediction in predictions:
        label = labels.get(prediction.turn_id)
        checks = []
        if label and not label.ambiguity and not prediction.ambiguity_flag:
            for reference in label.relevant_source_turn_ids:
                prior = seen.get(reference)
                prior_label = labels.get(reference)
                if prior and prior_label and not prior_label.ambiguity and not prior.ambiguity_flag:
                    checks.append(
                        (prediction.actual_thread_id == prior.actual_thread_id)
                        == (label.gold_topic_id == prior_label.gold_topic_id)
                    )
        failed = not all(checks) if checks else None
        if failed is not None:
            correct += sum(checks)
            edges += len(checks)
            evaluated += 1
            failures += failed
            if previous_failed is True:
                opportunities += 1
                after_failure += failed
        streak = streak + 1 if failed else 0
        streaks += streak == 2
        max_streak = max(max_streak, streak)
        previous_failed = failed
        seen[prediction.turn_id] = prediction
    return {
        "reference_relation_accuracy": rate(correct, edges),
        "error_propagation": {
            "definition": "adjacent annotated relation failures; association, not causation",
            "adjacent_failure_rate": rate(after_failure, opportunities),
            "failure_turns": failures,
            "evaluated_turn_support": evaluated,
            "max_consecutive_failed_turns": max_streak,
            "failure_streaks_ge2": streaks,
        },
    }


def pair_scores(tp: int, fp: int, fn: int, pairs: int) -> dict:
    return {
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "pair_support": pairs,
        "precision_support": tp + fp,
        "recall_support": tp + fn,
    }


def score(predictions: list[Prediction], gold: ScenarioGold) -> dict:
    by_id = {p.turn_id: p for p in predictions}
    eligible = [g for g in gold.routing if not g.ambiguity and not by_id[g.turn_id].ambiguity_flag]
    tp = fp = fn = 0
    pairs = list(combinations(eligible, 2))
    for a, b in pairs:
        same_gold = a.gold_topic_id == b.gold_topic_id
        same_actual = by_id[a.turn_id].actual_thread_id == by_id[b.turn_id].actual_thread_id
        tp += same_gold and same_actual
        fp += not same_gold and same_actual
        fn += same_gold and not same_actual
    ambiguous = [g for g in gold.routing if g.ambiguity]
    clarified = sum(by_id[g.turn_id].ambiguity_flag for g in ambiguous)
    required = [g for g in gold.routing if set(g.acceptable_actions) == {"clarify"}]
    forbidden = [g for g in gold.routing if "clarify" not in g.acceptable_actions]
    predicted = [g for g in gold.routing if by_id[g.turn_id].ambiguity_flag]
    misses = sum(not by_id[g.turn_id].ambiguity_flag for g in required)
    unnecessary = sum(by_id[g.turn_id].ambiguity_flag for g in forbidden)
    topic_threads = defaultdict(set)
    thread_topics = defaultdict(set)
    for g in eligible:
        topic_threads[g.gold_topic_id].add(by_id[g.turn_id].actual_thread_id)
        thread_topics[by_id[g.turn_id].actual_thread_id].add(g.gold_topic_id)
    return {
        "scorer_version": SCORER_VERSION,
        "pairwise": pair_scores(tp, fp, fn, len(pairs)),
        "eligible_turns": len(eligible),
        "gold_turns": len(gold.routing),
        "ambiguous_gold_turns": len(ambiguous),
        "prediction_abstentions": sum(p.ambiguity_flag for p in predictions),
        "clarified": clarified,
        "clarification_support": len(ambiguous),
        "clarification_rate": clarified / len(ambiguous) if ambiguous else None,
        "clarification": {
            "required_recall": rate(len(required) - misses, len(required)),
            "miss_rate": rate(misses, len(required)),
            "unnecessary_rate": rate(unnecessary, len(forbidden)),
            "precision": rate(
                sum("clarify" in g.acceptable_actions for g in predicted), len(predicted)
            ),
        },
        "routing_coverage": rate(len(eligible), sum(not g.ambiguity for g in gold.routing)),
        **relation_metrics(predictions, gold),
        "action_accuracy": None,
        "fragmentation": identity_groups(topic_threads),
        "cross_thread_contamination": identity_groups(thread_topics),
        "resume_qa_accuracy": None,
    }


def aggregate(rows: list[dict]) -> dict:
    pairs = [r["pairwise"] for r in rows]
    micro = pair_scores(*(sum(p[k] for p in pairs) for k in ("tp", "fp", "fn", "pair_support")))
    macro = {}
    for key in ("precision", "recall", "f1"):
        values = [p[key] for p in pairs if p[key] is not None]
        macro[key] = {"value": mean(values) if values else None, "scenario_support": len(values)}
    return {
        "scorer_version": SCORER_VERSION,
        "micro": micro,
        "macro": macro,
        "scenario_support": len(rows),
        "clarification": {
            key: aggregate_rate([r["clarification"][key] for r in rows])
            for key in ("required_recall", "miss_rate", "unnecessary_rate", "precision")
        },
        "routing_coverage": aggregate_rate([r["routing_coverage"] for r in rows]),
        "reference_relation_accuracy": aggregate_rate(
            [r["reference_relation_accuracy"] for r in rows]
        ),
        "fragmentation": aggregate_identity(rows, "fragmentation"),
        "cross_thread_contamination": aggregate_identity(rows, "cross_thread_contamination"),
        "error_propagation": {
            "adjacent_failure_rate": aggregate_rate(
                [r["error_propagation"]["adjacent_failure_rate"] for r in rows]
            ),
            "failure_turns": sum(r["error_propagation"]["failure_turns"] for r in rows),
            "evaluated_turn_support": sum(
                r["error_propagation"]["evaluated_turn_support"] for r in rows
            ),
            "max_consecutive_failed_turns": max(
                (r["error_propagation"]["max_consecutive_failed_turns"] for r in rows), default=0
            ),
            "failure_streaks_ge2": sum(r["error_propagation"]["failure_streaks_ge2"] for r in rows),
        },
    }


def aggregate_rate(rows: list[dict]) -> dict:
    values = [row["rate"] for row in rows if row["rate"] is not None]
    return {
        "micro": rate(sum(r["numerator"] for r in rows), sum(r["support"] for r in rows)),
        "macro": {"value": mean(values) if values else None, "scenario_support": len(values)},
    }


def identity_groups(groups: dict) -> dict:
    return {
        "mean_extra_assignments": rate(sum(len(v) - 1 for v in groups.values()), len(groups)),
        "affected_group_rate": rate(sum(len(v) > 1 for v in groups.values()), len(groups)),
    }


def aggregate_identity(rows: list[dict], key: str) -> dict:
    return {
        name: aggregate_rate([r[key][name] for r in rows])
        for name in ("mean_extra_assignments", "affected_group_rate")
    }
