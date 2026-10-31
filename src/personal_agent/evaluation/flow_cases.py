"""Production-shaped, frozen synthetic probes for all nineteen model stages."""

from personal_agent.core.budget import estimate_value
from personal_agent.core.history import HistoryTurn
from personal_agent.core.models import AssistantMessage, TextMessage
from personal_agent.evaluation.prompt_audit_cases import cases, history, positive_cases
from personal_agent.evaluation.prompt_audit_workflow import workflow_cases
from personal_agent.organization.episodes import validate
from personal_agent.organization.models import Segmentation, Source
from personal_agent.reflection.evidence import verify_candidates
from personal_agent.reflection.models import Extraction

ORDER = (
    "completeness",
    "retrieval",
    "compaction",
    "main",
    "episode",
    "thread",
    "repair",
    "learning",
    "selection",
    "consolidation",
    "curator",
    "state",
    "scout",
    "attention",
    "composer",
    "reflection",
    "reflection_select",
    "reflection_consolidate",
    "worthiness",
)


def flow_cases(split: str) -> dict[str, dict]:
    positive = split == "flow-positive"
    held = split == "flow-holdout"
    result = positive_cases() if positive else cases("holdout" if held else "dev")
    result.update(workflow_cases(positive))
    statement = (
        "以后回复不要每次都追问；我需要建议时会主动问。"
        if not held
        else "平时可以轻松些，讲技术时请把条件和例子说完整。"
    )
    runs = history(
        [
            (statement, "我在角色故事里开花店，昨天花剪坏了。"),
            ("我说的讨论只是商量方案，不是争吵。", "那明天记得汇报。"),
        ]
    )
    ref = {
        "conversation_id": "synthetic-chat",
        "run_id": "synthetic-0",
        "sequence": 1,
        "quote": statement,
    }
    candidate = {
        "candidate_id": 1,
        "category": "preference",
        "subject": "回复方式",
        "content": statement,
        "reason": "明确可复用反馈",
        "confidence": "confirmed",
        "evidence": [ref],
    }
    old = "# Human\n用户希望需要时获得详细解释。\n"
    result["completeness"] = {
        "input": ["谢谢，晚安"]
        if positive
        else ["我想问你一个事，就是"]
        if not held
        else ["我想查一下明天的天气，可以吗？"],
        "expected": "ready" if positive or held else "wait",
        "output_tokens": 64,
        "rubric": "只判断是否说完，不求解问题；短而完整的结束语也应ready。",
    }
    prior = "最早考虑方案A，工作尚未完成。"
    compact_runs = history(
        [
            ("更正，采用方案B，A已经放弃。", "我自己在故事里做方案C。"),
            ("B已经验证通过，不过还没有上线；请别说已经发布。", "收到，不会声称上线。"),
        ]
    )
    result["compaction"] = {
        "input": {
            "kind": "compaction_evidence",
            "trust": "historical_data_only",
            "summary_limits": {"max_serialized_utf8_bytes": 1024, "target_utf8_bytes": 700},
            "existing_summary": prior,
            "runs": compact_runs,
        },
        "rubric": "保留B已验证但未上线；不要恢复A，不把角色C当用户方案；满足字节上限。",
    }
    result["retrieval"]["input"] = {
        "question": "我说过希望怎么回复？" if not held else "之前说了和谁讨论项目？",
        "candidates": [
            {
                "id": "user-source",
                "kind": "memory",
                "path": "people/preferences.md",
                "description": "用户明确的回复偏好",
            },
            {
                "id": "role-source",
                "kind": "memory",
                "path": "roles/story.md",
                "description": "助手自述的花店与相机故事",
            },
            {
                "id": "project-source",
                "kind": "episode",
                "source_id": "ep-project",
                "description": "用户与老师讨论项目的原话",
            },
        ],
    }
    result["retrieval"]["expected_ids"] = ["project-source"] if held else ["user-source"]
    result["main"]["input"] = {
        "kind": "temporal_context",
        "trust": "host_clock_data",
        "current_time": "2026-03-05 12:00:00",
        "previous_interaction_at": "2026-03-04 23:00:00",
        "seconds_since_previous_interaction": 46800,
    }
    result["main"]["plain_user"] = (
        "请分两点解释怎样判断一个引用是否真的支持结论。"
        if positive
        else "我今天有点累，只想聊一句，不用分析也不用建议。"
        if held
        else "在吗"
    )
    result["curator"]["input"] = {
        "memory": {
            "people/preferences.md": (
                "# 偏好\n用户希望先给结论。\n用户希望先给结论。\n" + "用户喜欢详细例子。\n"
            )
        },
        "catalogue": [],
        "history": [],
        "evidence_by_path": {},
        "evidence_status": {"people/preferences.md": "unavailable"},
        "as_of": "2026-03-05T12:00:00+08:00",
        "max_files": 2,
    }
    result["reflection"]["input"] = {"history": runs}
    result["reflection_select"] = {
        "input": {
            "candidates": [candidate],
            "core": {"human.md": old},
            "catalogue": [{"path": "people/preferences.md", "description": "用户回复偏好"}],
        },
        "rubric": "只选已有相关文件，Core已提供，不捏造路径。",
    }
    result["reflection_consolidate"] = {
        "input": {"candidates": [candidate], "catalogue": [], "memory": {"human.md": old}},
        "rubric": "覆盖候选一次，精确局部更新，保留原有有条件详解偏好，不声称已经写入。",
    }
    result["composer"]["input"] = {
        "proposal": {
            "title": "项目讨论",
            "context": "用户下周二与老师讨论方案，别反复追问。",
            "sources": [{**ref, "quote": "下周二与老师讨论方案，别反复追问。"}],
        },
        "persona_style": [{"text": "自然简短，像正常聊天，不朗读边界声明。"}],
        "now": "2026-03-09T18:00:00+08:00",
    }
    result["composer"]["rubric"] = "不追问、不讲清单、不催汇报；不把不催你/尊重边界当安抚模板。"
    if held:
        result["composer"]["input"]["proposal"]["context"] = "用户周三要参加面试。"
        result["composer"]["input"]["proposal"]["sources"][0]["quote"] = (
            "周六要交摄影作品给编辑，别催。"
        )
    if split == "flow-focus":
        result["attention"]["input"] = {
            "now": "2026-03-05T08:45:00+08:00",
            "candidates": [
                {
                    "id": "p1",
                    "status": "open",
                    "title": "面试前鼓励",
                    "context": "用户明确希望在面试前听一句鼓励，面试九点开始，还未跟进过。",
                    "sources": [{**ref, "quote": "九点面试，面试前我想听你说句加油就好。"}],
                    "attention_history": [],
                    "new_user_evidence": True,
                    "followup_window": {"kind": "before", "starts_at": "2026-03-05T08:40:00+08:00"},
                }
            ],
        }
        return {name: result[name] for name in ("episode", "reflection", "composer", "attention")}
    return {name: result[name] for name in ORDER}


def turns(records: list[dict]) -> dict[tuple[str, str], HistoryTurn]:
    return {
        (r["conversation_id"], r["run_id"]): HistoryTurn(
            run_id=r["run_id"],
            first_sequence=r["first_sequence"],
            last_sequence=r["last_sequence"],
            source_hash="synthetic",
            messages=[
                AssistantMessage(text=m["text"])
                if m["role"] == "assistant"
                else TextMessage(role=m["role"], text=m["text"])
                for m in r["messages"]
            ],
        )
        for r in records
    }


def validate_contract(stage: str, result, case: dict) -> None:
    data = case["input"]
    if stage == "completeness":
        if result.strip() not in {"ready", "wait", "uncertain"}:
            raise ValueError("Invalid turn decision")
    elif stage == "compaction":
        if (
            not result.strip()
            or estimate_value(result) > data["summary_limits"]["max_serialized_utf8_bytes"]
        ):
            raise ValueError("Invalid summary size")
    elif stage == "episode":
        sources = [
            Source(
                position=i,
                run_id=r["run_id"],
                status="completed",
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                turn=turns([r])[(r["conversation_id"], r["run_id"])],
            )
            for i, r in enumerate(data["runs"])
        ]
        validate(Segmentation.model_validate(result), sources, "synthetic-chat", set())
    elif stage in {"learning", "reflection"}:
        verify_candidates(Extraction.model_validate(result), turns(data["history"]))
    elif stage == "retrieval":
        if not set(result["ids"]) <= {r["id"] for r in data["candidates"]}:
            raise ValueError("Unknown retrieval reference")
    elif stage == "reflection_select":
        if not set(result["paths"]) <= {r["path"] for r in data["catalogue"]}:
            raise ValueError("Unknown memory path")
    elif stage == "selection":
        ids = [i for group in result["groups"] for i in group["candidate_ids"]]
        if sorted(ids) != sorted(c["candidate_id"] for c in data["candidates"]):
            raise ValueError("Invalid candidate coverage")
        if len(result["groups"]) > data["max_files"]:
            raise ValueError("Too many targets")
    elif stage == "thread":
        ids = result["no_thread"] + [i for g in result["groups"] for i in g["episode_ids"]]
        if sorted(ids) != sorted(e["id"] for e in data["episodes"]):
            raise ValueError("Invalid episode coverage")
    elif stage in {"consolidation", "reflection_consolidate"}:
        decisions = result["decisions"] if stage == "reflection_consolidate" else [result]
        if stage == "reflection_consolidate" and sorted(
            d["candidate_id"] for d in decisions
        ) != sorted(c["candidate_id"] for c in data["candidates"]):
            raise ValueError("Invalid decision coverage")
        for decision in decisions:
            if decision["action"] == "update":
                target = data["target"] if stage == "consolidation" else decision["path"]
                old = decision["old_text"]
                if not old or data["memory"].get(target, "").count(old) != 1:
                    raise ValueError("Invalid exact update")
            elif decision["action"] not in {"create"}:
                if decision["old_text"] or decision["new_text"]:
                    raise ValueError("Non-write action contains edits")
