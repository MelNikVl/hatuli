"""
Entity resolution, фаза 1 (ЖК) — см. docs/entity_resolution_plan.md.

Единый постоянный ID объекта — complexes.id (PK, без семантики). Этот
модуль:
  - генерирует человеко-читаемый code (JK-000123) для отображения в UI;
  - скорит уверенность связи "источник -> ЖК" по сигналам (имя точное/
    похожее/гео/застройщик/адрес) и пишет её в complex_source_links (spine)
    ЛИБО в очередь на проверку (complex_source_link_candidates), в
    зависимости от confidence;
  - помнит руками отклонённые пары (complex_source_link_rejections) —
    не предлагает их повторно;
  - конфликт (source_id уже привязан к другому ЖК) не перезаписывает
    молча — уходит в очередь как kind='conflict'.

Ревью 2026-08-13 (см. коммит) добавило: fuzzy-ступень имени (pg_trgm
similarity), сигнал адреса, реальное разделение auto-match/review-queue
по факту хранения (раньше и то, и другое одинаково писалось в спайн),
роутинг конфликтов в очередь вместо тихой перезаписи, память отклонений.

Фаза 2 (юниты — unit_source_links, тот же spine на уровне
newbuild_units.id) зафиксирована в плане, но НЕ реализуется здесь —
ждём, пока фаза 1 покажет уровень шума автоматического матчинга в проде.
"""
from __future__ import annotations

import math

# Пороги авто-матч / очередь на ручную проверку / не создавать связь
# вовсе — согласовано в плане (docs/entity_resolution_plan.md).
AUTO_MATCH_THRESHOLD = 0.8
REVIEW_QUEUE_THRESHOLD = 0.5

# Веса сигналов. Имя — не бинарно (точное/непохоже), а по шкале схожести:
# 1.0 (точное совпадение) даёт полный _W_NAME_EXACT; в диапазоне
# [FUZZY_NAME_THRESHOLD, 1.0) — fuzzy-ступень, вес линейно уменьшается от
# _W_NAME_EXACT к _W_NAME_FUZZY_MIN (частичное доверие — рядом, но не точно,
# типичный случай ребрендинга/опечатки на одном из сайтов-источников).
_W_NAME_EXACT = 0.6
_W_NAME_FUZZY_MIN = 0.35
FUZZY_NAME_THRESHOLD = 0.55  # ниже — считаем разными именами, сигнал 0
_W_GEO = 0.25
_W_DEVELOPER = 0.15
_W_ADDRESS = 0.15
GEO_MATCH_RADIUS_M = 150.0


def generate_complex_code(complex_id: int) -> str:
    """JK-000123 — только на отображение, семантики источника/застройщика
    в коде нет (те ребрендятся, транслит плодит дубли)."""
    return f"JK-{complex_id:06d}"


async def ensure_complex_code(complex_id: int) -> str:
    """Идемпотентно проставляет code, если его ещё нет."""
    from bot.db.pg import fetchval, execute
    code = await fetchval("SELECT code FROM complexes WHERE id = $1", complex_id)
    if code:
        return code
    code = generate_complex_code(complex_id)
    await execute("UPDATE complexes SET code = $2 WHERE id = $1", complex_id, code)
    return code


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def name_similarity(name_a: str, name_b: str) -> float:
    """Триграммное сходство имён через pg_trgm (0..1, 1 = идентичны после
    normalize). Считаем в БД (SELECT similarity(...)), не в Python —
    та же функция, что использовалась бы для GIN-индексного поиска
    кандидатов, результат должен совпадать 1-в-1."""
    if not name_a or not name_b:
        return 0.0
    if name_a.strip().lower() == name_b.strip().lower():
        return 1.0
    from bot.db.pg import fetchval
    val = await fetchval("SELECT similarity(lower(trim($1)), lower(trim($2)))", name_a, name_b)
    return float(val or 0.0)


_ADDR_NOISE = [
    "рк,", "г. астана", "г.астана", "астана г.", "астана,", "район ", "р-н ",
    "жилой массив", "ж/м", "мкр", "мкр.", "проспект", "пр.", "улица", "ул.",
    "переулок", "пер.",
]


def _normalize_address(addr: str) -> set[str]:
    s = (addr or "").lower()
    for token in _ADDR_NOISE:
        s = s.replace(token, " ")
    return {t for t in s.replace(",", " ").split() if len(t) > 1}


def address_match(addr_a: str | None, addr_b: str | None) -> bool | None:
    """Грубое, но дешёвое сравнение адресов: после вырезания шумных слов
    (город/район/тип улицы) сравниваем оставшиеся токены (название улицы,
    номер дома) — пересечение >= 50% значимых токенов одной из сторон
    считаем совпадением. None, если сравнивать нечего (адреса пустые) —
    сигнал просто не участвует, не штрафуем."""
    if not addr_a or not addr_b:
        return None
    ta, tb = _normalize_address(addr_a), _normalize_address(addr_b)
    if not ta or not tb:
        return None
    overlap = len(ta & tb)
    return overlap / min(len(ta), len(tb)) >= 0.5


async def score_match(
    name_a: str, name_b: str, *,
    existing_lat: float | None = None, existing_lon: float | None = None,
    candidate_lat: float | None = None, candidate_lon: float | None = None,
    developer_match: bool | None = None,
    existing_address: str | None = None, candidate_address: str | None = None,
) -> tuple[float, str]:
    """Считает confidence + human-readable match_method по сигналам,
    которые реально удалось проверить (сигнал без данных просто не
    участвует — не штрафуем и не выдумываем). Имя — единственный
    обязательный сигнал (без него нет базы для сравнения вовсе)."""
    sim = await name_similarity(name_a, name_b)
    if sim >= 1.0:
        score, parts = _W_NAME_EXACT, ["name_exact"]
    elif sim >= FUZZY_NAME_THRESHOLD:
        # линейная интерполяция веса между порогом (min) и точным (exact)
        t = (sim - FUZZY_NAME_THRESHOLD) / (1.0 - FUZZY_NAME_THRESHOLD)
        score = _W_NAME_FUZZY_MIN + t * (_W_NAME_EXACT - _W_NAME_FUZZY_MIN)
        parts = [f"name_fuzzy({sim:.2f})"]
    else:
        return 0.0, "no_match"

    geo_ok = (existing_lat is not None and existing_lon is not None
              and candidate_lat is not None and candidate_lon is not None)
    if geo_ok:
        dist = _haversine_m(existing_lat, existing_lon, candidate_lat, candidate_lon)
        if dist <= GEO_MATCH_RADIUS_M:
            score += _W_GEO
            parts.append("geo")

    if developer_match:
        score += _W_DEVELOPER
        parts.append("developer")

    addr_ok = address_match(existing_address, candidate_address)
    if addr_ok:
        score += _W_ADDRESS
        parts.append("address")

    return round(min(score, 1.0), 2), "+".join(parts)


async def record_source_link(
    complex_id: int, source: str, source_id: str, *,
    confidence: float, method: str, url: str | None = None,
    matched_by: str = "auto",
) -> str:
    """Пишет связь — auto (>= AUTO_MATCH_THRESHOLD) сразу в spine
    (complex_source_links); review (>= REVIEW_QUEUE_THRESHOLD, < auto) —
    в очередь на подтверждение (complex_source_link_candidates), НЕ в
    спайн, пока кто-то не approve; ниже REVIEW_QUEUE_THRESHOLD — не
    создаём ничего. Конфликт (source_id уже привязан к ДРУГОМУ complex_id)
    не перезаписывает существующую связь молча — тоже уходит в очередь
    (kind='conflict'), независимо от confidence нового варианта. Пары,
    которые уже руками отклонили (complex_source_link_rejections), больше
    не предлагаются вовсе.

    Возвращает: 'auto' | 'review' | 'conflict' | 'rejected' | 'skipped'."""
    if confidence < REVIEW_QUEUE_THRESHOLD:
        return "skipped"
    from bot.db.pg import fetchrow, execute

    rejected = await fetchrow(
        "SELECT 1 FROM complex_source_link_rejections WHERE source=$1 AND source_id=$2 AND complex_id=$3",
        source, str(source_id), complex_id)
    if rejected:
        return "rejected"

    existing = await fetchrow(
        "SELECT complex_id FROM complex_source_links WHERE source=$1 AND source_id=$2",
        source, str(source_id))
    if existing and existing["complex_id"] != complex_id:
        await execute("""
            INSERT INTO complex_source_link_candidates
                (complex_id, source, source_id, url, match_method, confidence, kind, conflict_with_complex_id)
            VALUES ($1, $2, $3, $4, $5, $6, 'conflict', $7)
            ON CONFLICT (source, source_id, complex_id) DO UPDATE SET
                confidence = EXCLUDED.confidence, match_method = EXCLUDED.match_method
        """, complex_id, source, str(source_id), url, method, confidence, existing["complex_id"])
        return "conflict"

    if is_auto_match(confidence):
        await execute("""
            INSERT INTO complex_source_links
                (complex_id, source, source_id, url, match_method, confidence, matched_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (source, source_id) DO NOTHING
        """, complex_id, source, str(source_id), url, method, confidence, matched_by)
        return "auto"

    await execute("""
        INSERT INTO complex_source_link_candidates
            (complex_id, source, source_id, url, match_method, confidence, kind)
        VALUES ($1, $2, $3, $4, $5, $6, 'review')
        ON CONFLICT (source, source_id, complex_id) DO UPDATE SET confidence = EXCLUDED.confidence
    """, complex_id, source, str(source_id), url, method, confidence)
    return "review"


async def approve_candidate(candidate_id: int, approved_by: str = "admin") -> None:
    """Подтвердить кандидата (review или conflict) — переносит связь в
    спайн. Для conflict: перезаписывает существующую связь (осознанно,
    не молча — через это действие)."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM complex_source_link_candidates WHERE id = $1", candidate_id)
    if not c:
        return
    await execute("""
        INSERT INTO complex_source_links
            (complex_id, source, source_id, url, match_method, confidence, matched_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (source, source_id) DO UPDATE SET
            complex_id = EXCLUDED.complex_id, url = EXCLUDED.url,
            match_method = EXCLUDED.match_method, confidence = EXCLUDED.confidence,
            matched_by = EXCLUDED.matched_by
    """, c["complex_id"], c["source"], c["source_id"], c["url"], c["match_method"],
        c["confidence"], approved_by)
    await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", candidate_id)


async def reject_candidate(candidate_id: int, rejected_by: str = "admin") -> None:
    """Отклонить кандидата — запоминает пару (память отклонений), больше
    не предложится тем же parser-прогоном."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM complex_source_link_candidates WHERE id = $1", candidate_id)
    if not c:
        return
    await execute("""
        INSERT INTO complex_source_link_rejections (source, source_id, complex_id, rejected_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (source, source_id, complex_id) DO NOTHING
    """, c["source"], c["source_id"], c["complex_id"], rejected_by)
    await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", candidate_id)


def is_auto_match(confidence: float) -> bool:
    return confidence >= AUTO_MATCH_THRESHOLD


def is_review_queue(confidence: float) -> bool:
    return REVIEW_QUEUE_THRESHOLD <= confidence < AUTO_MATCH_THRESHOLD
