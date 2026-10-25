import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    api_key: SecretStr
    model: str = Field(min_length=1)
    base_url: HttpUrl | None = None
    max_rounds: int = Field(default=10, ge=1)
    request_timeout_seconds: float = Field(default=60, gt=0)
    tool_timeout_seconds: float = Field(default=10, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("model", mode="before")
    @classmethod
    def strip_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def require_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API key is required")
        return SecretStr(value.get_secret_value().strip())


ENV_FIELDS = {
    "OPENAI_API_KEY": "api_key",
    "OPENAI_MODEL": "model",
    "OPENAI_BASE_URL": "base_url",
    "AGENT_MAX_ROUNDS": "max_rounds",
    "AGENT_REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
    "AGENT_TOOL_TIMEOUT_SECONDS": "tool_timeout_seconds",
    "AGENT_LOG_LEVEL": "log_level",
}


def load_settings(env_file: Path | None = None) -> Settings:
    # Explicit project-local file only: never search parent directories for credentials.
    path = env_file if env_file is not None else Path.cwd() / ".env"
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    data = {}
    for env_name, field in ENV_FIELDS.items():
        value = os.environ.get(env_name, values.get(env_name))
        if value is not None:
            data[field] = value
    return Settings.model_validate(data)
