#!/usr/bin/env python3
"""
Одноразовая чистка: удаляет ПОСЛЕДНЕЕ фото из массива photos у всех
объявлений — это рекламный баннер Крыши, а не фото квартиры.

Парсер уже исправлен и больше не сохраняет последний кадр галереи,
поэтому этот скрипт нужен ровно ОДИН раз — для уже накопленной базы.

Защита от повторного запуска: ставит флаг AD_PHOTO_CLEANUP_DONE=1
в app_settings; при повторном запуске ничего не делает (иначе каждый
запуск срезал бы по одному настоящему фото).

Запуск:  python drop_ad_photos.py
Форс (если точно надо ещё раз):  python drop_ad_photos.py --force
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("drop_ad_photos")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

FLAG = "AD_PHOTO_CLEANUP_DONE"


async def main() -> None:
    from bot.db.pg import init_pool, execute as pg_exec, fetchval as pg_fetchval

    await init_pool(DATABASE_URL)

    force = "--force" in sys.argv
    done = await pg_fetchval(
        "SELECT value FROM app_settings WHERE key = $1", FLAG
    )
    if done == "1" and not force:
        log.info("Чистка уже выполнялась (флаг %s=1). Повторный запуск срезал бы "
                 "настоящие фото — выходим. Если очень надо: --force", FLAG)
        return

    # jsonb "- N" удаляет элемент массива по индексу; берём последний.
    # Трогаем только записи, где фото >= 2: у единственного фото ничего
    # не удаляем (лучше баннер, чем пустая карточка).
    result = await pg_exec("""
        UPDATE apartment_listings
        SET photos = photos - (jsonb_array_length(photos) - 1)
        WHERE photos IS NOT NULL
          AND jsonb_typeof(photos) = 'array'
          AND jsonb_array_length(photos) >= 2
    """)
    log.info("apartment_listings: %s", result)

    await pg_exec("""
        INSERT INTO app_settings (key, value, updated_at) VALUES ($1, '1', now())
        ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = now()
    """, FLAG)
    log.info("Готово, флаг %s=1 поставлен.", FLAG)


if __name__ == "__main__":
    asyncio.run(main())
