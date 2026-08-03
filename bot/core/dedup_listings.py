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
  4) координаты (в пределах ~60м) + комнаты + этаж + площадь ±3 м² + цена
     (округлена до 100к) — запасной вариант для случаев, когда правила
     2/3 не сработали из-за того, что разные агенты пишут адрес одного и
     того же дома по-разному: у улиц Астаны часто есть и имя, и индекс
     (напр. "Сыганак" он же "Е-10"), а район иногда указывают неверно —
     нормализация адреса это не ловит, а координаты с детальной страницы
     объявления — надёжнее текста.
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
    # Крыша часто добавляет к адресу " — <ориентир>" (напр. "Асемконыр 8"
    # vs "Асемконыр 8 — Коктал" на разных объявлениях того же дома) — этот
    # хвост различается между копиями одной и той же квартиры и раньше
    # ломал совпадение по адресу (rule 2/3 ниже), давая пропущенные дубли.
    addr = addr.split(" — ")[0]
    addr = re.sub(r"\b(р-н|район|ул\.|улица|д\.|дом|кв\.|пр\.|проспект|город)\b", "", addr)
    # "Куйши-Дина" vs "Куйши Дина" — тоже одна и та же улица, дефис/пробел
    # пишут по-разному в разных объявлениях.
    addr = addr.replace("-", " ")
    return re.sub(r"\s+", " ", addr).strip()


def _extract_photo_uuid(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/", url)
    return m.group(1) if m else None


def find_duplicates(listings: list[dict],
                    ) -> tuple[dict[str, str], dict[str, str]]:
    """Возвращает ({duplicate_id: primary_id}, {duplicate_id: match_rule}).
    listings — отсортированы по возрасту (старые первыми), primary по
    умолчанию — более старое.
    match_rule: 'photo' (тот же файл фото = точно тот же объект),
    'addr_area' (адрес+комнаты+площадь), 'addr_price' (адрес+цена+этаж)."""
    by_id = {str(l["id"]): l for l in listings}
    duplicates: dict[str, str] = {}
    match_rules: dict[str, str] = {}
    photo_idx: dict[str, str] = {}
    addr_idx: dict[tuple, list[str]] = {}
    price_addr_idx: dict[tuple, list[str]] = {}
    geo_idx: dict[tuple, list[str]] = {}
    # ~0.00055° по широте/долготе в Астане — примерно 55-60м, т.е. один дом
    # или соседние подъезды, не соседний квартал.
    GEO_EPS = 0.00055
    # Допуск по цене для правил 3/4 (адрес+этаж+цена / гео+этаж+площадь+цена):
    # та же квартира часто перевыставляется с небольшим торгом/переоценкой
    # (десятки-сотни тысяч тенге), а раньше цена входила В КЛЮЧ словаря
    # (округлена до 100к), из-за чего малейший сдвиг через границу бакета
    # ломал совпадение при полностью идентичных остальных признаках. 250к —
    # заметно меньше порога dup_needs_review (1 млн), т.е. не расширяет
    # число ложных склеек в разных по цене квартирах того же дома/этажа.
    PRICE_TOLERANCE = 250_000

    def is_owner(l: dict) -> bool:
        return l.get("is_owner") is True

    def _compatible(a_id: str, b_id: str) -> bool:
        """Тот же физический юнит: этаж, если известен у обоих, ДОЛЖЕН совпадать
        (иначе это гарантированно другая квартира), и площадь — в пределах ±3м²."""
        a, b = by_id.get(a_id), by_id.get(b_id)
        if not a or not b:
            return False
        af, bf = a.get("floor"), b.get("floor")
        if af is not None and bf is not None and af != bf:
            return False
        aa, ba = float(a.get("area") or 0), float(b.get("area") or 0)
        if aa > 0 and ba > 0 and abs(aa - ba) > 3:
            return False
        return True

    def mark(dup_id: str, primary_id: str, rule: str = ""):
        # Если дубль сам был primary для кого-то — переподвешиваем ИХ на нового
        # primary, но ТОЛЬКО если они реально совпадают и с новым primary тоже
        # (этаж/площадь) — иначе получится транзитивное склеивание случайных
        # квартир того же дома через цепочку переподвязок (было: владелец
        # "перехватывает" primary у чужого объявления, и с ним утаскивает ВСЕХ
        # прежних дублей того primary, включая квартиры с других этажей).
        # Несовместимых просто оставляем как есть (дублями старого primary),
        # чтобы они не терялись из is_duplicate, но и не показывались в чужой
        # группе на странице /admin/duplicates.
        for d, p in list(duplicates.items()):
            if p == dup_id and _compatible(d, primary_id):
                duplicates[d] = primary_id
        duplicates[dup_id] = primary_id
        if rule:
            match_rules[dup_id] = rule

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
        lat = lst.get("lat")
        lon = lst.get("lon")
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None

        matched_primary: str | None = None
        matched_rule = ""

        # 1. UUID фото
        if uuid and uuid in photo_idx and photo_idx[uuid] != lid:
            matched_primary = photo_idx[uuid]
            matched_rule = "photo"

        # 2. адрес + комнаты + площадь (НО не дубль, если этажи ИЗВЕСТНЫ и
        # РАЗНЫЕ — это явный признак разных квартир в одном доме/ЖК; адрес
        # у Крыши часто на уровне дома, без номера квартиры, поэтому без
        # этой проверки разные квартиры того же дома склеивались в один дубль)
        if matched_primary is None and addr and rooms is not None and area > 0:
            key = (addr, rooms, round(area / 5) * 5)
            for ex_id in addr_idx.get(key, []):
                ex = by_id.get(ex_id)
                if not ex or abs(float(ex.get("area") or 0) - area) > 3:
                    continue
                ex_floor = ex.get("floor")
                if floor is not None and ex_floor is not None and floor != ex_floor:
                    continue  # разные этажи — точно разные квартиры
                matched_primary = ex_id
                matched_rule = "addr_area"
                break

        # 3. адрес + цена + этаж + КОМНАТЫ (запасной). Раньше не проверяли
        # комнатность вообще — две РАЗНЫЕ квартиры на одном этаже одного
        # дома с похожей ценой (округлённой до 100к) склеивались в дубль
        # (видели на скрине: 9 "дублей" одной однушки, среди них квартира
        # с другой планировкой). Комнаты — самый дешёвый и надёжный
        # дополнительный сигнал "это точно другая квартира".
        if (matched_primary is None and addr and price > 0 and floor is not None
                and rooms is not None):
            # ИЩЕМ по адресу+этажу+комнатам (без цены в ключе) и проверяем
            # цену отдельно с допуском PRICE_TOLERANCE — раньше цена входила
            # в ключ словаря округлённой до 100к, и та же квартира с
            # торгом/переоценкой всего на 200к (напр. 23.2М -> 23.0М) уже не
            # совпадала по ключу, хотя адрес/этаж/комнаты идентичны.
            key2 = (addr, floor, rooms)
            for cand2 in price_addr_idx.get(key2, []):
                if cand2 == lid:
                    continue
                ex2 = by_id.get(cand2)
                if not ex2:
                    continue
                ex2_price = int(ex2.get("price") or 0)
                if ex2_price <= 0 or abs(ex2_price - price) > PRICE_TOLERANCE:
                    continue
                # Раньше это правило не проверяло площадь вообще — две РАЗНЫЕ
                # квартиры на одном этаже одного дома с похожей ценой и
                # комнатностью, но разной площадью, склеивались в дубль.
                # area > 0 у обеих — единственный случай, когда стоит это
                # правило применять (без площади у одной из сторон оставляем
                # как запасной вариант без проверки, как раньше).
                ex2_area = float(ex2.get("area") or 0)
                if area <= 0 or ex2_area <= 0 or abs(ex2_area - area) <= 3:
                    matched_primary = cand2
                    matched_rule = "addr_price"
                    break

        # 4. координаты + комнаты + этаж + площадь + цена (см. docstring —
        # ловит те же дубли, что и правила 2/3, но независимо от текста
        # адреса, который у разных агентов на один и тот же дом расходится).
        if (matched_primary is None and lat is not None and lon is not None
                and rooms is not None and floor is not None and price > 0):
            # ИЩЕМ по (комнаты, этаж) без цены в ключе — цена проверяется
            # отдельно с допуском PRICE_TOLERANCE. Раньше цена входила в
            # ключ (округлена до 100к), и тот же дубль на границе разных
            # названий улицы (см. docstring: "Сыганак" vs "Е-10") с небольшим
            # торгом по цене (напр. Е-429 14 за 23.2М и Айтматова 48 за
            # 23.0М — тот же дом, тот же этаж/площадь) не ловился, т.к. 23.2М
            # и 23.0М округляются в РАЗНЫЕ бакеты по 100к.
            for ex_id in geo_idx.get((rooms, floor), []):
                ex = by_id.get(ex_id)
                if not ex:
                    continue
                ex_lat, ex_lon = ex.get("lat"), ex.get("lon")
                if ex_lat is None or ex_lon is None:
                    continue
                if abs(float(ex_lat) - lat) > GEO_EPS or abs(float(ex_lon) - lon) > GEO_EPS:
                    continue
                if abs(float(ex.get("area") or 0) - area) > 3:
                    continue
                ex_price = int(ex.get("price") or 0)
                if ex_price <= 0 or abs(ex_price - price) > PRICE_TOLERANCE:
                    continue
                matched_primary = ex_id
                matched_rule = "geo"
                break

        if matched_primary:
            ex = by_id.get(matched_primary)
            # приоритет хозяина: текущее от хозяина, существующий primary — нет
            if cur_owner and ex is not None and not is_owner(ex):
                mark(matched_primary, lid, matched_rule)
                if uuid:
                    photo_idx[uuid] = lid
            else:
                mark(lid, matched_primary, matched_rule)
                continue  # дубль не индексируем

        # индексируем как потенциальный primary
        if uuid and uuid not in photo_idx:
            photo_idx[uuid] = lid
        if addr and rooms is not None and area > 0:
            addr_idx.setdefault((addr, rooms, round(area / 5) * 5), []).append(lid)
        if addr and price > 0 and floor is not None and rooms is not None:
            price_addr_idx.setdefault((addr, floor, rooms), []).append(lid)
        if lat is not None and lon is not None and rooms is not None and floor is not None and price > 0:
            geo_idx.setdefault((rooms, floor), []).append(lid)

    return duplicates, match_rules


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
    # когда именно объявление было помечено дублем — для статистики
    # «сколько дублей нашли и убрали с карты сегодня» на дашборде
    await execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS dup_marked_at TIMESTAMPTZ")
    # по какому правилу признан дублем: photo / addr_area / addr_price
    await execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS dup_match TEXT")
    # цена primary и дубля разошлась больше чем на 1 млн ₸ — подозрительно,
    # возможно это НЕ одна и та же квартира (см. duplicates.html — такие
    # группы подсвечиваются отдельно, "присмотреться подробнее").
    await execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS dup_needs_review BOOLEAN DEFAULT FALSE")

    have = await _table_columns(table)
    want = ["id", "url", "address", "rooms", "area", "price", "floor", "is_owner", "lat", "lon"]
    cols = [c for c in want if c in have]

    rows = await fetch(f"""
        SELECT {', '.join(cols)} FROM {table}
        WHERE COALESCE(is_duplicate, FALSE) = FALSE
        ORDER BY {order_col} ASC
    """)
    row_dicts = [dict(r) for r in rows]
    by_id = {str(r["id"]): r for r in row_dicts}
    duplicates, match_rules = find_duplicates(row_dicts)
    logger.info("%s: found %d duplicates", table, len(duplicates))

    PRICE_REVIEW_THRESHOLD = 1_000_000
    needs_review_cnt = 0
    for dup_id, primary_id in duplicates.items():
        dup_row, primary_row = by_id.get(dup_id), by_id.get(primary_id)
        needs_review = False
        if dup_row and primary_row and dup_row.get("price") and primary_row.get("price"):
            needs_review = abs(int(dup_row["price"]) - int(primary_row["price"])) > PRICE_REVIEW_THRESHOLD
        if needs_review:
            needs_review_cnt += 1
        await execute(
            f"UPDATE {table} SET is_duplicate=TRUE, duplicate_of=$1, "
            f"dup_marked_at=COALESCE(dup_marked_at, NOW()), "
            f"dup_match=COALESCE(dup_match, $3), dup_needs_review=$4 WHERE id=$2",
            primary_id, dup_id, match_rules.get(dup_id) or None, needs_review)
    # ВАЖНО: is_active НЕ трогаем — иначе затирался бы архив.

    await execute("""
        CREATE TABLE IF NOT EXISTS dedup_scan_log (
            id SERIAL PRIMARY KEY,
            table_name TEXT NOT NULL,
            scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            listings_scanned INT NOT NULL,
            duplicates_found INT NOT NULL,
            needs_review_found INT NOT NULL DEFAULT 0
        )
    """)
    await execute(
        "INSERT INTO dedup_scan_log (table_name, listings_scanned, duplicates_found, needs_review_found) "
        "VALUES ($1, $2, $3, $4)",
        table, len(row_dicts), len(duplicates), needs_review_cnt)
    return len(duplicates)


async def deduplicate_apartment_listings() -> int:
    return await _dedup_table("apartment_listings", "first_seen")


async def deduplicate_rental_listings() -> int:
    return await _dedup_table("rental_listings", "found_at")
