"""Application settings loaded safely from environment variables and configs/demo.yaml."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEMO_CONFIG_PATH = PROJECT_ROOT / "configs" / "demo.yaml"


class Settings(BaseSettings):
    """Runtime settings with safe local-first defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development")
    database_url: str = Field(default=f"sqlite:///{PROJECT_ROOT / 'vani_ledger.db'}")
    vani_api_key: str = Field(default="demo-vani-key-2026")
    secondary_api_key: str = Field(default="other-merchant-key-2026")
    model_version: str = Field(default="lgbm-conformal-v1.0")
    upload_dir: Path = Field(default=PROJECT_ROOT / "uploads")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024)
    gemini_api_key: str = Field(default="")
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000"]
    )
    rate_limit_enabled: bool = Field(default=True)


def hash_api_key(raw_key: str) -> str:
    """Return deterministic SHA-256 hex digest for constant-time API-key verification."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached runtime settings."""
    return Settings()


@lru_cache(maxsize=1)
def load_demo_config() -> dict[str, Any]:
    """Load deterministic demo calibration and festival calendar from configs/demo.yaml."""
    with DEMO_CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("configs/demo.yaml must contain a YAML mapping")
    return data
