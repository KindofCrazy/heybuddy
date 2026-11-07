"""Validate all 150 inputs, gold, temporal/source contracts and frozen hashes."""

from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import validate

if __name__ == "__main__":
    print(validate(Path(__file__).resolve().parent))
