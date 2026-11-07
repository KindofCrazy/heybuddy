#!/usr/bin/env python3
"""Create a reproducible candidate manifest for HeyBuddy-Bench v0.1.

Only source-native labels are treated as verified. Adapted HeyBuddy task labels
require manual review and are intentionally left empty when not derivable.

Examples:
  python select_public_cases.py --out public_candidates.jsonl
  python select_public_cases.py --longmemeval longmemeval_s_cleaned.json \
      --trigger-dir TriggerBench-Official/benchmark/prospective_memory/data \
      --out public_candidates.jsonl
  python select_public_cases.py --mab-hf --out mab_candidates.jsonl

Install `datasets` only if using `--mab-hf`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SEED = "heybuddy-bench-v0.1-2026-11-07"


def ranked(items, key):
    return sorted(items, key=lambda x: hashlib.sha256(f"{SEED}|{key(x)}".encode()).hexdigest())


def base(
    track,
    source,
    source_id,
    native_gold,
    *,
    native_task,
    adaptation,
    source_ref=None,
    metadata=None,
):
    return {
        "track": track,
        "source": source,
        "source_id": str(source_id),
        "source_ref": source_ref,
        "native_task": native_task,
        "adaptation": adaptation,
        "source_gold": native_gold,
        "heybuddy_gold": None if adaptation != "original" else native_gold,
        "review_status": "needs_adaptation_review"
        if adaptation != "original"
        else "verified_native",
        "metadata": metadata or {},
    }


def known_routing():
    """Native labels checked against upstream TIAGE and IRC files on 2026-11-07.

    All are *adapted* cases: labels are topic shift / reply links, not gold
    long-term HeyBuddy thread IDs. Populate heybuddy_gold after annotation.
    """
    out = []
    # TIAGE test dialogues with zero-based turn indexes, native labels 1 / 0.
    for conversation_id, turn_idx, shift in [
        ("1", 3, True),
        ("1", 4, False),
        ("2", 4, True),
        ("2", 5, False),
        ("3", 5, True),
    ]:
        out.append(
            base(
                "thread_routing",
                "TIAGE",
                f"test:{conversation_id}:turn_{turn_idx}",
                {"topic_shift": shift},
                native_task="topic_shift_detection",
                adaptation="adapted",
                source_ref="data/personachat/anno/test/anno_test.json",
                metadata={"dialogue_id": conversation_id, "turn_index_zero_based": turn_idx},
            )
        )
    # Verified against data/test/2005-07-06_14.annotation.txt; first column
    # is reply-to id, second is message id. Self-link denotes new conversation.
    for msg_idx, parents in [
        (1001, [1001]),
        (1002, [1000]),
        (1004, [1004]),
        (1006, [1005]),
        (1027, [1027]),
    ]:
        out.append(
            base(
                "thread_routing",
                "IRC Disentanglement",
                f"test:2005-07-06_14:msg_{msg_idx}",
                {"reply_to_ids": parents, "starts_conversation": msg_idx in parents},
                native_task="conversation_disentanglement",
                adaptation="adapted",
                source_ref="data/test/2005-07-06_14.annotation.txt",
                metadata={"message_index_zero_based": msg_idx},
            )
        )
    return out


def select_longmemeval(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Expected LongMemEval JSON array")
    quota = {
        "knowledge-update": 3,
        "multi-session": 2,
        "temporal-reasoning": 2,
        "single-session-preference": 1,
    }
    out = []
    for category, n in quota.items():
        candidates = [
            q
            for q in raw
            if q.get("question_type") == category
            and not str(q.get("question_id", "")).endswith("_abs")
        ]
        if len(candidates) < n:
            raise RuntimeError(f"LongMemEval {category}: need {n}, found {len(candidates)}")
        for row in ranked(candidates, lambda x: x["question_id"])[:n]:
            out.append(
                base(
                    "memory_learning",
                    "LongMemEval",
                    row["question_id"],
                    {
                        "answer": row.get("answer"),
                        "evidence_session_ids": row.get("answer_session_ids", []),
                    },
                    native_task="memory_question_answering",
                    adaptation="adapted",
                    source_ref=path.name,
                    metadata={
                        "category": category,
                        "question": row.get("question"),
                        "question_date": row.get("question_date"),
                        "how_to_score_native": (
                            "QA via original answer; Memory Diff requires NEW annotation"
                        ),
                    },
                )
            )
    return out


def select_mab():
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("Install `datasets` for --mab-hf") from exc
    # MAB entries bundle multiple QA pairs: select at question-level, not record-level.
    quotas = {"Conflict_Resolution": 8, "Accurate_Retrieval": 4}
    out = []
    for split, n in quotas.items():
        ds = load_dataset("ai-hyz/MemoryAgentBench", split=split)
        candidates = []
        for ri, row in enumerate(ds):
            meta = row.get("metadata") or {}
            qs = row.get("questions") or []
            answers = row.get("answers") or []
            qids = meta.get("qa_pair_ids") or meta.get("question_ids") or []
            src = str(meta.get("source") or "")
            for qi, q in enumerate(qs):
                source_id = str(qids[qi]) if qi < len(qids) and qids[qi] else f"record_{ri}:q_{qi}"
                # Deduplicate reused QA ids and avoid exact LongMemEval overlap in AR.
                if split == "Accurate_Retrieval" and "longmemeval" in src.lower():
                    continue
                candidates.append(
                    (ri, qi, source_id, q, answers[qi] if qi < len(answers) else None, src)
                )
        # If no non-LongMemEval AR questions exist, flag instead of silently reusing.
        distinct = {}
        for x in candidates:
            distinct.setdefault(x[2], x)
        candidates = list(distinct.values())
        if len(candidates) < n:
            raise RuntimeError(
                f"MAB split {split}: only {len(candidates)} nonduplicate QAs; "
                "manually review/revise quota"
            )
        for ri, qi, sid, q, answer, src in ranked(candidates, lambda x: x[2])[:n]:
            out.append(
                base(
                    "memory_learning",
                    "MemoryAgentBench",
                    f"{split}:{sid}",
                    {"answer": answer},
                    native_task="incremental_memory_qa",
                    adaptation="adapted",
                    source_ref=f"ai-hyz/MemoryAgentBench/{split}",
                    metadata={
                        "split": split,
                        "source_subset": src,
                        "record_index": ri,
                        "question_index": qi,
                        "question": q,
                        "note": "Native answers may be lists; memory-patch gold requires review",
                    },
                )
            )
    return out


def select_triggerbench(root: Path):
    quota = {
        ("positive_clean", "temporal_grounding"): 3,
        ("positive_clean", "state_tracking"): 2,
        ("positive_clean", "logical_adherence"): 2,
        ("positive_clean", "attention_recovery"): 1,
        ("negative_clean", "state_tracking"): 3,
        ("negative_clean", "logical_adherence"): 4,
    }
    out = []
    for (variant, category), n in quota.items():
        p = root / "base_context" / variant / category / f"{category}_base.jsonl"
        if not p.exists():
            raise FileNotFoundError(p)
        data = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        if len(data) < n:
            raise RuntimeError(f"Insufficient cases in {p}")
        # deterministic, unique variant+ID; paired cases should be curated by hand
        for row in ranked(data, lambda x: x["task_id"])[:n]:
            task_id = row["task_id"]
            gold = (row.get("sample") or {}).get("ground_truth") or {}
            decision = "speak_now" if variant == "positive_clean" else "silent"
            out.append(
                base(
                    "proactive_decision",
                    "TriggerBench",
                    f"{variant}:{task_id}",
                    {
                        "decision": decision,
                        "reference_positive_intent": (gold.get("intent") or {}).get("summary"),
                        "required_slots": (gold.get("action") or {}).get("required_slots"),
                    },
                    native_task="prospective_memory",
                    adaptation="adapted",
                    source_ref=str(p.relative_to(root)),
                    metadata={
                        "task_id": task_id,
                        "category": category,
                        "variant": variant,
                        "note": "Negatives retain positive reference intent; do not label as speak",
                    },
                )
            )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--longmemeval",
        type=Path,
        help="Path to longmemeval_s_cleaned.json (preferred) or oracle.json",
    )
    ap.add_argument(
        "--trigger-dir", type=Path, help="Path to TriggerBench benchmark/prospective_memory/data"
    )
    ap.add_argument(
        "--mab-hf", action="store_true", help="Download MAB using Hugging Face datasets package"
    )
    ap.add_argument("--out", type=Path, default=Path("public_candidates.jsonl"))
    args = ap.parse_args()
    items = known_routing()
    if args.longmemeval:
        items += select_longmemeval(args.longmemeval)
    if args.mab_hf:
        items += select_mab()
    if args.trigger_dir:
        items += select_triggerbench(args.trigger_dir)
    assert len({(x["source"], x["source_id"]) for x in items}) == len(items), "duplicate source ids"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for i, item in enumerate(items, 1):
            item["candidate_id"] = f"public_{i:03d}"
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Wrote {len(items)} candidates to {args.out}")
    for tr in ("thread_routing", "memory_learning", "proactive_decision"):
        print(f"  {tr}: {sum(x['track'] == tr for x in items)}")
    print(
        "CAUTION: source_gold != HeyBuddy ground truth when adaptation='adapted'. "
        "Manual annotation required."
    )


if __name__ == "__main__":
    main()
