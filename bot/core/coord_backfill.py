"""
Бэкфилл координат + ЖК со детальной страницы объявления (krisha.kz).

Единственный источник ТОЧНЫХ координат и официального названия ЖК —
детальная страница объявления (JS-блоб с lat/lon, сайдбар «Жилой комплекс»
со ссылкой на карточку). Запрашиваем её бережно: задержка между запросами
настраивается в /admin/settings (COORD_FETCH_DELAY_MIN/MAX, по умолчанию
8-15с) — это НЕ узкое место производительности, а осознанное ограничение,
чтобы не словить бан от krisha.kz. Батч за цикл — COORD_BACKFILL_BATCH там же.

Используется:
  - service_apartments.py — маленький батч (по умолчанию 15) раз в час,
    как часть обычного цикла парсинга.
  - backfill_coords_bulk.py — разовый догоняющий прогон по всему бэклогу
    объявлений без координат/ЖК (тот же rate limit, просто без ограничения
    на батч и без часового цикла вокруг).
"""
from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

_DELAY_DEFAULT = (8.0, 15.0)  # секунд между запросами к krisha.kz — дефолт, если настройка не задана


async def backfill_coords_and_complex(limit: int, min_age_days: float = 3.0) -> dict:
    """
    Докачивает координаты/ЖК для активных объявлений без lat или без
    complex_name (детальная страница даёт оба сигнала за один запрос).
    Идемпотентно: coord_fetch_attempted_at защищает от повторного долбления
    одного и того же объявления раньше чем через min_age_days.
    """
    from bot.db.pg import execute as pg_exec, fetch as pg_fetch
    from bot.core.apartment_details import fetch_apartment_details
    from bot.db import settings as app_settings

    delay_min = app_settings.get_float("COORD_FETCH_DELAY_MIN", _DELAY_DEFAULT[0])
    delay_max = app_settings.get_float("COORD_FETCH_DELAY_MAX", _DELAY_DEFAULT[1])
    if delay_max < delay_min:
        delay_min, delay_max = delay_max, delay_min

    await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_url TEXT")
    await pg_exec("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS krisha_url TEXT")
    await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS is_urgent BOOLEAN")

    missing = await pg_fetch("""
        SELECT id, url FROM apartment_listings
        WHERE (lat IS NULL OR complex_name IS NULL OR btrim(complex_name) = ''
               OR (COALESCE(is_owner, FALSE) = FALSE AND (seller_name IS NULL OR btrim(seller_name) = ''))
               OR photos IS NULL OR photos::text IN ('[]', 'null')
               OR description IS NULL OR btrim(description) = '')
          AND is_active IS NOT FALSE AND url IS NOT NULL
          AND (coord_fetch_attempted_at IS NULL
               OR coord_fetch_attempted_at < now() - ($2 || ' days')::interval)
        ORDER BY coord_fetch_attempted_at ASC NULLS FIRST, first_seen ASC
        LIMIT $1
    """, limit, str(min_age_days))

    got = got_cx = 0
    for i, m in enumerate(missing):
        await asyncio.sleep(random.uniform(delay_min, delay_max))
        if i and i % 50 == 0:
            logger.info("coord_backfill: %d/%d обработано (координаты %d, ЖК %d)",
                        i, len(missing), got, got_cx)
        try:
            details = await fetch_apartment_details(m["url"])
        except Exception as e:
            logger.warning("backfill fetch failed %s: %s", m["id"], e)
            details = None

        if not details or not (details.get("lat") or details.get("complex_name")):
            await pg_exec(
                "UPDATE apartment_listings SET coord_fetch_attempted_at=now() WHERE id=$1",
                m["id"])
            continue

        if details.get("lat") and details.get("lon"):
            await pg_exec(
                "UPDATE apartment_listings SET lat=$2, lon=$3, geo_source='krisha', "
                "coord_fetch_attempted_at=now() WHERE id=$1",
                m["id"], details["lat"], details["lon"])
            got += 1
        else:
            await pg_exec(
                "UPDATE apartment_listings SET coord_fetch_attempted_at=now() WHERE id=$1",
                m["id"])

        if details.get("complex_name"):
            # не принимаем названия, помеченные аудитом как улицы/мусор
            try:
                from bot.core.complex_audit import street_names, _JUNK_NAMES
                import re as _re_b
                streets = await street_names()
                n = (details["complex_name"] or "").lower()
                n = _re_b.sub(r"^\s*(жк|кг)\.?\s+", "", n)
                n = _re_b.sub(r"[«»\"'()]", " ", n)
                n = _re_b.sub(r"\s+", " ", n).strip()
                if n in streets or n in _JUNK_NAMES or len(n) < 4:
                    details.pop("complex_name", None)
                    details.pop("complex_url", None)
            except Exception:
                pass

        if details.get("complex_name"):
            await pg_exec(
                "UPDATE apartment_listings SET complex_name=$2, "
                "complex_url=COALESCE($3, complex_url) WHERE id=$1",
                m["id"], details["complex_name"], details.get("complex_url"))
            got_cx += 1
            if details.get("complex_url"):
                await pg_exec("""
                    INSERT INTO complexes (name, krisha_url)
                    SELECT $1, $2
                    WHERE NOT EXISTS (
                        SELECT 1 FROM complexes
                        WHERE lower(name) = lower($1) OR krisha_url = $2)
                """, details["complex_name"], details["complex_url"])

        if details.get("photos"):
            import json as _json
            await pg_exec(
                "UPDATE apartment_listings SET photos=$2::jsonb WHERE id=$1",
                m["id"], _json.dumps(details["photos"]))
        if details.get("seller_name"):
            await pg_exec(
                "UPDATE apartment_listings SET seller_name=$2 WHERE id=$1",
                m["id"], details["seller_name"])
        if "is_urgent" in details:
            await pg_exec(
                "UPDATE apartment_listings SET is_urgent=$2 WHERE id=$1",
                m["id"], details["is_urgent"])
        if details.get("views_count"):
            await pg_exec(
                "UPDATE apartment_listings SET views_count=$2 WHERE id=$1",
                m["id"], details["views_count"])
        if details.get("description"):
            await pg_exec(
                "UPDATE apartment_listings SET description=$2 WHERE id=$1",
                m["id"], details["description"])

    if missing:
        logger.info("coord_backfill: готово — координаты %d/%d, ЖК %d",
                    got, len(missing), got_cx)
    return {"attempted": len(missing), "got_coords": got, "got_complex": got_cx}
