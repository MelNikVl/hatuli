"""
Новостройки BI Group (bi.group) — шахматки квартир по ЖК Астаны.

Пилотный парсер раздела "Новостройки" (см. migrations/041_newbuild.sql).
DB-запись — общий пайплайн в newbuild_common.py; здесь только сбор данных
с сайта BI Group.

API: apigw.bi.group/sales-picker/microfe-v3 — открытый JSON, без авторизации
(проверено вручную curl'ом). Три релевантных эндпоинта:

  - POST /filter {}            -> каталог ВСЕХ ЖК компании по всем городам
                                   (161 шт на момент разведки), у каждого
                                   blocks[] с deadline/count/minSum/maxSum.
                                   Никакой ключ (cities/cityUuids/city/...)
                                   город не фильтрует — проверено, всегда
                                   отдаёт полный каталог.
  - POST /placementList {...}  -> шахматка: список квартир (юнитов) ЖК.
    ВАЖНО (стоило часа реверса): pageNo 1-based. pageNo=0 -> generic
    400 "Неверный запрос (id: ...)" без внятного сообщения — похоже на
    необработанное исключение где-то в пагинации на их стороне, а вовсе
    не на невалидный формат тела запроса (я потратил время именно на это
    заблуждение). pageSize можно сразу большим (проверено 800 -> отдал
    все 470 без обрезки), но пагинируем честно, растим pageNo, пока
    страница не вернётся короче pageSize — на случай ЖК покрупнее.
  - propertyTypes нужно явно ограничивать uuid "Квартира", иначе в выдаче
    вперемешку паркинги/кладовки/офисы/цоколь.

Статус юнита: явного "продано" в API нет — проданные просто пропадают из
выдачи (проверено: среди 470 юнитов Arna Urpaq встретились только статусы
"Свободно"/"Снятие брони"/"Снятие резерва"/"Расторжение" — путь ДО продажи,
"Продано" как отдельного статуса нет вообще). isSale=false означает "занят
бронированием прямо сейчас" -> 'reserved', иначе -> 'available'.

Город: раз /filter не фильтрует по городу, Астану вычисляем по
blockAddress (приходит только в placementList, у каталога /filter его
нет) — 1 дешёвый пробный запрос (pageSize=1) на ЖК; если "Астана" не
встретилось в адресе — весь проект пропускаем.

Запуск:
    venv/bin/python bi_group_import.py --test --limit 3   # посмотреть, без записи
    venv/bin/python bi_group_import.py --limit 10
    venv/bin/python bi_group_import.py                    # весь каталог Астаны
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys

import httpx

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from newbuild_common import ComplexData, UnitData, run_source, parse_deadline_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bi_group")

API = "https://apigw.bi.group/sales-picker/microfe-v3"
FILTER_URL = f"{API}/filter"
PLACEMENTS_URL = f"{API}/placementList"

KVARTIRA_UUID = "5990a172-812a-4fee-b4f5-c860cca824d7"  # propertyType "Квартира"
CITY_MARK = "Астана"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SOURCE = "bi_group"
DEVELOPER_NAME = "BI Group"
DEVELOPER_WEBSITE = "https://bi.group"
# Короткий номер, как показан у них в шапке сайта ("📞 360" рядом с "Заказать
# звонок") — по договорённости берём контакт с сайта застройщика как есть;
# полноценный номер при желании правится вручную в /admin/developer/{id}.
DEVELOPER_PHONE = "360"


async def _pause() -> None:
    await asyncio.sleep(random.uniform(0.4, 0.9))  # API открытый и быстрый, но вежливость не помешает


async def fetch_catalog(client: httpx.AsyncClient) -> list[dict]:
    """POST /filter {} -> список ЖК компании по всем городам (см. докстринг)."""
    resp = await client.post(FILTER_URL, json={})
    resp.raise_for_status()
    data = resp.json()
    return data.get("realEstates") or []


async def probe_units(client: httpx.AsyncClient, re_uuid: str, page_no: int,
                       page_size: int) -> list[dict]:
    body = {
        "realEstateUUIDs": [re_uuid],
        "propertyTypes": [KVARTIRA_UUID],
        "pageNo": page_no,
        "pageSize": page_size,
    }
    resp = await client.post(PLACEMENTS_URL, json=body)
    resp.raise_for_status()
    data = resp.json()
    return data.get("placements") or []


async def fetch_all_units(client: httpx.AsyncClient, re_uuid: str,
                           page_size: int = 100) -> list[dict]:
    """Полная пагинация шахматки одного ЖК. pageNo — 1-based (см. докстринг)."""
    all_units: list[dict] = []
    page = 1
    while True:
        batch = await probe_units(client, re_uuid, page, page_size)
        all_units.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
        if page > 200:  # защита от бесконечного цикла, если что-то пойдёт не так
            log.warning("re_uuid=%s: остановился на странице %d (похоже на баг)", re_uuid, page)
            break
        await _pause()
    return all_units


def _to_unit_data(u: dict) -> UnitData:
    price = u.get("totalPriceWithDiscount") or u.get("totalPrice")
    return UnitData(
        source_unit_id=u["uuid"],
        rooms=u.get("roomCount"),
        area=u.get("square"),
        floor=u.get("floor"),
        floors_total=u.get("maxFloor"),
        price=int(round(price)) if price else None,
        price_per_m2=u.get("priceBySquare"),
        building=u.get("blockName"),
        section=str(u["entrance"]) if u.get("entrance") is not None else None,
        layout_photo_url=u.get("photoURL1600") or u.get("photoURL400"),
        photos=None,
        is_available=u.get("isSale") is not False,
        deadline=parse_deadline_iso(u.get("deadLine")),
        raw=u,
    )


async def fetch_astana_complexes(client: httpx.AsyncClient, limit: int | None) -> list[ComplexData]:
    """Пробным pageSize=1 запросом на каждый ЖК каталога определяет город
    (по blockAddress — единственное место, где город вообще есть, см.
    докстринг), отбирает только Астану, тянет полную шахматку."""
    catalog = await fetch_catalog(client)
    log.info("Каталог BI Group: %d ЖК по всем городам", len(catalog))
    result: list[ComplexData] = []
    for i, re_entry in enumerate(catalog, 1):
        if limit and len(result) >= limit:
            break
        re_uuid = re_entry["uuid"]
        try:
            probe = await probe_units(client, re_uuid, 1, 1)
        except Exception as e:
            log.warning("[%d/%d] %s: probe failed: %s", i, len(catalog), re_entry["name"], e)
            continue
        await _pause()
        if not probe:
            continue  # ЖК без квартир в продаже (только паркинг/коммерция) — пропускаем
        address = probe[0].get("blockAddress") or ""
        if CITY_MARK not in address:
            continue
        log.info("[%d/%d] %s: Астана, тяну шахматку...", i, len(catalog), re_entry["name"])
        raw_units = await fetch_all_units(client, re_uuid)
        log.info("  -> %d квартир", len(raw_units))
        if not raw_units:
            continue
        units = [_to_unit_data(u) for u in raw_units]
        name = raw_units[0].get("realEstateName") or re_entry["name"]
        housing_class = next((u.get("propertyClassName", [None])[0] for u in raw_units
                               if u.get("propertyClassName")), None)
        result.append(ComplexData(source_id=re_uuid, name=name, address=address,
                                   housing_class=housing_class, units=units))
        await _pause()
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="показать без записи в БД")
    parser.add_argument("--limit", type=int, default=None,
                        help="только первые N ЖК Астаны (для проверки)")
    args = parser.parse_args()

    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        complexes = await fetch_astana_complexes(client, args.limit)

    total_units = sum(len(c.units) for c in complexes)
    log.info("Астана: %d ЖК, %d квартир в шахматках", len(complexes), total_units)
    for c in complexes:
        log.info("  %-30s %4d квартир · %s", c.name[:30], len(c.units), c.address)

    if not complexes:
        log.error("Ничего не собрано — возможно BI Group поменяли API.")
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
