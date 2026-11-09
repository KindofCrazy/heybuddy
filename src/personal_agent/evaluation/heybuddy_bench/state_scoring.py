"""v0.3 bounded state assertions; deliberately not a general semantic judge."""

from pathlib import Path

from personal_agent.core.memory import CoreMemoryError
from personal_agent.evaluation.heybuddy_bench.scoring import score_case
from personal_agent.memory.store import MemoryFS


def valid_topic(path: str) -> bool:
    try:
        MemoryFS(Path(".cache/state-path-validation")).path(path)
    except CoreMemoryError:
        return False
    return "/" in path and path.endswith(".md") and not path.endswith("/MEMORY.md")


def score_state(track: str, gold: dict, prediction: dict) -> dict:
    original = score_case(track, gold, prediction)
    if track != "memory_learning" or gold["action"] not in {"create", "update"}:
        return original
    changes = prediction.get("changes", [])
    options = [gold] + [{**gold, **alt} for alt in gold.get("storage_alternatives", [])]
    scored = []
    for option in options:
        action = option["action"]
        acceptable = prediction.get("action") in option.get("acceptable_actions", [action])
        # An alternative action is explicit, not inherited from the original label.
        if option is not gold:
            acceptable = prediction.get("action") == action
        matched = [
            change
            for change in changes
            if (
                valid_topic(change["path"])
                if action == "create"
                else change["path"] == option["target_path"]
            )
        ]
        text = "\n".join(c["after"] for c in matched)
        groups = gold.get("content_checks", [])
        content = all(
            any(alias.casefold() in text.casefold() for alias in group) for group in groups
        )
        old = gold.get("old_text", "")
        obsolete = bool(old and old not in gold.get("new_text", ""))
        removed = not obsolete or old not in text
        preserved = all(
            term.casefold() in text.casefold() for term in gold.get("preserve_terms", [])
        )
        allowed = {c["path"] for c in matched} | set(gold.get("optional_extra_paths", []))
        scope = all(c["path"] in allowed for c in changes)
        target = bool(matched) and (action != "create" or len(matched) == 1)
        scored.append(
            dict(
                original,
                acceptable_action_correct=acceptable,
                target_correct=target,
                content_check_pass=content,
                obsolete_text_removed=removed,
                unrelated_files_preserved=scope and preserved,
                decision_correct=acceptable
                and target
                and content
                and removed
                and scope
                and preserved,
            )
        )
    return max(
        scored,
        key=lambda score: (
            score["decision_correct"],
            score["acceptable_action_correct"],
            score["target_correct"],
        ),
    )
