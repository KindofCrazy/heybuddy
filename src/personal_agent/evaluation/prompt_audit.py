"""Opt-in, tool-free prompt probes on frozen synthetic evidence; no production storage."""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from openai import AsyncOpenAI

from personal_agent.automation.models import Worthiness
from personal_agent.automation.service import WORTHINESS_PROMPT
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.models import StateProposal
from personal_agent.character.prompt import compile_system
from personal_agent.character.state import STATE_PROMPT
from personal_agent.config import load_settings
from personal_agent.core.models import AssistantMessage, Model, TextMessage
from personal_agent.evaluation.prompt_audit_cases import cases, fresh_cases, positive_cases
from personal_agent.evaluation.prompt_audit_workflow import workflow_cases
from personal_agent.learning.curator import CURATE, Curation
from personal_agent.learning.models import ExistingFileDecision, Targets
from personal_agent.learning.service import LEARN_CONSOLIDATE, LEARN_EXTRACT, LEARN_SELECT
from personal_agent.llm.base import ProviderError
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.privacy import require_private_directory
from personal_agent.memory.retrieval import PREFLIGHT, RetrievalChoice
from personal_agent.organization.episodes import EPISODE_PROMPT
from personal_agent.organization.models import Segmentation, ThreadDecision, ThreadRepair
from personal_agent.organization.threads import REPAIR_PROMPT, THREAD_PROMPT
from personal_agent.proactive import attention, composer, scout
from personal_agent.proactive.models import AttentionDecision, ScoutResult
from personal_agent.reflection.models import Extraction
from personal_agent.reflection.service import EXTRACT


def stages() -> dict[str, tuple[str, type[Model] | None]]:
    return {
        "main": (compile_system(CharacterLoader().load("luna"), [], include_behavior=True), None),
        "episode": (EPISODE_PROMPT, Segmentation),
        "thread": (THREAD_PROMPT, ThreadDecision),
        "learning": (LEARN_EXTRACT, Extraction),
        "state": (STATE_PROMPT, StateProposal),
        "scout": (scout.PROMPT, ScoutResult),
        "attention": (attention.PROMPT, AttentionDecision),
        "composer": (composer.PROMPT, composer.MessageDraft),
        "retrieval": (PREFLIGHT, RetrievalChoice),
        "curator": (CURATE, Curation),
        "selection": (LEARN_SELECT, Targets),
        "consolidation": (LEARN_CONSOLIDATE, ExistingFileDecision),
        "reflection": (EXTRACT, Extraction),
        "worthiness": (WORTHINESS_PROMPT, Worthiness),
        "repair": (REPAIR_PROMPT, ThreadRepair),
    }


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def run(args: argparse.Namespace) -> None:
    folder = args.output.resolve()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", args.variant):
        raise ValueError("Invalid variant name")
    require_private_directory(folder)
    folder.mkdir(parents=True, exist_ok=True)
    definitions = stages()
    prompts = {key: pair[0] for key, pair in definitions.items()}
    if args.prompts:
        supplied = json.loads(args.prompts.read_text(encoding="utf-8"))
        if not set(supplied) <= set(definitions) or not all(
            isinstance(p, str) for p in supplied.values()
        ):
            raise ValueError("Invalid prompt snapshot")
        prompts.update(supplied)
    snapshot = folder / (args.variant + "-prompts.json")
    if snapshot.exists() and json.loads(snapshot.read_text(encoding="utf-8")) != prompts:
        raise ValueError("Variant already exists; keep comparison snapshots immutable")
    snapshot.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.live:
        return
    settings = load_settings()
    ledger = folder / "ledger.jsonl"
    used = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
    if args.split == "fresh":
        selected = fresh_cases()
    elif args.split.startswith("workflow"):
        selected = workflow_cases(args.split == "workflow-positive")
    else:
        selected = positive_cases() if args.split == "positive" else cases(args.split)
    if args.stages:
        names = args.stages.split(",")
        if len(names) != len(set(names)) or not set(names) <= selected.keys():
            raise ValueError("Unknown or duplicate stages")
        selected = {name: selected[name] for name in names}
    if used + len(selected) > args.max_requests:
        raise ValueError("Request cap would be exceeded; no calls made")
    if any((folder / f"{args.variant}-{stage}.json").exists() for stage in selected):
        raise ValueError("Results already exist; use a new variant for explicit repetition")
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        max_retries=0,
        timeout=settings.request_timeout_seconds,
    ) as client:
        provider = OpenAIProvider(client, settings.model)
        for stage, case in selected.items():
            prompt, schema = prompts[stage], definitions[stage][1]
            data = case["input"]
            if schema:
                data = {
                    "trust": "historical_data_only",
                    "response_schema": schema.model_json_schema(),
                    **data,
                }
            messages = [TextMessage(role="system", text=prompt)]
            messages.extend(
                AssistantMessage(text=m["text"])
                if m["role"] == "assistant"
                else TextMessage.model_validate(m)
                for m in case.get("history", [])
            )
            messages.append(TextMessage(role="user", text=json.dumps(data, ensure_ascii=False)))
            row = {
                "variant": args.variant,
                "split": args.split,
                "stage": stage,
                "case_hash": digest(case),
                "prompt_hash": digest(prompt),
                "model": settings.model,
                "request": used + 1,
            }
            # Charge attempted requests before network I/O, including failures and process exits.
            with ledger.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
            used += 1
            try:
                response = await provider.generate(messages, [], max_output_tokens=4096)
                row["response_text"] = response.text
                row["usage"] = response.usage.model_dump() if response.usage else None
                row["tool_calls"] = bool(response.tool_calls)
                result = (
                    schema.model_validate_json(response.text).model_dump(mode="json")
                    if schema
                    else response.text
                )
                row["schema_valid"] = not response.tool_calls
                row["result"] = result
            except Exception as error:
                row["schema_valid"] = False
                row["error_type"] = type(error).__name__
                if isinstance(error, ProviderError):
                    row["provider_code"] = error.code
                    if error.diagnostics and error.diagnostics.usage:
                        row["usage"] = error.diagnostics.usage.model_dump()
            (folder / f"{args.variant}-{stage}.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({k: row[k] for k in ("request", "stage", "schema_valid")}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--stages", help="Comma-separated stage names; default all ten")
    parser.add_argument(
        "--split",
        choices=("dev", "holdout", "positive", "workflow", "workflow-positive", "fresh"),
        default="dev",
    )
    parser.add_argument("--prompts", type=Path)
    parser.add_argument("--max-requests", type=int, choices=range(1, 101), default=30)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as error:
        parser.exit(1, f"Prompt audit stopped: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
