"""Load and verify frozen, independently versioned corpus, gold and splits."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from personal_agent.evaluation.long_horizon.models import (
    BenchmarkProtocol,
    Config,
    Scenario,
    ScenarioGold,
    Schema,
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Corpus(Schema):
    version: str
    scenarios: list[Scenario]


class Gold(Schema):
    version: str
    scenarios: list[ScenarioGold]


class Splits(Schema):
    version: str
    splits: dict[str, list[str]]


@dataclass(frozen=True)
class Dataset:
    corpus: Corpus
    gold: Gold
    splits: Splits
    config: Config
    protocol: BenchmarkProtocol | None = None

    def __post_init__(self):
        ids = [s.scenario_id for s in self.corpus.scenarios]
        gold_ids = [g.scenario_id for g in self.gold.scenarios]
        if len(ids) != len(set(ids)) or sorted(ids) != sorted(gold_ids):
            raise ValueError("Scenario/gold coverage mismatch")
        for scenario, gold in zip(
            sorted(self.corpus.scenarios, key=lambda s: s.scenario_id),
            sorted(self.gold.scenarios, key=lambda g: g.scenario_id),
            strict=True,
        ):
            gold.validate_for(scenario)
        assigned = [sid for rows in self.splits.splits.values() for sid in rows]
        if sorted(assigned) != sorted(ids):
            raise ValueError("Splits must partition the corpus")
        if self.protocol:
            if {k: len(v) for k, v in self.splits.splits.items()} != self.protocol.expected_splits:
                raise ValueError("Protocol split sizes mismatch")
            if set(self.protocol.scenario_notes) != set(ids) or any(
                not self.protocol.min_turns <= len(s.turns) <= self.protocol.max_turns
                for s in self.corpus.scenarios
            ):
                raise ValueError("Protocol scenario coverage/length mismatch")
        if any(
            "routing" in s.tags and not self.gold_for(s.scenario_id).routing
            for s in self.corpus.scenarios
        ):
            raise ValueError("Routing scenarios require gold")

    @property
    def hashes(self) -> dict[str, str]:
        result = {
            name + "_hash": digest(getattr(self, name).model_dump(mode="json"))
            for name in ("corpus", "gold", "splits", "config")
        }
        if self.protocol:
            result["protocol_hash"] = digest(self.protocol.model_dump(mode="json"))
        return result

    def select(self, split: str, scenario_id: str | None = None) -> list[Scenario]:
        if split not in self.splits.splits:
            raise ValueError("Unknown split")
        ids = self.splits.splits[split]
        if scenario_id is not None and scenario_id not in ids:
            raise ValueError("Unknown scenario in split")
        return [
            s
            for s in self.corpus.scenarios
            if s.scenario_id in ids and (scenario_id is None or s.scenario_id == scenario_id)
        ]

    def gold_for(self, scenario_id: str) -> ScenarioGold:
        return next(g for g in self.gold.scenarios if g.scenario_id == scenario_id)


def load(root: Path) -> Dataset:
    values = {}
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (root / "protocol.json").exists() and "protocol_hash" not in manifest:
        raise ValueError("Protocol file requires a frozen protocol hash")
    for name, kind in (("corpus", Corpus), ("gold", Gold), ("splits", Splits), ("config", Config)):
        value = kind.model_validate_json((root / f"{name}.json").read_text(encoding="utf-8"))
        if digest(value.model_dump(mode="json")) != manifest.get(name + "_hash"):
            raise ValueError("Frozen dataset hash mismatch")
        values[name] = value
    if "protocol_hash" in manifest:
        protocol = BenchmarkProtocol.model_validate_json(
            (root / "protocol.json").read_text(encoding="utf-8")
        )
        if digest(protocol.model_dump(mode="json")) != manifest["protocol_hash"]:
            raise ValueError("Frozen protocol hash mismatch")
        values["protocol"] = protocol
    return Dataset(**values)
