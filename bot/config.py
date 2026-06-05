from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass
class Config:
    # Telegram
    bot_token: str
    admin_telegram_id: int

    # Database
    db_path: str

    # AI (Phase 3)
    anthropic_api_key: str

    # Parser defaults (override per-user at runtime)
    city: str
    deal_type: str
    max_price: int
    min_rooms: int
    max_rooms: int

    # App
    test_mode: bool
    admin_password: str
    bot_version: str


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    # Resolve .env: repo root → krisha_bot/ subfolder → cwd
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in [
        os.path.join(_root, ".env"),
        os.path.join(_root, "krisha_bot", ".env"),
        ".env",
    ]:
        if os.path.exists(candidate):
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise ValueError("BOT_TOKEN not found. Create .env with BOT_TOKEN=... (see .env.example)")

    return Config(
        bot_token=bot_token,
        admin_telegram_id=int(os.getenv("ADMIN_TELEGRAM_ID", "0")),
        db_path=os.getenv("DB_PATH", "bot.db"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        city=os.getenv("CITY", "astana"),
        deal_type=os.getenv("DEAL_TYPE", "rent"),
        max_price=int(os.getenv("MAX_PRICE", "200000")),
        min_rooms=int(os.getenv("MIN_ROOMS", "1")),
        max_rooms=int(os.getenv("MAX_ROOMS", "4")),
        test_mode=_bool(os.getenv("TEST"), default=False),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin"),
        bot_version=os.getenv("BOT_VERSION", "1.0.0"),
    )
