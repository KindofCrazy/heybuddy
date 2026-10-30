"""QQ options do not require credentials until the QQ entry point is used."""

import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class QQError(Exception):
    def __init__(self, code: str, *, fatal: bool = False, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = fatal
        self.uncertain = uncertain


class QQSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    qq_enabled: bool = False
    qq_app_id: str = Field(default="", pattern=r"^(?:[0-9]{1,32})?$")
    qq_app_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    qq_allowed_openids: tuple[str, ...] = ()
    qq_sandbox: bool = False
    qq_queue_size: int = Field(default=16, ge=1, le=100)
    qq_send_timeout_seconds: float = Field(default=10, gt=0, le=60)
    qq_connect_timeout_seconds: float = Field(default=20, gt=0, le=60)
    qq_reconnect_attempts: int = Field(default=8, ge=0, le=20)
    qq_reconnect_base_seconds: float = Field(default=5, ge=1, le=30)
    qq_reconnect_max_seconds: float = Field(default=60, ge=1, le=300)
    # Current official pages disagree (60 min / 5 min): use the shorter window.
    qq_reply_window_seconds: float = Field(default=240, gt=0, le=300)
    qq_reply_max_parts: int = Field(default=4, ge=1, le=4)
    # Conservative local UTF-8 cap; not a claim about QQ's character counter.
    qq_text_max_bytes: int = Field(default=1500, ge=128, le=1500)
    qq_input_max_bytes: int = Field(default=16384, ge=128, le=65536)
    qq_images_enabled: bool = True
    qq_image_max_count: int = Field(default=4, ge=1, le=4)
    qq_image_download_bytes: int = Field(default=8388608, ge=1024, le=8388608)
    qq_image_download_seconds: float = Field(default=15, gt=0, le=30)
    qq_send_interval_seconds: float = Field(default=1, ge=0.1, le=10)
    qq_humanize_enabled: bool = True
    qq_humanize_ms_per_char: float = Field(default=25, ge=0, le=150)
    qq_humanize_max_delay_seconds: float = Field(default=1.5, ge=0, le=5)
    qq_humanize_gap_seconds: float = Field(default=1.2, ge=0, le=2)
    qq_humanize_jitter: float = Field(default=0.2, ge=0, le=0.5)
    qq_shutdown_seconds: float = Field(default=10, gt=0, le=30)
    qq_discovery_seconds: float = Field(default=120, gt=0, le=600)

    @field_validator("qq_allowed_openids", mode="before")
    @classmethod
    def parse_openids(cls, value):
        if isinstance(value, str):
            value = tuple(part.strip() for part in value.split(",") if part.strip())
        if len(value) > 1 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x) for x in value):
            raise ValueError("QQ MVP requires one explicit OpenID; wildcards are forbidden")
        return value

    @model_validator(mode="after")
    def valid_qq_bounds(self):
        if self.qq_reconnect_max_seconds < self.qq_reconnect_base_seconds:
            raise ValueError("QQ reconnect maximum must cover the base interval")
        return self

    def require_qq(self, *, discover: bool = False) -> None:
        secret = self.qq_app_secret.get_secret_value()
        if not self.qq_enabled or not self.qq_app_id or not secret.strip() or len(secret) > 1024:
            raise QQError("qq_configuration", fatal=True)
        if not discover and len(self.qq_allowed_openids) != 1:
            raise QQError("qq_openid_required", fatal=True)
