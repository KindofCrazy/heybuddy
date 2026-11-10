"""Trusted, explicitly approved upper-bound accounting; never retries the failed request."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from personal_agent.evaluation.heybuddy_bench.experiment import (
    ExperimentProvider,
    SessionBudget,
    cny,
    production_code_hash,
    write_json,
)
from personal_agent.evaluation.heybuddy_bench.long_data import DATA, score, validate
from personal_agent.evaluation.heybuddy_bench.long_replay import run_cases
from personal_agent.evaluation.heybuddy_bench.longitudinal import implementation_hash, select
from personal_agent.evaluation.heybuddy_bench.sequence_data import rows, sha
from personal_agent.evaluation.heybuddy_bench.sequence_data import score as score_short
from personal_agent.evaluation.runner import Budget, EvaluationLimit
from personal_agent.file_lock import FileLock
from personal_agent.memory.privacy import require_private_directory


class ReservedUsageBudget(SessionBudget):
    """Keep Provider token totals exact; separately charge approved unknown ceilings.

    Meter's internal usage_complete indicates that NEW calls are accounted safely. The
    persisted/public usage_complete remains False because the old Provider usage is unknown.
    accounting_complete authorizes continuation only after a trusted, bounded reservation.
    """

    def __init__(self, path, protocol, protocol_hash):
        Budget.__init__(
            self,
            protocol["max_requests"],
            protocol["max_tokens"],
            input_price=protocol["input_price_cny"],
            output_price=protocol["output_price_cny"],
            max_cost=protocol["max_cost_cny"],
        )
        self.path, self.protocol_hash = path, protocol_hash
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            state["protocol_hash"] != protocol_hash
            or state["pending"]
            or not state.get("accounting_complete")
        ):
            raise EvaluationLimit("unreviewed_unknown_usage")
        self.reservations = state["unknown_usage_reservations"]
        self.unknown_cost = sum(r["cost_cny_ceiling"] for r in self.reservations)
        self.unknown_tokens = sum(
            r["input_token_ceiling"] + r["output_token_ceiling"] for r in self.reservations
        )
        for name in ("requests", "input_tokens", "output_tokens", "reasoning_tokens"):
            setattr(self, name, state[name])
        self.pending = False
        self.reserved_tokens = self.input_tokens + self.output_tokens + self.unknown_tokens
        self.reserved_cost = (
            self.input_tokens * self.input_price + self.output_tokens * self.output_price
        ) / 1_000_000 + self.unknown_cost

    def state(self):
        state = super().state()
        state.update(
            usage_complete=False,
            accounting_complete=self.usage_complete,
            unknown_usage_reservations=self.reservations,
            cost_cny_upper_estimate=state["cost_cny_upper_estimate"] + self.unknown_cost,
            token_totals_note=(
                "input/output/reasoning totals include ONLY known Provider usage; "
                "unknown bounds are separate"
            ),
        )
        return state

    def observe(self, usage):
        Budget.observe(self, usage)
        if usage is not None:
            self.pending = False
            self.reserved_tokens = self.input_tokens + self.output_tokens + self.unknown_tokens
            self.reserved_cost = (
                self.input_tokens * self.input_price + self.output_tokens * self.output_price
            ) / 1_000_000 + self.unknown_cost
        write_json(self.path, self.state())


def settle(root, protocol, protocol_hash):
    path = root / "long-session.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    receipt_path = root / "upper-reservation-001.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return ReservedUsageBudget(path, protocol, protocol_hash), receipt
    proposal = json.loads((root / "pending-accounting-proposal.json").read_text(encoding="utf-8"))
    if state != proposal["ledger_before"] or not state["pending"] or state["usage_complete"]:
        raise ValueError("Pending ledger differs from explicitly approved proposal")
    inp, out = proposal["input_ceiling"], proposal["output_ceiling"]
    bound = (inp * protocol["input_price_cny"] + out * protocol["output_price_cny"]) / 1_000_000
    if abs(bound - proposal["unknown_cost_ceiling"]) > 1e-9 or abs(bound - 0.05483) > 1e-9:
        raise ValueError("Only the user-approved CNY0.05483 request can be reconciled")
    if abs(state["reserved_cost_cny"] - state["cost_cny_upper_estimate"] - bound) > 1e-9:
        raise ValueError("Reservation does not match pending request")
    reservation = dict(
        request="live-long-dev/" + proposal["request_file"],
        request_sha256=sha(root / "live-long-dev" / proposal["request_file"]),
        input_token_ceiling=inp,
        output_token_ceiling=out,
        cost_cny_ceiling=bound,
        provider_usage_known=False,
        approved_by="user, 2026-11-10",
        retry=False,
    )
    receipt = dict(
        action=(
            "Preserve known Provider totals; permanently occupy full pending reservation; "
            "no refund/no retry; continue unstarted cases only"
        ),
        ledger_before=state,
        reservation=reservation,
        protocol_hash=protocol_hash,
        model_policy_changed=False,
        scorer_changed=False,
    )
    write_json(root / "long-session-before-reservation-001.json", state)
    write_json(receipt_path, receipt)
    updated = {
        **state,
        "pending": False,
        "usage_complete": False,
        "accounting_complete": True,
        "unknown_usage_reservations": [reservation],
        "cost_cny_upper_estimate": state["cost_cny_upper_estimate"] + bound,
    }
    write_json(path, updated)
    return ReservedUsageBudget(path, protocol, protocol_hash), receipt


async def continuation(args, stage, budget, provider, receipt):
    cases = select(args.dataset, stage)
    old_path = args.root / ("live-long-" + stage) / "predictions.jsonl"
    prior = rows(old_path) if old_path.exists() else []
    prior_ids = {p["case_id"] for p in prior}
    if len(prior_ids) != len(prior) or prior_ids - {c["case_id"] for c in cases}:
        raise ValueError("Original predictions mismatch stage")
    remaining = [c for c in cases if c["case_id"] not in prior_ids]
    output = args.root / ("live-long-" + stage + "-reconciled")
    if output.exists():
        raise ValueError("Continuation exists; never rerun original or completed cases")
    output.mkdir()
    with (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for result in prior:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        stream.flush()

        def save(result):
            stream.write(json.dumps(cny(result), ensure_ascii=False) + "\n")
            stream.flush()
            print(
                json.dumps(
                    dict(
                        stage=stage,
                        case_id=result["case_id"],
                        steps=len(result["steps"]),
                        error_code=result["error_code"],
                        requests=budget.requests,
                        cost_cny_upper_estimate=budget.state()["cost_cny_upper_estimate"],
                    )
                ),
                flush=True,
            )

        def event(cid, sid, error):
            if sid == "review" or (sid.startswith("u") and int(sid[1:]) % 8 == 0):
                print(
                    json.dumps(
                        dict(case_id=cid, step=sid, error_code=error, requests=budget.requests)
                    ),
                    flush=True,
                )

        new = await run_cases(
            remaining,
            temp_parent=output / "temporary",
            budget=budget,
            provider=ExperimentProvider(provider, output),
            short=stage == "short-reserved",
            on_result=save,
            on_step=event,
        )
    results = prior + new
    report = (
        score_short(cases, rows(args.dataset.parent / "sequence_v1/gold.jsonl"), results)
        if stage == "short-reserved"
        else score(cases, rows(args.dataset / "long/gold.jsonl"), results)
    )
    report.update(
        stage=stage,
        usage=budget.state(),
        errors=[
            r["case_id"]
            for r in results
            if r["error_code"] or any(s.get("error_code") for s in r["steps"])
        ],
        hashes=dict(
            suite=sha(args.dataset / "manifest.json"),
            evaluation=implementation_hash(),
            production=production_code_hash(),
            reconciliation=sha(args.root / "upper-reservation-001.json"),
            continuation_code=sha(Path(__file__)),
        ),
        configuration=dict(
            memory="unchanged explicit checkpoints and persistent state",
            proactive="unchanged incremental Scout/one final Attention; no Sender",
            routing="unchanged production rules/recent24/LLM12",
            assistant="fixed acknowledgements; no end-to-end answer evaluation",
            billing="known Provider tokens + separately reserved unknown upper bound",
        ),
        continuation=dict(
            preserved=len(prior),
            newly_attempted=len(new),
            original_failed_request_retried=False,
            receipt_request=receipt["reservation"]["request"],
        ),
    )
    write_json(output / "report.json", report)
    print(
        json.dumps(
            dict(stage=stage, expected=len(cases), observed=len(results), usage=budget.state())
        ),
        flush=True,
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATA)
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/sequence_20261110")
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--approve-reservation",
        action="store_true",
        help="Trusted user approval for the single frozen CNY0.05483 reservation",
    )
    args = parser.parse_args()
    if not args.live or not args.approve_reservation:
        parser.error("Explicit --live and --approve-reservation are required")
    validate(args.dataset)
    require_private_directory(args.root.absolute())
    protocol = json.loads((args.dataset / "live_protocol.json").read_text(encoding="utf-8"))
    frozen = json.loads((args.root / "long-freeze.json").read_text(encoding="utf-8"))
    if frozen != dict(
        protocol=sha(args.dataset / "live_protocol.json"),
        evaluation=implementation_hash(),
        production=production_code_hash(),
    ):
        raise ValueError("Frozen model/scorer implementation changed")
    from openai import AsyncOpenAI
    from pydantic import ValidationError

    from personal_agent.config import load_settings
    from personal_agent.llm.openai_provider import OpenAIProvider

    try:
        settings = load_settings()
    except (ValidationError, OSError):
        raise ValueError("Configuration unavailable") from None
    endpoint = urlsplit(str(settings.base_url))
    if (
        settings.model != protocol["model"]
        or endpoint.scheme != "https"
        or endpoint.hostname != protocol["endpoint_host"]
        or endpoint.port not in {None, 443}
        or endpoint.username
        or endpoint.password
    ):
        raise ValueError("Model/endpoint differ from priced protocol")
    with FileLock(args.root / "session.lock"):
        budget, receipt = settle(args.root, protocol, sha(args.dataset / "live_protocol.json"))
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            max_retries=0,
            timeout=60,
        ) as client:
            provider = OpenAIProvider(client, settings.model, json_mode=True)
            for stage in ["dev", "reserved", "short-reserved"]:
                await continuation(args, stage, budget, provider, receipt)
                if budget.exhausted or not budget.usage_complete:
                    raise EvaluationLimit("Stop: new unaccounted usage or cumulative cap reached")


if __name__ == "__main__":
    asyncio.run(main())
