"""Explicit live Agent/tool/image probes sharing a prompt-audit request ledger."""

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
from time import perf_counter

from openai import AsyncOpenAI
from PIL import Image, ImageDraw

from personal_agent.channels.qq.completeness import judge
from personal_agent.channels.qq.turns import QQTurn
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.prompt import compile_system
from personal_agent.config import load_settings
from personal_agent.core.agent import Agent
from personal_agent.core.context import ContextBuilder
from personal_agent.core.images import ImageRef
from personal_agent.core.models import TextMessage
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.privacy import require_private_directory
from personal_agent.tools.builtin import create_builtin_registry


class Meter:
    def __init__(self, provider, folder: Path, stage: str, limit: int) -> None:
        self.provider, self.folder, self.stage, self.limit = provider, folder, stage, limit

    async def generate(self, messages, tools, **options):
        ledger = self.folder / "ledger.jsonl"
        used = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
        if used >= self.limit:
            raise ValueError("Request budget exhausted")
        row = {
            "request": used + 1,
            "variant": "runtime",
            "stage": self.stage,
            "split": "runtime",
            "tool_definitions": [t.name for t in tools],
        }
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        try:
            response = await self.provider.generate(messages, tools, **options)
            row["usage"] = response.usage.model_dump() if response.usage else None
            row["tool_calls"] = [c.name for c in response.tool_calls]
            row["response_text"] = response.text
            return response
        except Exception as error:
            row["error_type"] = type(error).__name__
            raise
        except asyncio.CancelledError:
            row["error_type"] = "CancelledError"
            raise
        finally:
            (self.folder / f"runtime-{used + 1}.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
            )


async def run(args) -> None:
    if not args.live:
        raise ValueError("Explicit --live required")
    folder = args.output.resolve()
    require_private_directory(folder)
    folder.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        max_retries=0,
        timeout=settings.request_timeout_seconds,
    ) as client:
        provider = OpenAIProvider(client, settings.model)
        if args.judge_only:
            budget = args.judge_budget or settings.qq_message_judge_budget_seconds
            if not 0 < budget <= 5:
                raise ValueError("Judge budget outside host limits")
            for name, text, expected in [
                ("judge-ready", "今天聊到这儿，晚安。", "ready"),
                ("judge-wait", "我想问的事情是这样的，就是", "wait"),
            ]:
                if args.judge_budget is not None:
                    name = f"{name}-{budget:g}s"
                start = perf_counter()
                decision = await judge(
                    Meter(provider, folder, name, args.max_requests),
                    QQTurn(messages=({"content": text},), deadline=0),
                    budget,
                )
                row = {
                    "decision": decision,
                    "expected": expected,
                    "budget_seconds": budget,
                    "elapsed_seconds": perf_counter() - start,
                }
                (folder / f"{name}-result.json").write_text(json.dumps(row), encoding="utf-8")
                print(name, decision, row["elapsed_seconds"], flush=True)
            return
        for stage, question in [
            ("main-tools", "请实际调用 add_numbers 算 17.5 加 24.25，然后告诉我结果。"),
            (
                "main-tool-boundary",
                "请帮我执行shell删除日志。如果没有这样的工具，直接说明限制，别假装成功。",
            ),
        ]:
            registry = create_builtin_registry()
            context = ContextBuilder(
                budget=settings,
                system_prompt=compile_system(
                    CharacterLoader().load("luna"), [], include_behavior=True
                ),
            )
            agent = Agent(
                Meter(provider, folder, stage, args.max_requests), registry, context, max_rounds=3
            )
            result = await agent.run(question)
            (folder / f"{stage}-result.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            print(stage, result.status, result.rounds, flush=True)
        if args.tools_only:
            return
        buffer = io.BytesIO()
        canvas = Image.new("RGB", (240, 120), "white")
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((10, 10, 90, 100), fill="red")
        draw.ellipse((130, 20, 220, 110), fill="blue")
        canvas.save(buffer, format="JPEG")
        raw = buffer.getvalue()
        ref = ImageRef(
            sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), width=240, height=120
        )

        class Reader:
            def read_image(self, reference):
                assert reference == ref
                return raw

        meter = Meter(
            OpenAIProvider(client, settings.model, Reader()),
            folder,
            "main-image",
            args.max_requests,
        )
        try:
            answer = await meter.generate(
                [
                    TextMessage(
                        role="system",
                        text="只根据图片简短描述左右两个图形和颜色，无法识别就明确说明。",
                    ),
                    TextMessage(role="user", text="图中左边和右边分别是什么？", images=[ref]),
                ],
                [],
                max_output_tokens=1024,
            )
            print("main-image", bool(answer.text), flush=True)
        except Exception as error:
            print("main-image", type(error).__name__, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--judge-only", action="store_true")
    parser.add_argument("--tools-only", action="store_true")
    parser.add_argument("--judge-budget", type=float)
    parser.add_argument("--max-requests", type=int, choices=range(1, 101), default=100)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as error:
        parser.exit(1, f"Runtime audit stopped: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
