"""Private replay bundles and body-free summaries, re-scoreable without inference."""

import asyncio
import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from uuid import uuid4

from personal_agent.evaluation.long_horizon.dataset import Corpus, Dataset, Gold, Splits, digest
from personal_agent.evaluation.long_horizon.metrics import SCORER_VERSION, aggregate, score
from personal_agent.evaluation.long_horizon.models import (
    BenchmarkProtocol,
    Config,
    ReplayResult,
    Schema,
)
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.memory.privacy import require_private_directory


class Bundle(Schema):
    version: str = "1"
    evaluation_type: str = "Offline Contract Test"
    corpus: Corpus
    gold: Gold
    splits: Splits
    config: Config
    protocol: BenchmarkProtocol | None = None
    split: str
    scenario_id: str | None
    git_commit: str | None
    git_dirty: bool
    code_hash: str | None = None
    hashes: dict[str, str]
    results: list[ReplayResult]


def write(path: Path, value: object) -> None:
    # Publish each file atomically; only a completed bundle is eligible for scoring.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def summary(bundle: Bundle) -> dict:
    dataset = Dataset(bundle.corpus, bundle.gold, bundle.splits, bundle.config, bundle.protocol)
    if dataset.hashes != bundle.hashes or bundle.version != "1":
        raise ValueError("Result provenance mismatch")
    if bundle.evaluation_type != "Offline Contract Test":
        raise ValueError("Unsupported evaluation type")
    scenarios = dataset.select(bundle.split, bundle.scenario_id)
    if [r.scenario_id for r in bundle.results] != [s.scenario_id for s in scenarios]:
        raise ValueError("Result coverage incomplete")
    rows = {}
    predictions = []
    for scenario, result in zip(scenarios, bundle.results, strict=True):
        result.validate_for(scenario)
        rows[scenario.scenario_id] = score(
            result.predictions, dataset.gold_for(scenario.scenario_id)
        )
        rows[scenario.scenario_id]["replay"] = {
            "completed_turns": len(result.predictions),
            "checkpoint_snapshots": len(result.snapshots),
            "actual_requests": sum(p.request_count for p in result.predictions),
        }
        predictions.extend(result.predictions)
    latencies = sorted(p.latency_ms for p in predictions)
    return {
        "evaluation_type": bundle.evaluation_type,
        "scorer_version": SCORER_VERSION,
        "status": "completed",
        "replay_mode": bundle.config.replay_mode,
        "variant": bundle.config.model_dump(mode="json"),
        "split": bundle.split,
        "annotation_status": (
            "human-reviewed"
            if bundle.protocol.human_reviewed
            else "agent-authored provisional gold"
        )
        if bundle.protocol
        else "legacy",
        **bundle.hashes,
        "git_commit": bundle.git_commit,
        "git_dirty": bundle.git_dirty,
        "code_hash": bundle.code_hash,
        "scenarios": rows,
        "split_aggregate": aggregate(list(rows.values())),
        "errors_by_stage": {stage: {} for stage in ("setup", "routing", "journal", "checkpoint")},
        "routing_reasons": dict(Counter(p.routing_reason for p in predictions)),
        "usage": {
            "actual_requests": sum(p.request_count for p in predictions),
            "input_tokens": "unknown",
            "output_tokens": "unknown",
        },
        "latency_ms": {
            "support": len(latencies),
            "p50": latencies[math.ceil(0.5 * len(latencies)) - 1] if len(latencies) >= 2 else None,
            "p95": latencies[math.ceil(0.95 * len(latencies)) - 1]
            if len(latencies) >= 20
            else None,
        },
    }


def prepare_output(output: Path):
    require_private_directory(output)
    output.mkdir(parents=True, exist_ok=False)
    write(output / "status.json", {"status": "running"})
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=5
    )


def working_tree_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False, timeout=5
    )
    return result.returncode != 0 or bool(result.stdout.strip())


def evaluation_code_hash() -> str:
    package = Path(__file__).resolve().parents[2]
    paths = [
        *Path(__file__).parent.glob("*.py"),
        package / "organization/router.py",
        package / "storage/routing.py",
        package / "llm/openai_provider.py",
        package / "organization/episodes.py",
        package / "organization/threads.py",
        package / "organization/provider.py",
        package / "storage/episodes.py",
        package / "storage/threads.py",
        package / "storage/thread_repair.py",
    ]
    return digest({p.relative_to(package).as_posix(): p.read_text(encoding="utf-8") for p in paths})


async def evaluate(dataset: Dataset, output: Path, *, split="dev", scenario_id=None) -> dict:
    scenarios = dataset.select(split, scenario_id)
    revision = prepare_output(output)
    results = []
    try:
        for scenario in scenarios:
            results.append(await replay(scenario))
        bundle = Bundle(
            corpus=dataset.corpus,
            gold=dataset.gold,
            splits=dataset.splits,
            config=dataset.config,
            protocol=dataset.protocol,
            split=split,
            scenario_id=scenario_id,
            git_commit=revision.stdout.strip() if revision.returncode == 0 else None,
            git_dirty=working_tree_dirty(),
            code_hash=evaluation_code_hash(),
            hashes=dataset.hashes,
            results=results,
        )
        payload = bundle.model_dump(mode="json")
        write(output / "private-trace.json", payload)
        write(output / "trace-hash.json", {"sha256": digest(payload)})
        report = summary(bundle)
        write(output / "summary.json", report)
        write(output / "status.json", {"status": "completed"})
        return report
    except asyncio.CancelledError:
        write(output / "status.json", {"status": "cancelled"})
        raise
    except Exception:
        errors = Counter((r.error_stage, r.error_code) for r in results if r.status != "completed")
        write(
            output / "status.json",
            {
                "status": "failed",
                "errors": [
                    {"stage": stage, "code": code, "count": count}
                    for (stage, code), count in errors.items()
                ],
            },
        )
        raise


def recompute(output: Path) -> dict:
    require_private_directory(output)
    if json.loads((output / "status.json").read_text(encoding="utf-8")) != {"status": "completed"}:
        raise ValueError("Result is incomplete")
    raw = (output / "private-trace.json").read_text(encoding="utf-8")
    expected = json.loads((output / "trace-hash.json").read_text(encoding="utf-8"))["sha256"]
    if digest(json.loads(raw)) != expected:
        raise ValueError("Trace hash mismatch")
    report = summary(Bundle.model_validate_json(raw))
    write(output / "summary.json", report)
    return report


def default_output() -> Path:
    return Path(".heybuddy/evaluations/long-horizon") / uuid4().hex
