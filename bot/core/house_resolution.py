"""
House-resolution — задача 2026-08-13: когда ЖК стал "зонтиком" (у него
есть дома, complexes.parent_complex_id), объявления apartment_listings
всё ещё привязываются к нему ИМЕНЕМ (complex_name = зонтика generic-имя,
"ЖК Qaiyndy"), а не к конкретному дому ("Qaiyndy 3") — Крыша обычно не
пишет в объявлении номер корпуса/очереди явно в поле "ЖК", это надо
вытаскивать из адреса/текста/координат самого объявления.

Приоритет резолва (решение заказчика):
  1. Адрес — номер дома/участка (extract_house_token из адреса)
  2. Текстовый токен ("блок N"/"очередь N"/"- N") в title/description
  3. Гео ≤150м до координат дома

Неуверенность на любом шаге -> None (остаётся на зонтике, НЕ гадаем).
Если адрес указывает на НЕСКОЛЬКО домов сразу (коллизия участка — живой
случай UIA.DARYN, несколько домов на одном "уч. 6") — сужаем до этого
подмножества и добираем гео-тайбрейком (метод 'address_geo'), если и
это не разрешает — не резолвим.
"""
from __future__ import annotations

import re

from bot.core.entity_resolution import _haversine_m

GEO_MAX_M = 150.0


def _extract_house_number(addr: str | None) -> str | None:
    """Номер дома/участка из адреса — 'уч. N' (участок, частый паттерн у
    застройщиков-новостроек) или голое число в конце строки (может быть
    вида '14/2' — корпус/подъезд, или '20Б' — литера). Комментарии вида
    " — рядом с..." отбрасываем ДО поиска числа в хвосте — иначе не
    находим (число не в конце строки)."""
    if not addr:
        return None
    addr = addr.strip()
    m = re.search(r"уч\.?\s*(\d+)", addr, re.IGNORECASE)
    if m:
        return "уч" + m.group(1)
    head = re.split(r"\s+[—-]\s+", addr)[0].strip()
    m2 = re.search(r"(\d+(?:/\d+)?[а-яa-zА-ЯA-Z]?)\s*$", head)
    if m2:
        return m2.group(1).lower()
    return None


def _house_text_token(house_name: str, umbrella_name: str) -> str | None:
    """Отличительный суффикс имени дома относительно зонтика — 'Qaiyndy 3'
    минус 'ЖК Qaiyndy' -> '3'; 'Камал-2' минус 'Камал' -> '2'. Возвращает
    None, если после вычитания префикса ничего значимого не осталось
    (короче 1 символа) — слишком слабый токен, дал бы случайные совпадения."""
    h = (house_name or "").strip()
    u = (umbrella_name or "").strip()
    # без "ЖК"/кавычек — визуальный шум, не часть отличительного токена
    h_clean = re.sub(r'^(жк|кг)\s*["\']?', "", h, flags=re.IGNORECASE).strip().strip('"\'')
    u_clean = re.sub(r'^(жк|кг)\s*["\']?', "", u, flags=re.IGNORECASE).strip().strip('"\'')
    if h_clean.lower().startswith(u_clean.lower()) and u_clean:
        tail = h_clean[len(u_clean):].strip(" -–—.\"'")
        return tail if len(tail) >= 1 else None
    return None


def _text_token_match(token: str, title: str | None, description: str | None) -> bool:
    """Ищем токен НЕ голой цифрой где попало (title почти всегда содержит
    числа — площадь/этаж/комнатность, ложные совпадения гарантированы),
    а рядом со словом-маркером блока/очереди/корпуса, либо как суффикс
    "- N"/"№N", максимально похожий на то, как дома называются в этом
    проекте (см. живые примеры — 'Qaiyndy 3', 'Камал-2', 'Техникум-2')."""
    if not token:
        return False
    text = f"{title or ''} {description or ''}".lower()
    tok = re.escape(token.lower())
    patterns = [
        rf"(блок|очеред[ьи]|корпус|литер|дом)\s*№?\s*{tok}\b",
        rf"[-–—]\s*{tok}\b",
        rf"№\s*{tok}\b",
    ]
    return any(re.search(p, text) for p in patterns)


async def get_umbrella_children(umbrella_id: int) -> list[dict]:
    from bot.db.pg import fetch
    rows = await fetch(
        "SELECT id, name, address, lat, lon FROM complexes WHERE parent_complex_id = $1 "
        "AND COALESCE(is_garbage, FALSE) = FALSE ORDER BY id",
        umbrella_id)
    return [dict(r) for r in rows]


async def resolve_complex_geo_centroid(complex_id: int, complex_name: str) -> tuple[float, float] | None:
    """Координаты ЖК/дома = центроид координат его объявлений (в
    complexes своих координат нет). Вынесено из terminal_extras.py
    (Фаза B, п.5, задача 2026-08-14, docs/verdict_strategy.md) — этот
    запрос дублировался буквально (карточка ЖК + /admin/api/complex/
    {id}/location-score), обе точки теперь зовут одну функцию.

    resolved_house_id (задача "House-resolution в скоринге", 2026-08-13) —
    объявления дома под зонтиком могут по-прежнему называть его именем
    зонтика в тексте; resolve_house() уже привязал их к ЭТОМУ дому по
    адресу/токену/гео. Без OR resolved_house_id = $2 центроид дома либо
    молча считался бы по чужим (умбреловым) координатам, либо не
    находился бы вовсе. Возвращает None, если объявлений с координатами
    нет вообще (Unknown != average — не гадаем, не 404 с нулями)."""
    from bot.db.pg import fetchrow
    geo = await fetchrow("""
        SELECT AVG(lat) AS lat, AVG(lon) AS lon
        FROM apartment_listings
        WHERE (lower(trim(complex_name)) = lower(trim($1)) OR resolved_house_id = $2)
          AND lat IS NOT NULL
    """, complex_name, complex_id)
    if not geo or geo["lat"] is None:
        return None
    return float(geo["lat"]), float(geo["lon"])


async def resolve_house(
    *, umbrella_id: int, umbrella_name: str,
    listing_address: str | None, listing_title: str | None,
    listing_description: str | None, listing_lat: float | None, listing_lon: float | None,
    children: list[dict] | None = None,
) -> dict | None:
    """Пытается определить конкретный дом (ребёнка зонтика umbrella_id) для
    одного объявления. Возвращает {"house_id", "method", "detail"} или
    None (остаётся на зонтике — недостаточно уверенности)."""
    children = children if children is not None else await get_umbrella_children(umbrella_id)
    if not children:
        return None

    # ── 1. Адрес — номер дома/участка ────────────────────────────────────
    listing_num = _extract_house_number(listing_address)
    if listing_num:
        matched = [c for c in children if _extract_house_number(c.get("address")) == listing_num]
        if len(matched) == 1:
            return {"house_id": matched[0]["id"], "method": "address",
                    "detail": f"номер «{listing_num}» совпал с адресом дома"}
        if len(matched) > 1 and listing_lat and listing_lon:
            # Коллизия участка на несколько домов (живой случай UIA.DARYN,
            # несколько домов на одном "уч. 6") — сужаем гео-тайбрейком
            # ТОЛЬКО среди уже отфильтрованных по адресу кандидатов.
            best = _nearest_within(matched, listing_lat, listing_lon)
            if best:
                house, dist = best
                return {"house_id": house["id"], "method": "address_geo",
                        "detail": f"номер «{listing_num}» совпал с {len(matched)} домами, ближайший — {dist:.0f}м"}

    # ── 2. Текстовый токен (очередь/блок) в title/description ───────────
    for c in children:
        token = _house_text_token(c["name"], umbrella_name)
        if token and _text_token_match(token, listing_title, listing_description):
            return {"house_id": c["id"], "method": "token",
                    "detail": f"токен «{token}» найден в тексте объявления"}

    # ── 3. Гео ≤150м ──────────────────────────────────────────────────────
    if listing_lat and listing_lon:
        best = _nearest_within(children, listing_lat, listing_lon)
        if best:
            house, dist = best
            return {"house_id": house["id"], "method": "geo",
                    "detail": f"{dist:.0f}м до дома"}

    return None


def _nearest_within(candidates: list[dict], lat: float, lon: float) -> tuple[dict, float] | None:
    best = None
    for c in candidates:
        if not c.get("lat") or not c.get("lon"):
            continue
        d = _haversine_m(lat, lon, float(c["lat"]), float(c["lon"]))
        if d <= GEO_MAX_M and (best is None or d < best[1]):
            best = (c, d)
    return best


async def maybe_resolve_listing_house(listing_id: str, complex_name: str | None, *,
                                       address: str | None, title: str | None,
                                       description: str | None,
                                       lat: float | None, lon: float | None) -> dict | None:
    """Точка входа для парсера (задача "Применять и при первичном
    матчинге новых объявлений") — по имени комплекса объявления находит
    complexes-строку; если та зонтик (есть дети), пробует резолв и
    сохраняет результат. No-op (возвращает None), если имя не найдено
    или у него нет детей — обычный, самый частый случай, не зонтик."""
    if not complex_name:
        return None
    from bot.db.pg import fetchrow, execute
    cx = await fetchrow("SELECT id, name FROM complexes WHERE lower(trim(name)) = lower(trim($1))", complex_name)
    if not cx:
        return None
    children = await get_umbrella_children(cx["id"])
    if not children:
        return None
    result = await resolve_house(
        umbrella_id=cx["id"], umbrella_name=cx["name"],
        listing_address=address, listing_title=title, listing_description=description,
        listing_lat=lat, listing_lon=lon, children=children,
    )
    if result:
        await execute(
            "UPDATE apartment_listings SET resolved_house_id=$2, house_attribution=$3, house_attribution_detail=$4 WHERE id=$1",
            listing_id, result["house_id"], result["method"], result["detail"])
    return result
