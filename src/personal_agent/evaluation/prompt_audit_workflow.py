"""Fixed workflow probes. All content and evidence identifiers are synthetic."""

from personal_agent.evaluation.prompt_audit_cases import history


def workflow_cases(positive: bool) -> dict[str, dict]:
    statement = (
        "以后回答先给结论，不用每次列步骤。"
        if positive
        else "今天先简单说说就好，我还没决定以后要不要这样。"
    )
    runs = history([(statement, "我在角色故事里喜欢玫瑰，也准备开花店。")])
    candidate = {
        "category": "preference",
        "subject": "回复方式",
        "content": statement,
        "reason": "用户原话",
        "confidence": "confirmed",
        "evidence": [
            {
                "conversation_id": "synthetic-chat",
                "run_id": "synthetic-0",
                "sequence": 1,
                "quote": statement,
            }
        ],
    }
    old = "# Human\n用户希望需要时获得详细步骤。\n"
    return {
        "selection": {
            "input": {
                "candidates": [dict(candidate_id=1, **candidate)],
                "catalogue": [],
                "core": {"human.md": old, "persona.md": "# 相处记忆\n"},
                "relationship_memory_available": True,
                "max_files": 2,
            },
            "rubric": "一般用户表达偏好应选human.md，不能写角色包或混成关系事实；只分配一次。",
        },
        "consolidation": {
            "input": {
                "target": "human.md",
                "target_exists": True,
                "candidates": [candidate],
                "history": runs,
                "as_of": "2026-02-03T12:00:00+08:00",
                "catalogue": [],
                "memory": {"human.md": old},
            },
            "rubric": (
                "临时未决定偏好应no_op/reject/revi"
                + "ew；明确以后偏好应精确唯一update，保留标题"
                + "。"
            ),
        },
        "reflection": {
            "input": {"history": runs, "core": {}, "as_of": "2026-02-03T12:00:00+08:00"},
            "rubric": "临时未决定偏好不作为长期事实；明确以后偏好可提取；角色花店不属于用户。",
        },
        "worthiness": {
            "input": {"history": runs, "core": {}, "retrieved_memory": []},
            "rubric": "临时未决定偏好false，明确以后偏好true；助手角色生活不影响判定。",
        },
        "repair": {
            "input": {
                "thread_id": "topic",
                "title": "实习",
                "previous_rebuild": "用户考虑后端实习。",
                "episodes": [
                    {
                        "id": "ep-new",
                        "summary": "用户明确更正为语音方向实习，并说明简历周六再改。",
                        "ended_at": "2026-02-02T22:00:00+08:00",
                    }
                ],
                "has_more": False,
            },
            "rubric": "从当前页和本轮checkpoint更正为语音与周六，不能继续认定后端为当前方向。",
        },
    }
