"""Fixed official HTTPS endpoints, bounded responses, no message send retries."""

import asyncio
import json
import time
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from personal_agent.channels.qq.config import QQError, QQSettings

API_BASE = "https://api.bot.qq.com"
SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"
GATEWAY_HOSTS = {"api.bot.qq.com", "api.sgroup.qq.com", "sandbox.api.sgroup.qq.com"}


class QQAPI:
    def __init__(self, settings: QQSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.base = SANDBOX_BASE if settings.qq_sandbox else API_BASE
        self._token = SecretStr("")
        self._expires = 0.0
        self._token_lock = asyncio.Lock()

    async def _request(self, method: str, url: str, *, sending: bool = False, **kwargs) -> dict:
        try:
            async with self.client.stream(
                method, url, timeout=self.settings.qq_send_timeout_seconds, **kwargs
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise QQError("qq_response_limit", uncertain=sending)
                if response.status_code in {401, 403}:
                    raise QQError("qq_authorization", fatal=True)
                if not 200 <= response.status_code < 300:
                    raise QQError(
                        "qq_http_error", uncertain=sending and response.status_code >= 500
                    )
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError
                if data.get("code", 0) != 0:
                    # Audit / platform business failures aren't proof of non-delivery.
                    raise QQError("qq_api_error", uncertain=sending)
                return data
        except (httpx.HTTPError, TimeoutError):
            raise QQError("qq_network_error", uncertain=sending) from None
        except (ValueError, UnicodeError):
            raise QQError("qq_invalid_response", uncertain=sending) from None

    async def token(self) -> str:
        async with self._token_lock:
            if time.monotonic() >= self._expires:
                data = await self._request(
                    "POST",
                    API_BASE + "/app/getAppAccessToken",
                    json={
                        "appId": self.settings.qq_app_id,
                        "clientSecret": self.settings.qq_app_secret.get_secret_value(),
                    },
                )
                token = data.get("access_token")
                try:
                    expires = int(data.get("expires_in", 0))
                except (TypeError, ValueError):
                    expires = 0
                if not isinstance(token, str) or not token or not 0 < expires <= 7200:
                    raise QQError("qq_token_rejected", fatal=True)
                self._token = SecretStr(token)
                self._expires = time.monotonic() + max(0, expires - 60)
            return self._token.get_secret_value()

    async def headers(self) -> dict[str, str]:
        return {
            "Authorization": "QQBot " + await self.token(),
            "X-Union-Appid": self.settings.qq_app_id,
        }

    async def gateway(self) -> str:
        data = await self._request("GET", self.base + "/gateway", headers=await self.headers())
        url = data.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed.scheme != "wss"
            or parsed.hostname not in GATEWAY_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise QQError("qq_gateway_url", fatal=True)
        return url

    async def send_text(self, openid: str, message_id: str, sequence: int, text: str) -> None:
        if not message_id or not 1 <= sequence <= 4 or not text:
            raise QQError("qq_invalid_reply", fatal=True)
        data = await self._request(
            "POST",
            self.base + "/v2/users/" + quote(openid, safe="") + "/messages",
            headers=await self.headers(),
            json={"content": text, "msg_type": 0, "msg_id": message_id, "msg_seq": sequence},
            sending=True,
        )
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise QQError("qq_send_unconfirmed", uncertain=True)
