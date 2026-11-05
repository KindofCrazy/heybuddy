"""UUID-independent pairwise scoring; no inferred action equivalences."""

from itertools import combinations
from statistics import mean

from personal_agent.evaluation.long_horizon.models import Prediction, ScenarioGold


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
    return {
        "pairwise": pair_scores(tp, fp, fn, len(pairs)),
        "eligible_turns": len(eligible),
        "gold_turns": len(gold.routing),
        "ambiguous_gold_turns": len(ambiguous),
        "prediction_abstentions": sum(p.ambiguity_flag for p in predictions),
        "clarified": clarified,
        "clarification_support": len(ambiguous),
        "clarification_rate": clarified / len(ambiguous) if ambiguous else None,
        "action_accuracy": None,
        "fragmentation": None,
        "cross_thread_contamination": None,
        "resume_qa_accuracy": None,
    }


def aggregate(rows: list[dict]) -> dict:
    pairs = [r["pairwise"] for r in rows]
    micro = pair_scores(*(sum(p[k] for p in pairs) for k in ("tp", "fp", "fn", "pair_support")))
    macro = {}
    for key in ("precision", "recall", "f1"):
        values = [p[key] for p in pairs if p[key] is not None]
        macro[key] = {"value": mean(values) if values else None, "scenario_support": len(values)}
    return {"micro": micro, "macro": macro, "scenario_support": len(rows)}
