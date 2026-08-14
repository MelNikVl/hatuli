#!/usr/bin/env python3
"""Фото и адрес ЖК — приоритет источников (задача 2026-08-13, "фото ЖК
проверить, чтобы на всех страницах ЖК были фото от (по приоритетности):
1 сайт застройщика, 2 homeportal, 3 крыша, 4 korter, 5 homsters"):

Живая проверка сайтов застройщиков (см. терминал 2026-08-13) — ни один из
5 текущих importer'ов (sensata/bi_group/bazis/orda_invest/nak,
newbuild_common.py) не собирает фото самого ЖК — только фото планировок
отдельных юнитов (`layout_photo_url`, поле `photos` у них жёстко `None`).
Korter/homsters — та же картина, фото ЖК никогда не парсились. Реально
работающих источника фото ЖК — 2: homeportal (уже в БД, никаких сетевых
запросов не нужно) и krisha (hype_tracker/krisha_complex_scan.py, сеть,
щадящий батч). Этот скрипт закрывает уровень #2 (homeportal) — дёшево,
локально, без сети. Уровень #3 (krisha) — отдельный уже существующий
скрипт, ЕГО тоже нужно было включить в расписание (см. systemd timer
krisha-complex-scan) — раньше существовал, но не был запущен по крону,
отсюда живая жалоба "проверь — может уже есть и просто не запущен".

photos_source (migrations/056) — метка текущего источника, чтобы более
низкоприоритетный парсер (krisha) не затирал более приоритетный
(homeportal), если тот когда-нибудь запустится позже по времени.

Тот же прогон заодно бэкфилит complexes.address (тоже просьба — "можно
объединить с поиском адресов для всех ЖК") — homeportal_objects.address
уже есть в БД для тех же строк, второй сетевой поход не нужен.
"""
import argparse
import asyncio
import json
import logging

logger = logging.getLogger("complex_photos_address_backfill")

# Приоритет: чем МЕНЬШЕ число, тем важнее источник. Драйвер решения
# "можно ли перезаписать текущие фото" в этом скрипте и в krisha_complex_scan.py.
SOURCE_PRIORITY = {"developer": 1, "homeportal": 2, "krisha": 3, "korter": 4, "homsters": 5}


def _extract_homeportal_images(images_raw) -> list[str]:
    if isinstance(images_raw, str):
        try:
            images_raw = json.loads(images_raw)
        except ValueError:
            return []
    if not isinstance(images_raw, list):
        return []
    out = []
    for im in images_raw:
        if not isinstance(im, dict):
            continue
        link = im.get("image_link") or im.get("preview_link")
        if link and link not in out:
            out.append(link)
    return out[:10]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    import os
    from bot.db.pg import init_pool, close_pool, fetch, execute

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        for line in open(".env"):
            if line.startswith("DATABASE_URL="):
                dsn = line.strip().split("=", 1)[1].strip('"').strip("'")
                break
    await init_pool(dsn)

    # Та же связка legacy matched_complex_id + актуальный complex_source_links,
    # что уже чинили в terminal_extras.py (комментарий "живой баг #2481") —
    # matched_complex_id может годами указывать на уже is_garbage=TRUE строку
    # после merge_complex_pair(), пока complex_source_links свежий.
    rows = await fetch("""
        SELECT DISTINCT ON (c.id)
               c.id AS complex_id, c.photos_source, c.photos, c.address AS cx_address,
               ho.images, ho.address AS hp_address
        FROM homeportal_objects ho
        JOIN complexes c ON (
            c.id = ho.matched_complex_id
            OR EXISTS (
                SELECT 1 FROM complex_source_links csl
                WHERE csl.source = 'homeportal' AND csl.source_id = ho.object_id::text
                  AND csl.complex_id = c.id
            )
        )
        WHERE COALESCE(c.is_garbage, FALSE) = FALSE
          AND (ho.images IS NOT NULL AND ho.images != 'null'::jsonb)
        ORDER BY c.id, ho.object_id
    """)
    logger.info("кандидатов (ЖК с homeportal-фото): %d", len(rows))

    photos_updated, addr_updated, skipped_higher_prio = 0, 0, 0
    for r in rows:
        current_prio = SOURCE_PRIORITY.get(r["photos_source"], 99)  # неизвестный источник — низший приоритет
        homeportal_prio = SOURCE_PRIORITY["homeportal"]
        can_overwrite_photos = homeportal_prio <= current_prio
        images = _extract_homeportal_images(r["images"])

        if images and can_overwrite_photos:
            if not args.dry:
                await execute(
                    "UPDATE complexes SET photos = $2::jsonb, photo_url = COALESCE($3, photo_url), "
                    "photos_source = 'homeportal', updated_at = now() WHERE id = $1",
                    r["complex_id"], json.dumps(images, ensure_ascii=False), images[0])
            photos_updated += 1
        elif images:
            skipped_higher_prio += 1

        if not r["cx_address"] and r["hp_address"]:
            if not args.dry:
                await execute("UPDATE complexes SET address = $2, updated_at = now() WHERE id = $1",
                              r["complex_id"], r["hp_address"])
            addr_updated += 1

    logger.info("итог: фото обновлено %d, адрес заполнен %d, пропущено (уже выше приоритет) %d%s",
                photos_updated, addr_updated, skipped_higher_prio, " [DRY]" if args.dry else "")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
