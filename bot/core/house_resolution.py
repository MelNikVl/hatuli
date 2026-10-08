"""
House-resolution — задача 2026-08-13: когда ЖК стал "зонтиком" (у него
есть дома, complexes.parent_complex_id), объявления apartment_listings
всё ещё привязываются к нему ИМЕНЕМ (complex_name = зонтика generic-имя,
"ЖК Qaiyndy"), а не к конкретному дому ("Qaiyndy 3") — Крыша обычно не
пишет в объявлении номер корпуса/очереди явно в поле "ЖК", это надо
вытаскивать из адреса/текста/координат самого объявления.

Приоритет резолва (решение заказчика):
  1. Адрес — улица + номер дома; для участка — номер и доступная улица
  2. Текстовый токен ("блок N"/"очередь N"/"- N") в title/description
  3. Гео ≤150м до координат дома, без близкого конкурирующего дома

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
GEO_MARGIN_M = 15.0


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


def _address_street_key(addr: str | None) -> str | None:
    """Консервативный ключ улицы без номера дома и административных префиксов.

    Номер сам по себе не идентифицирует адрес: «Анет баба, 4» и
    «Туркестан, 4» — разные дома. Переименования улиц/транслит здесь
    не угадываем; если ключ получить нельзя, остаются текст и гео.
    """
    if not addr:
        return None
    head = re.split(r"\s+[—-]\s+", addr.strip())[0].strip()
    plot = re.search(r"уч\.?\s*\d+", head, re.IGNORECASE)
    if plot:
        head = head[:plot.start()].strip(" ,")
    else:
        number = re.search(r"\d+(?:/\d+)?[а-яa-zА-ЯA-Z]?\s*$", head)
        if number:
            head = head[:number.start()].strip(" ,")
    markers = list(re.finditer(
        r"(?<!\w)(?:улица|ул\.|проспект|пр\.|переулок|пер\.)\s*", head,
        re.IGNORECASE))
    if markers:
        street = head[markers[-1].end():]
    else:
        parts = [part.strip() for part in head.split(",") if part.strip()]
        street = parts[-1] if parts else ""
    street = re.sub(r"[\W_]+", " ", street.casefold().replace("ё", "е")).strip()
    if not street or street in {"астана", "г астана", "рк"}:
        return None
    if re.search(r"\b(?:район|р н|р он|р)\b", street):
        return None
    return street


def _same_house_address(listing_address: str | None, child_address: str | None) -> bool:
    number = _extract_house_number(listing_address)
    if not number or number != _extract_house_number(child_address):
        return False
    listing_street = _address_street_key(listing_address)
    child_street = _address_street_key(child_address)
    if listing_street and child_street:
        return listing_street == child_street
    # Участок часто приходит без улицы. Сохраняем его как кандидата;
    # коллизии нескольких домов разрешаются только внутри этой группы.
    return number.startswith("уч")


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
    """Observed listing centroid under the same canonical membership as the page.

    An umbrella includes its children; a child includes only its own listings.
    Display names cannot override a bound ID. This remains an observed centroid,
    not a verified building location (outlier filtering is a separate step).
    complex_name is retained for compatibility with existing callers.
    """
    from bot.db.pg import fetchrow
    from bot.core.complex_membership import listing_complex_match_sql
    canonical = await fetchrow("SELECT COALESCE(canonical_id, id) AS id FROM complexes WHERE id = $1", complex_id)
    if not canonical:
        return None
    membership = listing_complex_match_sql(include_children=True)
    geo = await fetchrow(f"""
        SELECT AVG(lat) AS lat, AVG(lon) AS lon
        FROM apartment_listings
        WHERE {membership}
          AND lat IS NOT NULL AND lon IS NOT NULL
          AND COALESCE(is_duplicate, FALSE) = FALSE
    """, canonical['id'])
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

    # ── 1. Адрес — улица + номер дома/участка ────────────────────────────
    candidates = children
    listing_num = _extract_house_number(listing_address)
    if listing_num:
        matched = [c for c in children if _same_house_address(listing_address, c.get("address"))]
        if len(matched) == 1:
            return {"house_id": matched[0]["id"], "method": "address",
                    "detail": f"адрес дома совпал (номер «{listing_num}»)"}
        if len(matched) > 1:
            candidates = matched
            # Коллизия участка на несколько домов (живой случай UIA.DARYN,
            # несколько домов на одном "уч. 6") — сужаем гео-тайбрейком
            # ТОЛЬКО среди уже отфильтрованных по адресу кандидатов.
            best = (_nearest_within(matched, listing_lat, listing_lon)
                    if listing_lat is not None and listing_lon is not None else None)
            if best:
                house, dist = best
                return {"house_id": house["id"], "method": "address_geo",
                        "detail": f"номер «{listing_num}» совпал с {len(matched)} домами, ближайший — {dist:.0f}м"}

    # ── 2. Текстовый токен (очередь/блок) в title/description ───────────
    token_matches = []
    for c in candidates:
        token = _house_text_token(c["name"], umbrella_name)
        if token and _text_token_match(token, listing_title, listing_description):
            token_matches.append((c, token))
    if len(token_matches) == 1:
        house, token = token_matches[0]
        return {"house_id": house["id"], "method": "token",
                "detail": f"токен «{token}» найден в тексте объявления"}
    if len(token_matches) > 1:
        return None

    # ── 3. Гео ≤150м и без близкого конкурента ──────────────────────────
    if listing_lat is not None and listing_lon is not None:
        best = _nearest_within(candidates, listing_lat, listing_lon)
        if best:
            house, dist = best
            return {"house_id": house["id"], "method": "geo",
                    "detail": f"{dist:.0f}м до дома"}

    return None


def _nearest_within(candidates: list[dict], lat: float, lon: float) -> tuple[dict, float] | None:
    distances = []
    for c in candidates:
        if c.get("lat") is None or c.get("lon") is None:
            continue
        d = _haversine_m(lat, lon, float(c["lat"]), float(c["lon"]))
        distances.append((c, d))
    distances.sort(key=lambda item: item[1])
    if not distances or distances[0][1] > GEO_MAX_M:
        return None
    # Второй дом учитываем и за границей радиуса: 149м против 151м
    # не превращаются в уверенный выбор из-за порога в 150м.
    if len(distances) > 1 and distances[1][1] - distances[0][1] < GEO_MARGIN_M:
        return None
    return distances[0]


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
