from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8787
    log_level: str = "INFO"
    data_dir: Path = Path("./data")

    adb_path: str = "adb"
    default_device: str | None = None
    appium_url: str = "http://127.0.0.1:4723"
    appium_enabled: bool = True
    bridge_enabled: bool = True
    bridge_socket: str = "semantic_mobile_agent"
    bridge_port_base: int = Field(default=17300, ge=1024, le=60000)
    bridge_token: str | None = None
    action_settle_ms: int = Field(default=180, ge=0, le=5000)
    ui_cache_ttl_ms: int = Field(default=120, ge=0, le=5000)

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_s: float = Field(default=20, ge=1, le=120)
    llm_max_replans: int = Field(default=3, ge=0, le=10)

    require_confirmation: bool = True
    allow_unsafe: bool = False

    app_catalog_path: Path = Path("config/apps.yaml")
    safety_path: Path = Path("config/safety.yaml")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "semantic-mobile-agent.sqlite3"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
