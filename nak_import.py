"""
Новостройки NAK (nak.kz) — шахматки квартир по ЖК Астаны.

Самый простой источник из четырёх: НЕ требует Playwright вообще. Общий
список проектов — Strapi CMS (admin.nak.kz/api/projects, см. докстринг
попытки в bi_group_import.py — нет, это отдельное открытие), а сама
шахматка квартир — открытый JSON на СВОЁМ ЖЕ домене:
    https://www.nak.kz/api/apartments/{protrendProjectId}
(same-origin, без авторизации, без CORS-плясок — несмотря на то, что
данные явно приходят из стороннего Protrend, NAK сами проксируют их на
своём домене, поэтому обычный httpx работает с первого раза).

Структура (проверена вручную curl'ом):
  - admin.nak.kz/api/projects?pagination[pageSize]=100 — все проекты NAK
    (сейчас 6 в Астане), у каждого name/slug/address/district/coordinates/
    deadline + protrendProjectId. У ДЕЙСТВУЮЩИХ (с онлайн-шахматкой) это
    поле заполнено (проверено: 2 из 6 — BAYAAN=30, Dala Jusan=29; у
    остальных 4 либо шахматка ещё не запущена, либо продажи через другой
    канал) — фильтруем по нему.
  - www.nak.kz/api/apartments/{protrendProjectId} — ВСЕ квартиры проекта
    ОДНИМ ответом (total + items[], без пагинации, проверено на 333
    квартирах) со статусом ПРЯМО в ответе (FREE/RESERVED/SOLD — точное
    совпадение со счётчиками "Свободно/Бронь/Продано" на самой странице
    шахматки, см. /ru/projects/{slug}, кнопка "Шахматка").
    price = цена за м², cost = полная цена (проверено: area*price == cost).

SOLD квартиры сюда сознательно не берём (как и в sensata_import.py) —
пропавшее между обходами newbuild_common сам пометит sold_at.

Запуск:
    venv/bin/python nak_import.py --test   # без записи
    venv/bin/python nak_import.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import httpx

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from newbuild_common import ComplexData, UnitData, parse_deadline_iso, run_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nak")

PROJECTS_URL = "https://admin.nak.kz/api/projects"
APARTMENTS_URL = "https://www.nak.kz/api/apartments/{project_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SOURCE = "nak"
DEVELOPER_NAME = "NAK"
DEVELOPER_WEBSITE = "https://www.nak.kz"
DEVELOPER_PHONE = None  # телефон в шапке сайта смахивает на общий контакт-центр, а не отдел продаж — уточнить вручную


def _parse_latlon(s: str | None) -> tuple[float, float] | None:
    if not s or "," not in s:
        return None
    try:
        lat_s, lon_s = s.split(",", 1)
        return float(lat_s.strip()), float(lon_s.strip())
    except ValueError:
        return None


def _to_unit_data(item: dict, deadline) -> UnitData | None:
    status = item.get("status")
    if status not in ("FREE", "RESERVED"):
        return None  # SOLD пропускаем, см. докстринг модуля
    area = item.get("area")
    price_per_m2 = item.get("price")
    cost = item.get("cost")
    building = item.get("block")
    if item.get("subblock"):
        building = f"{building}-{item['subblock']}"
    return UnitData(
        source_unit_id=str(item["id"]),
        rooms=int(item["roomcount"]) if item.get("roomcount") not in (None, "") else None,
        area=float(area) if area not in (None, "") else None,
        floor=int(item["floor"]) if item.get("floor") not in (None, "") else None,
        floors_total=None,
        price=int(cost) if cost is not None else None,
        price_per_m2=int(price_per_m2) if price_per_m2 is not None else None,
        building=building,
        section=item.get("queue"),  # очередь стройки — ближайший аналог "секции" у этого источника
        layout_photo_url=None,      # это API отдаёт только числа, без фото — фото на самой странице шахматки, не за отдельным юнитом
        photos=None,
        is_available=(status == "FREE"),
        deadline=deadline,
        raw=item,
    )


async def fetch_astana_complexes(client: httpx.AsyncClient) -> list[ComplexData]:
    resp = await client.get(PROJECTS_URL, params={"pagination[pageSize]": 100})
    resp.raise_for_status()
    projects = resp.json().get("data") or []
    with_protrend = [p for p in projects if p.get("protrendProjectId")]
    log.info("Проектов всего: %d, с активной шахматкой (protrendProjectId): %d",
             len(projects), len(with_protrend))

    complexes: list[ComplexData] = []
    for p in with_protrend:
        pid = p["protrendProjectId"]
        name = p.get("name") or f"NAK #{pid}"
        try:
            r = await client.get(APARTMENTS_URL.format(project_id=pid))
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("[%s] apartments/%s failed: %s", name, pid, e)
            continue
        items = data.get("items") or []
        deadline = parse_deadline_iso(p.get("deadline"))
        units = [u for it in items if (u := _to_unit_data(it, deadline)) is not None]
        log.info("[%s] всего в шахматке: %d, доступно+бронь: %d", name, len(items), len(units))
        if not units:
            continue
        latlon = _parse_latlon(p.get("coordinates"))
        address_bits = [p.get("address"), p.get("district")]
        address = ", ".join(b for b in address_bits if b) or None
        complexes.append(ComplexData(
            source_id=str(p["id"]), name=name, address=address, housing_class=None, units=units,
            lat=latlon[0] if latlon else None, lon=latlon[1] if latlon else None,
        ))
    return complexes


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="показать без записи в БД")
    args = parser.parse_args()

    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        complexes = await fetch_astana_complexes(client)

    total_units = sum(len(c.units) for c in complexes)
    log.info("Итого: %d ЖК, %d квартир (available+reserved)", len(complexes), total_units)
    for c in complexes:
        log.info("  %-30s %4d квартир · координаты=%s", c.name[:30], len(c.units),
                  (c.lat, c.lon) if c.lat else "нет")

    if not complexes:
        log.error("Ничего не собрано — возможно NAK поменяли API.")
        sys.exit(1)

    if args.test:
        log.info("--test: в БД НЕ записано.")
        return

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        stats = await run_source(SOURCE, DEVELOPER_NAME, DEVELOPER_WEBSITE, DEVELOPER_PHONE, complexes)
        log.info("=== Итог: ЖК=%d новых_юнитов=%d обновлено=%d продано=%d изменений_цены=%d ===",
                  stats.get("complexes", 0), stats.get("units_new", 0), stats.get("units_seen", 0),
                  stats.get("units_sold", 0), stats.get("price_changes", 0))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
