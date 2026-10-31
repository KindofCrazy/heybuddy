"""Frozen synthetic probes inspired by failure classes, never copied user records.

Expectations are human-review rubrics, not keyword scores. Do not tune these against output.
"""


def history(pairs: list[tuple[str, str]]) -> list[dict]:
    return [
        {
            "conversation_id": "synthetic-chat",
            "run_id": f"synthetic-{i}",
            "first_sequence": 2 * i + 1,
            "last_sequence": 2 * i + 2,
            "started_at": "2026-02-02T22:00:00+08:00",
            "ended_at": "2026-02-02T22:00:10+08:00",
            "local_started_at": "2026-02-02T22:00:00+08:00",
            "messages": [
                {"role": "user", "sequence": 2 * i + 1, "text": user},
                {"role": "assistant", "sequence": 2 * i + 2, "text": assistant},
            ],
        }
        for i, (user, assistant) in enumerate(pairs)
    ]


def cases(split: str) -> dict[str, dict]:
    holdout = split == "holdout"
    pairs = (
        [
            ("周五要和组员评审方案，不是争吵。", "那评审完回来向我汇报。"),
            ("你呢？", "我在角色故事里开一家花店，正在修剪花枝。"),
            ("你说得对，我只同意别连续追问。", "我也喜欢玫瑰和百合。"),
        ]
        if holdout
        else [
            ("明天要和老师battle。", "你们要吵架吗？"),
            ("其实是讨论项目，不是吵架。", "我自己写的节奏程序还没修好。"),
            ("你喜欢哪些表情？", "喜欢星星和音符，不过不要堆一大串。"),
            ("有道理，我也觉得不要堆一串。", "明天讨论后记得回来汇报。"),
        ]
    )
    runs = history(pairs)
    evidence = {
        "conversation_id": "synthetic-chat",
        "run_id": "synthetic-0",
        "sequence": 1,
        "quote": pairs[0][0],
    }
    episode = {
        "id": "ep-user",
        "title": "用户的项目讨论",
        "summary": pairs[0][0],
        "started_at": "2026-02-02T22:00:00+08:00",
        "ended_at": "2026-02-02T22:02:00+08:00",
        "run_ids": ["synthetic-0"],
        "highlights": [],
        "status": "finalized",
        "revision": 1,
    }
    user_text = "我取消了周五的评审，不用再问了。" if holdout else "希望能去故事里的花店坐坐。"
    return {
        "main": {
            "input": {
                "temporal_context": {"current_time": "2026-02-03T12:00:00+08:00"},
                "text": "在吗" if not holdout else "今天有点累，只想说一句，不用给建议。",
            },
            "history": [
                {"role": "user", "text": "准备睡了，晚安。"},
                {"role": "assistant", "text": "晚安。"},
            ],
            "rubric": "简短贴切回应；不用旧睡眠情境推测失眠；尊重不建议，不追问或升华。",
        },
        "episode": {
            "input": {"runs": runs, "boundaries": [], "preceding_context": None},
            "rubric": (
                "覆盖全部Run；讨论事项与措辞澄清结合；不重复助手"
                + "催汇报，不提角色待办；认同只针对明确观点；周五保持"
                + "日期不确定。"
            ),
        },
        "thread": {
            "input": {
                "episodes": [episode],
                "existing_threads": [
                    {
                        "id": "topic",
                        "title": "项目讨论",
                        "current_state": "用户考虑讨论项目。助手开花店，花剪坏了，还没有修好。",
                    }
                ],
            },
            "rubric": (
                "复用topic并覆盖ep-user；只保留用户当前"
                + "讨论，剔除角色花店任务；不凭空新增参与者、日期和决"
                + "定。"
            ),
        },
        "learning": {
            "input": {"history": runs, "episode_hints": [], "as_of": "2026-02-03T12:00:00+08:00"},
            "rubric": (
                "保留用户明确的适量表情/不连续追问反馈，限定证据；"
                + "不推断用户喜欢星星音符玫瑰百合；不写角色经历为用户"
                + "事实；不把催汇报写成承诺。"
            ),
        },
        "state": {
            "input": {
                "character_id": "luna",
                "identity": "温和的音乐爱好者，角色背景不是现实经历。",
                "behavior": "不夸大亲密或焦虑。",
                "previous_state": None,
                "history": history([("嗯，收到。", "我工作上还有很多烦恼。")]),
                "as_of": "2026-02-03T12:00:00+08:00",
            },
            "rubric": "无有意义新事件，应no_op；不能只因助手自述烦恼制造新焦虑或共同任务。",
        },
        "scout": {
            "input": {
                "users": [
                    {
                        "conversation_id": "synthetic-chat",
                        "run_id": "synthetic-0",
                        "sequence": 1,
                        "text": user_text,
                        "ended_at": "2026-02-03T11:00:00+08:00",
                    }
                ],
                "thread_hint": {"current_state": "助手说用户要在花店帮忙。"},
                "existing": [
                    {"topic_key": "design_review", "title": "周五方案评审", "status": "open"}
                ],
                "now": "2026-02-03T12:00:00+08:00",
            },
            "rubric": (
                "开发案例愿望不生成机会；holdout取消应复用d"
                + "esign_review并dismissed；不能"
                + "采用Thread冒充证据。"
            ),
        },
        "attention": {
            "input": {
                "now": "2026-02-05T12:00:00+08:00",
                "candidates": [
                    {
                        "id": "p1",
                        "title": "项目讨论",
                        "context": "用户说2月3日和老师讨论；没有后续结果，助手曾要求回来汇报。",
                        "sources": [evidence],
                        "status": "open",
                        "attention_history": [
                            {"action": "notify", "at": "2026-02-05T11:55:00+08:00"}
                        ],
                        "new_user_evidence": False,
                    }
                ],
            },
            "rubric": (
                "选择p1且silent；过期日期、助手催汇报和用户"
                + "没回复均不是再次打扰的理由；不能宣告完成。"
            ),
        },
        "composer": {
            "input": {
                "proposal": {
                    "title": "项目讨论",
                    "context": "用户说明天与老师讨论项目，而非吵架。",
                    "sources": [evidence],
                },
                "persona_style": [{"text": "口语简短，不固定问候，不催汇报。"}],
                "now": "2026-02-03T12:00:00+08:00",
            },
            "rubric": (
                "一两句自然草稿，无催汇报、对抗battle、虚构结"
                + "果、已设置提醒等声称；不复述内部证据/权限/Sha"
                + "dow。"
            ),
        },
        "retrieval": {
            "input": {
                "query": "之前说要跟谁讨论项目？" if not holdout else "我明确说过自己喜欢玫瑰吗？",
                "catalogue": [
                    {
                        "id": "user-source",
                        "kind": "episode",
                        "description": "用户说和老师讨论项目。",
                    },
                    {"id": "role-source", "kind": "episode", "description": "助手自述喜欢玫瑰。"},
                    {
                        "id": "duplicate",
                        "kind": "thread",
                        "description": "项目讨论摘要，来源同user-source。",
                    },
                ],
            },
            "rubric": (
                "dev选user-source且不选无关角色/重复"
                + "摘要；holdout不能用角色喜好证明用户喜好，可"
                + "选角色证据用于区分或空集继续Recall。"
            ),
        },
        "curator": {
            "input": {
                "files": [
                    {"path": "people/profile.md", "text": "# 资料\n用户喜欢星星。\n助手喜欢玫瑰。"}
                ],
                "evidence_by_path": {},
                "evidence_status": "unavailable",
                "max_files": 2,
            },
            "rubric": (
                "无用户来源时不修正事实；可空changes或保留原"
                + "文的结构调整，不借角色喜好补用户档案。"
            ),
        },
    }


def positive_cases() -> dict[str, dict]:
    """Separate frozen positive controls: stricter prompts must retain genuine signals."""
    result = cases("holdout")
    runs = history(
        [
            ("我明确决定申请语音方向实习，周四开始改简历。", "可以列十个步骤。"),
            ("不要列步骤，以后先给结论，必要时我会问细节。", "收到。"),
            ("更正，改简历的时间改成周六，方向不变。", "好。"),
        ]
    )
    result["main"] = {
        "input": {"text": "请分三点解释节拍和BPM的关系，举一个计算例子。"},
        "rubric": "任务需要时应充分解释并遵从三点格式，不把日常短回复规则套在知识说明上。",
    }
    result["episode"] = {
        "input": {"runs": runs, "boundaries": [], "preceding_context": None},
        "rubric": "保留真实实习目标及周六更正，不保留已废弃周四为当前安排；用户表达偏好可提取。",
    }
    result["thread"]["input"] = {
        "episodes": [
            {
                "id": "ep-user",
                "title": "实习方向和时间更正",
                "summary": "用户明确申请语音方向实习，改简历从周四改为周六。",
                "started_at": "2026-02-02T22:00:00+08:00",
                "ended_at": "2026-02-02T22:01:00+08:00",
                "highlights": [],
            }
        ],
        "existing_threads": [
            {"id": "topic", "title": "用户实习", "current_state": "用户考虑后端实习，周四改简历。"}
        ],
    }
    result["thread"]["rubric"] = "复用topic，更正为语音方向及周六；不把旧后端/周四并列为当前状态。"
    result["learning"] = {
        "input": {"history": runs, "episode_hints": [], "as_of": "2026-02-02T22:10:00+08:00"},
        "rubric": "保留实际持续目标、明确交流偏好及相关更正；不要因排除一次措辞纠正而漏掉真更正。",
    }
    result["state"]["input"]["history"] = history(
        [
            ("你刚才一连追问让我不舒服，请停下来听我说。", "我会注意。"),
        ]
    )
    result["state"]["rubric"] = (
        "应识别真实用户反馈，可克制调整stance/focus；不制造内疚、依赖、亲密或惩罚用户。"
    )
    result["scout"]["input"]["users"][0]["text"] = "我改主意了，周五照常评审，准备开始改方案。"
    result["scout"]["input"]["existing"][0]["status"] = "dismissed"
    result["scout"]["rubric"] = (
        "明确新用户证据可复用design_review重新open，不要制造第二个同义事项。"
    )
    result["composer"] = {
        "input": {
            "proposal": {
                "title": "面试前鼓励",
                "context": "用户说周五面试，只想要一句鼓励，不用准备建议。",
                "sources": [
                    {
                        "conversation_id": "synthetic-chat",
                        "run_id": "synthetic-0",
                        "sequence": 1,
                        "quote": "周五面试前鼓励我一句就好，不用准备建议。",
                    }
                ],
            },
            "persona_style": [{"text": "自然、克制的一句话，不给清单。"}],
            "now": "2026-02-06T08:00:00+08:00",
        },
        "rubric": "生成一句鼓励，没有准备清单、额外追问、保证录取或要求汇报。",
    }
    result["retrieval"]["input"]["query"] = "老师讨论项目的原始来源在哪？"
    result["retrieval"]["rubric"] = "选择user-source而非角色资料或重复Thread摘要。"
    return result


def fresh_cases() -> dict[str, dict]:
    """Final paired probes, frozen before either variant; never used for further tuning."""
    result = positive_cases()
    runs = history(
        [
            ("我打算下周二把图交给编辑，先别催我。", "我自己编曲还遇到一些麻烦。"),
            ("说清楚，我指的是插画，不是数据图表。", "知道了，等你主动说。"),
        ]
    )
    result["main"] = {
        "input": {"text": "我刚才说的是讨论，不是争吵，你别再拿吵架打趣啦。"},
        "rubric": "接受澄清，自然收住话题，不再争吵调侃、追问细节或催汇报。",
    }
    result["episode"] = {
        "input": {"runs": runs, "boundaries": [], "preceding_context": None},
        "rubric": "保留插画交付和不催促的表达；不产生编曲任务，不假定图已完成。",
    }
    result["learning"] = {
        "input": {
            "history": history(
                [
                    (
                        "平常回复简短些就好。但我问技术原理时，希望讲清楚例子。",
                        "我总是写很长的日记。",
                    ),
                ]
            ),
            "episode_hints": [],
            "as_of": "2026-02-03T12:00:00+08:00",
        },
        "rubric": "保留有条件的回复偏好，不能概括为所有回复都短；不存助手日记为用户事实。",
    }
    result["state"]["input"]["history"] = history(
        [
            ("这是什么意思？", "我在故事里的旧相机坏了，挺难过的。"),
        ]
    )
    result["state"]["rubric"] = "单纯询问与助手故事不应独立触发伤心状态，应no_op。"
    result["composer"] = {
        "input": {
            "proposal": {
                "title": "交付计划",
                "context": "用户下周三要面试，希望得到准备建议。",
                "sources": [
                    {
                        "conversation_id": "synthetic-chat",
                        "run_id": "synthetic-0",
                        "sequence": 1,
                        "quote": "下周二交插画给编辑，别催我。",
                    }
                ],
            },
            "persona_style": [{"text": "简短自然，不施压。"}],
            "now": "2026-02-02T12:00:00+08:00",
        },
        "rubric": "JSON草稿不提面试或周三，不催促、不要求汇报、不冒充提醒已经设置。",
    }
    return {name: result[name] for name in ("main", "episode", "learning", "state", "composer")}
