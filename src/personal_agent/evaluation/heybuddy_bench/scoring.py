"""Decision/effect scoring. Missing/error predictions are failures, never silently dropped."""

import math
from collections import Counter

from personal_agent.evaluation.heybuddy_bench.dataset import TRACKS, require


def score_case(track, gold, prediction):
    action = prediction.get("action")
    strict = action == gold["action"]
    acceptable = action in gold.get("acceptable_actions", [gold["action"]])
    target = True
    content = None
    if track == "thread_routing" and gold["action"] in {"continue", "select"}:
        target = (acceptable and action not in {"continue", "select"}) or (
            prediction.get("thread_id") == gold["thread_id"]
        )
    elif track == "memory_learning":
        changes = prediction.get("changes", [])
        if gold["action"] in {"create", "update"}:
            expected = gold["target_path"]
            if gold["action"] == "create":
                # A new filename is a model choice. Existing canonical paths remain strict.
                target = len(changes) == 1 and changes[0]["path"].startswith("topics/")
            else:
                target = len(changes) == 1 and changes[0]["path"] == expected
            text = "\n".join(c["after"] for c in changes).casefold()
            checks = gold.get("content_checks", [])
            content = (
                all(
                    any(alias.casefold() in text for alias in alternatives)
                    for alternatives in checks
                )
                if checks
                else None
            )
        else:
            target = not changes
    elif track == "proactive_decision" and gold["action"] != "silent":
        target = set(prediction.get("proposal_ids", [])) == set(gold["proposal_ids"])
        if action == "defer":
            from datetime import datetime

            review = prediction.get("effective_next_review_at") or prediction.get("next_review_at")
            try:
                target = target and datetime.fromisoformat(review) > datetime.fromisoformat(
                    gold["as_of"]
                )
            except (TypeError, ValueError):
                target = False
    return dict(
        strict_action_correct=strict,
        acceptable_action_correct=acceptable,
        target_correct=target,
        decision_correct=acceptable and target,
        content_check_pass=content,
    )


def wilson(correct, count):
    if not count:
        return None
    p, z = correct / count, 1.96
    center = (p + z * z / (2 * count)) / (1 + z * z / count)
    half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / (1 + z * z / count)
    return [round(center - half, 4), round(center + half, 4)]


def aggregate(inputs, gold_rows, results, *, score_fn=score_case):
    gold = {r["case_id"]: r for r in gold_rows}
    supplied = {c["case_id"] for c in inputs}
    require(supplied == set(gold), "Scoring input/gold mismatch")
    predictions = {}
    for row in results:
        cid = row["case_id"]
        require(cid in supplied and cid not in predictions, "Unknown/duplicate prediction")
        predictions[cid] = row
    details, tracks = [], {}
    for case in inputs:
        cid, track = case["case_id"], case["track"]
        result = predictions.get(cid, {"prediction": {"action": "__missing__"}})
        g = gold[cid]["gold"]
        checks = score_fn(track, g, result["prediction"])
        details.append(
            dict(
                case_id=cid,
                track=track,
                category=case["category"],
                source=case["source"],
                gold_action=g["action"],
                predicted_action=result["prediction"].get("action"),
                **checks,
            )
        )
    for track, classes in TRACKS.items():
        rows = [r for r in details if r["track"] == track]
        if not rows:
            continue
        count = len(rows)
        per = {}
        for label in classes:
            tp = sum(r["gold_action"] == label == r["predicted_action"] for r in rows)
            fp = sum(r["gold_action"] != label == r["predicted_action"] for r in rows)
            fn = sum(r["gold_action"] == label != r["predicted_action"] for r in rows)
            per[label] = dict(
                f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0,
                support=sum(r["gold_action"] == label for r in rows),
            )
        calls = [predictions[r["case_id"]]["usage"] for r in rows if r["case_id"] in predictions]
        target_rows = [
            r
            for r in rows
            if (
                r["gold_action"] in {"continue", "select"}
                if track == "thread_routing"
                else r["gold_action"] in {"create", "update"}
                if track == "memory_learning"
                else r["gold_action"] != "silent"
            )
        ]

        def total(key, calls=calls):
            return sum(c[key] for c in calls) if all(c[key] is not None for c in calls) else None

        correct = sum(r["decision_correct"] for r in rows)
        silent = [r for r in rows if r["gold_action"] == "silent"]
        notified = [r for r in rows if r["predicted_action"] in {"notify", "propose_task"}]
        breakdown = {}
        for r in rows:
            source = r["source"].get("name", "custom")
            group = breakdown.setdefault(source, {"count": 0, "correct": 0})
            group["count"] += 1
            group["correct"] += int(r["decision_correct"])
        tracks[track] = dict(
            expected=count,
            observed=sum(r["case_id"] in predictions for r in rows),
            strict_action_accuracy=sum(r["strict_action_correct"] for r in rows) / count,
            acceptable_action_accuracy=sum(r["acceptable_action_correct"] for r in rows) / count,
            action_macro_f1=sum(v["f1"] for v in per.values()) / len(classes),
            decision_accuracy=correct / count,
            target_accuracy_on_applicable_gold=(
                sum(r["target_correct"] for r in target_rows) / len(target_rows)
                if target_rows
                else None
            ),
            target_cases=len(target_rows),
            valid_decision_rate=sum(r["predicted_action"] in classes for r in rows) / count,
            decision_wilson_95=wilson(correct, count),
            by_class=per,
            by_source=breakdown,
            false_interruption_rate=(
                sum(r["predicted_action"] in {"notify", "propose_task"} for r in silent)
                / len(silent)
                if silent
                else None
            ),
            notification_precision=(
                sum(r["gold_action"] in {"notify", "propose_task"} for r in notified)
                / len(notified)
                if notified
                else None
            ),
            content_checks=Counter(str(r["content_check_pass"]) for r in rows),
            usage={
                key: total(key)
                for key in [
                    "provider_calls",
                    "api_requests",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "api_cost_usd",
                ]
            },
            failures=[r["case_id"] for r in rows if not r["decision_correct"]],
        )
    return dict(expected=len(inputs), observed=len(results), tracks=tracks, cases=details)
