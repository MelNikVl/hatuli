"""
Entity resolution, фаза 1 (ЖК) — см. docs/entity_resolution_plan.md.

Единый постоянный ID объекта — complexes.id (PK, без семантики). Этот
модуль:
  - генерирует человеко-читаемый code (JK-000123) для отображения в UI;
  - скорит уверенность связи "источник -> ЖК" по сигналам (имя/гео/
    застройщик) и пишет её в complex_source_links (spine);
  - решает auto-match vs review-queue vs no-match по порогам confidence.

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

# Веса сигналов (см. план: "точное совпадение имени — базовый сигнал,
# + гео-близость резко поднимает уверенность, + застройщик — доп. сигнал").
_W_NAME_EXACT = 0.6
_W_GEO = 0.25
_W_DEVELOPER = 0.15
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


def score_match(
    name_exact: bool,
    existing_lat: float | None = None, existing_lon: float | None = None,
    candidate_lat: float | None = None, candidate_lon: float | None = None,
    developer_match: bool | None = None,
) -> tuple[float, str]:
    """Считает confidence + human-readable match_method по сигналам,
    которые реально удалось проверить (сигнал без данных просто не
    участвует — не штрафуем и не выдумываем)."""
    if not name_exact:
        return 0.0, "no_match"
    score = _W_NAME_EXACT
    parts = ["name_exact"]

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

    return round(min(score, 1.0), 2), "+".join(parts)


async def record_source_link(
    complex_id: int, source: str, source_id: str, *,
    confidence: float, method: str, url: str | None = None,
    matched_by: str = "auto",
) -> None:
    """Пишет связь в spine, если confidence проходит хотя бы порог
    review-queue (ниже — не создаём запись вовсе, см. план). Существующая
    связь (source, source_id) не перезаписывается — matched_at/confidence
    остаются от первого раза (аудит-трейл), если только ЖК не поменялся."""
    if confidence < REVIEW_QUEUE_THRESHOLD:
        return
    from bot.db.pg import execute
    await execute("""
        INSERT INTO complex_source_links
            (complex_id, source, source_id, url, match_method, confidence, matched_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (source, source_id) DO UPDATE SET
            complex_id = EXCLUDED.complex_id
        WHERE complex_source_links.complex_id IS DISTINCT FROM EXCLUDED.complex_id
    """, complex_id, source, str(source_id), url, method, confidence, matched_by)


def is_auto_match(confidence: float) -> bool:
    return confidence >= AUTO_MATCH_THRESHOLD


def is_review_queue(confidence: float) -> bool:
    return REVIEW_QUEUE_THRESHOLD <= confidence < AUTO_MATCH_THRESHOLD
