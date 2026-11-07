"""Freeze selected upstream excerpts. Optional preparation only; runner never imports this.

Run this script with --sources .cache/bench_sources from the repository root.
Source cache layout and pinned download URLs are documented in SOURCES.md.
"""

import argparse
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import dump_rows, sha256

ROOT = Path(__file__).resolve().parent
REVISIONS = {
    "TIAGE": "08f98dfa066a5f2fba2bbd1d6ed6ee9a783a2b1d",
    "IRC Disentanglement": "82ed04f9627a45d0d6f2ec3c8683c88eb408a0d5",
    "TriggerBench": "9e2185c3d071f73c195196ba1859631898017c8d",
}


def curate(root):
    out = []
    hf = json.loads((root / "hf_revisions.json").read_text())

    def add(source, sid, path, native, excerpt, **meta):
        revision = REVISIONS.get(source) or hf[meta.pop("hf_repo")]
        locations = {
            "TIAGE": ("HuiyuanXie/tiage", "data/personachat/anno/test/anno_test.json"),
            "IRC Disentanglement": (
                "jkkummerfeld/irc-disentanglement",
                "data/test/2005-07-06_14.ascii.txt",
            ),
            "TriggerBench": ("KristenZHANG/TriggerBench-Official", path),
        }
        if source in locations:
            repository, native_path = locations[source]
            reference = f"https://raw.githubusercontent.com/{repository}/{revision}/{native_path}"
        else:
            repository, native_path = (
                ("xiaowu0162/longmemeval-cleaned", "longmemeval_oracle.json")
                if source == "LongMemEval"
                else ("ai-hyz/MemoryAgentBench", "data/Conflict_Resolution-00000-of-00001.parquet")
            )
            reference = (
                f"https://huggingface.co/datasets/{repository}/resolve/{revision}/{native_path}"
            )
        out.append(
            dict(
                case_id=f"public_{len(out) + 1:03d}",
                source=source,
                source_id=sid,
                source_ref=reference,
                cache_path=path,
                revision=revision,
                sha256=sha256(root / path),
                native_gold=native,
                excerpt=excerpt,
                **meta,
            )
        )

    tiage = json.loads((root / "tiage.json").read_text())
    for cid, turn in [("1", 3), ("1", 4), ("2", 4), ("2", 5), ("3", 5)]:
        dialogue = tiage[cid]
        add(
            "TIAGE",
            f"test:{cid}:turn_{turn}",
            "tiage.json",
            {"topic_shift": dialogue[turn][1]},
            {"prefix": dialogue[: turn + 1], "turn_index": turn, "dialogue_id": cid},
        )
    lines = (root / "irc.txt").read_text().splitlines()
    annotations = [
        line.split()
        for line in (root / "irc.annotation.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    for msg in [1001, 1002, 1005]:
        parents = [int(a[0]) for a in annotations if int(a[1]) == msg]
        add(
            "IRC Disentanglement",
            f"test:2005-07-06_14:msg_{msg}",
            "irc.txt",
            {"reply_to_ids": parents, "starts_conversation": msg in parents},
            {
                "lines": [{"index": i, "text": lines[i]} for i in range(985, msg + 1)],
                "message_index": msg,
            },
            annotation_sha256=sha256(root / "irc.annotation.txt"),
        )
    longmem = json.loads((root / "longmemeval.json").read_text(encoding="utf-8"))
    for qid in [
        "6a1eabeb",
        "6aeb4375",
        "830ce83f",
        "71315a70",
        "89941a93",
        "ce6d2d27",
        "a1eacc2a",
        "184da446",
    ]:
        row = next(r for r in longmem if r["question_id"] == qid)
        messages = []
        for sid, date, session in zip(
            row["haystack_session_ids"],
            row["haystack_dates"],
            row["haystack_sessions"],
            strict=True,
        ):
            for i, message in enumerate(session):
                if message["role"] == "user" and message.get("has_answer"):
                    messages.append(
                        dict(session_id=sid, date=date, message_index=i, text=message["content"])
                    )
        add(
            "LongMemEval",
            qid,
            "longmemeval.json",
            {"answer": row["answer"], "answer_session_ids": row["answer_session_ids"]},
            {
                "messages": messages,
                "question": row["question"],
                "question_date": row["question_date"],
            },
            category=row["question_type"],
            hf_repo="xiaowu0162/longmemeval-cleaned",
        )
    mab = json.loads((root / "mab_rows.json").read_text())[4]
    # Single-hop facts only: preserve every revision of the exact subject/relation.
    prefixes = [
        "goaltender is associated",
        "Nobuhiro Watsuki is famous",
        "rugby union was created",
        "Ferdowsi is famous",
    ]
    for qi, prefix in enumerate(prefixes):
        facts = []
        for line in mab["context"].splitlines():
            if ". " in line and line.split(". ", 1)[1].startswith(prefix):
                idx, text = line.split(". ", 1)
                facts.append(dict(index=int(idx), text=text))
        add(
            "MemoryAgentBench",
            "Conflict_Resolution:" + mab["metadata"]["qa_pair_ids"][qi],
            "mab.parquet",
            {"answers": mab["answers"][qi]},
            {
                "facts": facts,
                "question": mab["questions"][qi],
                "question_index": qi,
                "record_index": 4,
                "subset": mab["metadata"]["source"],
            },
            hf_repo="ai-hyz/MemoryAgentBench",
        )
    quotas = [
        ("positive_clean", "temporal_grounding", 3),
        ("positive_clean", "state_tracking", 2),
        ("positive_clean", "logical_adherence", 2),
        ("positive_clean", "attention_recovery", 1),
        ("negative_clean", "state_tracking", 3),
        ("negative_clean", "logical_adherence", 4),
    ]
    for variant, category, count in quotas:
        path = (
            f"benchmark/prospective_memory/data/base_context/{variant}/{category}/"
            f"{category}_base.jsonl"
        )
        rows = [json.loads(line) for line in (root / path).read_text(encoding="utf-8").splitlines()]
        for row in rows[:count]:
            sample = row["sample"]
            # Save all user turns, not just function-marked constraints/triggers.
            add(
                "TriggerBench",
                f"{variant}:{category}:{row['task_id']}",
                path,
                {"variant": variant, "ground_truth": sample["ground_truth"]},
                {"turns": [t for t in sample["turns"] if t["role"] == "user"]},
                category=category,
            )
    dump_rows(ROOT / "selected_source_records.jsonl", out)
    print("Frozen", len(out), "public excerpts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    curate(parser.parse_args().sources)
