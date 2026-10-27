import os
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import ConfigDict, Field, HttpUrl, SecretStr, field_validator, model_validator

from personal_agent.automation.models import AutomationPolicy
from personal_agent.core.budget import ContextBudget
from personal_agent.core.retrieval import RetrievalPolicy
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.reflection.models import ReflectionPolicy


class Settings(ContextBudget):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    api_key: SecretStr
    model: str = Field(min_length=1)
    base_url: HttpUrl | None = None
    max_rounds: int = Field(default=10, ge=1)
    request_timeout_seconds: float = Field(default=60, gt=0)
    tool_timeout_seconds: float = Field(default=10, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_path: Path = Path(".heybuddy/heybuddy.sqlite3")
    memory_root: Path | None = None
    memory_max_bytes: int = Field(default=8192, ge=512, le=1_048_576)
    organization_enabled: bool = True
    organization_idle_seconds: float = Field(default=1500, gt=0, le=86400)
    organization_run_threshold: int = Field(default=8, ge=1, le=64)
    organization_page_runs: int = Field(default=8, ge=1, le=16)
    organization_input_tokens: int = Field(default=24576, ge=4096, le=131072)
    organization_output_tokens: int = Field(default=8192, ge=256, le=32768)
    organization_timeout_seconds: float = Field(default=60, gt=0, le=300)
    organization_weak_gap_seconds: int = Field(default=900, ge=1)
    organization_strong_gap_seconds: int = Field(default=5400, ge=1)
    organization_split_gap_seconds: int = Field(default=10800, ge=1)
    organization_revision_seconds: int = Field(default=10800, ge=1, le=86400)
    organization_thread_threshold: int = Field(default=3, ge=2, le=16)
    organization_thread_interval_seconds: float = Field(default=14400, gt=0, le=604800)
    organization_check_seconds: float = Field(default=60, gt=0, le=3600)
    organization_cooldown_seconds: float = Field(default=60, gt=0, le=86400)
    organization_maintenance_batches: int = Field(default=4, ge=1, le=32)
    organization_maintenance_requests: int = Field(default=4, ge=1, le=32)
    organization_maintenance_tokens: int = Field(default=131072, ge=4096, le=1048576)
    organization_maintenance_seconds: float = Field(default=120, gt=0, le=600)
    organization_repair_retry_seconds: float = Field(default=300, gt=0, le=86400)
    organization_repair_max_failures: int = Field(default=3, ge=1, le=10)
    organization_response_bytes: int = Field(default=32768, ge=1024, le=65536)
    organization_source_bytes: int = Field(default=65536, ge=1024, le=262144)
    retrieval_episode_limit: int = Field(default=3, ge=1, le=3)
    retrieval_raw_run_limit: int = Field(default=2, ge=1, le=3)
    retrieval_max_run_bytes: int = Field(default=65536, ge=1024, le=131072)
    retrieval_optional_tokens: int = Field(default=12288, ge=512, le=32768)
    retrieval_scan_episodes: int = Field(default=200, ge=3, le=1000)

    reflection_input_tokens: int = Field(default=16384, ge=2048, le=262144)
    reflection_output_tokens: int = Field(default=16384, ge=256, le=65536)
    reflection_response_max_bytes: int = Field(default=32768, ge=1024, le=131072)
    reflection_timeout_seconds: float = Field(default=60, gt=0)
    reflection_page_runs: int = Field(default=8, ge=1, le=32)

    memory_automation: Literal["off", "suggest", "auto"] = "suggest"
    memory_auto_create_grants: dict[str, str] = Field(default_factory=dict)
    memory_automation_max_requests: int = Field(default=4, ge=1, le=4)
    memory_automation_max_tokens: int = Field(default=131072, ge=4096, le=524288)
    memory_automation_timeout: float = Field(default=180, gt=0)

    @field_validator("memory_auto_create_grants", mode="before")
    @classmethod
    def parse_grants(cls, value):
        import json

        return json.loads(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_automation(self):
        self.automation_policy()
        self.organization_policy()
        return self

    def organization_policy(self) -> OrganizationPolicy:
        return OrganizationPolicy(
            **{
                name: getattr(self, "organization_" + name)
                for name in OrganizationPolicy.model_fields
                if hasattr(self, "organization_" + name)
            }
        )

    def retrieval_policy(self) -> RetrievalPolicy:
        return RetrievalPolicy(
            **{name: getattr(self, "retrieval_" + name) for name in RetrievalPolicy.model_fields}
        )

    def automation_policy(self) -> AutomationPolicy:
        return AutomationPolicy(
            mode=self.memory_automation,
            auto_create_grants=self.memory_auto_create_grants,
            max_requests=self.memory_automation_max_requests,
            max_tokens=self.memory_automation_max_tokens,
            timeout_seconds=self.memory_automation_timeout,
        )

    def reflection_policy(self) -> ReflectionPolicy:
        return ReflectionPolicy(
            input_tokens=self.reflection_input_tokens,
            output_tokens=self.reflection_output_tokens,
            response_max_bytes=self.reflection_response_max_bytes,
            timeout_seconds=self.reflection_timeout_seconds,
            page_runs=self.reflection_page_runs,
        )

    def context_budget(self) -> ContextBudget:
        return ContextBudget.model_validate(
            {name: getattr(self, name) for name in ContextBudget.model_fields}
        )

    @field_validator("base_url")
    @classmethod
    def require_secure_base_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None or value.scheme == "https":
            return value
        # Check the parsed hostname, never substrings or DNS results. Pydantic
        # brackets IPv6 hosts; localhost is the only permitted hostname alias.
        host = (value.host or "").strip("[]").removesuffix(".")
        if host == "localhost":
            return value
        try:
            if ip_address(host).is_loopback:
                return value
        except ValueError:
            pass
        raise ValueError("API Base URL requires HTTPS unless the host is loopback")

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
    **{
        "AGENT_" + name.upper(): name
        for name in Settings.model_fields
        if name.startswith("retrieval_")
    },
    **{
        "AGENT_ORGANIZATION_" + name.removeprefix("organization_").upper(): name
        for name in Settings.model_fields
        if name.startswith("organization_")
    },
    "AGENT_MEMORY_AUTOMATION": "memory_automation",
    "AGENT_MEMORY_AUTO_CREATE_GRANTS": "memory_auto_create_grants",
    "AGENT_MEMORY_AUTOMATION_MAX_REQUESTS": "memory_automation_max_requests",
    "AGENT_MEMORY_AUTOMATION_MAX_TOKENS": "memory_automation_max_tokens",
    "AGENT_MEMORY_AUTOMATION_TIMEOUT_SECONDS": "memory_automation_timeout",
    "AGENT_REFLECTION_INPUT_TOKENS": "reflection_input_tokens",
    "AGENT_REFLECTION_OUTPUT_TOKENS": "reflection_output_tokens",
    "AGENT_REFLECTION_RESPONSE_MAX_BYTES": "reflection_response_max_bytes",
    "AGENT_REFLECTION_TIMEOUT_SECONDS": "reflection_timeout_seconds",
    "AGENT_REFLECTION_PAGE_RUNS": "reflection_page_runs",
    "OPENAI_API_KEY": "api_key",
    "OPENAI_MODEL": "model",
    "AGENT_CONTEXT_WINDOW": "context_window",
    "AGENT_RESERVED_OUTPUT_TOKENS": "reserved_output_tokens",
    "AGENT_CONTEXT_SAFETY_MARGIN": "safety_margin",
    "AGENT_COMPACTION_TRIGGER_RATIO": "trigger_ratio",
    "AGENT_RECENT_RUNS": "recent_runs",
    "AGENT_HISTORY_PAGE_RUNS": "page_runs",
    "AGENT_SUMMARY_TOKENS": "summary_tokens",
    "AGENT_SUMMARY_OUTPUT_TOKENS": "summary_output_tokens",
    "AGENT_COMPACTION_INPUT_TOKENS": "compaction_input_tokens",
    "AGENT_COMPACTION_TIMEOUT_SECONDS": "compaction_timeout",
    "AGENT_MAX_COMPACTION_STEPS": "max_compaction_steps",
    "OPENAI_BASE_URL": "base_url",
    "AGENT_MAX_ROUNDS": "max_rounds",
    "AGENT_REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
    "AGENT_TOOL_TIMEOUT_SECONDS": "tool_timeout_seconds",
    "AGENT_LOG_LEVEL": "log_level",
    "AGENT_DATABASE_PATH": "database_path",
    "AGENT_MEMORY_ROOT": "memory_root",
    "AGENT_CORE_MEMORY_MAX_BYTES": "memory_max_bytes",
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


def default_memory_root() -> Path:
    """User data, outside source checkouts by default; no additional dependency."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return base / "HeyBuddy" / "memory"
    return Path.home() / ".local/share/heybuddy/memory"


def memory_root_for(settings: Settings, agent_id: str) -> Path:
    if settings.memory_root is not None:
        return settings.memory_root
    # Preserve the known V0.4 location on upgrade and subsequent restarts. Do not
    # scan unrelated paths or silently copy private data to another location.
    adjacent = Path(str(settings.database_path) + ".memory")
    agent = adjacent / agent_id
    if agent.exists() or agent.is_symlink() or agent.is_junction():
        return adjacent
    return default_memory_root()
