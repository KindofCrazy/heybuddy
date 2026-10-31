"""Explicit synthetic checks through production Organizer and multi-turn Agent paths."""

import argparse
import asyncio
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from openai import AsyncOpenAI
from PIL import Image, ImageDraw

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.config import load_settings
from personal_agent.core.agent import Agent
from personal_agent.core.context import ContextBuilder
from personal_agent.core.images import ImageRef
from personal_agent.evaluation.flow_runtime import Meter
from personal_agent.evaluation.p1 import evaluate
from personal_agent.evaluation.runner import Budget
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.privacy import require_private_directory
from personal_agent.tools.registry import ToolRegistry

CONVERSATION = (
    ("2026-10-30 23:00:00", "我准备睡了，晚安。"),
    ("2026-10-31 10:00:00", "在吗"),
    ("2026-10-31 10:01:00", "明天要和老师商量开题，不是去争吵。"),
    ("2026-10-31 10:02:00", "别让我讨论完回来汇报，也别一直反问我。"),
    ("2026-10-31 10:03:00", "我今天有点累，只想聊一句，不要建议。"),
    ("2026-10-31 10:04:00", "给我一个列表推导式把 nums 翻倍，只给表达式。"),
    ("2026-10-31 10:05:00", "技术题可以讲详细些，解释 Python 字典 get 的默认值，给一个例子。"),
    ("2026-10-31 10:06:00", "先这样，晚安。"),
)
FRESH_CONVERSATION = (
    ("2026-11-03 23:40:00", "先睡了，明早聊。"),
    ("2026-11-04 09:00:00", "早，在吗"),
    ("2026-11-04 09:01:00", "周日把采访提纲给校刊，是提纲，不是成稿。"),
    ("2026-11-04 09:02:00", "你少问几个问题就好，正常聊天。"),
    ("2026-11-04 09:03:00", "我有点烦，就陪我聊一句，不要解决方案。"),
    ("2026-11-04 09:04:00", "只给我一个取 items 最后一项的表达式。"),
    (
        "2026-11-04 09:05:00",
        "解释 dict.get 和 setdefault 的默认参数是否都会立即求值，用一个小例子。",
    ),
    ("2026-11-04 09:06:00", "聊到这儿，晚安。"),
)


class FixtureImages:
    def __init__(self, reference: ImageRef, raw: bytes) -> None:
        self.reference, self.raw = reference, raw

    def read_image(self, reference: ImageRef) -> bytes:
        assert reference == self.reference
        return self.raw


async def run(args: argparse.Namespace) -> None:
    if not args.live:
        raise ValueError("Explicit --live required")
    folder = args.output.resolve()
    require_private_directory(folder)
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / f"{args.mode}-{args.variant}.json"
    if result_path.exists():
        raise ValueError("Result exists; use a new variant")
    settings = load_settings()
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        max_retries=0,
        timeout=settings.request_timeout_seconds,
    ) as client:
        provider = OpenAIProvider(client, settings.model)
        meter = Meter(provider, folder, args.mode + "-" + args.variant, args.max_requests)
        if args.mode == "pipeline":
            result = await evaluate(
                folder / ("pipeline-" + uuid4().hex[:8]),
                meter,
                settings,
                Budget(args.max_requests, 2_000_000),
            )
        elif args.mode == "conversation":
            temporal = {}
            context = ContextBuilder(
                budget=settings,
                character=CharacterContextCompiler(CharacterLoader().load("luna")),
                temporal=lambda: temporal.copy(),
            )
            agent = Agent(meter, ToolRegistry(), context, max_rounds=3)
            result = []
            previous = None
            conversation = CONVERSATION if args.case_set == "dev" else FRESH_CONVERSATION
            for index, (at, text) in enumerate(conversation):
                temporal.update(
                    kind="temporal_context",
                    trust="host_clock_data",
                    current_time=at,
                    previous_interaction_at=previous,
                    seconds_since_previous_interaction=(
                        (
                            datetime.fromisoformat(at) - datetime.fromisoformat(previous)
                        ).total_seconds()
                        if previous
                        else None
                    ),
                )
                reply = await agent.run(text)
                result.append(reply.model_dump(mode="json"))
                previous = at
                print(json.dumps({"turn": index + 1, "status": reply.status}), flush=True)
        else:
            result = []
            for name, bounds in (
                ("near_square", (10, 10, 90, 100)),
                ("rectangle", (10, 30, 110, 80)),
            ):
                canvas = Image.new("RGB", (240, 120), "white")
                draw = ImageDraw.Draw(canvas)
                draw.rectangle(bounds, fill="red")
                draw.ellipse((130, 20, 220, 110), fill="blue")
                if args.case_set == "fresh":
                    canvas = Image.new("RGB", (240, 120), "white")
                    draw = ImageDraw.Draw(canvas)
                    draw.ellipse((10, 20, 100, 110), fill="green")
                    right = (140, 15, 220, 105) if name == "near_square" else (130, 35, 220, 85)
                    draw.rectangle(right, fill="yellow")
                if args.image_scale != 1:
                    canvas = canvas.resize(
                        (240 * args.image_scale, 120 * args.image_scale), Image.Resampling.NEAREST
                    )
                buffer = io.BytesIO()
                canvas.save(buffer, format="JPEG")
                raw = buffer.getvalue()
                reference = ImageRef(
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                    width=canvas.width,
                    height=canvas.height,
                )

                image_meter = Meter(
                    OpenAIProvider(client, settings.model, FixtureImages(reference, raw)),
                    folder,
                    "image-" + args.variant + "-" + name,
                    args.max_requests,
                )
                context = ContextBuilder(
                    budget=settings,
                    character=CharacterContextCompiler(CharacterLoader().load("luna")),
                )
                reply = await Agent(image_meter, ToolRegistry(), context, max_rounds=3).run(
                    "图中左边和右边分别是什么图形、什么颜色？", images=[reference]
                )
                result.append({"fixture": name, **reply.model_dump(mode="json")})
            if args.case_set == "fresh":
                context = ContextBuilder(
                    budget=settings,
                    character=CharacterContextCompiler(CharacterLoader().load("luna")),
                )
                positive_meter = Meter(
                    image_meter.provider,
                    folder,
                    "image-" + args.variant + "-defined-square",
                    args.max_requests,
                )
                reply = await Agent(positive_meter, ToolRegistry(), context, max_rounds=3).run(
                    "已知一个四边形四边相等、四个角都是直角，它叫什么？只给名称。",
                    images=[reference],
                )
                result.append({"fixture": "defined_square", **reply.model_dump(mode="json")})
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"mode": args.mode, "recorded": True}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--mode", choices=("pipeline", "conversation", "image"), required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--case-set", choices=("dev", "fresh"), default="dev")
    parser.add_argument("--image-scale", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, choices=range(1, 101), default=100)
    try:
        args = parser.parse_args()
        if not args.variant.isascii() or not args.variant.replace("-", "").isalnum():
            raise ValueError("Invalid variant")
        asyncio.run(run(args))
    except Exception as error:
        parser.exit(1, f"Quality runtime stopped: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
