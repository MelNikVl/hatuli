"""
Дедупликация объявлений в rental_listings и apartment_listings.

Критерии дубля (достаточно 2 из 3):
  1. Одинаковый адрес (нормализованный) + комнаты + площадь ±2м²
  2. Одинаковый UUID в URL фото (из photo_url/photo_urls)
  3. Одинаковая цена + адрес + этаж

Дубли не удаляются — помечаются флагом is_duplicate=True,
оставляем самое старое объявление (первичное).
"""
from __future__ import annotations
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _norm_address(addr: str) -> str:
    """Нормализовать адрес: нижний регистр, убрать р-н/ул./д. и лишние пробелы."""
    if not addr:
        return ""
    addr = addr.lower()
    addr = re.sub(r"\b(р-н|район|ул\.|улица|д\.|дом|кв\.)\b", "", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def _extract_photo_uuid(photo_url: str | None) -> str | None:
    """Извлечь UUID из URL фото Krisha."""
    if not photo_url:
        return None
    m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/", photo_url)
    return m.group(1) if m else None


def find_duplicates(listings: list[dict]) -> dict[str, str]:
    """
    Найти дубли в списке объявлений.
    Возвращает {duplicate_id: primary_id} — какое объявление является дублем какого.
    """
    duplicates: dict[str, str] = {}
    
    # Индексы для быстрого поиска
    addr_idx: dict[tuple, list[str]] = {}   # (norm_addr, rooms, area_bucket) → [ids]
    photo_idx: dict[str, str] = {}          # photo_uuid → first_id
    price_addr_idx: dict[tuple, str] = {}   # (norm_addr, price_bucket, floor) → first_id

    for lst in listings:
        lid = lst["id"]
        if lid in duplicates:
            continue

        addr = _norm_address(lst.get("address", ""))
        rooms = lst.get("rooms")
        area = lst.get("area") or 0
        price = lst.get("price") or 0
        floor = lst.get("floor")
        photo_url = lst.get("photo_url") or (lst.get("photo_urls") or "")
        if isinstance(photo_url, list):
            photo_url = photo_url[0] if photo_url else ""

        # Bucket площади: округляем до 5м²
        area_bucket = round(area / 5) * 5

        # 1. Проверка по фото UUID
        uuid = _extract_photo_uuid(photo_url)
        if uuid:
            if uuid in photo_idx:
                duplicates[lid] = photo_idx[uuid]
                continue
            photo_idx[uuid] = lid

        # 2. Проверка по адрес + комнаты + площадь
        if addr and rooms is not None:
            key = (addr, rooms, area_bucket)
            if key in addr_idx:
                # Проверяем что площадь реально близка (±2м²)
                for existing_id in addr_idx[key]:
                    existing = next((l for l in listings if l["id"] == existing_id), None)
                    if existing:
                        existing_area = existing.get("area") or 0
                        if abs(existing_area - area) <= 2:
                            duplicates[lid] = existing_id
                            break
                if lid not in duplicates:
                    addr_idx[key].append(lid)
            else:
                addr_idx[key] = [lid]

        # 3. Проверка по адрес + цена + этаж
        if addr and price and floor:
            price_bucket = round(price / 100_000) * 100_000
            key2 = (addr, price_bucket, floor)
            if key2 in price_addr_idx:
                duplicates[lid] = price_addr_idx[key2]
            else:
                price_addr_idx[key2] = lid

    return duplicates


async def deduplicate_rental_listings() -> int:
    """Найти и пометить дубли в rental_listings."""
    from bot.db.pg import fetch, execute

    # Добавить колонку если нет
    try:
        await execute("ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE")
        await execute("ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS duplicate_of TEXT")
    except Exception:
        pass

    rows = await fetch("""
        SELECT id, address, rooms, area, price, floor, photo_url
        FROM rental_listings
        WHERE is_duplicate IS NOT TRUE
        ORDER BY found_at ASC
    """)
    listings = [dict(r) for r in rows]
    
    duplicates = find_duplicates(listings)
    logger.info("Found %d duplicates in rental_listings", len(duplicates))

    for dup_id, primary_id in duplicates.items():
        await execute(
            "UPDATE rental_listings SET is_duplicate=TRUE, duplicate_of=$1 WHERE id=$2",
            primary_id, dup_id
        )

    return len(duplicates)


async def deduplicate_apartment_listings() -> int:
    """Найти и пометить дубли в apartment_listings."""
    from bot.db.pg import fetch, execute

    try:
        await execute("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE")
        await execute("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS duplicate_of TEXT")
    except Exception:
        pass

    rows = await fetch("""
        SELECT id, address, rooms, area, price, floor, photo_url
        FROM apartment_listings
        WHERE is_duplicate IS NOT TRUE
        ORDER BY first_seen ASC NULLS LAST
    """)
    listings = [dict(r) for r in rows]

    duplicates = find_duplicates(listings)
    logger.info("Found %d duplicates in apartment_listings", len(duplicates))

    for dup_id, primary_id in duplicates.items():
        await execute(
            "UPDATE apartment_listings SET is_duplicate=TRUE, duplicate_of=$1 WHERE id=$2",
            primary_id, dup_id
        )

    return len(duplicates)
