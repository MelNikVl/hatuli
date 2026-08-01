"""
AI-анализ текста объявления через DeepSeek.

Включается настройкой AI_TEXT_ANALYSIS=1 (по умолчанию выключен, чтобы
не тратить API без явного решения). Ключ: DEEPSEEK_API_KEY в .env.
Стоимость: ~$0.0001 на объявление — копейки даже на потоке.

Что вытаскиваем из описания (JSON):
  finish        — отделка точнее словаря ключевых слов
  urgency       — срочность продажи (аргумент торга!)
  exchange      — «обмен/поменяю» (обычно не инвест-вариант)
  defects       — упомянутые дефекты/нюансы
  furniture     — мебель остаётся?
  layout        — распашонка/линейка/студия, если сказано
  negotiation   — 1-2 зацепки для торга из текста
  red_flags     — тревожные звоночки

Результат пишется в apartment_listings.ai_analysis (JSONB)
и показывается в карточке объекта.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from bot.db.pg import execute, fetch

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

_SYSTEM = """Ты аналитик рынка недвижимости Астаны. Тебе дают текст объявления о продаже квартиры.
Верни ТОЛЬКО валидный JSON без markdown и пояснений, со схемой:
{
 "finish": "черновая|предчистовая|чистовая|ремонт|мебель|null",
 "urgency": "высокая|средняя|нет",
 "urgency_quote": "цитата из текста или null",
 "exchange": true/false,
 "furniture": true/false/null,
 "layout": "распашонка|линейка|студия|null",
 "defects": ["..."],
 "negotiation": ["1-2 конкретные зацепки для торга из текста"],
 "red_flags": ["..."]
}
Если информации нет — null/false/пустые списки. Не выдумывай."""


async def analyze_one(listing_id: str, title: str, description: str) -> dict | None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY не задан — AI-анализ пропущен")
        return None

    text = f"Заголовок: {title}\n\nОписание: {description[:3000]}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
            )
        if resp.status_code != 200:
            logger.warning("deepseek %s: %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(content)
    except Exception as exc:
        logger.warning("ai analyze failed %s: %s", listing_id, exc)
        return None


async def analyze_top_listings(limit: int = 10) -> int:
    """
    Прогнать через DeepSeek топовые объявления с описанием и без ai_analysis.
    Возвращает число обработанных.
    """
    rows = await fetch("""
        SELECT id, title, description FROM apartment_listings
        WHERE description IS NOT NULL AND length(description) > 80
          AND ai_analysis IS NULL
          AND is_active IS NOT FALSE
          AND score_total IS NOT NULL
        ORDER BY score_total DESC
        LIMIT $1
    """, limit)

    done = 0
    for r in rows:
        result = await analyze_one(r["id"], r["title"] or "", r["description"] or "")
        if result is None:
            continue
        await execute(
            "UPDATE apartment_listings SET ai_analysis = $2::jsonb WHERE id = $1",
            r["id"], json.dumps(result, ensure_ascii=False),
        )
        done += 1
    logger.info("ai text analysis: %d listings processed", done)
    return done
