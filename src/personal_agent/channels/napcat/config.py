"""Explicit local account binding; no official QQ credentials required."""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from personal_agent.channels.qq.config import QQError, QQSettings


class NapCatDelivery(QQSettings):
    """Host delivery bounds, independent of the official bot's passive quota."""

    qq_reply_max_parts: int = Field(default=32, ge=1, le=64)
    qq_text_max_bytes: int = Field(default=8000, ge=128, le=16384)
    qq_reply_window_seconds: float = Field(default=900, gt=0, le=3600)


class NapCatSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    napcat_enabled: bool = False
    napcat_ws_url: str = "ws://127.0.0.1:3001"
    napcat_token: SecretStr = Field(default=SecretStr(""), repr=False)
    napcat_self_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{4,15})?$")
    napcat_peer_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{4,15})?$")
    napcat_reply_max_parts: int = Field(default=32, ge=1, le=64)
    napcat_text_max_bytes: int = Field(default=8000, ge=128, le=16384)
    napcat_reply_window_seconds: float = Field(default=900, gt=0, le=3600)
    napcat_send_interval_seconds: float = Field(default=0.3, ge=0.1, le=10)
    napcat_humanize_enabled: bool = True
    napcat_humanize_ms_per_char: float = Field(default=25, ge=0, le=150)
    napcat_humanize_max_delay_seconds: float = Field(default=3, ge=0, le=5)
    napcat_humanize_gap_seconds: float = Field(default=0.4, ge=0, le=2)
    napcat_humanize_jitter: float = Field(default=0.2, ge=0, le=0.5)
    napcat_input_typing_enabled: bool = True
    napcat_input_typing_ttl_seconds: float = Field(default=30, ge=1, le=30)
    napcat_reminders_enabled: bool = True

    @field_validator("napcat_ws_url")
    @classmethod
    def local_websocket(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "ws"
                or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.port is None
                or not 1024 <= parsed.port <= 65535
                or parsed.path not in {"", "/"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(ch.isspace() for ch in value)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("NapCat requires a literal loopback WebSocket URL and port") from None
        return value

    def require_napcat(self) -> None:
        token = self.napcat_token.get_secret_value()
        if (
            not self.napcat_enabled
            or not self.napcat_self_id
            or not self.napcat_peer_id
            or self.napcat_self_id == self.napcat_peer_id
            or not re.fullmatch(r"[!-~]{1,1024}", token)
        ):
            raise QQError("napcat_configuration", fatal=True)

    @property
    def napcat_scope(self) -> str:
        # Include the peer: changing the whitelist cannot collide with old receipts.
        return f"napcat:{self.napcat_self_id}:{self.napcat_peer_id}"

    def delivery_settings(self, shared: QQSettings) -> NapCatDelivery:
        """Reuse bounded inbox/turn/image policy, with a separate transport binding."""
        values = {name: getattr(shared, name) for name in QQSettings.model_fields}
        values.update(qq_app_id=self.napcat_self_id, qq_allowed_openids=(self.napcat_peer_id,))
        for name in (
            "reply_max_parts",
            "text_max_bytes",
            "reply_window_seconds",
            "send_interval_seconds",
            "humanize_enabled",
            "humanize_ms_per_char",
            "humanize_max_delay_seconds",
            "humanize_gap_seconds",
            "humanize_jitter",
        ):
            values["qq_" + name] = getattr(self, "napcat_" + name)
        return NapCatDelivery.model_validate(values)
