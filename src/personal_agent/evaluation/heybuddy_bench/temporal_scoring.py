"""v0.3.3 distinguishes no contact now from an allowed later internal review."""

from datetime import datetime

from personal_agent.evaluation.heybuddy_bench.fact_scoring import score_facts


def score_temporal(track: str, gold: dict, prediction: dict) -> dict:
    result = score_facts(track, gold, prediction)
    if track != "proactive_decision" or prediction.get("action") != "defer":
        return result
    try:
        review = datetime.fromisoformat(
            prediction.get("effective_next_review_at") or prediction.get("next_review_at")
        )
        now = datetime.fromisoformat(gold["as_of"])
        floor = datetime.fromisoformat(gold.get("review_not_before", gold["as_of"]))
        valid = review > now and review >= floor
    except (ValueError, TypeError):
        valid = False
    valid = valid and set(prediction.get("proposal_ids", [])) == set(gold["proposal_ids"])
    result["review_time_valid"] = valid
    result["decision_correct"] = result["decision_correct"] and valid
    return result
