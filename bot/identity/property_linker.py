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

## match_mode (задача 2026-08-16, "безопасный deterministic exact-only
property linker" — прямое следствие scripts/audit_property_linker_fuzzy.py:
на реальных данных текущее fuzzy-правило complex+floor+area±1м² дало
76.9% high-risk совпадений, 94.4% пар были одновременно активны на
рынке (сигнал ДВУХ разных квартир, не одного relist), и КРИТИЧЕСКИЙ
дефект — 6.9-7.6% итоговых assignments зависели от порядка обработки
listing'ов. Полный отчёт см. в PR "audit(property-linker): read-only
fuzzy match quality audit".)

ПРИНЦИП (дан явно в задаче, это не моя эвристика): false positive merge
хуже false negative duplicate. Склеить две разные квартиры в один
property_id — испортить true DOM/price timeline/relist_count молча и
навсегда (нет способа автоматически расклеить задним числом, кто был
кем). Не связать relist (оставить его отдельной property) — это
дешевле: аналитика недосчитывает несколько relist'ов, но НИЧЕГО не лжёт.

## Эволюция режимов (2026-08-16, три задачи подряд)

  match_mode="candidate_only" (ТЕКУЩИЙ ДЕФОЛТ, задача "безопасная
    инфраструктура кандидатов" — прямое следствие scripts/audit_
    property_match_signals.py: даже exact_only даёт 4.3% доказанных
    rejected-пар, значит "безопасный" exact-only тоже недостаточно
    безопасен для АВТОМАТИЧЕСКОГО hard-link). НИКАКОЙ сигнал (exact
    hash, fuzzy, bot/core/dedup_listings.py's is_duplicate/duplicate_of)
    не создаёт hard link САМ ПО СЕБЕ. Вместо этого:
      - listing → ВСЕГДА своя provisional property (deterministic
        bootstrap, НЕ поиск существующей — см. docs/property_identity_
        v2_architecture_audit.md §7: "не ставить match confidence=1.0
        только за одинаковый address_hash" — здесь bootstrap-confidence
        1.0 означает СОВСЕМ ДРУГОЕ: "этот listing точно равен
        только что созданной ИЗ НЕГО property", тавтология, не
        совпадение с чем-то ДРУГИМ);
      - все три сигнала (exact_hash/fuzzy/dedup_listings) генерируют
        property_match_candidates строки (не пишут property_listings) —
        задача "Property Identity v2" уже показала, что даже exact_hash
        не гарантирован, значит НИ ОДИН сигнал не имеет права молча
        слить две property.
    См. классы/функции ниже: BootstrapIndex, classify_relationship,
    _conflict_reasons, _generate_candidates.
  match_mode="exact_only" (LEGACY с этой задачи — был дефолтом ДО неё,
    остаётся для исследования/сравнения, БОЛЬШЕ НЕ прод-дефолт нигде).
    Линкует ТОЛЬКО по точному address_hash, fuzzy — информационно (см.
    докстринг ниже, поведение НЕ изменилось).
  match_mode="fuzzy" (LEGACY, НЕБЕЗОПАСНЫЙ — только явный opt-in,
    НИКОГДА не дефолт) — старое greedy-поведение, order-dependent (см.
    аудит выше). Оставлен для исследования/сравнения, не для прод-записи.

### exact_only (сохранено как есть — детали режима)

  match_mode="exact_only" — линкуем ТОЛЬКО по точному address_hash.
    Fuzzy-кандидат (тот же complex+floor+area в допуске) по-прежнему
    ВЫЧИСЛЯЕТСЯ и возвращается в result["fuzzy_candidate"] (задача:
    "fuzzy-кандидат можно залогировать... но не должен мешать созданию
    отдельного property") — для будущей ручной проверки/property_match_
    candidates, НО НИКОГДА не используется, чтобы связать listing с
    существующей property. Если exact hash не нашёлся — ВСЕГДА создаём
    НОВУЮ property (даже если fuzzy-кандидат есть) — "никаких greedy
    fuzzy assignments". skipped остаётся ТОЛЬКО за настоящей нехваткой
    данных (адрес/этаж/площадь), см. ниже.

Уровни попытки связать listing_id с property_id для exact_only/fuzzy
(address_hash — см. ТАКЖЕ scripts/audit_address_hash_exact.py: exact
hash НЕ содержит apartment_number/complex_id/rooms — НЕ гарантированно
идентифицирует физическую квартиру САМ ПО СЕБЕ, особенно в
многоподъездных ЖК с повторяющейся планировкой; exact-only СУЩЕСТВЕННО
снижает риск ложного объединения относительно fuzzy, но не обнуляет
его полностью, см. §4.3% rejected в audit_property_match_signals.py):
  1. Уже связан (property_listings.listing_id) — короткое замыкание,
     возвращаем существующий property_id без пересчёта (идемпотентность
     backfill'а: повторный прогон не переоценивает уже принятые решения,
     даже если исходные address/floor/area с тех пор изменились в
     apartment_listings).
  2. Точный hash (address_hash = SHA1(норм_адрес|этаж|площадь)) уже есть
     в properties — та же квартира, method='auto', confidence=1.0.
  3. Fuzzy-кандидат вычисляется ВСЕГДА (для result["fuzzy_candidate"]),
     но СВЯЗЫВАЕТ только при match_mode="fuzzy". При "exact_only" —
     чисто информационный, идёт в лог/будущую candidates-таблицу.
  4. Ничего не связали — НОВАЯ квартира, INSERT в properties,
     method='auto', confidence=1.0 (первое появление — не с чем
     конфликтовать, неопределённости нет).

Адрес/этаж/площадь неизвестны (хотя бы один) -> НЕ линкуем вовсе
(method='skipped') — Unknown ≠ average (verdict_strategy.md §3.1): без
всех трёх компонентов хэш ненадёжен, гадать не будем. ЭТО единственная
причина skipped — недостаток fuzzy-кандидата НЕ является причиной
skipped ни в одном режиме, ВКЛЮЧАЯ candidate_only.
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


def _skip_reason(address: str | None, floor: int | None, area: float | None) -> str:
    """Задача 2026-08-16 ("безопасный exact-only property linker"), тест
    "недостаточно данных -> skipped с причиной" — какое ИМЕННО поле не
    хватило (адрес после нормализации пуст / floor None / area None),
    не просто общее 'skipped'."""
    missing = []
    if not normalize_address(address):
        missing.append("address")
    if floor is None:
        missing.append("floor")
    if area is None:
        missing.append("area")
    return "missing: " + ", ".join(missing) if missing else "unknown"


def _fuzzy_confidence(area: float, candidate_area: float) -> float:
    """confidence < 1.0, линейно убывает с разницей площадей внутри
    допуска (0 расхождения -> 0.9, полный допуск ±1м² -> 0.6) — сама
    формула не претендует на калибровку, просто гарантирует строгое
    '< 1.0' из задачи и монотонность (ближе по площади -> увереннее)."""
    diff = abs(area - candidate_area)
    return round(max(0.6, 0.9 - diff * 0.3), 2)


# ── candidate_only: bootstrap + candidate generation (задача 2026-08-16,
# "безопасная инфраструктура кандидатов" — миграция 086) ────────────────

_MATCHER_VERSION = "candidate_only_v1"
_PRICE_SEVERE_DIFF_PCT = 0.30  # >30% — прямое противоречие, тот же порог,
# что scripts/audit_property_match_signals.py (не откалибровано на
# ground truth, локальная эвристика этого matcher_version).

# Best-effort номер дома — та же эвристика, что была в scripts/audit_
# property_linker_fuzzy.py::extract_house_number, теперь КАНОНИЧЕСКАЯ
# здесь (нужна production candidate-generation коду, не только аудиту).
_HOUSE_NUMBER_RE = re.compile(r"(\d+[а-яА-Яa-zA-Z]?(?:[/\-]\d+[а-яА-Яa-zA-Z]?)?)\s*,?\s*$")


def extract_house_number(address: str | None) -> str | None:
    """Последняя группа "число(+буква/дробь)" в конце СЫРОГО адреса.
    Эвристика для candidate-сигналов, НЕ участвует в самом address_hash
    (compute_address_hash его не трогает — см. её докстринг)."""
    if not address:
        return None
    m = _HOUSE_NUMBER_RE.search(address.strip())
    return m.group(1).lower() if m else None


def _normalize_seller_name(raw: str | None) -> str | None:
    """trim+lower+схлопнутые пробелы — тот же нормализатор, что
    seller_profile_snapshot.py::_normalize_name, ЛОКАЛЬНО продублирован
    (не импортируем repo-root скрипт из bot/-пакета — обратная
    архитектурная зависимость, сама функция — одна строка)."""
    if not raw:
        return None
    return re.sub(r"\s+", " ", raw.strip()).lower()


def _active_overlap(a: dict, b: dict) -> bool | None:
    """Пересекались ли a/b по времени экспозиции — None, если дат не
    хватает. Та же логика, что в read-only аудитах этой ветки задач
    (scripts/audit_property_match_signals.py::_active_overlap)."""
    from datetime import datetime, timezone
    a_start, b_start = a.get("first_seen"), b.get("first_seen")
    if a_start is None or b_start is None:
        return None
    a_end = a.get("archived_at") or datetime.now(timezone.utc)
    b_end = b.get("archived_at") or datetime.now(timezone.utc)
    return a_start <= b_end and b_start <= a_end


def _conflict_reasons(anchor: dict, candidate: dict) -> list[str]:
    """Прямые структурные противоречия — задача 2026-08-16 (мид-ту-
    уточнение): "simultaneous activity НЕ безусловный конфликт" —
    ПОЭТОМУ её здесь НЕТ. Только rooms/номер дома/цена — то же, что
    scripts/audit_property_match_signals.py::classify_tier() считало
    'rejected', но БЕЗ simultaneously_active (тот аудит-скрипт не
    трогаем, он read-only историческая находка; здесь — production
    правило, сознательно уже правильное)."""
    reasons = []
    a_rooms, c_rooms = anchor.get("rooms"), candidate.get("rooms")
    if a_rooms is not None and c_rooms is not None and a_rooms != c_rooms:
        reasons.append(f"rooms mismatch: {a_rooms} vs {c_rooms}")

    a_hn, c_hn = extract_house_number(anchor.get("address")), extract_house_number(candidate.get("address"))
    if a_hn is not None and c_hn is not None and a_hn != c_hn:
        reasons.append(f"house_number mismatch: {a_hn} vs {c_hn}")

    a_price, c_price = anchor.get("price"), candidate.get("price")
    if a_price and c_price:
        diff_pct = abs(a_price - c_price) / max(a_price, c_price)
        if diff_pct > _PRICE_SEVERE_DIFF_PCT:
            reasons.append(f"price differs {diff_pct * 100:.0f}% (>{_PRICE_SEVERE_DIFF_PCT * 100:.0f}%)")
    return reasons


def classify_relationship(anchor: dict, candidate: dict) -> str:
    """concurrent_duplicate | relist | possible_same_property | unknown
    — задача 2026-08-16, мид-ту-уточнение: "Одна физическая квартира
    может одновременно продаваться через 5 агентов с разными listing_id
    ... это concurrent_duplicate, а не разные квартиры" — ОТДЕЛЬНО от
    status (pending/accepted/rejected), НЕ влияет на conflict_reasons.

    strong_evidence сейчас — ТОЛЬКО совпадение номера дома (photo-сигнал
    — задача явно откладывает perceptual matching на следующий PR, evidence
    JSONB готов принять его позже, см. migrations/086, но здесь не
    считается)."""
    overlap = _active_overlap(anchor, candidate)

    a_hn, c_hn = extract_house_number(anchor.get("address")), extract_house_number(candidate.get("address"))
    strong_evidence = a_hn is not None and a_hn == c_hn

    a_seller = _normalize_seller_name(anchor.get("seller_name"))
    c_seller = _normalize_seller_name(candidate.get("seller_name"))
    seller_equal = (a_seller == c_seller) if (a_seller and c_seller) else None
    some_evidence = strong_evidence or seller_equal is True

    if overlap is True:
        if strong_evidence:
            return "concurrent_duplicate"
        if some_evidence:
            return "possible_same_property"
        return "unknown"
    if overlap is False:
        if strong_evidence or some_evidence:
            return "relist"
        return "unknown"
    return "unknown"


def _exact_hash_match_score(sibling_count: int) -> float:
    """НЕ 1.0 просто за совпадение address_hash (задача, явно: "Не
    ставить match confidence=1.0 только за одинаковый address_hash") —
    понижается по числу "соседей" (других properties того же (complex_id,
    floor) в допуске площади — структурный риск многоподъездного ЖК с
    повторяющейся планировкой, см. docs/property_identity_v2_
    architecture_audit.md)."""
    base = 0.9
    penalty = min(sibling_count, 3) * 0.1
    return round(max(0.5, base - penalty), 2)


_DEDUP_METHOD_SCORE = {"addr_area": 0.8, "addr_price": 0.75, "geo": 0.65, "photo": 0.9}


def _dedup_match_score(dup_match: str | None) -> float:
    """bot/core/dedup_listings.py's dup_match правило -> базовый score —
    "независимый evidence, но НЕ автоматическое подтверждение" (задача,
    явно) — попадает в match_score, НЕ форсирует status='accepted'."""
    return _DEDUP_METHOD_SCORE.get(dup_match, 0.7)


class BootstrapIndex:
    """"Виртуальные" (dry-run) properties этого прохода — та же
    мотивация, что DryRunCache выше (docstring класса): без него второй
    listing ТОЙ ЖЕ ещё не виденной квартиры в одном dry-run прогоне не
    нашёл бы кандидата вовсе (реальная properties за dry-run не
    меняется). В отличие от DryRunCache — хранит СПИСКИ (не "видели ли
    вообще"), т.к. candidate_only генерирует МНОЖЕСТВЕННЫЕ кандидаты,
    не выбирает одного "победителя"."""

    def __init__(self) -> None:
        self._by_hash: dict[str, list[dict]] = {}
        self._by_complex_floor: dict[tuple, list[dict]] = {}
        self._by_listing_id: dict[str, dict] = {}

    def add(self, listing_row: dict, address_hash: str, complex_id: int | None,
            floor: int | None, area: float | None) -> None:
        self._by_hash.setdefault(address_hash, []).append(listing_row)
        if complex_id is not None and floor is not None and area is not None:
            self._by_complex_floor.setdefault((complex_id, floor), []).append(listing_row)
        self._by_listing_id[listing_row["id"]] = listing_row

    def find_exact(self, address_hash: str) -> list[dict]:
        return self._by_hash.get(address_hash, [])

    def find_listing(self, listing_id: str) -> dict | None:
        """Задача, dedup_listings-сигнал в dry-run: находит "своя ли
        provisional property" у ДРУГОГО listing'а, если он УЖЕ обработан
        РАНЬШЕ в этом же проходе — property_listings (реальная БД) за
        dry-run не меняется, тот же класс проблемы, что и у find_exact/
        find_fuzzy выше. Листинг, ещё не дошедший до обработки в этом
        проходе (или обработанный ПОЗЖЕ по текущему порядку), НЕ
        находится — то же фундаментальное ограничение имел бы и
        РЕАЛЬНЫЙ (не dry-run) прогон при том же порядке (нельзя увидеть
        будущее), не отдельный баг dry-run."""
        return self._by_listing_id.get(listing_id)

    def find_fuzzy(self, complex_id: int, floor: int, area: float, tolerance: float) -> list[dict]:
        return [r for r in self._by_complex_floor.get((complex_id, floor), [])
                if r.get("area") is not None and abs(r["area"] - area) <= tolerance]


async def _find_exact_hash_properties(address_hash: str) -> list[dict]:
    """properties с этим address_hash (МОЖЕТ быть несколько — UNIQUE
    снят миграцией 086) + данные listing'а, из которого КАЖДАЯ была
    bootstrap'нута (JOIN через property_listings — на данный момент
    ВСЕГДА 1:1, merge-процесс — следующий PR, см. migrations/086
    докстринг; если это когда-то перестанет быть 1:1, здесь появятся
    дубли строк на одну property — не проблема этого PR)."""
    from bot.db.pg import fetch
    return await fetch("""
        SELECT p.property_id, al.id AS listing_id, al.address, al.floor, al.area, al.rooms,
               al.seller_name, al.price, al.first_seen, al.last_seen, al.archived_at
        FROM properties p
        JOIN property_listings pl ON pl.property_id = p.property_id
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE p.address_hash = $1
    """, address_hash)


async def _find_fuzzy_properties(complex_id: int, floor: int, area: float, tolerance: float) -> list[dict]:
    """properties того же (complex_id, floor) с area_sqm в допуске —
    МНОЖЕСТВЕННЫЕ кандидаты (не "ближайший один", в отличие от legacy
    _find_fuzzy_candidate выше — candidate_only логирует ВСЕ, решение
    не принимает сам)."""
    from bot.db.pg import fetch
    return await fetch("""
        SELECT p.property_id, al.id AS listing_id, al.address, al.floor, al.area, al.rooms,
               al.seller_name, al.price, al.first_seen, al.last_seen, al.archived_at
        FROM properties p
        JOIN property_listings pl ON pl.property_id = p.property_id
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE p.complex_id = $1 AND p.floor = $2
          AND p.area_sqm BETWEEN $3::real - $4::real AND $3::real + $4::real
    """, complex_id, floor, area, tolerance)


async def _find_dedup_candidate_property(listing_row: dict,
                                          bootstrap_index: "BootstrapIndex | None" = None) -> dict | None:
    """bot/core/dedup_listings.py's is_duplicate/duplicate_of — живой,
    независимый прод-сигнал (задача 2026-08-16, "Property Identity v2"
    нашла: 29.5% apartment_listings уже так помечены, property_linker
    их не консультировал вовсе). Симметрично: либо ЭТОТ listing
    указывает duplicate_of на другой, либо какой-то ДРУГОЙ listing
    указывает duplicate_of на ЭТОТ.

    bootstrap_index — НАЙДЕНО на реальном dry-run прогоне (та же
    ошибка, что уже была у exact_hash/fuzzy выше): "другой" listing
    ищется через property_listings, а она в dry-run НИКОГДА не меняется
    — реальный прогон на 50352 строках дал dedup_candidates=0, хотя
    29.5% listing'ов помечены is_duplicate. Раз bootstrap_index
    передан — ищем виртуальную provisional property "другого" listing'а
    там (если он УЖЕ обработан РАНЬШЕ в этом же проходе); иначе (prod
    без dry-run) — обычный DB JOIN, как раньше."""
    from bot.db.pg import fetchrow
    listing_id = listing_row["id"]
    duplicate_of = listing_row.get("duplicate_of")

    other_id = None
    dup_match = listing_row.get("dup_match")
    if duplicate_of:
        other_id = duplicate_of
    else:
        row = await fetchrow(
            "SELECT id, dup_match FROM apartment_listings WHERE duplicate_of = $1 LIMIT 1", listing_id)
        if row:
            other_id, dup_match = row["id"], row["dup_match"]

    if other_id is None:
        return None

    if bootstrap_index is not None:
        other_row = bootstrap_index.find_listing(other_id)
        if other_row is None:
            return None
        result = dict(other_row)
        result["property_id"] = None  # виртуальная — см. _build_candidate_record
        result["_dup_match"] = dup_match
        return result

    row = await fetchrow("""
        SELECT p.property_id, al.id AS listing_id, al.address, al.floor, al.area, al.rooms,
               al.seller_name, al.price, al.first_seen, al.last_seen, al.archived_at
        FROM property_listings pl
        JOIN properties p ON p.property_id = pl.property_id
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.listing_id = $1
    """, other_id)
    if row is None:
        return None
    result = dict(row)
    result["_dup_match"] = dup_match
    return result


def _build_candidate_record(listing_row: dict, candidate_row: dict, match_method: str,
                             match_score: float) -> dict:
    """Одна запись-кандидат (для INSERT в property_match_candidates или
    для dry-run статистики) — evidence/conflict_reasons/relationship_type
    все посчитаны здесь, единая точка правды для ВСЕХ трёх источников
    сигнала (exact_hash/fuzzy/dedup_listings) — задача: "переиспользовать
    существующие механизмы, не писать четвёртый matcher" — это НЕ
    четвёртый matcher, это ОБЩАЯ evidence-обвязка над уже существующими
    тремя источниками (address_hash/fuzzy tolerance из этого модуля,
    is_duplicate/duplicate_of из bot/core/dedup_listings.py)."""
    conflict_reasons = _conflict_reasons(listing_row, candidate_row)
    relationship_type = classify_relationship(listing_row, candidate_row)
    overlap = _active_overlap(listing_row, candidate_row)
    a_hn, c_hn = extract_house_number(listing_row.get("address")), extract_house_number(candidate_row.get("address"))
    a_seller = _normalize_seller_name(listing_row.get("seller_name"))
    c_seller = _normalize_seller_name(candidate_row.get("seller_name"))

    evidence = {
        "rooms_a": listing_row.get("rooms"), "rooms_b": candidate_row.get("rooms"),
        "house_number_a": a_hn, "house_number_b": c_hn,
        "seller_equal": (a_seller == c_seller) if (a_seller and c_seller) else None,
        "price_a": listing_row.get("price"), "price_b": candidate_row.get("price"),
        "simultaneously_active": overlap,
        # Perceptual photo-matching — СЛЕДУЮЩИЙ PR (задача, явно: "не
        # реализовывать автоматический photo merge в текущем PR, но не
        # закладывать в схему ошибочное правило simultaneous_active =
        # rejected" — ключи зарезервированы, метод пока не реализован;
        # URL фотографий СОЗНАТЕЛЬНО не используется как идентификатор
        # здесь, см. migrations/086 докстринг).
        "photo_signal": {"method": "not_implemented", "shared_rare_photo_count": None,
                          "shared_common_photo_count": None},
    }
    if match_method == "dedup_listings":
        evidence["dedup_dup_match"] = candidate_row.get("_dup_match")

    is_rejected_by_conflict = bool(conflict_reasons)

    return {
        "listing_id": listing_row["id"],
        "candidate_property_id": candidate_row["property_id"],
        "match_method": match_method,
        "match_score": match_score,
        "relationship_type": relationship_type,
        "evidence": evidence,
        "conflict_reasons": conflict_reasons or None,
        "matcher_version": _MATCHER_VERSION,
        "status": "rejected" if is_rejected_by_conflict else "pending",
        "rejected_by_conflict": is_rejected_by_conflict,
    }


async def _generate_candidates(listing_row: dict, address_hash: str, complex_id: int | None,
                                bootstrap_index: "BootstrapIndex | None") -> list[dict]:
    """Собирает кандидатов из ВСЕХ трёх источников сигнала для ОДНОГО
    listing'а против УЖЕ СУЩЕСТВУЮЩИХ properties (см. вызывающий код —
    candidate-generation идёт ДО bootstrap'а этого же listing'а, чтобы
    его собственная property не попала в кандидаты сама к себе)."""
    listing_id = listing_row["id"]
    floor, area, rooms = listing_row.get("floor"), listing_row.get("area"), listing_row.get("rooms")
    candidates: list[dict] = []
    seen_property_ids: set[int | None] = set()

    # exact_hash
    exact_rows = list(await _find_exact_hash_properties(address_hash))
    if bootstrap_index is not None:
        exact_rows += [{"property_id": None, **r} for r in bootstrap_index.find_exact(address_hash)
                       if r["id"] != listing_id]
    sibling_count = max(len(exact_rows) - 1, 0)
    for row in exact_rows:
        row = dict(row)
        if row.get("listing_id", row.get("id")) == listing_id:
            continue
        pid = row.get("property_id")
        key = pid if pid is not None else ("virtual", row.get("listing_id") or row.get("id"))
        if key in seen_property_ids:
            continue
        seen_property_ids.add(key)
        row.setdefault("id", row.get("listing_id"))
        # НАЙДЕНО на первом реальном dry-run прогоне: "if pid is not
        # None" здесь молча выбрасывал ВСЕ виртуальные (bootstrap_index,
        # ещё не вставленные в БД) кандидаты из результата — тот же
        # класс бага, что уже дважды ловили в этой ветке задач
        # (DryRunCache для fuzzy в P1, потом exact_only) — dry-run
        # candidate_only показывал 0 exact/fuzzy кандидатов на ВСЕЙ базе
        # (50352 строк), хотя предыдущие read-only аудиты нашли тысячи.
        # candidate_property_id=None для виртуальных — их и так никогда
        # не вставляют в БД (см. _link_candidate_only: INSERT кандидатов
        # только в НЕ-dry-run ветке, где bootstrap_index всегда None,
        # pid всегда реальный) — здесь они нужны ТОЛЬКО для честной
        # dry-run статистики.
        candidates.append(_build_candidate_record(
            listing_row, row, "exact_hash", _exact_hash_match_score(sibling_count)))

    # fuzzy (только с известным complex_id — тот же принцип, что exact_only)
    if complex_id is not None and floor is not None and area is not None:
        fuzzy_rows = list(await _find_fuzzy_properties(complex_id, floor, area, _FUZZY_AREA_TOLERANCE))
        if bootstrap_index is not None:
            fuzzy_rows += [{"property_id": None, **r}
                           for r in bootstrap_index.find_fuzzy(complex_id, floor, area, _FUZZY_AREA_TOLERANCE)
                           if r["id"] != listing_id]
        for row in fuzzy_rows:
            row = dict(row)
            listing_key = row.get("listing_id", row.get("id"))
            if listing_key == listing_id:
                continue
            pid = row.get("property_id")
            key = pid if pid is not None else ("virtual", listing_key)
            if key in seen_property_ids:
                continue
            seen_property_ids.add(key)
            row.setdefault("id", listing_key)
            # Тот же фикс, что выше в exact_hash — виртуальные (dry-run)
            # кандидаты тоже считаются, не только реальные.
            score = _fuzzy_confidence(area, row.get("area") or area)
            candidates.append(_build_candidate_record(listing_row, row, "fuzzy", score))

    # dedup_listings — bootstrap_index передаём (см. _find_dedup_candidate_
    # property докстринг: реальная property_listings за dry-run не
    # меняется, тот же класс бага, что уже был у exact_hash/fuzzy).
    dedup_row = await _find_dedup_candidate_property(listing_row, bootstrap_index)
    if dedup_row is not None:
        pid = dedup_row.get("property_id")
        # НАЙДЕНО на прогоне детерминированности (задача 2026-08-16, "последняя
        # проверка перед production backfill"): виртуальные (bootstrap_index)
        # dedup-строки несут ключ "id" (см. _find_dedup_candidate_property —
        # dict(other_row), other_row это исходный listing_row с полем "id",
        # "listing_id" на нём никогда не было), а НЕ "listing_id" — .get(
        # "listing_id") здесь всегда молча давал None, значит виртуальный
        # dedup-ключ всегда был ("virtual", None), не ("virtual", <id>), и не
        # совпадал с ключом, под которым тот же "другой" listing уже мог быть
        # добавлен в seen_property_ids шагом exact_hash/fuzzy выше — пара,
        # совпавшая ОБОИМИ сигналами, задваивалась (exact_hash-строка И
        # dedup_listings-строка на одну и ту же пару) вместо дедупликации.
        key = pid if pid is not None else ("virtual", dedup_row.get("listing_id", dedup_row.get("id")))
        if key not in seen_property_ids:
            seen_property_ids.add(key)
            score = _dedup_match_score(dedup_row.get("_dup_match"))
            candidates.append(_build_candidate_record(listing_row, dedup_row, "dedup_listings", score))

    return candidates


_VALID_MATCH_MODES = frozenset({"candidate_only", "exact_only", "fuzzy"})


async def _link_candidate_only(listing_row: dict, address_hash: str, complex_id: int | None,
                                dry_run: bool, bootstrap_index: "BootstrapIndex | None") -> dict:
    """match_mode="candidate_only" — задача 2026-08-16, "безопасная
    инфраструктура кандидатов". listing → ВСЕГДА своя provisional
    property (bootstrap, НЕ поиск существующей); exact_hash/fuzzy/
    dedup_listings сигналы генерируют property_match_candidates строки,
    НИКОГДА не создают hard link (задача: "exact address_hash НЕ
    создаёт hard link... все совпадения становятся property_match_
    candidates"). bootstrap_index — BootstrapIndex (НЕ DryRunCache —
    разный тип по дизайну, candidate_only генерирует множественных
    кандидатов, не выбирает "победителя" как exact_only/fuzzy режимы)."""
    from bot.db.pg import execute, fetchval
    import json

    listing_id = listing_row["id"]
    floor, area, rooms = listing_row.get("floor"), listing_row.get("area"), listing_row.get("rooms")
    listing_first_seen = listing_row.get("first_seen")
    listing_evidence_at = listing_row.get("archived_at") or listing_row.get("last_seen")

    # Кандидаты — ПРОТИВ уже существующих properties, ДО bootstrap'а
    # ЭТОГО listing'а (иначе его собственная новая property попала бы
    # в кандидаты сама к себе).
    candidates = await _generate_candidates(listing_row, address_hash, complex_id, bootstrap_index)

    if dry_run:
        if bootstrap_index is not None:
            bootstrap_index.add(listing_row, address_hash, complex_id, floor, area)
        return {"property_id": None, "method": "bootstrap", "confidence": 1.0, "created": True,
                "match_mode": "candidate_only", "fuzzy_candidate": None, "skip_reason": None,
                "candidates": candidates}

    # НЕТ ON CONFLICT на address_hash — UNIQUE снят миграцией 086,
    # bootstrap ВСЕГДА создаёт НОВУЮ строку, никогда не находит/апсертит
    # существующую (задача: "новый listing → отдельная provisional
    # property").
    new_id = await fetchval(
        """
        INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms, first_seen_at, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()), COALESCE($7, $6, now()))
        RETURNING property_id
        """,
        complex_id, address_hash, floor, area, rooms, listing_first_seen, listing_evidence_at,
    )
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence, matcher_version) "
        "VALUES ($1, $2, 'bootstrap', 1.0, $3) ON CONFLICT (listing_id) DO NOTHING",
        new_id, listing_id, _MATCHER_VERSION,
    )
    for c in candidates:
        await execute(
            """
            INSERT INTO property_match_candidates
                (listing_id, candidate_property_id, match_method, match_score, relationship_type,
                 evidence, conflict_reasons, matcher_version, status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
            ON CONFLICT (listing_id, candidate_property_id) DO NOTHING
            """,
            c["listing_id"], c["candidate_property_id"], c["match_method"], c["match_score"],
            c["relationship_type"], json.dumps(c["evidence"], default=str, ensure_ascii=False),
            json.dumps(c["conflict_reasons"], ensure_ascii=False) if c["conflict_reasons"] else None,
            c["matcher_version"], c["status"],
        )

    return {"property_id": new_id, "method": "bootstrap", "confidence": 1.0, "created": True,
            "match_mode": "candidate_only", "fuzzy_candidate": None, "skip_reason": None,
            "candidates": candidates}


# ── candidate_only BULK BACKFILL: two-phase deterministic construction
# (задача 2026-08-16, "последняя проверка детерминированности candidate
# graph перед production backfill") ─────────────────────────────────────
#
# НАЙДЕНО эмпирически (4 порядка: id ASC/DESC/listed_at/shuffled_42 на
# полной таблице apartment_listings): однопроходный цикл выше
# (_link_candidate_only + BootstrapIndex, вызываемый scripts/backfill_
# property_ids.py::run_backfill по одному listing'у за раз) даёт
# candidate-граф, ЗАВИСЯЩИЙ от порядка обработки. Причина структурная, не
# в отдельном "баге" какого-то сигнала: candidate-generation для listing'а
# X происходит ПОСЛЕ bootstrap'а всех РАНЕЕ обработанных listing'ов, но
# ДО bootstrap'а тех, что будут обработаны позже — значит "видимое" X
# множество кандидатов = "кто уже успел бутстрапнуться к этому моменту в
# ЭТОМ конкретном порядке", а не "кто вообще существует". Для exact_hash/
# fuzzy это ещё даёт ПОЛНЫЙ граф по каждой паре (кто раньше в порядке —
# тот и не найдёт, кто позже — найдёт, ровно одно ребро на пару, СУММА
# рёбер по факту не зависит от порядка) — но для dedup_listings это НЕ
# так: обратный поиск ("кто ссылается duplicate_of на МЕНЯ") идёт
# ОДНИМ SQL с LIMIT 1 (см. _find_dedup_candidate_property) — если на
# один "канонический" listing ссылается НЕСКОЛЬКО duplicate_of, LIMIT 1
# произвольно выбирает ОДНУ фиксированную строку (не зависит от порядка
# backfill'а сам по себе, это факт физического порядка чтения Postgres),
# но УСПЕЕТ ли она попасть в bootstrap_index к моменту, когда канонический
# listing станет anchor'ом — уже зависит от порядка, и если "повезло"
# выбранной LIMIT 1 строке не быть готовой — пара теряется целиком, хотя
# forward-путь (у самого duplicate-listing'а) мог бы её найти, будь он
# обработан позже "канонического" — то есть теряется НЕ в одном порядке,
# а в другом находится частично, в третьем иначе. Отсюда наблюдаемая
# order-dependence dedup_candidates (см. отчёт).
#
# Фикс — РАЗДЕЛИТЬ backfill на две фазы (задача, явно):
#   Фаза A (bootstrap_all_provisional) — КАЖДЫЙ listing получает СВОЮ
#     provisional property, независимо от остальных (bootstrap и раньше
#     не искал существующую — эта фаза уже была структурно order-
#     independent, здесь она просто явно завершается ПОЛНОСТЬЮ раньше,
#     чем начинается фаза B).
#   Фаза B (generate_all_candidates) — запускается ТОЛЬКО когда mapping
#     listing→property ПОЛНЫЙ. Ищет кандидатов среди ВСЕХ забутстрапленных
#     listing'ов (плюс отдельно — среди РЕАЛЬНО уже существующих properties
#     из ПРЕДЫДУЩИХ прогонов, см. ниже), не только "уже обработанных
#     раньше в произвольном порядке". Направление ребра (кто anchor)
#     выбирается ДЕТЕРМИНИРОВАННО по _canonical_rank(listing_id)
#     (численное сравнение) — ЧИСТАЯ функция двух id, не зависящая от
#     порядка обхода вообще, поэтому один и тот же listing либо ВСЕГДА
#     генерит ребро для данной пары, либо НИКОГДА, независимо от order_by.
#     Обратный dedup-поиск здесь БЕЗ LIMIT 1 (см. _find_all_dedup_partners)
#     — фикс той же находки: несколько duplicate_of на один канонический
#     listing теперь все учитываются, не только первый попавшийся.
#
# scripts/backfill_property_ids.py::run_backfill вызывает ЭТИ функции для
# match_mode="candidate_only" (bulk backfill); _link_candidate_only/
# link_listing_to_property выше НЕ изменены и остаются точкой входа для
# ЖИВОГО поштучного связывания новых listing'ов (там "порядок обработки"
# — это реальный порядок появления объявлений, не искусственный выбор
# backfill-скрипта, и БД к моменту вызова уже содержит всё, что появилось
# раньше по-настоящему — та же order-independence гарантия, только без
# двух явных фаз, они там и не нужны).


def _canonical_rank(listing_id: str) -> tuple:
    """Тотальный, ДЕТЕРМИНИРОВАННЫЙ порядок на listing_id для фазы B —
    ЧИСЛОВОЙ, не строковый: id — TEXT, но исторически числовые krisha
    listing id разной длины ("7571569" короче "1010897984") —
    лексикографическое сравнение дало бы другой порядок, чем численное
    (найдено на реальных данных, см. audit_address_hash_exact.py про
    разброс длины id). Нечисловые id (на практике не встречались, кроме
    __test_..__ в тестах) идут ПОСЛЕ всех числовых, сравниваются как
    строки между собой — важно только чтобы правило было тотальным и
    одинаковым в любом прогоне, не что оно "естественное"."""
    try:
        return (0, int(listing_id))
    except (TypeError, ValueError):
        return (1, listing_id)


async def bootstrap_all_provisional(rows: list[dict], dry_run: bool) -> dict[str, dict]:
    """ФАЗА A. Возвращает {listing_id: {"status": "already_linked" |
    "skipped" | "bootstrap", "property_id": int | None, "row": dict}} —
    "row" при status="bootstrap" дополнен "_address_hash"/"_complex_id"
    (посчитаны здесь один раз, фаза B переиспользует, не пересчитывает).
    property_id реальный при dry_run=False, иначе None (см. DryRunCache/
    BootstrapIndex докстринги выше — dry-run никогда не вставляет строк)."""
    from bot.db.pg import execute, fetchval

    mapping: dict[str, dict] = {}
    for row in rows:
        listing_id = row["id"]
        # Проверка "уже связан" — ВСЕГДА (это SELECT, не запись; dry_run
        # запрещает только WRITE'ы — см. докстринг link_listing_to_property
        # выше). НАЙДЕНО при ревью: изначально стояло "if not dry_run", из-за
        # чего dry-run прогон считал КАЖДЫЙ listing бутстрапящимся заново,
        # даже реально уже связанные предыдущим прогоном — ломало и
        # already_linked-статистику, и фазу B (см. test_phase_b_finds_
        # candidates_against_previously_linked_listing).
        already = await fetchval(
            "SELECT property_id FROM property_listings WHERE listing_id = $1", listing_id)
        if already is not None:
            mapping[listing_id] = {"status": "already_linked", "property_id": already, "row": row}
            continue

        address = row.get("address")
        floor, area, rooms = row.get("floor"), row.get("area"), row.get("rooms")
        address_hash = compute_address_hash(address, floor, area)
        if address_hash is None:
            mapping[listing_id] = {"status": "skipped", "property_id": None, "row": row,
                                    "skip_reason": _skip_reason(address, floor, area)}
            continue

        complex_id = await _resolve_complex_id(row.get("complex_name"))
        row = dict(row)
        row["_address_hash"] = address_hash
        row["_complex_id"] = complex_id

        if dry_run:
            mapping[listing_id] = {"status": "bootstrap", "property_id": None, "row": row}
            continue

        listing_first_seen = row.get("first_seen")
        listing_evidence_at = row.get("archived_at") or row.get("last_seen")
        new_id = await fetchval(
            """
            INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()), COALESCE($7, $6, now()))
            RETURNING property_id
            """,
            complex_id, address_hash, floor, area, rooms, listing_first_seen, listing_evidence_at,
        )
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence, matcher_version) "
            "VALUES ($1, $2, 'bootstrap', 1.0, $3) ON CONFLICT (listing_id) DO NOTHING",
            new_id, listing_id, _MATCHER_VERSION,
        )
        mapping[listing_id] = {"status": "bootstrap", "property_id": new_id, "row": row}
    return mapping


async def _find_all_dedup_partners(listing_row: dict) -> list[dict]:
    """dedup_listings-сигнал БЕЗ LIMIT 1 (см. блок-докстринг выше про
    находку: обратный поиск "кто ссылается duplicate_of на меня" раньше
    брал ОДНУ произвольную строку — если на канонический listing
    ссылались 2+ дубля, остальные молча терялись). Возвращает ВСЕ
    строки-партнёры {"id", "dup_match"} — forward (на кого ссылается ЭТОТ
    listing) с dup_match САМОГО listing_row (это ОН объявлен дублем);
    reverse (кто ссылается на ЭТОТ listing) с dup_match КАЖДОЙ найденной
    строки (это ОНА объявлена дублем этого listing_row) — то же различие
    направления dup_match, что было в _find_dedup_candidate_property выше,
    просто без обрезки reverse-варианта до одной строки."""
    from bot.db.pg import fetch, fetchrow
    listing_id = listing_row["id"]
    duplicate_of = listing_row.get("duplicate_of")
    partners: list[dict] = []
    if duplicate_of:
        row = await fetchrow("SELECT id FROM apartment_listings WHERE id = $1", duplicate_of)
        if row is not None:
            partners.append({"id": row["id"], "dup_match": listing_row.get("dup_match")})
    reverse_rows = await fetch(
        "SELECT id, dup_match FROM apartment_listings WHERE duplicate_of = $1", listing_id)
    partners.extend({"id": r["id"], "dup_match": r["dup_match"]} for r in reverse_rows)
    return partners


async def _resolve_real_candidate_row(other_listing_id: str) -> dict | None:
    """Реальная (уже связанная — из ЭТОГО ИЛИ предыдущего отдельного
    прогона) property для other_listing_id, для dedup-партнёров,
    которых нет в mapping ЭТОЙ фазы B (см. generate_all_candidates)."""
    from bot.db.pg import fetchrow
    real_row = await fetchrow(
        """
        SELECT p.property_id, al.id AS listing_id, al.address, al.floor, al.area, al.rooms,
               al.seller_name, al.price, al.first_seen, al.last_seen, al.archived_at
        FROM property_listings pl
        JOIN properties p ON p.property_id = pl.property_id
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.listing_id = $1
        """, other_listing_id)
    if real_row is None:
        return None
    row = dict(real_row)
    row.setdefault("id", other_listing_id)
    return row


async def generate_all_candidates(mapping: dict[str, dict]) -> list[dict]:
    """ФАЗА B — вызывать ТОЛЬКО после того, как mapping из bootstrap_all_
    provisional ПОЛНЫЙ (все listing'и этого backfill-прохода уже
    забутстрапнуты). Возвращает плоский список candidate-записей (тот же
    формат, что _build_candidate_record) — caller (run_backfill) решает,
    писать их в БД или считать dry-run статистику.

    Направление ребра для КАЖДОЙ пары ИЗ ЭТОГО прохода выбирается через
    _canonical_rank — целиком по идентичности (listing_id vs listing_id),
    БЕЗ обращения к тому, в каком порядке эта функция сама что-то
    перебирает: цикл ниже можно было бы гонять хоть в случайном порядке,
    результат тот же (задача, явно: "candidate generation не должен
    зависеть от того, какое объявление обработано первым"). Партнёры из
    ПРЕДЫДУЩИХ (уже завершённых) прогонов backfill'а всегда "раньше" по
    определению — ранговое сравнение к ним не применяется."""
    bootstrapped = [(lid, m) for lid, m in mapping.items() if m["status"] == "bootstrap"]

    by_hash: dict[str, list[str]] = {}
    by_complex_floor: dict[tuple, list[str]] = {}
    for lid, m in bootstrapped:
        row = m["row"]
        by_hash.setdefault(row["_address_hash"], []).append(lid)
        cid, floor, area = row.get("_complex_id"), row.get("floor"), row.get("area")
        if cid is not None and floor is not None and area is not None:
            by_complex_floor.setdefault((cid, floor), []).append(lid)

    candidates: list[dict] = []
    for lid, m in bootstrapped:
        anchor_row = m["row"]
        rank_x = _canonical_rank(lid)
        address_hash = anchor_row["_address_hash"]
        cid, floor, area = anchor_row.get("_complex_id"), anchor_row.get("floor"), anchor_row.get("area")

        # found: other_listing_id -> (match_method, extra_for_score,
        # in_run: bool). Порядок вставки ниже (exact_hash, затем fuzzy,
        # затем dedup) задаёт приоритет при коллизии на одного и того же
        # "другого" — тот же приоритет, что в _generate_candidates
        # (однопроходная версия) выше.
        found: dict[str, tuple[str, object, bool]] = {}

        # exact_hash — ВСЕ остальные забутстрапленные в ЭТОМ прогоне с тем
        # же address_hash (полный граф по кластеру, см. блок-докстринг),
        # ПЛЮС реальные properties из предыдущих прогонов (идемпотентность
        # повторного/добавочного backfill'а).
        cluster = by_hash.get(address_hash, [])
        exact_sibling_count = max(len(cluster) - 1, 0)
        for other in cluster:
            if other != lid:
                found[other] = ("exact_hash", None, True)
        for real_row in await _find_exact_hash_properties(address_hash):
            other_lid = real_row["listing_id"]
            # НЕ "not in mapping" — already_linked ИЗ ЭТОГО ЖЕ mapping (был
            # уже связан ДО этого прогона, просто входил в rows фазы A) тоже
            # реальный, тоже должен резолвиться через _resolve_real_candidate_
            # row ниже, а не молча теряться (найдено при ревью: "not in
            # mapping" отбрасывал ИМЕННО этот случай, единственный, для
            # которого cluster выше его не подхватывает — cluster содержит
            # только status="bootstrap").
            if other_lid != lid and mapping.get(other_lid, {}).get("status") != "bootstrap":
                found.setdefault(other_lid, ("exact_hash", None, False))

        # fuzzy — тот же принцип (в проходе + реальные предыдущие).
        if cid is not None and floor is not None and area is not None:
            for other in by_complex_floor.get((cid, floor), []):
                if other == lid:
                    continue
                other_area = mapping[other]["row"].get("area")
                if other_area is not None and abs(other_area - area) <= _FUZZY_AREA_TOLERANCE:
                    found.setdefault(other, ("fuzzy", None, True))
            for real_row in await _find_fuzzy_properties(cid, floor, area, _FUZZY_AREA_TOLERANCE):
                other_lid = real_row["listing_id"]
                if other_lid != lid and mapping.get(other_lid, {}).get("status") != "bootstrap":
                    found.setdefault(other_lid, ("fuzzy", None, False))

        # dedup_listings — БЕЗ LIMIT 1 (см. _find_all_dedup_partners).
        for partner in await _find_all_dedup_partners(anchor_row):
            other_lid = partner["id"]
            if other_lid == lid or other_lid in found:
                continue
            other_status = mapping.get(other_lid, {}).get("status")
            if other_status == "skipped":
                continue  # адрес/этаж/площадь не резолвились -> нет property вовсе, нечего предлагать
            in_run = other_status == "bootstrap"
            found[other_lid] = ("dedup_listings", partner.get("dup_match"), in_run)

        for other_lid, (method, extra, in_run) in found.items():
            if in_run:
                if _canonical_rank(other_lid) >= rank_x:
                    continue
                candidate_row = dict(mapping[other_lid]["row"])
                candidate_row["property_id"] = mapping[other_lid]["property_id"]
            else:
                candidate_row = await _resolve_real_candidate_row(other_lid)
                if candidate_row is None:
                    continue

            if method == "exact_hash":
                score = _exact_hash_match_score(exact_sibling_count)
            elif method == "fuzzy":
                score = _fuzzy_confidence(area, candidate_row.get("area") or area)
            else:
                score = _dedup_match_score(extra)
            rec = _build_candidate_record(anchor_row, candidate_row, method, score)
            # other_listing_id — НЕ часть схемы property_match_candidates
            # (та каноническая модель — candidate_property_id, см. migrations/
            # 086 докстринг), но полезна caller'у здесь же (run_backfill не
            # использует, тесты/аудит детерминированности — используют, чтобы
            # канонизировать пару listing/listing независимо от направления).
            rec["other_listing_id"] = candidate_row.get("id", other_lid)
            candidates.append(rec)

    return candidates


async def link_listing_to_property(listing_row: dict, dry_run: bool = False,
                                    dry_run_cache: "DryRunCache | BootstrapIndex | None" = None,
                                    match_mode: str = "candidate_only") -> dict:
    """Основная точка входа. listing_row — строка apartment_listings (или
    dict с теми же ключами): id, address, floor, area, rooms, complex_name.

    match_mode — см. докстринг модуля ("false positive merge хуже false
    negative duplicate"): "candidate_only" (ДЕФОЛТ, задача 2026-08-16
    "безопасная инфраструктура кандидатов" — единственный режим для
    прод-записи) | "exact_only" (LEGACY с этой задачи, только явный
    opt-in — линкует по точному address_hash, БЕЗ candidate-таблицы) |
    "fuzzy" (LEGACY, только явный opt-in,
    НИКОГДА не дефолт — задача: "старый unsafe fuzzy режим нельзя
    случайно включить по умолчанию"). ValueError на любое другое
    значение — опечатка в вызывающем коде не должна тихо деградировать
    в непонятный режим.

    Возвращает {"property_id": int | None, "method": str, "confidence":
    float | None, "created": bool, "match_mode": str, "fuzzy_candidate":
    dict | None}. method: 'already_linked' (шаг 1 докстринга модуля) |
    'auto' (точный хэш или новая квартира) | 'fuzzy' (СВЯЗАЛ — только
    возможно при match_mode="fuzzy") | 'skipped' (адрес/этаж/площадь
    неизвестны — property_id=None). fuzzy_candidate — {"candidate_
    property_id", "confidence", "area_diff", "cache_only"} | None:
    заполнен, когда при match_mode="exact_only" был БЫ fuzzy-кандидат,
    но связывание НЕ произошло (см. докстринг модуля, п.3) — для лога/
    будущей property_match_candidates; при match_mode="fuzzy" остаётся
    None (кандидат либо стал реальной связью method='fuzzy', либо его
    не было вовсе).

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
    if match_mode not in _VALID_MATCH_MODES:
        raise ValueError(f"match_mode должен быть одним из {sorted(_VALID_MATCH_MODES)}, получено {match_mode!r}")

    from bot.db.pg import execute, fetchrow, fetchval

    listing_id = listing_row["id"]

    already = await fetchval(
        "SELECT property_id FROM property_listings WHERE listing_id = $1", listing_id)
    if already is not None:
        return {"property_id": already, "method": "already_linked", "confidence": None,
                "created": False, "match_mode": match_mode, "fuzzy_candidate": None, "skip_reason": None}

    address = listing_row.get("address")
    floor = listing_row.get("floor")
    area = listing_row.get("area")
    rooms = listing_row.get("rooms")
    complex_name = listing_row.get("complex_name")
    listing_first_seen = listing_row.get("first_seen")
    listing_evidence_at = listing_row.get("archived_at") or listing_row.get("last_seen")

    address_hash = compute_address_hash(address, floor, area)
    if address_hash is None:
        return {"property_id": None, "method": "skipped", "confidence": None, "created": False,
                "match_mode": match_mode, "fuzzy_candidate": None,
                "skip_reason": _skip_reason(address, floor, area)}

    complex_id = await _resolve_complex_id(complex_name)

    if match_mode == "candidate_only":
        return await _link_candidate_only(listing_row, address_hash, complex_id, dry_run, dry_run_cache)

    # Шаг 2: точный хэш — либо реальная строка в properties, либо (только
    # dry-run) хэш, который УЖЕ "создан" бы этим же прогоном раньше
    # (см. dry_run_cache в докстринге). Тот же путь для ОБОИХ match_mode
    # — exact hash безопасен, риск (см. scripts/audit_address_hash_exact.py)
    # ниже, чем у fuzzy, но не предмет match_mode.
    exact = await fetchrow("SELECT property_id FROM properties WHERE address_hash = $1", address_hash)
    if exact is None and dry_run and dry_run_cache is not None and dry_run_cache.has_hash(address_hash):
        return {"property_id": None, "method": "auto", "confidence": 1.0,
                "created": False, "match_mode": match_mode, "fuzzy_candidate": None, "skip_reason": None}
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
        return {"property_id": exact["property_id"], "method": "auto", "confidence": 1.0,
                "created": False, "match_mode": match_mode, "fuzzy_candidate": None, "skip_reason": None}

    # Шаг 3: fuzzy-кандидат (только при известном complex_id) — ВСЕГДА
    # ВЫЧИСЛЯЕТСЯ (реальная БД + dry_run_cache, см. класс DryRunCache),
    # НЕЗАВИСИМО от match_mode. СВЯЗЫВАЕТ (INSERT в property_listings)
    # только при match_mode="fuzzy" — задача, п.4: "fuzzy-кандидат можно
    # залогировать как candidate, но он не должен мешать созданию
    # отдельного property" — при "exact_only" кандидат уходит в
    # fuzzy_candidate результата, НЕ в UPDATE/INSERT, и код проваливается
    # к шагу 4 (новая property) независимо от того, нашёлся кандидат
    # или нет — "никаких greedy fuzzy assignments".
    fuzzy_candidate_info = None
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
            area_diff = round(abs(area - candidate["area_sqm"]), 3)
            if match_mode == "fuzzy":
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
                        "method": "fuzzy", "confidence": confidence, "created": False,
                        "match_mode": match_mode, "fuzzy_candidate": None, "skip_reason": None}
            # exact_only: НЕ связываем — только запоминаем для результата,
            # проваливаемся к шагу 4.
            fuzzy_candidate_info = {
                "candidate_property_id": None if cache_only else candidate["property_id"],
                "confidence": confidence, "area_diff": area_diff, "cache_only": cache_only,
            }

    # Шаг 4: новая квартира (exact_only — ВСЕГДА сюда, если шаг 2 не
    # связал, даже при наличии fuzzy_candidate_info; fuzzy — только если
    # шаг 3 не нашёл кандидата вовсе).
    if dry_run:
        if dry_run_cache is not None:
            dry_run_cache.add(address_hash, complex_id, floor, area)
        return {"property_id": None, "method": "auto", "confidence": 1.0, "created": True,
                "match_mode": match_mode, "fuzzy_candidate": fuzzy_candidate_info, "skip_reason": None}

    # НАЙДЕНО задачей 2026-08-16 ("безопасная инфраструктура кандидатов",
    # миграция 086): UNIQUE(address_hash) СНЯТ (сознательно — она
    # структурно запрещала 2 properties с одинаковым хэшем даже когда
    # это 2 реальные разные квартиры, см. migrations/086 докстринг) —
    # "ON CONFLICT (address_hash)" больше НЕ РАБОТАЕТ (нет unique/
    # exclusion constraint на эту колонку, Postgres ошибается
    # InvalidColumnReferenceError). exact_only/fuzzy — LEGACY-режимы с
    # этой же задачи (research-only, НЕ прод-дефолт, single-process
    # backfill без конкурентной записи) — race-condition защита была
    # чисто оборонительной на случай конкурентного прогона, которого
    # штатно не бывает; терять её здесь — приемлемый, задокументированный
    # компромисс, не переносим её обратно как отдельный UNIQUE-индекс
    # (это откатило бы миграцию 086 наполовину). Простой INSERT.
    new_id = await fetchval(
        """
        INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms, first_seen_at, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()), COALESCE($7, $6, now()))
        RETURNING property_id
        """,
        complex_id, address_hash, floor, area, rooms, listing_first_seen, listing_evidence_at,
    )
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1, $2, 'auto', 1.0) ON CONFLICT (listing_id) DO NOTHING",
        new_id, listing_id,
    )
    return {"property_id": new_id, "method": "auto", "confidence": 1.0, "created": True,
            "match_mode": match_mode, "fuzzy_candidate": fuzzy_candidate_info, "skip_reason": None}
