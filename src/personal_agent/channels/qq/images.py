"""Download only official QQ image CDN resources; normalize without metadata."""

import asyncio
import io
import logging
import warnings
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.core.images import ImageRef
from personal_agent.storage.images import SQLiteImages

_media_request = ContextVar("qq_private_media_request", default=False)


class _MediaPrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _media_request.get()


# httpx INFO logs contain full signed URLs. Suppress wire logs only within this
# task's download scope; other concurrent Provider/API diagnostics keep their rules.
for _logger_name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
    logging.getLogger(_logger_name).addFilter(_MediaPrivacyFilter())


def validate_image_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        valid_host = host in {
            "multimedia.nt.qq.com.cn",
            "multimedia.nt.qq.com",
            "qpic.cn",
        } or host.endswith(".qpic.cn")
        if (
            not 1 <= len(url) <= 8192
            or parsed.scheme != "https"
            or not valid_host
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
    except ValueError:
        raise QQError("qq_image_url") from None
    return url


def normalize_image(data: bytes) -> tuple[bytes, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                    raise ValueError
                width, height = source.size
                if width > 8192 or height > 8192 or width * height > 16777216:
                    raise ValueError
                source.seek(0)  # Bounded first-frame interpretation; never iterate an animation.
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((1536, 1536))
                rgb = Image.new("RGB", image.size, "white")
                rgb.paste(image, mask=image.getchannel("A"))
                for quality in (85, 65, 45):
                    output = io.BytesIO()
                    rgb.save(output, format="JPEG", quality=quality)
                    value = output.getvalue()
                    if len(value) <= 262144:
                        return value, rgb.width, rgb.height
                raise ValueError
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise QQError("qq_image_invalid") from None


class QQImages:
    def __init__(
        self, settings: QQSettings, client: httpx.AsyncClient, store: SQLiteImages
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store

    async def prepare(self, attachments: list[dict]) -> list[ImageRef]:
        if (
            not self.settings.qq_images_enabled
            or len(attachments) > self.settings.qq_image_max_count
        ):
            raise QQError("qq_image_unsupported")
        # Validate the complete list before doing any I/O. No platform auth headers are sent.
        urls = []
        for item in attachments:
            if item.get("kind") != "image":
                raise QQError("qq_image_unsupported")
            urls.append(validate_image_url(item["url"]))
        prepared = []
        scope = _media_request.set(True)
        try:
            async with asyncio.timeout(self.settings.qq_image_download_seconds):
                for url in urls:
                    async with self.client.stream(
                        "GET", url, follow_redirects=False, headers={"Accept-Encoding": "identity"}
                    ) as response:
                        if response.status_code != 200:
                            raise QQError("qq_image_download")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self.settings.qq_image_download_bytes:
                                raise QQError("qq_image_too_large")
                            data.extend(chunk)
                    prepared.append(await asyncio.to_thread(normalize_image, bytes(data)))
        except (httpx.HTTPError, TimeoutError):
            raise QQError("qq_image_download") from None
        finally:
            _media_request.reset(scope)
        # Short SQLite commits stay on the event loop; cancelled decode threads publish nothing.
        return [self.store.save_image(data, width, height) for data, width, height in prepared]
