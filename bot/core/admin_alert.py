"""Общий хелпер "уведомить админа в Telegram" — вынесено из
bot/core/ai_text_analysis.py (Фаза L1 продуктового трека «Локация»,
docs/location_product_design.md §7, задача 2026-08-14) при добавлении
второго вызывающего места (osm_mirrors_healthcheck.py) — та же логика,
что уже не раз дублировалась в проекте до централизации (_CLASS_SCORE,
_activity_filter, геоцентроид ЖК) и каждый раз расходилась бы, если
оставить копию на копии.

notify_admin(text) — единственная функция. Молчаливо no-op, если
BOT_TOKEN/ADMIN_TELEGRAM_ID не заданы (тот же принцип, что был в
исходном _notify_admin — предупреждение в лог, не исключение наружу:
уведомление — побочный эффект, не должно ронять вызывающий скрипт)."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def notify_admin(text: str) -> None:
    token = os.getenv("BOT_TOKEN")
    admin_id = os.getenv("ADMIN_TELEGRAM_ID")
    if not token or not admin_id:
        logger.warning("BOT_TOKEN/ADMIN_TELEGRAM_ID не заданы — уведомление не отправлено")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(TELEGRAM_API.format(token=token),
                               json={"chat_id": admin_id, "text": text})
    except Exception as exc:
        logger.warning("telegram notify failed: %s", exc)
