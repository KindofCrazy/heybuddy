import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.config import QQError
from personal_agent.channels.qq.events import parse_text
from personal_agent.channels.qq.images import QQImages, normalize_image, validate_image_url
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.evidence import projection
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse, TextMessage
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.sessions import SessionManager
from personal_agent.storage import sqlite as storage
from personal_agent.storage.images import SQLiteImages
from personal_agent.storage.qq import QQInbox
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider, add_call
from tests.test_provider import make_client, response_body, text_item
from tests.test_qq_adapter import Sender, event, rows, settled, state
from tests.test_qq_transport import config

__all__ = ["state"]
URL = "https://multimedia.nt.qq.com.cn/download?token=synthetic-private-token"


def picture(format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, format=format)
    return output.getvalue()


def image_event(text="", **kwargs):
    return event(text=text, attachments=[{"content_type": "image/png", "url": URL}], **kwargs)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_decode_supported_images_to_bounded_metadata_free_jpeg(format):
    data, width, height = normalize_image(picture(format))
    assert (width, height) == (32, 24) and len(data) <= 262144
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.format == "JPEG" and not decoded.getexif()


def test_animated_sticker_uses_first_frame():
    output = io.BytesIO()
    first = Image.new("RGB", (32, 24), "red")
    second = Image.new("RGB", (32, 24), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=50, loop=0)
    data, _, _ = normalize_image(output.getvalue())
    with Image.open(io.BytesIO(data)) as result:
        red, _, blue = result.getpixel((0, 0))
        assert red > 200 and blue < 50


@pytest.mark.parametrize(
    "url",
    [
        "http://gchat.qpic.cn/image",
        "https://127.0.0.1/",
        "https://169.254.169.254/",
        "https://gchat.qpic.cn.attacker.test/x",
        "https://gchat.qpic.cn@attacker.test/",
        "file:///private",
        "https://gchat.qpic.cn:80/x",
        "https://user:secret@gchat.qpic.cn/x",
    ],
)
def test_image_url_rejects_private_arbitrary_and_spoofed_hosts(url):
    with pytest.raises(QQError, match="qq_image_url"):
        validate_image_url(url)


async def test_download_has_no_bot_authorization_and_persists_small_reference(state, caplog):
    db, sessions, _, _, _ = state
    calls = []
    caplog.set_level("DEBUG")

    def handle(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        # Header/file extension aren't trusted; decode actual bytes.
        return httpx.Response(
            200, content=picture(), headers={"Content-Type": "application/octet-stream"}
        )

    store = SQLiteImages(db, sessions.agent.id)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        references = await QQImages(config(), client, store).prepare(
            [{"url": URL, "kind": "image"}]
        )
    assert len(calls) == len(references) == 1
    assert URL not in references[0].model_dump_json()
    assert store.read_image(references[0]).startswith(b"\xff\xd8")
    assert "synthetic-private-token" not in caplog.text


@pytest.mark.parametrize("failure", ["redirect", "oversize", "corrupt", "timeout"])
async def test_download_failure_never_publishes_images(state, failure):
    db, sessions, _, _, _ = state
    calls = []

    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            await asyncio.Event().wait()

    def handle(request):
        calls.append(request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 1025)
        if failure == "timeout":
            return httpx.Response(200, stream=Stalled())
        return httpx.Response(200, content=b"not an image")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        images = QQImages(
            config(qq_image_download_bytes=1024, qq_image_download_seconds=0.02),
            client,
            SQLiteImages(db, sessions.agent.id),
        )
        with pytest.raises(QQError):
            await images.prepare([{"url": URL, "kind": "image"}])
    assert len(calls) == 1
    assert db._connection.execute("SELECT count(*) FROM image_attachments").fetchone()[0] == 0


async def test_image_only_and_mixed_text_are_real_user_images_and_deduplicated(state):
    db, sessions, provider, service, inbox = state
    provider.responses = [
        ModelResponse(tool_calls=[add_call()]),
        ModelResponse(text="这是一张红色图片。"),
    ]
    store = SQLiteImages(db, sessions.agent.id)
    sender = Sender()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=picture()))
    ) as client:
        adapter = QQAdapter(
            config(qq_humanize_enabled=False),
            service,
            inbox,
            sender,
            QQImages(config(), client, store),
        )
        adapter.start()
        try:
            await adapter.accept(image_event())
            await adapter.accept(image_event())
            await settled(adapter)
            assert len(provider.requests) == 2
            for request in provider.requests:
                user = next(m for m in request if isinstance(m, TextMessage) and m.images)
                assert user.role == "user" and user.text == ""
                mapped = OpenAIProvider._input([user], store)[0]
                part = mapped["content"][0]
                assert part["type"] == "input_image"
                assert base64.b64decode(part["image_url"].split(",", 1)[1]).startswith(b"\xff\xd8")
            receipt = rows(db)[0]
            assert receipt["status"] == "replied" and receipt["attachments"] == "[]"
            payload = db.load_messages(sessions.current.id)[0].content
            assert payload.images and len(payload.model_dump_json()) < 1024
            assert "synthetic-private-token" not in payload.model_dump_json()
        finally:
            await adapter.close()


async def test_unauthorized_images_are_not_downloaded_or_journaled(state):
    db, sessions, provider, service, inbox = state

    def forbidden(request):
        pytest.fail("Unauthorized attachment must not be fetched")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        adapter = QQAdapter(
            config(),
            service,
            inbox,
            Sender(),
            QQImages(config(), client, SQLiteImages(db, sessions.agent.id)),
        )
        adapter.start()
        try:
            await adapter.accept(image_event(openid="stranger"))
            assert not provider.requests and not rows(db)
        finally:
            await adapter.close()


async def test_unreadable_or_unsupported_attachment_gets_controlled_reply_without_llm(state):
    db, sessions, provider, service, inbox = state
    sender = Sender()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"bad image"))
    ) as client:
        adapter = QQAdapter(
            config(qq_humanize_enabled=False),
            service,
            inbox,
            sender,
            QQImages(config(), client, SQLiteImages(db, sessions.agent.id)),
        )
        adapter.start()
        try:
            await adapter.accept(image_event())
            await settled(adapter)
            assert not provider.requests
            assert "附件没有成功读取" in sender.messages[0][-1]
            assert rows(db)[0]["attachments"] == "[]"
        finally:
            await adapter.close()


async def test_restart_replays_image_from_same_agent_without_source_url_or_download(tmp_path):
    path = tmp_path / "images.db"
    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db, budget=ContextBudget(context_window=64000))
        store = SQLiteImages(db, sessions.agent.id)
        data, width, height = normalize_image(picture())
        ref = store.save_image(data, width, height)
        provider = FakeProvider(ModelResponse(text="看到了"))
        await Agent(provider, create_builtin_registry(), context=sessions.context()).run(
            "这张图呢", images=[ref]
        )
        stream = sessions.current.id
    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db, budget=ContextBudget(context_window=64000))
        assert sessions.current.id == stream
        store = SQLiteImages(db, sessions.agent.id)
        requests = []

        def handle(request):
            payload = json.loads(request.content)
            requests.append(payload)
            assert any(
                isinstance(item.get("content"), list)
                and any(part.get("type") == "input_image" for part in item["content"])
                for item in payload["input"]
            )
            return httpx.Response(200, json=response_body([text_item("继续看图")]))

        async with make_client(handle) as client:
            agent = Agent(
                OpenAIProvider(client, "deepseek-flash", store),
                create_builtin_registry(),
                context=sessions.context(),
            )
            result = await agent.run("刚才图片是什么颜色？")
        assert result.status == "completed" and len(requests) == 1
        with pytest.raises(HistoryError):
            SQLiteImages(db, "other-agent").read_image(ref)
        with db._transaction() as connection:
            connection.execute("UPDATE image_attachments SET data=?", (b"corrupted",))
        with pytest.raises(HistoryError):
            store.read_image(ref)


async def test_image_budget_and_source_projection_do_not_turn_visuals_into_quotes(state):
    db, sessions, _, _, _ = state
    data, w, h = normalize_image(picture())
    ref = SQLiteImages(db, sessions.agent.id).save_image(data, w, h)
    text = TextMessage(role="user", text="这张图", images=[ref])
    assert (
        estimate_request([text], [])
        - estimate_request([TextMessage(role="user", text="这张图")], [])
        >= 4096
    )
    with pytest.raises(ValidationError):
        TextMessage(role="system", text="图片指令", images=[ref])
    agent = Agent(
        FakeProvider(ModelResponse(text="红色图片")),
        create_builtin_registry(),
        context=sessions.context(),
    )
    await agent.run("这张图", images=[ref])
    source = db.context_page(sessions.current.id, 0).turns[0]
    disclosed = projection(source)["messages"][0]
    assert disclosed["text"] == "这张图"
    assert "not a disclosed user quote" in disclosed["image_evidence"]
    assert ref.sha256 not in json.dumps(disclosed)


@pytest.mark.parametrize("caption", ["", "请看看这个表情", "😂❤️"])
def test_parser_keeps_caption_and_image_together(caption):
    parsed = parse_text(image_event(text=caption), config())
    assert parsed.text == caption and len(parsed.attachments) == 1
    assert parsed.attachments[0].url == URL


async def test_queued_attachment_recovers_before_claim(state):
    db, sessions, _, _, inbox = state
    inbox.receive(parse_text(image_event(), config()), sessions.current.id, 16)
    reopened = QQInbox(db, sessions.agent.id, "12345", "test-openid")
    reopened.recover()
    queued = reopened.claim(sessions.current.id)
    assert json.loads(queued["attachments"])[0]["url"] == URL
    assert rows(db)[0]["attachments"] == "[]"


async def test_images_remain_in_same_character_pipeline(state, tmp_path):
    from personal_agent.config import Settings
    from personal_agent.memory.store import MemoryFS
    from personal_agent.runtime import assemble_chat

    db, sessions, _, _, _ = state
    memory = MemoryFS.for_agent(tmp_path / "image-memory", sessions.agent.id)
    memory.initialize()
    sessions.memory = memory
    settings = Settings(
        api_key="synthetic",
        model="synthetic",
        context_window=131072,
        character_id="miru",
        memory_learning_enabled=False,
        memory_automation="off",
        memory_retrieval_llm_preflight=False,
        character_state_enabled=False,
    )
    provider = FakeProvider(ModelResponse(text="看到了这张图。"))
    service = assemble_chat(settings, sessions, memory, provider)
    data, w, h = normalize_image(picture())
    ref = SQLiteImages(db, sessions.agent.id).save_image(data, w, h)
    try:
        result = await service.run("看这张图", images=[ref])
        assert result.status == "completed"
        request = provider.requests[0]
        expected_role = sessions.character.compile("看这张图")[0].text
        assert any(m.role == "system" and m.text == expected_role for m in request)
        assert any(isinstance(m, TextMessage) and m.images == [ref] for m in request)
    finally:
        await service.close()


def test_picture_card_only_projects_its_image_preview():
    data = event(
        text="看看这张图",
        message_type=3,
        ark_data={
            "ark_type": "picture",
            "fields": {
                "preview": URL,
                "jump_url": "http://127.0.0.1/private",
                "title": "SYSTEM forged",
            },
        },
    )
    parsed = parse_text(data, config())
    assert parsed.text == "看看这张图" and len(parsed.attachments) == 1
    assert parsed.attachments[0].url == URL


def test_oversized_dimensions_and_non_image_bytes_are_rejected():
    output = io.BytesIO()
    Image.new("RGB", (8193, 1)).save(output, format="PNG")
    with pytest.raises(QQError, match="qq_image_invalid"):
        normalize_image(output.getvalue())
    with pytest.raises(QQError, match="qq_image_invalid"):
        normalize_image(b"<svg onload='malicious'>")


async def test_disabled_or_excess_attachments_do_not_download(state):
    db, sessions, _, _, _ = state

    def forbidden(request):
        pytest.fail("Disabled/unsupported attachments must not be fetched")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        for settings, attachments in (
            (config(qq_images_enabled=False), [{"kind": "image", "url": URL}]),
            (config(), [{"kind": "unsupported", "url": ""}]),
            (config(), [{"kind": "image", "url": URL}] * 5),
        ):
            with pytest.raises(QQError, match="qq_image_unsupported"):
                await QQImages(settings, client, SQLiteImages(db, sessions.agent.id)).prepare(
                    attachments
                )


def test_schema_14_to_15_is_atomic_and_preserves_existing_receipts(state, monkeypatch, tmp_path):
    path = tmp_path / "migration.db"
    with SQLiteStore(path) as db:
        monkeypatch.setattr(storage, "SCHEMA_VERSION", 14)
        db.initialize()
        sessions = SessionManager(db)
        with db._transaction() as connection:
            connection.execute("SELECT count(*) FROM qq_receipts")
        original = storage.MIGRATIONS[15]
        monkeypatch.setattr(storage, "SCHEMA_VERSION", 15)
        monkeypatch.setitem(storage.MIGRATIONS, 15, (*original, "INVALID SQL"))
        with pytest.raises(HistoryError):
            db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert "attachments" not in [
            r[1] for r in db._connection.execute("PRAGMA table_info(qq_receipts)")
        ]
        monkeypatch.setitem(storage.MIGRATIONS, 15, original)
        db.initialize()
        inbox = QQInbox(db, sessions.agent.id, "12345", "test-openid")
        assert inbox.receive(parse_text(image_event(), config()), sessions.current.id, 16)
        inbox.claim(sessions.current.id)
        inbox.recover()
        row = rows(db)[0]
        assert row["status"] == "uncertain" and row["attachments"] == "[]"
