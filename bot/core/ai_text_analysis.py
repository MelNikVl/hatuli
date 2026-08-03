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
  is_relayout       — была перепланировка?
  is_relayout_legal — перепланировка узаконена?
  is_free_layout    — свободная планировка (новостройки)?
  has_ac            — есть кондиционер?
  complex_facts     — факты про САМ ЖК (не квартиру): локация, что рядом,
                       архитектура/дизайн, холл, охрана, консьерж, закрытый
                       двор, детская площадка, паркинг, закрытая территория

Результат пишется в apartment_listings.ai_analysis (JSONB)
и показывается в карточке объекта.

Слой сканирует НОВЫЕ объявления (ORDER BY first_seen DESC) — так свежие
описания разбираются в первую очередь, а не только топ по скору.

Скоринг: распашонка/свободная планировка дают небольшой плюс — см.
apply_layout_bonus(), пишется в те же layer_bonus/layer_details, что и
остальные слои скоринга (bot/score_layers), см. /admin/info#score.

ЖК-факты: если для найденного по complex_name ЖК в complexes.ai_features
ЕЩЁ НЕТ такого поля — добавляем и шлём админу уведомление в Telegram
(BOT_TOKEN/ADMIN_TELEGRAM_ID из .env) со ссылкой на объявление-источник.
Уже заполненные поля не перезаписываем (первое найденное — источник истины,
правки руками через админку это не трогает).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import httpx

from bot.db.pg import execute, fetch, fetchrow

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Ключ в complex_facts -> человекочитаемое название (для телеграм-уведомления)
_COMPLEX_FACT_LABELS = {
    "location": "локация",
    "nearby": "что рядом",
    "architecture": "архитектура/дизайн",
    "lobby": "холл",
    "security": "охрана",
    "concierge": "консьерж",
    "closed_yard": "закрытый двор",
    "playground": "детская площадка",
    "parking": "паркинг",
    "closed_territory": "закрытая территория",
}

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
 "red_flags": ["..."],
 "is_relayout": true/false/null,
 "is_relayout_legal": true/false/null,
 "is_free_layout": true/false/null,
 "has_ac": true/false/null,
 "complex_facts": {
   "location": "краткое описание локации ЖК из текста, или null",
   "nearby": "что рядом (школы/магазины/парки и т.п.) из текста, или null",
   "architecture": "архитектура/дизайн ЖК из текста, или null",
   "lobby": "описание холла из текста, или null",
   "security": "описание охраны из текста, или null",
   "concierge": true/false/null,
   "closed_yard": true/false/null,
   "playground": true/false/null,
   "parking": "краткое описание паркинга (закрытый/рядом/подземный) из текста, или null",
   "closed_territory": true/false/null
 }
}
is_relayout/is_relayout_legal/is_free_layout/has_ac — только если текст явно об этом
говорит, иначе null (не угадывай).
complex_facts — ТОЛЬКО факты про сам ДОМ/ЖК (не про квартиру), и только если
текст реально это упоминает — иначе null у каждого поля. Не выдумывай."""


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
                    "max_tokens": 700,
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


async def _notify_admin(text: str) -> None:
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


async def apply_layout_bonus(listing_id: str, result: dict) -> None:
    """Небольшой плюс к скору за распашонку/свободную планировку — пишем в
    те же layer_bonus/layer_details, что и остальные слои скоринга."""
    bonus = 0
    parts = []
    if result.get("layout") == "распашонка":
        bonus += 3
        parts.append("распашонка +3")
    if result.get("is_free_layout"):
        bonus += 2
        parts.append("свободная планировка +2")
    if not bonus:
        return
    row = await fetchrow(
        "SELECT layer_bonus, layer_details FROM apartment_listings WHERE id = $1", listing_id)
    if row is None:
        return
    details = row["layer_details"]
    details = json.loads(details) if isinstance(details, str) else (details or {})
    details["layout"] = {"adj": bonus, "reason": ", ".join(parts)}
    new_bonus = (row["layer_bonus"] or 0) + bonus
    await execute(
        "UPDATE apartment_listings SET layer_bonus = $2, layer_details = $3::jsonb WHERE id = $1",
        listing_id, new_bonus, json.dumps(details, ensure_ascii=False),
    )


async def apply_complex_facts(listing_id: str, complex_name: str | None,
                               url: str | None, facts: dict) -> None:
    """Если по описанию нашлись факты о ЖК, которых ещё нет в
    complexes.ai_features — дополняем и уведомляем админа в Telegram."""
    if not complex_name or not facts:
        return
    complex_row = await fetchrow(
        "SELECT id, name, ai_features FROM complexes WHERE lower(trim(name)) = lower(trim($1))",
        complex_name,
    )
    if complex_row is None:
        return
    existing = complex_row["ai_features"]
    existing = json.loads(existing) if isinstance(existing, str) else (existing or {})

    added_labels = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for key, label in _COMPLEX_FACT_LABELS.items():
        val = facts.get(key)
        if val is None or val == "" or key in existing:
            continue
        existing[key] = {"value": val, "source_listing_id": listing_id,
                          "source_url": url, "added_at": now_iso}
        added_labels.append(label)

    if not added_labels:
        return

    await execute(
        "UPDATE complexes SET ai_features = $2::jsonb WHERE id = $1",
        complex_row["id"], json.dumps(existing, ensure_ascii=False),
    )
    await _notify_admin(
        f"🏢 ЖК «{complex_row['name']}»: AI дополнил из описания объявления — {', '.join(added_labels)}.\n"
        f"Источник: {url or '(без ссылки)'}"
    )


async def analyze_top_listings(limit: int = 10) -> int:
    """
    Прогнать через DeepSeek новые объявления с описанием и без ai_analysis
    (сначала свежие — ORDER BY first_seen DESC). Возвращает число обработанных.
    """
    rows = await fetch("""
        SELECT id, title, description, complex_name, url FROM apartment_listings
        WHERE description IS NOT NULL AND length(description) > 80
          AND ai_analysis IS NULL
          AND is_active IS NOT FALSE
        ORDER BY first_seen DESC
        LIMIT $1
    """, limit)

    done = 0
    for r in rows:
        result = await analyze_one(r["id"], r["title"] or "", r["description"] or "")
        if result is None:
            continue
        await execute(
            "UPDATE apartment_listings SET ai_analysis = $2::jsonb, ai_analyzed_at = now() WHERE id = $1",
            r["id"], json.dumps(result, ensure_ascii=False),
        )
        try:
            await apply_layout_bonus(r["id"], result)
        except Exception as exc:
            logger.warning("layout bonus failed %s: %s", r["id"], exc)
        try:
            await apply_complex_facts(r["id"], r["complex_name"], r["url"], result.get("complex_facts") or {})
        except Exception as exc:
            logger.warning("complex facts failed %s: %s", r["id"], exc)
        done += 1
    logger.info("ai text analysis: %d listings processed", done)
    return done
