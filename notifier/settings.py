"""Configure Notifier through TOML and environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from typing_extensions import override

DEFAULT_BOT_AVATAR_PATH = Path(__file__).resolve().parent.parent / "icon.png"


class NotifierSettings(BaseSettings):
    """Configuration loaded from config.toml and overridden by the environment."""

    model_config = SettingsConfigDict(
        env_prefix="NOTIFIER_",
        toml_file=os.environ.get("NOTIFIER_CONFIG", "config.toml"),
        extra="ignore",
    )

    homeserver: str
    bot_username: str
    bot_password: SecretStr
    api_token: SecretStr = Field(min_length=32)

    api_host: str = "0.0.0.0"  # noqa: S104 - expected listener inside the container
    api_port: int = Field(default=8085, ge=1, le=65535)
    database_path: Path = Path("/data/notifier.sqlite3")
    matrix_store_path: Path = Path("/data/store")
    matrix_session_path: Path = Path("/data/session.txt")

    bot_display_name: str = "Notifier"
    bot_avatar_path: Path = DEFAULT_BOT_AVATAR_PATH
    room_name: str = "Docs notifications"
    room_topic: str = (
        "Notifications about changes to your documents. "
        "Leave this room to stop receiving them."
    )
    dispatch_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    max_delivery_attempts: int = Field(default=5, ge=1, le=20)

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del dotenv_settings
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @field_validator("homeserver")
    @classmethod
    def validate_homeserver(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("homeserver must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("bot_username")
    @classmethod
    def validate_bot_username(cls, value: str) -> str:
        if not value:
            raise ValueError("bot_username must not be empty")
        return value

    def safe_summary(self) -> dict[str, Any]:
        """Return useful logging options without exposing secrets."""

        return {
            "homeserver": self.homeserver,
            "bot_username": self.bot_username,
            "api": f"{self.api_host}:{self.api_port}",
            "database_path": str(self.database_path),
            "matrix_store_path": str(self.matrix_store_path),
        }
