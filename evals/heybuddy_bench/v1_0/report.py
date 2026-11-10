"""Body-free 360-case report from frozen saved outcomes. No inference and no credentials."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
TRACKS = {
    "thread_routing": "routing",
    "memory_learning": "memory",
    "proactive_decision": "proactive",
}
LABELS = {"routing": "Thread Routing", "memory": "Memory Learning", "proactive": "Proactive"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s]


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def rate(passed, total):
    return round(100 * passed / total, 2) if total else None


def export_stage(root, stage):
    directory = root / ("live-long-" + stage)
    reconciled = root / ("live-long-" + stage + "-reconciled")
    if (reconciled / "report.json").exists():
        directory = reconciled
    if not (directory / "report.json").exists():
        return None
    report = read(directory / "report.json")
    predictions = rows(directory / "predictions.jsonl")
    observed = {p["case_id"]: p for p in predictions}
    source_path = (
        ROOT.parent / "sequence_v1/inputs.jsonl"
        if stage == "short-reserved"
        else ROOT / "long/inputs.jsonl"
    )
    inputs = {c["case_id"]: c for c in rows(source_path)}
    details = []
    for item in report["details"]:
        prediction = observed.get(item["case_id"], {})
        checks = item["checks"]
        details.append(
            dict(
                case_id=item["case_id"],
                track=item["track"],
                family=item["family"],
                split=item.get("split", "holdout"),
                observed=item["case_id"] in observed,
                passed=item["passed"],
                final_passed=item.get("final_passed", checks[-1]["passed"]),
                checkpoints=len(checks),
                checkpoints_passed=sum(c["passed"] for c in checks),
                failed_types=dict(
                    Counter(
                        k.split(":", 1)[0]
                        for c in checks
                        for k, ok in c["assertions"].items()
                        if not ok
                    )
                ),
                error_codes=sorted(
                    {s["error_code"] for s in prediction.get("steps", []) if s.get("error_code")}
                    | ({prediction["error_code"]} if prediction.get("error_code") else set())
                ),
                observed_steps=len(prediction.get("steps", [])),
                user_messages=item.get("user_messages"),
                planned_steps=len(inputs[item["case_id"]]["input"]["steps"]),
                observed_user_messages=sum(
                    bool(s.get("text"))
                    for s in inputs[item["case_id"]]["input"]["steps"][
                        : len(prediction.get("steps", []))
                    ]
                ),
            )
        )
    public = dict(
        stage=stage,
        expected=report["expected"],
        observed=report["observed"],
        tracks=report["tracks"],
        details=details,
        hashes=report["hashes"],
        usage=report["usage"],
        configuration=report["configuration"],
        human_reviewed=False,
    )
    write(ROOT / "results" / (stage + ".json"), public)
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/sequence_20261110")
    )
    args = parser.parse_args()
    base = ROOT.parent
    suite = read(ROOT / "manifest.json")
    for subset in suite["subsets"]:
        for filename, checksum in subset["sha256"].items():
            if (
                hashlib.sha256((base / subset["path"] / filename).read_bytes()).hexdigest()
                != checksum
            ):
                raise ValueError("Frozen suite data changed")
    cycle = read(base / "results/cycle_20261108_summary.json")
    collected = []
    for subset, stages, part in [
        ("primary", ["verification-development"], "primary"),
        ("challenge", ["verification-development", "final-reserved"], "challenge"),
    ]:
        for stage in stages:
            for item in cycle["stages"][stage]["reports"][part]["cases"]:
                collected.append(
                    dict(
                        subset=subset,
                        case_id=item["case_id"],
                        track=TRACKS[item["track"]],
                        family=item["category"],
                        observed=True,
                        passed=item["decision_correct"],
                        source_stage=stage,
                    )
                )
    short = read(base / "sequence_v1/results/initial_dev.json")
    if short["observed"] != 42:
        raise ValueError("Prior short development results incomplete")
    failed = {c["case_id"] for c in short["failed_checkpoints"]} | set(
        short["execution_error_cases"]
    )
    for case in rows(base / "sequence_v1/inputs.jsonl"):
        if case["split"] == "dev":
            collected.append(
                dict(
                    subset="short",
                    case_id=case["case_id"],
                    track=case["track"],
                    family=case["family"],
                    observed=True,
                    passed=case["case_id"] not in failed,
                    source_stage="initial-short-dev",
                )
            )
    exports = {
        stage: export_stage(args.root, stage) for stage in ["dev", "reserved", "short-reserved"]
    }
    hashes = {tuple(sorted(r["hashes"].items())) for r in exports.values() if r}
    if len(hashes) > 1:
        raise ValueError("New stages used different frozen implementations")
    for subset, path, stages in [
        ("long", ROOT / "long/inputs.jsonl", ["dev", "reserved"]),
        ("short", base / "sequence_v1/inputs.jsonl", ["short-reserved"]),
    ]:
        available = {
            d["case_id"]: {**d, "source_stage": stage}
            for stage in stages
            if exports[stage]
            for d in exports[stage]["details"]
        }
        for case in rows(path):
            if subset == "short" and case["split"] != "holdout":
                continue
            item = available.get(case["case_id"], {})
            collected.append(
                dict(
                    subset=subset,
                    case_id=case["case_id"],
                    track=case["track"],
                    family=case["family"],
                    split=case["split"],
                    observed=item.get("observed", False),
                    passed=item.get("passed", False),
                    source_stage=item.get("source_stage", "missing"),
                )
            )
    if len(collected) != 360 or len({(r["subset"], r["case_id"]) for r in collected}) != 360:
        raise ValueError("Overall denominator must be 360 unique named cases")
    tables = {}
    for subset in ["primary", "challenge", "short", "long", "all"]:
        tables[subset] = {}
        for track in LABELS:
            selected = [
                c
                for c in collected
                if c["track"] == track and (subset == "all" or c["subset"] == subset)
            ]
            passed = sum(c["passed"] for c in selected)
            tables[subset][track] = dict(
                expected=len(selected),
                observed=sum(c["observed"] for c in selected),
                passed=passed,
                pass_rate=rate(passed, len(selected)),
            )
    capabilities = defaultdict(lambda: dict(expected=0, observed=0, passed=0))
    for item in collected:
        cap = capabilities[item["subset"] + "/" + item["track"] + "/" + item["family"]]
        cap["expected"] += 1
        cap["observed"] += int(item["observed"])
        cap["passed"] += int(item["passed"])
    ledger = read(args.root / "long-session.json")
    if ledger["pending"] or not ledger.get("accounting_complete", ledger["usage_complete"]):
        raise ValueError("Billing is incomplete; do not publish a final cumulative cost")
    summary = dict(
        version="1.0.0",
        expected=360,
        observed=sum(c["observed"] for c in collected),
        passed=sum(c["passed"] for c in collected),
        pass_rate=rate(sum(c["passed"] for c in collected), 360),
        tables=tables,
        capabilities=dict(capabilities),
        cases=collected,
        new_stages=[s for s, r in exports.items() if r],
        new_usage=ledger,
        unknown_usage_requests=len(ledger.get("unknown_usage_reservations", [])),
        long_fully_replayed=sum(
            d["observed_steps"] == d["planned_steps"]
            for stage in ["dev", "reserved"]
            if exports[stage]
            for d in exports[stage]["details"]
        ),
        long_observed_user_messages=sum(
            d["observed_user_messages"]
            for stage in ["dev", "reserved"]
            if exports[stage]
            for d in exports[stage]["details"]
        ),
        long_planned_user_messages=3120,
        human_reviewed=False,
        aggregation=(
            "One case one vote; frozen latest final snapshots + short + long sequences. "
            "Missing/errors fail; smoke repeats and diagnostic probes excluded."
        ),
        limitations=[
            "Historical snapshot results are reused; not a single newly rerun 360-case experiment.",
            "Gold is Agent-authored/reviewed, not independently human verified.",
            "Long user inputs and acknowledgements are fixed; assistant answers are not generated.",
            "Lexical/structural checks; no general Faithfulness or user-satisfaction score.",
            "Templates/variants are correlated; 360 is not an independent sample count.",
            "Controlled checkpoints; automatic scheduling and real delivery remain untested.",
            "Unknown usage is separately charged at the user-approved full reservation ceiling.",
        ],
        hashes=dict(
            suite=hashlib.sha256((ROOT / "manifest.json").read_bytes()).hexdigest(),
            protocol=hashlib.sha256((ROOT / "live_protocol.json").read_bytes()).hexdigest(),
            historical_cycle=hashlib.sha256(
                (base / "results/cycle_20261108_summary.json").read_bytes()
            ).hexdigest(),
        ),
    )
    write(ROOT / "results/summary.json", summary)
    lines = [
        "# HeyBuddy-Bench v1.0：360 案例结果",
        "",
        (
            f"已测 {summary['observed']}/360，综合通过 {summary['passed']}/360"
            f"（{summary['pass_rate']}%）。每个案例一票；缺失按失败保留分母。"
        ),
        "",
        "| 集合 | Routing | Memory | Proactive |",
        "| --- | ---: | ---: | ---: |",
    ]
    for subset, label in [
        ("primary", "主集"),
        ("challenge", "挑战集"),
        ("short", "短序列"),
        ("long", "长序列"),
        ("all", "合计"),
    ]:
        values = [tables[subset][t] for t in LABELS]
        lines.append(
            "| "
            + label
            + " | "
            + " | ".join(f"{r['passed']}/{r['expected']}（{r['pass_rate']}%）" for r in values)
            + " |"
        )
    lines += [
        "",
        (
            "新增长序列为 120 个场景、3,120 条预定用户消息，每场景 20–32 条；"
            "未必每条都触发模型调用。"
            "Memory 按五个检查点消费中间新增 Run；Proactive 逐用户 Run 调用 Scout，"
            "并在最终时刻尝试一次 Attention。输出状态来自实际生产模块，不补入 Gold。"
        ),
        "",
        "新增长序列的最终状态与检查点成绩：",
        "",
        "| Part | 整场景 | 最终检查 | 全部检查点 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for track, label in LABELS.items():
        selected = [
            d
            for stage in ["dev", "reserved"]
            if exports[stage]
            for d in exports[stage]["details"]
            if d["track"] == track
        ]
        lines.append(
            f"| {label} | {sum(d['passed'] for d in selected)}/40 "
            f"| {sum(d['final_passed'] for d in selected)}/40 "
            f"| {sum(d['checkpoints_passed'] for d in selected)}/"
            f"{sum(d['checkpoints'] for d in selected)} |"
        )
    lines += [
        "",
        (
            f"本轮承接旧支出后的累计费用上界估计 ¥{ledger['cost_cny_upper_estimate']:.6f}，"
            f"累计请求 {ledger['requests']}，没有重置 ¥20 额度。"
            "费用包含此前短序列和重复 smoke；不等于外部最终账单。"
        ),
        (
            f"其中 {len(ledger.get('unknown_usage_reservations', []))} 次请求的 usage 未知，"
            "按用户批准的完整请求预留上界单独计费；input/output token 总数仅包含已知用量，"
            "没有伪造未知 token。原始超时场景保留失败，没有重试或从头重跑。"
        ),
        (
            f"长序列完整回放 {summary['long_fully_replayed']}/120，实际消费用户消息 "
            f"{summary['long_observed_user_messages']}/3120；中断案例的剩余检查仍计失败。"
        ),
        "",
        (
            "原 150 主集及 30 挑战题使用保存的最终结果，没有为本轮重新调用模型；"
            "42 个短序列开发场景也沿用冻结结果。本轮补测 18 个短序列保留场景，并新测 120 个长序列。"
            "四道事后诊断探针不计入 360，重复 smoke 不重复计分。"
        ),
        "",
        "该指标是工程综合通过率。短序列、长序列使用整场景通过口径，和快照的决策/状态通过口径难度不同；标签尚未人工复核，同模板变体相关。不能称为真实用户泛化准确率或完整长期任务成功率。长序列的助手回应是固定确认文本，未测试生成回答质量、自动调度、真实主动发送或独立语义忠实性。",
        "",
        (
            "机器可读明细及按能力分组见 [summary.json](results/summary.json)；"
            "新阶段无正文结果分别见 results/dev.json、reserved.json 和 short-reserved.json。"
            "原始请求、响应、文件状态和 SQLite 不提交。"
        ),
    ]
    (ROOT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: summary[k] for k in ["expected", "observed", "passed", "pass_rate"]}))


if __name__ == "__main__":
    main()
