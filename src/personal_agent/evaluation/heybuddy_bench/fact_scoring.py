"""Versioned cross-file fact assertions over reconstructed MemoryFS state."""

from personal_agent.evaluation.heybuddy_bench.state_scoring import score_state


def score_facts(track: str, gold: dict, prediction: dict) -> dict:
    result = score_state(track, gold, prediction)
    if track != "memory_learning" or not gold.get("fact_assertions"):
        return result
    state = dict(gold.get("_memory_before", {}))
    for change in prediction.get("changes", []):
        state[change["path"]] = change["after"]
    passed = []
    for assertion in gold["fact_assertions"]:
        text = "\n".join(state.get(path, "") for path in assertion["paths"])
        passed.append(
            all(
                any(alias.casefold() in text.casefold() for alias in aliases)
                for aliases in assertion.get("all_any", [])
            )
            and all(old not in text for old in assertion.get("none_exact", []))
        )
    result["cross_file_assertions_pass"] = all(passed)
    result["decision_correct"] = result["decision_correct"] and all(passed)
    return result
