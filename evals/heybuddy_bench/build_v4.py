"""Normalize prior public memory into real topic notes; never add the new answer to input."""

import copy
import json
from pathlib import Path

from revise_v3 import publish

from personal_agent.evaluation.heybuddy_bench.dataset import read_rows

ROOT = Path(__file__).resolve().parent
PRIOR = {
    "public_009": ("running", "Charity 5K running", "Personal best: 27:12."),
    "public_010": ("food", "Korean restaurant exploration", "Korean restaurants tried: three."),
    "public_011": (
        "friends",
        "Friend Rachel",
        "Rachel recently moved to an apartment in the city.",
    ),
    "public_012": (
        "sculpture",
        "Abstract ocean sculpture",
        "Time spent: around 5-6 hours; in progress.",
    ),
    "public_013": ("bikes", "Bikes owned", "Three bikes: road bike, mountain bike, commuter bike."),
    "public_014": ("cocktails", "Cocktail-making class", "Class is on Thursday."),
    "public_015": ("writing", "Short-story writing", "Completed four short stories."),
    "public_016": ("reading", "A Short History of Nearly Everything", "Current page: 200."),
}


def main():
    cases = read_rows(ROOT / "v0_3_3/inputs.jsonl")
    gold = {r["case_id"]: r for r in read_rows(ROOT / "v0_3_3/gold.jsonl")}
    audit = []
    for case in cases:
        cid = case["case_id"]
        if cid not in PRIOR:
            continue
        stem, title, fact = PRIOR[cid]
        body, row = case["input"], gold[cid]
        old_quote = body["memory_files"].pop("topics/reference-notes.md")
        path = f"topics/{stem}.md"
        body["memory_files"][path] = (
            f"---\nname: {stem}\ndescription: This user's previously recorded {title.lower()}\n"
            f"---\n\n# {title}\n- {fact}\n"
        )
        body["memory_catalogue"] = list(body["memory_files"])
        row["gold"]["target_path"] = path
        row["gold"]["old_text"] = "- " + fact
        if cid == "public_010":
            row["gold"]["content_checks"] = [
                ["four", "四家", "4家", "4 家", "4 restaurants", "4 Korean"]
            ]
        if cid == "public_013":
            row["gold"]["content_checks"] = [["four", "四辆", "4辆", "4 辆", "4 bikes"]]
        if cid == "public_015":
            row["gold"]["content_checks"] = [
                ["seven", "七篇", "7篇", "7 篇", "7 stories", "7 short", "count: 7"]
            ]
        if cid == "public_016":
            body["memory_files"]["human.md"] = (
                "# Human\n- Learning interest: AI-powered medical diagnosis.\n"
            )
            row["gold"]["fact_assertions"] = [
                dict(
                    paths=["human.md"],
                    all_any=[
                        ["AI", "人工智能"],
                        ["medical", "医疗"],
                    ],
                )
            ]
        row["provenance"]["prior_memory_source_quote"] = old_quote
        row["provenance"]["memory_fixture_revision"] = (
            "Prior user statement normalized into a topic note; later user evidence unchanged"
        )
        audit.append(
            dict(
                case_id=cid,
                old_path="topics/reference-notes.md",
                new_path=path,
                reason="Prior user memory, not an unattributed pasted question",
            )
        )
    publish(ROOT / "v0_4", [(case, gold[case["case_id"]]) for case in cases])
    challenge = read_rows(ROOT / "v0_3_3_challenge/inputs.jsonl")
    labels = read_rows(ROOT / "v0_3_3_challenge/gold.jsonl")
    publish(ROOT / "v0_4_challenge", list(zip(challenge, labels, strict=True)))
    for folder in ["v0_4", "v0_4_challenge"]:
        path = ROOT / folder / "manifest.json"
        manifest = copy.deepcopy(json.loads((ROOT / "v0_3_3/manifest.json").read_text()))
        current = json.loads(path.read_text())
        manifest.update(
            current, version="0.4.0", total=len(read_rows(ROOT / folder / "inputs.jsonl"))
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    (ROOT / "v0_4/revisions.json").write_text(
        json.dumps(audit, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
