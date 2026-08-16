"""Property Identity, слой связывания (задача 2026-08-16, "P1 — Property
Identity") — стабильный property_id для ФИЗИЧЕСКОЙ квартиры, к которому
привязываются все listing_id, под которыми она когда-либо публиковалась
на Krisha. Сейчас "квартира" в проекте = listing_id — одна физическая
квартира, перевыставленная 3-5 раз, живёт как 3-5 никак не связанных
между собой строк: нельзя отличить relist от нового объекта, посчитать
честный true DOM (время экспозиции РЕАЛЬНОГО объекта, не отдельного
объявления) или собрать price timeline на уровне квартиры, а не строки.

Метод — ДЕТЕРМИНИРОВАННЫЙ hash + tolerance, НЕ ML-матчинг (сознательное
решение задачи, см. её текст) — это первая, самая дешёвая и самая
проверяемая версия; более умный fuzzy/ML-матчинг — кандидат на будущее,
не эта задача.

Три уровня попытки связать listing_id с property_id:
  1. Уже связан (property_listings.listing_id) — короткое замыкание,
     возвращаем существующий property_id без пересчёта (идемпотентность
     backfill'а: повторный прогон не переоценивает уже принятые решения,
     даже если исходные address/floor/area с тех пор изменились в
     apartment_listings).
  2. Точный hash (address_hash = SHA1(норм_адрес|этаж|площадь)) уже есть
     в properties — та же квартира, method='auto', confidence=1.0.
  3. Fuzzy: тот же complex_id + тот же floor + area_sqm в пределах ±1м²
     — вероятно та же квартира, адрес просто записан по-другому
     (опечатка/сокращение) — method='fuzzy', confidence < 1.0. Только
     когда complex_id известен (без ЖК-якоря сигнал слишком слабый).
  4. Ничего не нашли — НОВАЯ квартира, INSERT в properties,
     method='auto', confidence=1.0 (первое появление — не с чем
     конфликтовать, неопределённости нет).

Адрес/этаж/площадь неизвестны (хотя бы один) -> НЕ линкуем вовсе
(method='skipped') — Unknown ≠ average (verdict_strategy.md §3.1): без
всех трёх компонентов хэш ненадёжен, гадать не будем.
"""
from __future__ import annotations

import hashlib
import re

# Шумовые токены при нормализации адреса — тот же принцип, что
# bot/core/entity_resolution.py::_ADDR_NOISE (та функция строит МНОЖЕСТВО
# токенов для fuzzy-пересечения ЖК/адресов, эта — канонической СТРОКОЙ
# для хэша: разные задачи, поэтому не переиспользуем ту же функцию
# напрямую, но список шумных слов один и тот же класс).
_ADDRESS_NOISE = [
    "г. астана", "г.астана", "астана,", "астана ", "район ", "р-н ", "р. ",
    "жилой массив", "ж/м", "мкр.", "мкр ", "проспект", "пр.", "улица", "ул.",
    "переулок", "пер.", "уч.", "участок",
]


def normalize_address(address: str | None) -> str:
    """Адрес -> каноническая строка для хэша: нижний регистр, вырезаны
    административные шумовые слова (город/район/тип улицы — те же
    источники ложных различий, что и в entity_resolution.py), схлопнуты
    пробелы/пунктуация. Пустая строка для None/пустого адреса (вызывающий
    код — compute_address_hash — сам решает, что делать с пустым
    результатом, эта функция не гадает)."""
    s = (address or "").strip().lower()
    for token in _ADDRESS_NOISE:
        s = s.replace(token, " ")
    s = re.sub(r"[^\w\s/]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compute_address_hash(address: str | None, floor: int | None, area: float | None) -> str | None:
    """SHA1(норм_адрес|этаж|площадь_с_точностью_0.1) — единственное место
    в проекте, где формула хэша определена (миграция 083 на неё только
    ссылается, не дублирует). None, если адрес после нормализации пуст,
    либо floor/area неизвестны — все три компонента обязательны, иначе
    хэш ненадёжен (совпадёт у заведомо разных квартир с неизвестными
    полями) и мы не линкуем вовсе, а не создаём мусорную запись."""
    norm_addr = normalize_address(address)
    if not norm_addr or floor is None or area is None:
        return None
    key = f"{norm_addr}|{floor}|{area:.1f}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


async def _resolve_complex_id(complex_name: str | None) -> int | None:
    """complex_name (свободный текст на apartment_listings) -> complexes.id
    — тот же lower(trim(name))-лукап, что уже используется в
    bot/core/listing_detail.py (единый источник правды для этого
    сопоставления, не вторая параллельная реализация)."""
    if not complex_name:
        return None
    from bot.db.pg import fetchval
    return await fetchval(
        "SELECT id FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
        complex_name,
    )


# Допуск fuzzy-совпадения по площади (задача: "±1м²").
_FUZZY_AREA_TOLERANCE = 1.0


class DryRunCache:
    """Хранилище 'бы созданных' properties за ОДИН dry-run прогон
    (scripts/backfill_property_ids.py --dry-run) — целиком в памяти, БД
    не трогаем. Нужна не только для точного хэша (шаг 2): fuzzy-поиск
    (шаг 3) в dry-run без неё бил бы исключительно по РЕАЛЬНОЙ таблице
    properties, которая за весь dry-run прогон НИКОГДА не меняется (по
    определению dry-run) — 0 fuzzy-совпадений гарантированно, даже если
    в реальном прогоне их были бы тысячи. Найдено эмпирически на живых
    данных при подготовке этой задачи: 0 fuzzy из 50266 listing до
    этого класса, при том что complex_id резолвится у 1878/2017 разных
    имён ЖК — сигнала было достаточно, просто dry-run не мог его увидеть."""

    def __init__(self) -> None:
        self._hashes: set[str] = set()
        self._by_complex_floor: dict[tuple[int, int], list[float]] = {}

    def has_hash(self, address_hash: str) -> bool:
        return address_hash in self._hashes

    def add(self, address_hash: str, complex_id: int | None, floor: int | None, area: float | None) -> None:
        self._hashes.add(address_hash)
        if complex_id is not None and floor is not None and area is not None:
            self._by_complex_floor.setdefault((complex_id, floor), []).append(area)

    def find_fuzzy_area(self, complex_id: int, floor: int, area: float, tolerance: float) -> float | None:
        """Площадь ближайшего кандидата среди 'бы созданных' в этом
        complex_id+floor, в пределах tolerance — None, если нет ни
        одного (в т.ч. если для этой пары (complex_id, floor) ещё ничего
        не 'создавалось')."""
        best, best_diff = None, None
        for a in self._by_complex_floor.get((complex_id, floor), []):
            diff = abs(a - area)
            if diff <= tolerance and (best_diff is None or diff < best_diff):
                best, best_diff = a, diff
        return best


async def _find_fuzzy_candidate(complex_id: int, floor: int, area: float) -> dict | None:
    """Ближайшая по площади property в том же ЖК на том же этаже, в
    пределах _FUZZY_AREA_TOLERANCE — None, если complex_id неизвестен
    (без ЖК-якоря пара "этаж+похожая площадь" сама по себе слишком
    слабый сигнал — на одном этаже одного дома почти всегда несколько
    квартир схожей площади) или ничего не нашлось."""
    from bot.db.pg import fetchrow
    # $3::real — без явного каста asyncpg не может вывести тип параметра
    # в ORDER BY ABS(area_sqm - $3) (AmbiguousFunctionError: "operator is
    # not unique" — нет столбца рядом, задающего тип напрямую в ЭТОМ
    # выражении, а BETWEEN выше это не подсказывает планировщику раньше).
    return await fetchrow(
        """
        SELECT property_id, area_sqm FROM properties
        WHERE complex_id = $1 AND floor = $2
          AND area_sqm BETWEEN $3::real - $4::real AND $3::real + $4::real
        ORDER BY ABS(area_sqm - $3::real) ASC
        LIMIT 1
        """,
        complex_id, floor, area, _FUZZY_AREA_TOLERANCE,
    )


def _fuzzy_confidence(area: float, candidate_area: float) -> float:
    """confidence < 1.0, линейно убывает с разницей площадей внутри
    допуска (0 расхождения -> 0.9, полный допуск ±1м² -> 0.6) — сама
    формула не претендует на калибровку, просто гарантирует строгое
    '< 1.0' из задачи и монотонность (ближе по площади -> увереннее)."""
    diff = abs(area - candidate_area)
    return round(max(0.6, 0.9 - diff * 0.3), 2)


async def link_listing_to_property(listing_row: dict, dry_run: bool = False,
                                    dry_run_cache: "DryRunCache | None" = None) -> dict:
    """Основная точка входа. listing_row — строка apartment_listings (или
    dict с теми же ключами): id, address, floor, area, rooms, complex_name.

    Возвращает {"property_id": int | None, "method": str, "confidence":
    float | None, "created": bool}. method: 'already_linked' (шаг 1
    докстринга модуля) | 'auto' (точный хэш или новая квартира) |
    'fuzzy' | 'skipped' (адрес/этаж/площадь неизвестны — property_id=None).

    Пишет в property_listings (INSERT ... ON CONFLICT (listing_id) DO
    NOTHING — идемпотентно: параллельный/повторный вызов на тот же
    listing_id не перезаписывает уже принятое решение).

    dry_run=True (scripts/backfill_property_ids.py --dry-run) — НИ ОДНОЙ
    записи в БД (ни UPDATE last_seen_at, ни INSERT в properties/
    property_listings), только определяет, ЧТО было бы сделано. Для
    'была бы создана новая квартира' в dry-run нет реального property_id
    (строка не вставлена) — возвращается None, created=True, вызывающий
    (backfill) считает это по created, не по property_id.

    dry_run_cache (DryRunCache) — "созданные" за ЭТОТ dry-run properties,
    целиком в памяти (передаётся и мутируется вызывающим циклом
    backfill'а). Без него два listing_id ОДНОЙ и той же ещё не виденной
    квартиры в одном dry-run прогоне оба посчитались бы 'created'
    (реальной вставки нет, второй не находит первую) — именно relist'ы
    новой квартиры это тот самый случай, который вся задача призвана
    ловить, поэтому dry-run обязан считать его честно. Покрывает и точный
    hash-путь (шаг 2/4), и fuzzy (шаг 3, см. докстринг класса DryRunCache
    — без него fuzzy в dry-run всегда 0, реальная properties за dry-run
    не меняется никогда, проверено на живых данных).

    first_seen_at/last_seen_at на properties — НЕ now() в момент запуска
    линковщика (backfill почти всегда идёт по ИСТОРИЧЕСКИМ данным, "когда
    я это обработал" было бы бессмысленно для true DOM, задача, пункт 6):
    first_seen_at = MIN(apartment_listings.first_seen) по всем listing_id
    этой квартиры, last_seen_at = MAX(archived_at, last_seen) — самая
    поздняя реально известная точка (архивная дата, если объявление уже
    снято, иначе последнее "видели живым"). GREATEST/LEAST ниже
    накапливают эти границы по мере линковки новых listing_id к уже
    существующей квартире, независимо от порядка обработки backfill'ом."""
    from bot.db.pg import execute, fetchrow, fetchval

    listing_id = listing_row["id"]

    already = await fetchval(
        "SELECT property_id FROM property_listings WHERE listing_id = $1", listing_id)
    if already is not None:
        return {"property_id": already, "method": "already_linked", "confidence": None, "created": False}

    address = listing_row.get("address")
    floor = listing_row.get("floor")
    area = listing_row.get("area")
    rooms = listing_row.get("rooms")
    complex_name = listing_row.get("complex_name")
    listing_first_seen = listing_row.get("first_seen")
    listing_evidence_at = listing_row.get("archived_at") or listing_row.get("last_seen")

    address_hash = compute_address_hash(address, floor, area)
    if address_hash is None:
        return {"property_id": None, "method": "skipped", "confidence": None, "created": False}

    complex_id = await _resolve_complex_id(complex_name)

    # Шаг 2: точный хэш — либо реальная строка в properties, либо (только
    # dry-run) хэш, который УЖЕ "создан" бы этим же прогоном раньше
    # (см. dry_run_cache в докстринге).
    exact = await fetchrow("SELECT property_id FROM properties WHERE address_hash = $1", address_hash)
    if exact is None and dry_run and dry_run_cache is not None and dry_run_cache.has_hash(address_hash):
        return {"property_id": None, "method": "auto", "confidence": 1.0, "created": False}
    if exact is not None:
        if not dry_run:
            await execute(
                "UPDATE properties SET "
                "  first_seen_at = LEAST(first_seen_at, COALESCE($2, first_seen_at)), "
                "  last_seen_at = GREATEST(last_seen_at, COALESCE($3, last_seen_at)) "
                "WHERE property_id = $1",
                exact["property_id"], listing_first_seen, listing_evidence_at,
            )
            await execute(
                "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                "VALUES ($1, $2, 'auto', 1.0) ON CONFLICT (listing_id) DO NOTHING",
                exact["property_id"], listing_id,
            )
        return {"property_id": exact["property_id"], "method": "auto", "confidence": 1.0, "created": False}

    # Шаг 3: fuzzy (только при известном complex_id) — реальная БД
    # (candidate) плюс, в dry-run, ещё и dry_run_cache (см. класс
    # DryRunCache про то, почему без него dry-run никогда не находит
    # fuzzy-совпадений вообще).
    if complex_id is not None and floor is not None and area is not None:
        candidate = await _find_fuzzy_candidate(complex_id, floor, area)
        cache_only = False
        if candidate is None and dry_run and dry_run_cache is not None:
            cache_area = dry_run_cache.find_fuzzy_area(complex_id, floor, area, _FUZZY_AREA_TOLERANCE)
            if cache_area is not None:
                candidate = {"property_id": None, "area_sqm": cache_area}
                cache_only = True
        if candidate is not None:
            confidence = _fuzzy_confidence(area, candidate["area_sqm"])
            if not dry_run:
                await execute(
                    "UPDATE properties SET "
                    "  first_seen_at = LEAST(first_seen_at, COALESCE($2, first_seen_at)), "
                    "  last_seen_at = GREATEST(last_seen_at, COALESCE($3, last_seen_at)) "
                    "WHERE property_id = $1",
                    candidate["property_id"], listing_first_seen, listing_evidence_at,
                )
                await execute(
                    "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                    "VALUES ($1, $2, 'fuzzy', $3) ON CONFLICT (listing_id) DO NOTHING",
                    candidate["property_id"], listing_id, confidence,
                )
            return {"property_id": None if cache_only else candidate["property_id"],
                    "method": "fuzzy", "confidence": confidence, "created": False}

    # Шаг 4: новая квартира.
    if dry_run:
        if dry_run_cache is not None:
            dry_run_cache.add(address_hash, complex_id, floor, area)
        return {"property_id": None, "method": "auto", "confidence": 1.0, "created": True}

    # ON CONFLICT (address_hash) — гонка с другим прогоном/листингом на
    # тот же хэш между шагом 2 и этой вставкой не создаёт дубль (DO
    # UPDATE вместо DO NOTHING — нужен property_id результата независимо
    # от того, эта ли вставка выиграла гонку; та же LEAST/GREATEST-логика
    # на случай, если конкурент уже вставил строку с другими датами).
    # COALESCE(..., now()) в INSERT — если у listing вообще нет
    # first_seen/last_seen (в теории невозможно, DEFAULT now() на самой
    # колонке, но не полагаемся на это молча).
    new_id = await fetchval(
        """
        INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms, first_seen_at, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()), COALESCE($7, $6, now()))
        ON CONFLICT (address_hash) DO UPDATE SET
          first_seen_at = LEAST(properties.first_seen_at, COALESCE($6, properties.first_seen_at)),
          last_seen_at = GREATEST(properties.last_seen_at, COALESCE($7, properties.last_seen_at))
        RETURNING property_id
        """,
        complex_id, address_hash, floor, area, rooms, listing_first_seen, listing_evidence_at,
    )
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1, $2, 'auto', 1.0) ON CONFLICT (listing_id) DO NOTHING",
        new_id, listing_id,
    )
    return {"property_id": new_id, "method": "auto", "confidence": 1.0, "created": True}
