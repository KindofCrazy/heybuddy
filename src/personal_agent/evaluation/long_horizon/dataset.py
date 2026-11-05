"""Load and verify frozen, independently versioned corpus, gold and splits."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from personal_agent.evaluation.long_horizon.models import Config, Scenario, ScenarioGold, Schema


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

    @property
    def hashes(self) -> dict[str, str]:
        return {
            name + "_hash": digest(getattr(self, name).model_dump(mode="json"))
            for name in ("corpus", "gold", "splits", "config")
        }

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
    for name, kind in (("corpus", Corpus), ("gold", Gold), ("splits", Splits), ("config", Config)):
        value = kind.model_validate_json((root / f"{name}.json").read_text(encoding="utf-8"))
        if digest(value.model_dump(mode="json")) != manifest.get(name + "_hash"):
            raise ValueError("Frozen dataset hash mismatch")
        values[name] = value
    return Dataset(**values)
