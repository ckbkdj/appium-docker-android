from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOBILE_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    api_token: str | None = Field(default=None, repr=False)
    database_path: Path = Path("./data/mobile-agent.db")
    app_profiles_path: Path | None = None

    llm_base_url: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_model: str | None = None
    llm_api_style: Literal["chat_completions", "responses"] = "chat_completions"
    llm_timeout_s: float = 30.0
    llm_max_output_tokens: int = 1800
    llm_temperature: float = 0.0

    adb_path: str = "adb"
    appium_url: str | None = "http://127.0.0.1:4723"
    bridge_socket_name: str = "semantic_mobile_agent"
    bridge_connect_timeout_s: float = 1.0
    bridge_command_timeout_s: float = 3.0

    default_max_steps: int = 30
    default_max_replans: int = 2
    task_retention: int = 1000
    require_confirmation: bool = True
    cache_min_successes: int = 2
    ui_max_nodes: int = 180
    ui_max_chars: int = 9000

    @property
    def profiles_file(self) -> Path:
        if self.app_profiles_path is not None:
            return self.app_profiles_path
        return Path(__file__).resolve().parent / "data" / "apps.yaml"
