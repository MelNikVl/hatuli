"""
Настройки приложения (app_settings в PostgreSQL) с кешем в памяти.

Использование:
    from bot.db import settings as app_settings

    await app_settings.load()              # при старте сервиса и в начале каждого цикла парсера
    rate = app_settings.get_float("DEPOSIT_RATE", 14.0)
    await app_settings.set("DEPOSIT_RATE", "15.0")

Приоритет: БД → переменная окружения → дефолт.
Каждый сервис (web, apartments, rental, alerts) — отдельный процесс,
поэтому парсеры должны вызывать load() в начале цикла, чтобы подтянуть
изменения, сделанные через веб-терминал.
"""
from __future__ import annotations

import logging
import os

from bot.db.pg import execute, fetch

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, str] = {
    "DEPOSIT_RATE": "14.0",
    "APPRECIATION_PCT": "8.0",
    "MORTGAGE_RATE": "17.0",
    "MORTGAGE_YEARS": "20",
    "MORTGAGE_DOWN_PCT": "20",
    "REALTOR_FEE_PCT": "2.0",
    "ALERT_THRESHOLD": "65",
    "PARSER_MAX_PAGES": "5",
    "PARSER_MAX_PRICE": "80000000",
    "MONETIZATION_ENABLED": "0",
}

_cache: dict[str, str] = {}


async def load() -> None:
    """Перечитать все настройки из БД в кеш."""
    try:
        rows = await fetch("SELECT key, value FROM app_settings")
        _cache.clear()
        _cache.update({r["key"]: r["value"] for r in rows})
    except Exception as exc:  # таблицы может не быть до миграции
        logger.warning("app_settings load failed: %s", exc)


def get(key: str, default: str | None = None) -> str:
    if key in _cache:
        return _cache[key]
    env = os.getenv(key)
    if env is not None:
        return env
    if default is not None:
        return default
    return DEFAULTS.get(key, "")


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get(key, str(default)))
    except ValueError:
        return default


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get(key, str(default))))
    except ValueError:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    return get(key, "1" if default else "0").strip() in ("1", "true", "True", "on")


async def set(key: str, value: str) -> None:  # noqa: A001
    await execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()
        """,
        key, str(value),
    )
    _cache[key] = str(value)


def all_settings() -> dict[str, str]:
    merged = dict(DEFAULTS)
    merged.update(_cache)
    return merged
