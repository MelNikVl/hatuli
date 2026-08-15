"""Сборка данных + ручные действия для /admin/kzk-registry (задача
2026-08-15, коммит 4) — «роут не знает SQL», тот же принцип, что уже
применён в Фазе B п.5 (bot/core/listing_detail.py/complex_location_
detail.py). Роут просто зовёт эти функции и переводит результат/
исключения в HTTP.

Действия (confirm/reject/manual-match) — ручное ревью review-кандидатов
из kzk_registry_match.py (Уровень 1). **Известное ограничение**: reject
только очищает developer_id/method (возврат в unresolved) — при
повторном ручном прогоне kzk_registry_match.py тот же неверный кандидат
может быть предложен снова (нет отдельной памяти "уже отклонено", как
у complex_source_link_rejections в bot/core/entity_resolution.py) — не
реализовано здесь, т.к. matching не автоматизирован таймером (только
ручной запуск, коммит 3), риск повторной ошибки низкий; если matching
когда-нибудь станет таймером — эту память стоит добавить."""
from __future__ import annotations

import json


class KzkRegistryNotFound(Exception):
    """kzk_registry.id не найден."""


async def build_kzk_registry_summary() -> dict:
    from bot.db.pg import fetchrow
    row = await fetchrow("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE developer_match_method IN ('bin', 'name_fuzzy_auto', 'manual_confirmed')) AS resolved,
            COUNT(*) FILTER (WHERE developer_match_method = 'name_fuzzy_review') AS review_pending,
            COUNT(*) FILTER (WHERE developer_id IS NULL AND developer_match_method IS NULL) AS unresolved,
            COUNT(*) FILTER (WHERE is_blacklisted) AS blacklisted
        FROM kzk_registry
    """)
    return dict(row) if row else {"total": 0, "resolved": 0, "review_pending": 0, "unresolved": 0, "blacklisted": 0}


async def list_kzk_registry(
    developer_query: str | None = None,
    match_status: str | None = None,   # 'resolved' | 'review' | 'unresolved' | None (все)
    blacklisted_only: bool = False,
) -> list[dict]:
    from bot.db.pg import fetch

    where = ["TRUE"]
    params: list = []

    if developer_query:
        params.append(f"%{developer_query}%")
        where.append(f"(k.developer_brand ILIKE ${len(params)} OR k.developer_legal ILIKE ${len(params)})")

    if match_status == "resolved":
        where.append("k.developer_match_method IN ('bin', 'name_fuzzy_auto', 'manual_confirmed')")
    elif match_status == "review":
        where.append("k.developer_match_method = 'name_fuzzy_review'")
    elif match_status == "unresolved":
        where.append("k.developer_id IS NULL AND k.developer_match_method IS NULL")

    if blacklisted_only:
        where.append("k.is_blacklisted")

    rows = await fetch(f"""
        SELECT k.id, k.bin, k.developer_legal, k.developer_brand, k.cities, k.objects_count,
               k.zhk_count, k.warranty_scheme, k.is_blacklisted, k.in_registry,
               k.developer_id, k.developer_match_method, k.zhk_matches, k.phone,
               k.source_snapshot_date, k.fetched_at,
               d.name AS matched_developer_name
        FROM kzk_registry k
        LEFT JOIN developers d ON d.id = k.developer_id
        WHERE {' AND '.join(where)}
        ORDER BY k.is_blacklisted DESC, k.developer_match_method NULLS FIRST, k.developer_brand
    """, *params)

    out = []
    for r in rows:
        d = dict(r)
        for key in ("cities", "zhk_matches"):
            v = d.get(key)
            if isinstance(v, str):
                try:
                    d[key] = json.loads(v)
                except ValueError:
                    d[key] = None
        out.append(d)
    return out


async def confirm_match(kzk_id: int) -> None:
    """Подтвердить review-кандидата — переводит в 'manual_confirmed'
    (та же семантика, что у 'name_fuzzy_auto', просто человек, не порог)."""
    from bot.db.pg import execute, fetchrow
    row = await fetchrow("SELECT id, developer_id FROM kzk_registry WHERE id=$1", kzk_id)
    if not row:
        raise KzkRegistryNotFound(kzk_id)
    if row["developer_id"] is None:
        raise ValueError("нет кандидата для подтверждения (developer_id пуст)")
    await execute(
        "UPDATE kzk_registry SET developer_match_method='manual_confirmed' WHERE id=$1", kzk_id)


async def reject_match(kzk_id: int) -> None:
    """Отклонить текущий матч (review ИЛИ уже применённый) — возврат в
    unresolved. См. докстринг модуля про известное ограничение (нет
    памяти отклонения)."""
    from bot.db.pg import execute, fetchrow
    row = await fetchrow("SELECT id FROM kzk_registry WHERE id=$1", kzk_id)
    if not row:
        raise KzkRegistryNotFound(kzk_id)
    await execute(
        "UPDATE kzk_registry SET developer_id=NULL, developer_match_method=NULL WHERE id=$1", kzk_id)


async def set_manual_match(kzk_id: int, developer_id: int) -> None:
    """Ручной матч на КОНКРЕТНОГО застройщика (не из предложенных
    кандидатов) — например, когда fuzzy ничего не нашла (unresolved),
    но админ знает связь сам."""
    from bot.db.pg import execute, fetchrow
    row = await fetchrow("SELECT id FROM kzk_registry WHERE id=$1", kzk_id)
    if not row:
        raise KzkRegistryNotFound(kzk_id)
    dev = await fetchrow("SELECT id FROM developers WHERE id=$1", developer_id)
    if not dev:
        raise ValueError(f"developers.id={developer_id} не найден")
    await execute(
        "UPDATE kzk_registry SET developer_id=$2, developer_match_method='manual_confirmed' WHERE id=$1",
        kzk_id, developer_id)
