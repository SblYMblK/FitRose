"""Configuration utilities for the calorie tracking bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    """Runtime configuration pulled from the environment."""

    telegram_token: str
    openai_api_key: str
    database_path: str = "calorie_bot.db"
    admin_ids: tuple[int, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

        openai_key = os.getenv("OPENAI_API_KEY", "")
        if not openai_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is required")

        db_path = os.getenv("CALORIE_BOT_DB", "calorie_bot.db")
        admin_raw = os.getenv("CALORIE_BOT_ADMINS", "")
        admin_ids = tuple(
            int(item.strip())
            for item in admin_raw.split(",")
            if item.strip()
        )
        return cls(
            telegram_token=token,
            openai_api_key=openai_key,
            database_path=db_path,
            admin_ids=admin_ids,
        )


settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return global settings singleton, loading from the environment on first use."""

    global settings
    if settings is None:
        settings = Settings.from_env()
    return settings
