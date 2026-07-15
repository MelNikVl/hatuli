"""
Дедупликация объявлений с приоритетом «от хозяина».

Основано на предложении DeepSeek с исправлениями:
1. УБРАНО `SET is_active = NOT is_duplicate` — это воскрешало бы все
   архивные объявления каждый цикл (архив затирался бы). Дубли скрываются
   ТОЛЬКО флагом is_duplicate — все запросы (топ-10, карта, Sheets,
   аналитика) его уже фильтруют.
2. Поиск existing по id — через словарь, а не линейный скан списка
   (иначе O(n²) на 15к объявлений).
3. seller_type не используется (такой колонки нет) — только is_owner.
4. Для rental колонки определяются динамически (floor/is_owner там может
   не быть).

Логика совпадений (по убыванию надёжности):
  1) UUID первой фотографии (один и тот же файл = одно и то же жильё)
  2) адрес + комнаты + площадь ±3 м²
  3) адрес + цена (округлена до 100к) + этаж
Приоритет: если новое объявление от хозяина, а существующий primary —
риелтор, primary переназначается на хозяйское.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _norm_address(addr: str) -> str:
    if not addr:
        return ""
    addr = addr.lower()
    addr = re.sub(r"\b(р-н|район|ул\.|улица|д\.|дом|кв\.|пр\.|проспект|город)\b", "", addr)
    return re.sub(r"\s+", " ", addr).strip()


def _extract_photo_uuid(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/", url)
    return m.group(1) if m else None


def find_duplicates(listings: list[dict]) -> dict[str, str]:
    """Возвращает {duplicate_id: primary_id}. listings — отсортированы по
    возрасту (старые первыми), primary по умолчанию — более старое."""
    by_id = {str(l["id"]): l for l in listings}
    duplicates: dict[str, str] = {}
    photo_idx: dict[str, str] = {}
    addr_idx: dict[tuple, list[str]] = {}
    price_addr_idx: dict[tuple, str] = {}

    def is_owner(l: dict) -> bool:
        return l.get("is_owner") is True

    def mark(dup_id: str, primary_id: str):
        # если дубль сам был primary для кого-то — переподвесим тех на нового primary
        for d, p in list(duplicates.items()):
            if p == dup_id:
                duplicates[d] = primary_id
        duplicates[dup_id] = primary_id

    for lst in listings:
        lid = str(lst["id"])
        if lid in duplicates:
            continue

        addr = _norm_address(lst.get("address") or "")
        rooms = lst.get("rooms")
        area = float(lst.get("area") or 0)
        price = int(lst.get("price") or 0)
        floor = lst.get("floor")
        uuid = _extract_photo_uuid(lst.get("url"))
        cur_owner = is_owner(lst)

        matched_primary: str | None = None

        # 1. UUID фото
        if uuid and uuid in photo_idx and photo_idx[uuid] != lid:
            matched_primary = photo_idx[uuid]

        # 2. адрес + комнаты + площадь
        if matched_primary is None and addr and rooms is not None and area > 0:
            key = (addr, rooms, round(area / 5) * 5)
            for ex_id in addr_idx.get(key, []):
                ex = by_id.get(ex_id)
                if ex and abs(float(ex.get("area") or 0) - area) <= 3:
                    matched_primary = ex_id
                    break

        # 3. адрес + цена + этаж (запасной)
        if matched_primary is None and addr and price > 0 and floor is not None:
            key2 = (addr, round(price / 100_000) * 100_000, floor)
            if key2 in price_addr_idx and price_addr_idx[key2] != lid:
                matched_primary = price_addr_idx[key2]

        if matched_primary:
            ex = by_id.get(matched_primary)
            # приоритет хозяина: текущее от хозяина, существующий primary — нет
            if cur_owner and ex is not None and not is_owner(ex):
                mark(matched_primary, lid)
                if uuid:
                    photo_idx[uuid] = lid
            else:
                mark(lid, matched_primary)
                continue  # дубль не индексируем

        # индексируем как потенциальный primary
        if uuid and uuid not in photo_idx:
            photo_idx[uuid] = lid
        if addr and rooms is not None and area > 0:
            addr_idx.setdefault((addr, rooms, round(area / 5) * 5), []).append(lid)
        if addr and price > 0 and floor is not None:
            price_addr_idx.setdefault(
                (addr, round(price / 100_000) * 100_000, floor), lid)

    return duplicates


async def _table_columns(table: str) -> set[str]:
    from bot.db.pg import fetch
    rows = await fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
        table)
    return {r["column_name"] for r in rows}


async def _dedup_table(table: str, order_col: str) -> int:
    from bot.db.pg import fetch, execute

    await execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE")
    await execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS duplicate_of TEXT")

    have = await _table_columns(table)
    want = ["id", "url", "address", "rooms", "area", "price", "floor", "is_owner"]
    cols = [c for c in want if c in have]

    rows = await fetch(f"""
        SELECT {', '.join(cols)} FROM {table}
        WHERE COALESCE(is_duplicate, FALSE) = FALSE
        ORDER BY {order_col} ASC
    """)
    duplicates = find_duplicates([dict(r) for r in rows])
    logger.info("%s: found %d duplicates", table, len(duplicates))

    for dup_id, primary_id in duplicates.items():
        await execute(
            f"UPDATE {table} SET is_duplicate=TRUE, duplicate_of=$1 WHERE id=$2",
            primary_id, dup_id)
    # ВАЖНО: is_active НЕ трогаем — иначе затирался бы архив.
    return len(duplicates)


async def deduplicate_apartment_listings() -> int:
    return await _dedup_table("apartment_listings", "first_seen")


async def deduplicate_rental_listings() -> int:
    return await _dedup_table("rental_listings", "found_at")
