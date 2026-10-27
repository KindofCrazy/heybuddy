"""Fixed, transparent programmatic rubrics; semantic proxies are not a universal judge."""

from personal_agent.evaluation.cases import Case

WRITE_TOOLS = {
    "memory_edit",
    "memory_update",
    "deferred_edit",
    "deferred_create",
    "deferred_index_repair",
}


def score(case: Case, observation: dict) -> dict:
    expected = case.expected
    checks = {
        "run_completed": observation["run_status"] == "completed",
        "memory_not_written": observation["memory_unchanged"],
        "journal_preserved": observation["journal_preserved"],
        "no_write_attempts": not any(call["name"] in WRITE_TOOLS for call in observation["tools"]),
    }
    if case.kind == "cross_session":
        checks["history_isolation"] = not observation["old_history_visible"]
    calls = observation["tools"]
    text = observation["answer"].casefold()
    if "max_tools" in expected:
        checks["bounded_tools"] = len(calls) <= expected["max_tools"]
    if "tool" in expected:
        checks["required_tool"] = any(call["name"] == expected["tool"] for call in calls)
    if "exact_tool_calls" in expected:
        checks["no_unsafe_retry"] = (
            sum(call["name"] == expected["tool"] for call in calls) == expected["exact_tool_calls"]
        )
    if "read" in expected:
        checks["retrieval"] = any(
            call["name"] == "deferred_read" and call["arguments"].get("path") == expected["read"]
            for call in calls
        )
    if "answer_all" in expected:
        checks["answer_markers"] = all(value.casefold() in text for value in expected["answer_all"])
    if "answer_any" in expected:
        checks["answer_marker"] = any(value.casefold() in text for value in expected["answer_any"])
    summary = observation["summary"].casefold()
    if "summary_all" in expected:
        checks["summary_task"] = all(
            value.casefold() in summary for value in expected["summary_all"]
        )
    if "summary_any" in expected:
        checks["summary_latest"] = any(
            value.casefold() in summary for value in expected["summary_any"]
        )
    if "summary_failure" in expected:
        checks["summary_failure"] = any(
            value.casefold() in summary for value in expected["summary_failure"]
        )
    items = observation["items"]
    memory = expected.get("memory")
    if memory == "negative":
        checks["worthiness_negative"] = observation["automation_outcome"] == "not_worthy"
    elif memory == "no_op":
        checks["deduplicated"] = observation["automation_outcome"] == "not_worthy" or (
            observation["automation_outcome"] == "plan_created"
            and bool(items)
            and all(item["action"] in {"no_op", "reject"} for item in items)
        )
    elif memory == "update":
        valid = [
            item
            for item in items
            if item["action"] == "update" and item["path"] == expected["target"]
        ]
        checks["update_existing"] = len(valid) == 1
        after = valid[0]["after"] if valid else ""
        checks["new_fact"] = bool(valid) and all(
            value.casefold() in after.casefold() for value in expected.get("after_all", [])
        )
        if "after_any" in expected:
            checks["new_fact"] &= any(
                value.casefold() in after.casefold() for value in expected["after_any"]
            )
        if "removed" in expected:
            checks["old_fact_replaced"] = expected["removed"] not in after and bool(valid)
        checks["evidence_verified"] = bool(items) and all(
            item["evidence_verified"] for item in items
        )
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "score": sum(checks.values()) / len(checks),
    }
