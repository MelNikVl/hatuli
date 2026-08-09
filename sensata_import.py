"""
Новостройки Sensata Group (sensata.kz) — шахматки квартир по ЖК Астаны.

Технически самый сложный источник из всех: виджет-каталог квартир — это
сторонняя SaaS-платформа ProfitBase (smart-catalog.profitbase.ru),
встроенная в cross-origin iframe. Реальные запросы этого iframe НЕВИДИМЫ
для обычного браузерного расширения (оно наблюдает только за исходным
доменом вкладки) — поэтому единственный рабочий способ достать данные это
Playwright: он видит сетевой трафик из ЛЮБого фрейма страницы, я просто
даю виджету открыться, как открыл бы обычный посетитель, и читаю ответы
его собственных XHR (свою аутентификацию/куки/токены виджет тащит сам,
повторить их вручную через httpx не вышло — сервер стабильно отвечал
"Not allowed", видимо там ещё и bearer-токен от OAuth, а не только cookie).

Устройство ProfitBase (аккаунт 4678, общий на ВСЮ группу Sensata, не на
один ЖК):
  - Открыть виджет можно с любой страницы sensata.kz — кликом по плавающей
    кнопке "sw-button" (тогда открывается общий каталог всех проектов) ИЛИ
    просто зайдя на URL с хэшем `#/catalog/house/{houseId}/smallGrid` —
    сам виджет слушает location.hash при загрузке (см. openIfHashExists в
    их sw.js) и сразу открывает нужный дом, без клика.
  - /api/v4/json/projects — ВСЕ проекты аккаунта (Астана и Алматы вперемешку,
    архивные и живые) — читаем locality + archiveState, чтобы отобрать
    активные астанинские (сам аккаунт явно ведёт застройки в обоих городах,
    выбранный вами пример sensatamangilik.kz оказался про Алматы — поэтому
    в этом парсере он не используется вообще, вместо него общий вход через
    sensata.kz + фильтр по городу).
  - /api/v4/json/house — дома (корпуса/очереди) по ВСЕМ проектам сразу,
    с house.projectId для связи с проектом.
  - /api/v4/json/property?houseId={id}&returnFilteredCount=true — ВСЕ
    квартиры дома ОДНИМ запросом (без пагинации — проверено на доме в 357
    квартир, пришли все) — и, в отличие от остальных источников, здесь
    статус ('AVAILABLE'/'BOOKED'/'SOLD') отдаётся ЯВНО, не нужно вычислять
    его по исчезновению из выдачи между обходами.
  - /api/v4/json/plan?houseId={id}&status[0]=AVAILABLE — планировки
    (сгруппированы по типу, каждая группа знает id всех квартир этого типа
    + фото) — используем только для карты "id квартиры -> фото планировки",
    сами квартиры берём из /property.

SOLD квартиры сюда сознательно НЕ загружаем (см. UnitData ниже) — общая
логика newbuild_common сама пометит их sold_at, когда они пропадут из
следующего обхода; это не даёт исторический бэкфилл уже проданного на
момент первого прохода, но зато не плодит отдельную ветку логики ради
одного источника. Явный статус тут просто ЗАМЕНЯЕТ вычисление по
отсутствию (надёжнее), а не расширяет модель данных.

Запуск:
    venv/bin/python sensata_import.py --test --limit 3   # без записи, первые 3 дома
    venv/bin/python sensata_import.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from newbuild_common import ComplexData, UnitData, run_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sensata")

ENTRY_URL = "https://sensata.kz/"
BASE = "https://sensata.kz"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SOURCE = "sensata"
DEVELOPER_NAME = "Sensata Group"
DEVELOPER_WEBSITE = "https://sensata.kz"
DEVELOPER_PHONE = None  # не нашли на страницах каталога — заполнить вручную в /admin/developer/{id}

_CITY_MARKS = ("Астана", "Нур-Султан")


def _unwrap(text: str):
    import json
    d = json.loads(text)
    return d["data"] if isinstance(d, dict) and "data" in d else d


async def discover_astana_houses(page) -> tuple[dict, list[dict]]:
    """Один заход на сайт + клик по плавающей кнопке виджета -> ловим
    /projects и /house (весь аккаунт сразу), отбираем астанинские активные
    проекты. Возвращает (project_id -> project dict, [house dict, ...])."""
    bodies: dict[str, str] = {}

    async def on_resp(resp):
        u = resp.url.split("?")[0]
        if u.endswith("/api/v4/json/house") or u.endswith("/api/v4/json/projects"):
            bodies.setdefault(resp.url, await resp.text())

    page.on("response", on_resp)
    await page.goto(ENTRY_URL, wait_until="load", timeout=45000)
    await page.wait_for_timeout(2000)
    try:
        await page.click("sw-button, .sw-button__wrapper, [class*='sw-button']", timeout=8000)
    except Exception as e:
        log.warning("не смог кликнуть по кнопке виджета: %s", e)
    await page.wait_for_timeout(5000)
    page.remove_listener("response", on_resp)

    projects_raw = next((b for u, b in bodies.items() if u.split("?")[0].rstrip("/").endswith("/projects")), None)
    houses_raw = next((b for u, b in bodies.items() if u.split("?")[0].rstrip("/").endswith("/house")), None)
    if not projects_raw or not houses_raw:
        log.error("не поймал /projects или /house — виджет не открылся?")
        return {}, []

    projects = _unwrap(projects_raw)
    houses = _unwrap(houses_raw)

    astana_projects = {p["id"]: p for p in projects
                        if p.get("locality") and any(m in p["locality"] for m in _CITY_MARKS)
                        and p.get("archiveState") != "ARCHIVED"}
    astana_houses = [h for h in houses if h.get("projectId") in astana_projects]
    log.info("Астана: %d активных проектов, %d домов", len(astana_projects), len(astana_houses))
    return astana_projects, astana_houses


async def fetch_house_units(browser, house: dict) -> list[UnitData]:
    """Хэш `#/catalog/house/{id}/smallGrid` на странице сайта сам триггерит
    открытие виджета сразу на этом доме (см. докстринг), НО только на
    честном полном reload — если просто поменять hash у уже открытой
    страницы (goto с тем же path), Chrome схлопывает это в soft-navigation
    без перезапуска скриптов, и виджет самого хэша не замечает (проверено —
    молча 0 юнитов). Поэтому на каждый дом — отдельная новая вкладка."""
    house_id = house["id"]
    bodies: dict[str, str] = {}
    page = await browser.new_page()

    async def on_resp(resp):
        u = resp.url.split("?")[0]
        if u.endswith("/api/v4/json/property") or u.endswith("/api/v4/json/plan"):
            bodies.setdefault(resp.url, await resp.text())

    page.on("response", on_resp)
    url = f"{BASE}/#/catalog/house/{house_id}/smallGrid"
    try:
        await page.goto(url, wait_until="load", timeout=45000)
        await page.wait_for_timeout(6000)
    except Exception as e:
        log.warning("house=%s: goto failed: %s", house_id, e)
        return []
    finally:
        await page.close()

    prop_raw = next((b for u, b in bodies.items()
                      if u.split("?")[0].rstrip("/").endswith("/property") and "houseId=" in u), None)
    plan_raw = next((b for u, b in bodies.items() if u.split("?")[0].rstrip("/").endswith("/plan")), None)
    if not prop_raw:
        log.warning("house=%s (%s): /property не поймали", house_id, house.get("title"))
        return []

    properties = (_unwrap(prop_raw) or {}).get("properties") or []
    photo_by_id: dict[int, str] = {}
    if plan_raw:
        for group in (_unwrap(plan_raw) or []):
            img = (group.get("image") or {}).get("big") or (group.get("image") or {}).get("preview")
            if not img:
                continue
            for pid in group.get("properties") or []:
                try:
                    photo_by_id[int(pid)] = img
                except (TypeError, ValueError):
                    pass

    units: list[UnitData] = []
    for p in properties:
        status = p.get("status")
        if status not in ("AVAILABLE", "BOOKED"):
            continue  # SOLD сознательно пропускаем, см. докстринг модуля
        price_obj = p.get("price") or {}
        area = (p.get("area") or {}).get("area_total")
        dev_end = house.get("developmentEndQuarter") or {}
        deadline = None
        if dev_end.get("year") and dev_end.get("quarter"):
            try:
                month = (int(dev_end["quarter"]) - 1) * 3 + 1
                from datetime import datetime as _dt
                deadline = _dt(int(dev_end["year"]), month, 1)
            except (TypeError, ValueError):
                pass
        units.append(UnitData(
            source_unit_id=str(p["id"]),
            rooms=p.get("rooms_amount"),
            area=float(area) if area is not None else None,
            floor=p.get("floor"),
            floors_total=house.get("maxFloor"),
            price=int(price_obj["value"]) if price_obj.get("value") else None,
            price_per_m2=price_obj.get("pricePerMeter"),
            building=house.get("title") or house.get("projectName"),
            section=p.get("sectionName"),
            layout_photo_url=photo_by_id.get(p["id"]),
            photos=None,
            is_available=(status == "AVAILABLE"),
            deadline=deadline,
            raw=p,
        ))
    return units


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="показать без записи в БД")
    parser.add_argument("--limit", type=int, default=None, help="только первые N домов (для проверки)")
    args = parser.parse_args()

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(page)
        except Exception as e:
            log.warning("playwright-stealth недоступен, работаем без него: %s", e)

        astana_projects, astana_houses = await discover_astana_houses(page)
        if args.limit:
            astana_houses = astana_houses[:args.limit]

        by_project: dict[int, list[UnitData]] = {}
        for i, h in enumerate(astana_houses, 1):
            units = await fetch_house_units(browser, h)
            log.info("[%d/%d] %s / %s: %d юнитов (available+booked)",
                      i, len(astana_houses), h.get("projectName"), h.get("title"), len(units))
            by_project.setdefault(h["projectId"], []).extend(units)
        await browser.close()

    complexes: list[ComplexData] = []
    for pid, units in by_project.items():
        if not units:
            continue
        proj = astana_projects.get(pid, {})
        addr_bits = [proj.get("region"), proj.get("district"), proj.get("locality")]
        address = ", ".join(b for b in addr_bits if b) or None
        complexes.append(ComplexData(
            source_id=str(pid), name=proj.get("title") or f"Sensata #{pid}",
            address=address, housing_class=None, units=units,
        ))

    total_units = sum(len(c.units) for c in complexes)
    log.info("Итого: %d ЖК, %d квартир (available+booked)", len(complexes), total_units)
    for c in complexes:
        log.info("  %-30s %4d квартир", c.name[:30], len(c.units))

    if not complexes:
        log.error("Ничего не собрано — возможно Sensata/ProfitBase поменяли структуру.")
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
