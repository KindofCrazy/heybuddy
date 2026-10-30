"""Explicit local account binding; no official QQ credentials required."""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from personal_agent.channels.qq.config import QQError, QQSettings


class NapCatSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    napcat_enabled: bool = False
    napcat_ws_url: str = "ws://127.0.0.1:3001"
    napcat_token: SecretStr = Field(default=SecretStr(""), repr=False)
    napcat_self_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{4,15})?$")
    napcat_peer_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{4,15})?$")

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

    def delivery_settings(self, shared: QQSettings) -> QQSettings:
        """Reuse bounded inbox/turn/image policy, with a separate transport binding."""
        values = {name: getattr(shared, name) for name in QQSettings.model_fields}
        values.update(qq_app_id=self.napcat_self_id, qq_allowed_openids=(self.napcat_peer_id,))
        return QQSettings.model_validate(values)
