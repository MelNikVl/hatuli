"""
Импорт застройщиков Астаны с homsters.kz/developers: карточка застройщика
(имя, год основания, описание, число объектов) + полный список его ЖК,
сопоставление ЖК с нашей таблицей complexes (developer_id).

Структура (проверена на живом сайте 19.07.2026):
- Каталог: /developers/astana + пагинация /developers/astana/page{N}.
  Карточка — div.b-developer.js-developer-item: ссылка a.b-developer__secondary-title
  ведёт на /{dev-slug}, «Год основания : <span>1995</span>», описание в
  div.b-developer__description-text.
- Страница застройщика /{dev-slug}: H1 = точное имя, в тексте блока фактов
  «1995 Год основания 125 Количество доступных объектов», «Все ЖК X : n из Y
  ЖК в продаже». Карточки ЖК — div.js-project-item (data-href = /{dev}/{jk},
  имя в <h2> вида «ЖК AinaLine», город — словом после имени). Пагинация
  /{dev-slug}/page{N}. Список ЖК — по всему Казахстану, поэтому фильтруем
  карточки по городу «Астана».

Сайт отдаёт 403 обычному httpx — используется curl_cffi (как homsters_import.py).

Запуск:
    venv/bin/python homsters_developers_import.py --test --limit 3
    venv/bin/python homsters_developers_import.py --limit 10
    venv/bin/python homsters_developers_import.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import sys

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from bot.core.site_enrichment import norm_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("homsters_devs")

BASE = "https://homsters.kz"
CATALOG_URL = f"{BASE}/developers/astana"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

CITY_FILTER = "Астана"

_FOUNDED_AFTER = re.compile(r"(\d{4})\s*Год основания")
_OBJECTS_AFTER = re.compile(r"(\d+)\s*Количество доступных объектов")
_JK_TOTAL = re.compile(r"из\s*(\d+)\s*ЖК в продаже")


def _slug_from_href(href: str) -> str | None:
    path = href.split("?")[0].split("#")[0]
    if path.startswith("http"):
        path = re.sub(r"^https?://[^/]+", "", path)
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) == 1 and parts[0] not in ("developers", "estate"):
        return parts[0]
    return None


def parse_catalog_page(html: str) -> list[dict]:
    """Карточки застройщиков со страницы каталога."""
    soup = BeautifulSoup(html, "html.parser")
    devs = []
    for card in soup.select("div.b-developer"):
        a = card.select_one("a.b-developer__secondary-title")
        if not a:
            continue
        slug = _slug_from_href(a.get("href", ""))
        name = a.get_text(strip=True)
        if not slug or not name:
            continue
        year_el = card.select_one("div.b-developer__short-content span")
        founded = None
        if year_el:
            m = re.search(r"\d{4}", year_el.get_text())
            if m:
                founded = int(m.group(0))
        desc_el = card.select_one("div.b-developer__description-text")
        desc = desc_el.get_text(" ", strip=True) if desc_el else None
        devs.append({"slug": slug, "name": name,
                     "founded_year": founded, "description": desc})
    return devs


def parse_dev_page(html: str, slug: str) -> dict:
    """Страница застройщика: точное имя, факты, ЖК (только Астана)."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {"jk": []}
    h1 = soup.find("h1")
    if h1:
        out["name"] = h1.get_text(strip=True)

    txt = soup.get_text(" ", strip=True)
    m = _FOUNDED_AFTER.search(txt)
    if m:
        out["founded_year"] = int(m.group(1))
    m = _OBJECTS_AFTER.search(txt)
    if m:
        out["projects_active"] = int(m.group(1))
    m = _JK_TOTAL.search(txt)
    if m:
        out["projects_total"] = int(m.group(1))

    for it in soup.select("div.js-project-item"):
        href = it.get("data-href") or ""
        if f"/{slug}/" not in href:
            continue
        h2 = it.find(["h2", "h3"])
        name = h2.get_text(strip=True) if h2 else None
        if not name:
            continue
        item_txt = it.get_text(" ", strip=True)
        if CITY_FILTER not in item_txt:
            continue  # ЖК застройщика в других городах нас не интересуют
        out["jk"].append({"name": name, "url": href})
    return out


def _fetch_curl_cffi(url: str) -> tuple[int, str]:
    from curl_cffi import requests as curl_requests
    resp = curl_requests.get(url, headers=HEADERS, impersonate="chrome", timeout=30)
    return resp.status_code, resp.text


async def _fetch(url: str, client: httpx.AsyncClient) -> tuple[int, str]:
    try:
        import curl_cffi  # noqa: F401
        return await asyncio.to_thread(_fetch_curl_cffi, url)
    except ImportError:
        resp = await client.get(url)
        return resp.status_code, resp.text


async def _pause() -> None:
    await asyncio.sleep(random.uniform(3, 5))


async def fetch_developers(limit: int | None = None,
                           max_jk_pages: int = 60) -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0,
                                 follow_redirects=True) as client:
        # 1) Каталог застройщиков
        devs: dict[str, dict] = {}
        for page in range(1, 100):
            url = CATALOG_URL if page == 1 else f"{CATALOG_URL}/page{page}"
            try:
                status, text = await _fetch(url, client)
            except Exception as e:
                log.warning("fetch %s failed: %s", url, e)
                break
            if status != 200:
                log.warning("%s -> %s, останавливаюсь", url, status)
                break
            new = 0
            for d in parse_catalog_page(text):
                if d["slug"] not in devs:
                    devs[d["slug"]] = d
                    new += 1
            log.info("catalog page %d: +%d застройщиков (всего %d)", page, new, len(devs))
            if new == 0:
                break
            if limit and len(devs) >= limit:
                break
            await _pause()

        result = list(devs.values())[:limit] if limit else list(devs.values())

        # 2) Страницы застройщиков: факты + все ЖК (Астана)
        for i, dev in enumerate(result, 1):
            slug = dev["slug"]
            seen_hrefs: set[str] = set()
            try:
                for page in range(1, max_jk_pages + 1):
                    url = f"{BASE}/{slug}" if page == 1 else f"{BASE}/{slug}/page{page}"
                    status, text = await _fetch(url, client)
                    if status != 200:
                        log.warning("%s -> %s", url, status)
                        break
                    info = parse_dev_page(text, slug)
                    if page == 1:
                        dev.update({k: v for k, v in info.items() if k != "jk"})
                    new = 0
                    for jk in info["jk"]:
                        if jk["url"] not in seen_hrefs:
                            seen_hrefs.add(jk["url"])
                            dev.setdefault("jk", []).append(jk)
                            new += 1
                    if page > 1 and new == 0:
                        break
                    if len(info["jk"]) == 0 and page == 1:
                        break  # вообще нет ЖК в Астане
                    await _pause()
            except Exception as e:
                log.warning("developer %s failed: %s", slug, e)
            log.info("[%d/%d] %s (%s): ЖК Астаны=%d, объектов=%s, осн.=%s",
                     i, len(result), dev.get("name"), slug,
                     len(dev.get("jk", [])), dev.get("projects_active"),
                     dev.get("founded_year"))
            await _pause()
    return result


async def save_to_db(devs: list[dict]) -> None:
    from bot.db.pg import fetch, fetchrow, fetchval, execute

    ours = await fetch("SELECT id, name, developer_id FROM complexes")
    by_norm = {}
    for r in ours:
        if r["name"]:
            by_norm.setdefault(norm_name(r["name"]), (r["id"], r["developer_id"]))

    stats = {"new": 0, "updated": 0, "jk_matched": 0, "jk_unmatched": 0,
             "links_set": 0}
    unmatched: list[str] = []

    for dev in devs:
        slug = dev["slug"]
        name = dev.get("name") or slug
        # Ищем существующего: по слагу -> по имени -> по алиасам
        row = await fetchrow(
            "SELECT id, aliases FROM developers WHERE homsters_slug = $1", slug)
        if not row:
            row = await fetchrow(
                "SELECT id, aliases FROM developers WHERE lower(name) = lower($1)", name)
        if not row:
            row = await fetchrow(
                "SELECT id, aliases FROM developers WHERE $1 = ANY(aliases)"
                " OR $2 = ANY(aliases)", slug, name)

        if row:
            dev_id = row["id"]
            aliases = list(row["aliases"] or [])
            for variant in (slug, name):
                if variant and variant not in aliases:
                    aliases.append(variant)
            await execute("""
                UPDATE developers SET
                    homsters_slug   = COALESCE(homsters_slug, $2),
                    founded_year    = COALESCE(founded_year, $3),
                    description     = COALESCE($4, description),
                    projects_active = COALESCE($5, projects_active),
                    projects_total  = COALESCE($6, projects_total),
                    aliases         = $7,
                    updated_at      = now()
                WHERE id = $1
            """, dev_id, slug, dev.get("founded_year"),
                dev.get("description"), dev.get("projects_active"),
                dev.get("projects_total"), aliases)
            stats["updated"] += 1
        else:
            dev_id = await fetchval("""
                INSERT INTO developers (name, homsters_slug, founded_year,
                                        description, projects_active,
                                        projects_total, aliases)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (name) DO UPDATE SET
                    homsters_slug = COALESCE(developers.homsters_slug, EXCLUDED.homsters_slug),
                    updated_at = now()
                RETURNING id
            """, name, slug, dev.get("founded_year"), dev.get("description"),
                dev.get("projects_active"), dev.get("projects_total"),
                [slug, name])
            stats["new"] += 1

        if dev_id is None:
            log.warning("не удалось определить id застройщика %s", slug)
            continue

        for jk in dev.get("jk", []):
            key = norm_name(jk["name"])
            hit = by_norm.get(key)
            if not hit:
                stats["jk_unmatched"] += 1
                unmatched.append(f"{name}: {jk['name']}")
                continue
            stats["jk_matched"] += 1
            cid, existing_dev = hit
            if existing_dev:
                continue
            await execute("""
                UPDATE complexes SET developer_id = $2, updated_at = now()
                WHERE id = $1 AND developer_id IS NULL
            """, cid, dev_id)
            stats["links_set"] += 1

    log.info("=== Итог: застройщиков новых=%d обновлено=%d · ЖК сопоставлено=%d "
             "не сопоставлено=%d · связей проставлено=%d ===",
             stats["new"], stats["updated"], stats["jk_matched"],
             stats["jk_unmatched"], stats["links_set"])
    if unmatched:
        log.info("Несопоставленные ЖК (%d):", len(unmatched))
        for u in unmatched:
            log.info("  - %s", u)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="ничего не писать в БД, только лог")
    parser.add_argument("--limit", type=int, default=None,
                        help="только первые N застройщиков каталога")
    parser.add_argument("--max-jk-pages", type=int, default=60,
                        help="макс. страниц ЖК на застройщика")
    args = parser.parse_args()

    devs = await fetch_developers(limit=args.limit, max_jk_pages=args.max_jk_pages)
    if not devs:
        log.error("Ничего не собрано — возможно, разметка homsters изменилась.")
        sys.exit(1)

    total_jk = sum(len(d.get("jk", [])) for d in devs)
    log.info("Собрано застройщиков: %d, ЖК Астаны: %d", len(devs), total_jk)
    for d in devs[:25]:
        log.info("  %-25s осн.=%-6s объектов=%-6s ЖК: %s",
                 (d.get("name") or d["slug"])[:25], d.get("founded_year") or "-",
                 d.get("projects_active") or "-",
                 ", ".join(j["name"] for j in d.get("jk", [])[:8]) or "-")

    if args.test:
        log.info("--test: в БД НЕ записано.")
        return

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await save_to_db(devs)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
