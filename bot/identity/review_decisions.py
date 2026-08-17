"""bot/identity/review_decisions.py — задача 2026-08-17, "Property Identity
— photo evidence + admin review queue", часть E: данные и решения для
/admin/property-match-review (bot/admin_web.py). Тот же паттерн, что
bot/core/entity_resolution.py::approve_unit_candidate/reject_unit_candidate/
skip_unit_candidate + unit_match_gold_labels (migrations/051) — тут
property_match_review_log (migrations/088) играет ту же роль журнала.

## Explicit scope (задача, явно)

"Одна квартира" -> decision='accepted' ЗАПИСЫВАЕТСЯ (property_match_
candidates.status + property_match_review_log), НО property_listings/
properties НЕ меняются — НИКАКОГО физического merge в этом PR. skip
('Недостаточно данных') НЕ меняет status (кандидат снова всплывёт в
очереди — тот же принцип, что skip_unit_candidate)."""
from __future__ import annotations

import json

_QUEUE_FILTERS = frozenset({
    "unique_photo", "multi_method", "house_number_conflict", "price_conflict",
    "concurrent", "relist", "unreviewed", "rejected", "reviewed",
})

# Порядок словаря — порядок чипов в UI (задача E, фильтры перечислены в
# этом порядке в тексте задачи).
QUEUE_FILTER_LABELS = {
    "unique_photo": "⭐ сильное совпадение уникальных фото",
    "multi_method": "🔗 несколько corroborating methods",
    "house_number_conflict": "🏠 house-number conflict",
    "price_conflict": "💰 price conflict",
    "concurrent": "👥 concurrent duplicates",
    "relist": "🔁 relists",
    "unreviewed": "◻ непроверенные",
    "rejected": "✗ rejected",
    "reviewed": "✓ reviewed",
}


def _where_for_filters(filters: set[str]) -> tuple[str, list]:
    """filters -> (SQL-условие, params) — AND всех выбранных фильтров
    (задача не уточняет OR/AND между фильтрами; AND — более полезный
    дефолт для "сузить очередь", чем OR)."""
    clauses = ["1=1"]
    params: list = []
    if "rejected" in filters:
        clauses.append("pmc.status = 'rejected'")
    if "unreviewed" in filters:
        clauses.append("NOT EXISTS (SELECT 1 FROM property_match_review_log l WHERE l.candidate_id = pmc.candidate_id)")
    if "reviewed" in filters:
        clauses.append("EXISTS (SELECT 1 FROM property_match_review_log l WHERE l.candidate_id = pmc.candidate_id)")
    if "unique_photo" in filters:
        clauses.append("COALESCE(pcpe.shared_unit_specific_count, 0) > 0")
    if "multi_method" in filters:
        clauses.append("jsonb_array_length(COALESCE(pmc.evidence->'corroborating_methods', '[]'::jsonb)) > 1")
    if "house_number_conflict" in filters:
        clauses.append("pmc.conflict_reasons::text LIKE '%house_number mismatch%'")
    if "price_conflict" in filters:
        clauses.append("pmc.conflict_reasons::text LIKE '%price differs%'")
    if "concurrent" in filters:
        clauses.append("pmc.relationship_type = 'concurrent_duplicate'")
    if "relist" in filters:
        clauses.append("pmc.relationship_type = 'relist'")
    return " AND ".join(clauses), params


# Сортировка очереди — задача, явно, приоритет по убыванию:
#   1. unique photo match + другие сигналы;
#   2. одна и та же пара подтверждается несколькими методами;
#   3. кандидаты с конфликтом адреса/цены;
#   4. остальные.
_PRIORITY_SQL = """
    CASE
        WHEN COALESCE(pcpe.shared_unit_specific_count, 0) > 0
             AND jsonb_array_length(COALESCE(pmc.evidence->'corroborating_methods', '[]'::jsonb)) > 1 THEN 1
        WHEN jsonb_array_length(COALESCE(pmc.evidence->'corroborating_methods', '[]'::jsonb)) > 1 THEN 2
        WHEN pmc.conflict_reasons IS NOT NULL THEN 3
        ELSE 4
    END
"""

_BASE_SELECT = f"""
    SELECT pmc.candidate_id, pmc.listing_id, pmc.candidate_property_id, pmc.match_method,
           pmc.match_score, pmc.relationship_type, pmc.evidence, pmc.conflict_reasons,
           pmc.matcher_version, pmc.status, pmc.reviewed_at, pmc.reviewed_by,
           pcpe.shared_unit_specific_count, pcpe.shared_common_count, pcpe.exact_shared_count,
           pcpe.perceptual_shared_count, pcpe.ai_similar_count, pcpe.max_similarity,
           pcpe.matched_photos, pcpe.processing_status AS photo_processing_status,
           {_PRIORITY_SQL} AS priority_tier
    FROM property_match_candidates pmc
    LEFT JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id
"""


def normalize_filters(raw: list[str] | None) -> set[str]:
    if not raw:
        return set()
    return {f for f in raw if f in _QUEUE_FILTERS}


async def queue_counts(filters: set[str]) -> dict:
    """Счётчики по КАЖДОМУ фильтру (не только выбранным) — для чипов
    "N кандидатов" в UI, независимо друг от друга (не кумулятивно с
    ВЫБРАННЫМИ фильтрами — иначе цифры на невыбранных чипах вводили бы в
    заблуждение, показывая "0" на всём, что несовместимо с уже выбранным)."""
    from bot.db.pg import fetchval

    total = await fetchval("SELECT count(*) FROM property_match_candidates")
    counts = {"total": total}
    for f in sorted(_QUEUE_FILTERS):
        where, params = _where_for_filters({f})
        counts[f] = await fetchval(
            f"SELECT count(*) FROM property_match_candidates pmc "
            f"LEFT JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id "
            f"WHERE {where}",
            *params,
        )
    return counts


async def _resolve_pair_listings(listing_id: str, candidate_property_id: int) -> tuple[str, str | None]:
    from bot.db.pg import fetch
    rows = await fetch(
        """
        SELECT al.id FROM property_listings pl
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = $1
        ORDER BY al.last_seen DESC NULLS LAST, al.id
        """,
        candidate_property_id,
    )
    return listing_id, (rows[0]["id"] if rows else None)


async def _listing_card(listing_id: str | None) -> dict | None:
    if listing_id is None:
        return None
    from bot.db.pg import fetchrow
    row = await fetchrow(
        """
        SELECT al.id, al.url, al.address, al.complex_name, al.rooms, al.area, al.floor, al.floors_total,
               al.price, al.seller_name, al.is_owner, al.first_seen, al.last_seen, al.archived_at,
               al.is_active, al.photos::text AS photos_json
        FROM apartment_listings al WHERE al.id = $1
        """,
        listing_id,
    )
    if row is None:
        return None
    card = dict(row)
    try:
        card["photos"] = json.loads(card.pop("photos_json") or "[]")
    except (TypeError, ValueError):
        card["photos"] = []
        card.pop("photos_json", None)
    return card


def _explain(row: dict) -> str:
    """Простое человекочитаемое объяснение, почему пара попала на
    проверку — задача E: "почему пара попала на проверку", простым
    языком, не жаргоном evidence JSONB."""
    parts = []
    method_names = {"exact_hash": "точный адрес+этаж+площадь", "fuzzy": "похожий адрес+этаж+площадь",
                     "dedup_listings": "уже помечены как дубли"}
    parts.append(f"Найдены как {method_names.get(row['match_method'], row['match_method'])}"
                 f" (score={float(row['match_score']):.2f}).")
    corroborating = (row.get("evidence") or {}).get("corroborating_methods") or []
    extra = [m for m in corroborating if m != row["match_method"]]
    if extra:
        parts.append(f"Дополнительно согласны: {', '.join(extra)}.")
    if (row.get("shared_unit_specific_count") or 0) > 0:
        parts.append(f"⭐ {row['shared_unit_specific_count']} совпадающих уникальных "
                      f"интерьерных/видовых фото — сильный сигнал одной и той же квартиры.")
    if (row.get("shared_common_count") or 0) > 0 and not row.get("shared_unit_specific_count"):
        parts.append(f"{row['shared_common_count']} совпадающих фото, но это план/фасад/рендер/общая "
                      f"зона — слабый сигнал сам по себе, не подтверждает одну квартиру.")
    reasons = row.get("conflict_reasons")
    if reasons:
        parts.append(f"⚠ Есть расхождения: {'; '.join(reasons)}.")
    if row.get("relationship_type") == "concurrent_duplicate":
        parts.append("Объявления были активны одновременно — это НЕ конфликт (одну квартиру "
                      "могут продавать несколько агентов).")
    return " ".join(parts)


async def get_candidate_detail(candidate_id: int) -> dict | None:
    from bot.db.pg import fetchrow
    row = await fetchrow(_BASE_SELECT + " WHERE pmc.candidate_id = $1", candidate_id)
    if row is None:
        return None
    return await _hydrate(dict(row))


async def get_next_candidate(filters: set[str]) -> dict | None:
    """Первая по приоритету/score пара, ЕЩЁ не решённая ('pending')
    среди выбранных фильтров — если оператор явно попросил --status
    'rejected'/'reviewed' фильтр, статус кандидата уже отражён в WHERE
    через _where_for_filters, не форсируем pending поверх этого."""
    from bot.db.pg import fetchrow

    where, params = _where_for_filters(filters)
    if not ({"rejected", "reviewed"} & filters):
        where += " AND pmc.status = 'pending'"
    sql = _BASE_SELECT + f" WHERE {where} ORDER BY priority_tier ASC, pmc.match_score DESC, pmc.candidate_id ASC LIMIT 1"
    row = await fetchrow(sql, *params)
    if row is None:
        return None
    return await _hydrate(dict(row))


async def _hydrate(row: dict) -> dict:
    import json as _json

    evidence = row.get("evidence")
    if isinstance(evidence, str):
        evidence = _json.loads(evidence)
    row["evidence"] = evidence or {}
    conflict_reasons = row.get("conflict_reasons")
    if isinstance(conflict_reasons, str):
        conflict_reasons = _json.loads(conflict_reasons)
    row["conflict_reasons"] = conflict_reasons or []
    matched_photos = row.get("matched_photos")
    if isinstance(matched_photos, str):
        matched_photos = _json.loads(matched_photos)
    row["matched_photos"] = matched_photos or []

    lid_a, lid_b = await _resolve_pair_listings(row["listing_id"], row["candidate_property_id"])
    row["listing_a"] = await _listing_card(lid_a)
    row["listing_b"] = await _listing_card(lid_b)
    row["corroborating_methods"] = row["evidence"].get("corroborating_methods") or [row["match_method"]]
    row["explanation"] = _explain(row)
    a, b = row["listing_a"], row["listing_b"]
    if a and b and a.get("first_seen") and b.get("first_seen"):
        a_end = a.get("archived_at") or a.get("last_seen")
        b_end = b.get("archived_at") or b.get("last_seen")
        row["simultaneously_active"] = a["first_seen"] <= (b_end or a["first_seen"]) and \
            b["first_seen"] <= (a_end or b["first_seen"])
    else:
        row["simultaneously_active"] = None
    return row


async def record_review_decision(candidate_id: int, decision: str, reviewed_by: str,
                                  comment: str | None = None) -> dict | None:
    """decision: 'accepted' | 'rejected' | 'skip'. Задача, явно: "В этом PR
    кнопка «Одна квартира» только записывает решение accepted, но НЕ
    переносит property_listings и НЕ удаляет properties" — эта функция
    НИКОГДА не трогает property_listings/properties, только property_
    match_candidates.status/reviewed_at/reviewed_by + append-only property_
    match_review_log (журнал решений, включая skip — задача: "Решение
    записывать с reviewed_at/reviewed_by/comment/matcher_version/evidence
    snapshot")."""
    if decision not in ("accepted", "rejected", "skip"):
        raise ValueError(f"decision должен быть accepted|rejected|skip, получено {decision!r}")

    from bot.db.pg import execute, fetchrow

    c = await fetchrow(
        "SELECT candidate_id, listing_id, candidate_property_id, evidence, matcher_version "
        "FROM property_match_candidates WHERE candidate_id = $1", candidate_id,
    )
    if c is None:
        return None

    await execute(
        """
        INSERT INTO property_match_review_log
            (candidate_id, listing_id, candidate_property_id, decision, comment,
             matcher_version, evidence_snapshot, reviewed_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        candidate_id, c["listing_id"], c["candidate_property_id"], decision, comment,
        c["matcher_version"], c["evidence"], reviewed_by,
    )

    if decision in ("accepted", "rejected"):
        await execute(
            "UPDATE property_match_candidates SET status = $2, reviewed_at = now(), reviewed_by = $3 "
            "WHERE candidate_id = $1",
            candidate_id, decision, reviewed_by,
        )
    # skip — status НЕ меняем (тот же принцип, что skip_unit_candidate:
    # "посмотрел, не уверен" != решение, кандидат снова всплывёт в очереди).

    return {"candidate_id": candidate_id, "decision": decision}
