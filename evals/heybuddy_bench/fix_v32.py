"""Reviewed storage/task-label corrections; task inputs remain equivalent."""

import json
from pathlib import Path

from revise_v3 import publish

from personal_agent.evaluation.heybuddy_bench.dataset import read_rows, sha256

ROOT = Path(__file__).resolve().parent


def main():
    for source, destination in [("v0_3_1", "v0_3_2"), ("v0_3_challenge", "v0_3_2_challenge")]:
        cases = read_rows(ROOT / source / "inputs.jsonl")
        gold = {r["case_id"]: r for r in read_rows(ROOT / source / "gold.jsonl")}
        audit = []
        if source == "v0_3_1":
            label = gold["extra_ml_004"]["gold"]
            label["storage_alternatives"] = [dict(action="update", target_path="human.md")]
            label["content_checks"].append(["导航", "navigation"])
            label["fact_assertions"] = [
                dict(
                    paths=["human.md"],
                    all_any=[
                        [
                            "平时中文",
                            "日常中文",
                            "默认中文",
                            "Chinese by default",
                            "平时用中文",
                            "日常用中文",
                            "平时使用中文",
                        ]
                    ],
                )
            ]
            audit.append(
                dict(case_id="extra_ml_004", reason="Core exception must retain Chinese default")
            )
        else:
            label = gold["challenge_ml_002"]["gold"]
            label["optional_extra_paths"] = ["human.md"]
            label["fact_assertions"] = [
                dict(paths=["human.md"], all_any=[["Python"]]),
                dict(
                    paths=["topics/atlas.md"],
                    all_any=[["Atlas"], ["Rust"]],
                    none_exact=["Atlas uses Python."],
                ),
            ]
            gold["challenge_pd_005"]["gold"]["acceptable_actions"] = ["notify", "propose_task"]
            audit.extend(
                [
                    dict(case_id="challenge_ml_002", reason="Global Python exception is supported"),
                    dict(
                        case_id="challenge_pd_005",
                        reason="Plan authorized; file movement unauthorized",
                    ),
                ]
            )
        publish(ROOT / destination, [(case, gold[case["case_id"]]) for case in cases])
        manifest_path = ROOT / destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.3.2"
        manifest["total"] = len(cases)
        manifest["fact_scorer_sha256"] = sha256(
            Path("src/personal_agent/evaluation/heybuddy_bench/fact_scoring.py")
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (ROOT / destination / "revisions.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    main()
