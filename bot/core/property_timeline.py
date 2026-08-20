"""bot/core/property_timeline.py — Property Timeline, Phase 1 (задача
2026-08-20, "Property Timeline как первый продуктовый слой поверх Property
Identity"). Единая, честная история ОДНОЙ физической квартиры (properties.
property_id) — union всех listing_id, когда-либо связанных с ней через
property_listings (migrations/084), плюс всё, что честно реконструируется
из уже существующих источников (price_history, listing_archive_history,
property_match_candidates + property_candidate_photo_evidence).

## Аудит схемы (перед реализацией, задача явно требует)

property_id -> property_listings -> apartment_listings — КАНОНИЧЕСКИЙ путь
(migrations/083, 084). property_identity.linked_property_id из постановки
задачи В СХЕМЕ НЕ СУЩЕСТВУЕТ — не используется нигде в этом модуле.

Источники (проверено чтением миграций/кода, не предположено):
  - properties (083, 086) — identity_status ('provisional'|'confirmed'|
    'merged', ТОЛЬКО эти три значения — CHECK constraint), first_seen_at/
    last_seen_at (running MIN/MAX по linked listing'ам, поддерживается
    bot/identity/property_linker.py при bootstrap/incremental link).
  - property_listings (084) — property_id -> listing_id, many-to-one,
    UNIQUE(listing_id). link_method/confidence/matcher_version/linked_at —
    как и когда ИДЕНТИЧНОСТЬ была установлена (это ОТДЕЛЬНЫЙ факт от
    "когда объявление появилось на рынке", см. events ниже).
  - apartment_listings (000_core_tables, 012, 014, 028, 089) — сама
    market-факта: first_seen/last_seen (ingestion — когда скрапер
    видел/чекал), archived_at (event time — начало ТЕКУЩЕГО периода
    архивации, migrations/089 докстринг), archive_reason
    ('confirmed_gone'|'archived_badge'), seller_name (СЫРОЙ, ненадёжный
    атрибут ОБЪЯВЛЕНИЯ — см. "observed_seller_name" ниже), price (ТЕКУЩАЯ
    цена, не история).
  - price_history (001_alerts, 008) — event-лог: (listing_id, old_price,
    new_price, changed_at). changed_at — когда бот ОБНАРУЖИЛ изменение
    (polling-based ingestion time), не гарантированно точное astronomical
    event time смены цены продавцом, но это лучшее, что есть — честно
    трактуем как event time с этой оговоркой.
  - listing_archive_history (090) — append-only, ОДНА строка на ЗАВЕРШЁННЫЙ
    цикл архивация->реактивация: archived_at/archive_reason — СТАРЫЕ
    значения (когда начался ТОТ период архивации), reactivated_at — когда
    реактивация была ПОДТВЕРЖДЕНА (event time обнаружения, см. bot/core/
    archive_check.py::_confirm_reactivation).
  - property_match_candidates (086) + property_candidate_photo_evidence
    (088) — evidence для property_identity_link/photo_evidence_observed
    событий. НЕ используется для физического связывания (задача явно:
    "НЕ создавать Unified Seller Profile", "НЕ merge" — read-only здесь).

НЕ существует в схеме и поэтому НЕ используется этим модулем:
  - "sold"/сделки — ни одной таблицы с подтверждённой продажей нет,
    поэтому событие "sold" и метрика time_between_sales НЕ реализуются
    (задача явно это запрещает — "Не называй событие sold").
  - unified seller identity — seller_name хранится ТОЛЬКО как атрибут
    объявления (apartment_listings.seller_name), без отдельной таблицы
    физических продавцов с надёжным ключом (seller_profiles.seller_name
    — нормализованное ИМЯ, не проверенная identity, см. её докстринг в
    migrations/077) — поэтому "observed_seller_name", не "seller_id".

## Events — что честно доказуемо, что нет

Реализованы ТОЛЬКО событий, для которых есть прямой источник в схеме
(см. _build_events ниже): listing_first_seen, new_listing_linked,
listing_relist, listing_archived, listing_reactivated, price_change,
seller_observed_change, property_identity_link, photo_evidence_observed.

property_identity_link — ОТДЕЛЬНО от listing_first_seen/listing_relist:
это факт СИСТЕМЫ ИДЕНТИЧНОСТИ (property_listings.linked_at — когда линкер/
backfill записал связь), не факт РЫНКА (apartment_listings.first_seen —
когда объявление появилось). Эти два timestamp'а МОГУТ отличаться
(backfill мог отработать днями позже появления объявления) — схлопывать
их в одно событие означало бы потерять эту разницу.

new_listing_linked/listing_relist — РЫНОЧНЫЙ факт: первый (по first_seen)
listing под property -> new_listing_linked, каждый следующий ->
listing_relist (задача явно просит "relist history" как фундамент).
listing_first_seen эмитится ДЛЯ КАЖДОГО listing'а отдельно (raw-факт
"мы начали наблюдать это объявление") — намеренно рядом с new_listing_
linked/listing_relist в один и тот же timestamp для не-первого listing'а:
это ДВА разных вопроса ("когда объявление появилось" vs "это релист?"),
не дублирование одного факта под двумя именами.

seller_observed_change — ТОЛЬКО между РАЗНЫМИ listing'ами одной property
(apartment_listings не хранит историю изменений seller_name ВНУТРИ одного
listing_id — это статичное поле, обновляемое при перескрапе, без event-
лога). Сравниваются ПОСЛЕДОВАТЕЛЬНЫЕ (по first_seen) listing'и с
известным seller_name — если различается, событие на first_seen более
позднего listing'а (единственный момент времени, которым мы честно можем
датировать наблюдение).

НЕ реализовано (задача явно запрещает выдумывать):
  - "sold" — нет данных о сделках.
  - time_between_sales — нет подтверждённых продаж.
  - seller identity change ВНУТРИ одного listing_id — нет history-лога
    этого поля.

## Метрики — что честно, что сознательно пропущено

true DOM (задача, явно): НЕ сумма длительностей listing'ов (задвоила бы
concurrent duplicate listings одной квартиры). Метрика называется
observed_market_days (НЕ true_dom_days — семантика "правда DOM"
предполагала бы подтверждённую продажу, которой у нас нет) — union
активных интервалов ([first_seen, archived_at|last_seen] на listing, с
разрывами по listing_archive_history), см. _merge_intervals/_listing_
intervals.

price_volatility — СОЗНАТЕЛЬНО НЕ реализована в Phase 1. Задача прямо
предупреждает: "не использовать stddev сырых цен разных listing snapshots
без объяснения" и делает её реализацию УСЛОВНОЙ ("если реализуешь").
Обязательный список метрик (задача, п.4) volatility не включает — трогать
её сейчас значило бы придумывать методологию под давлением дедлайна,
именно то, от чего задача предостерегает. Оставлено для отдельного PR,
когда методология (что считаем: pct-change между точками unified price
trajectory? между listing'ами? окно?) обсуждена отдельно.
"""
from __future__ import annotations

import re
from datetime import datetime


# ── нормализация seller_name (та же формула, что property_linker.py's
# _normalize_seller_name/seller_profile_snapshot.py's _normalize_name —
# локально продублирована по тому же принципу, что уже задокументирован
# в property_linker.py: "не импортируем repo-root скрипт из bot/-пакета,
# сама функция — одна строка", тот же довод применим и здесь для
# избежания импорта private-функции из bot.identity в bot.core) ──────────
def _normalize_seller_name(raw: str | None) -> str | None:
    if not raw:
        return None
    return re.sub(r"\s+", " ", raw.strip()).lower()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ── интервалы наблюдаемой активности одного listing'а ───────────────────

def _listing_intervals(listing: dict, archive_history: list[dict]) -> list[tuple[datetime, datetime]]:
    """[start, end) сегменты, когда listing БЫЛ активен/наблюдался —
    первый seen -> первая архивация (если была) -> реактивация -> ... ->
    текущий конец (archived_at, если СЕЙЧАС архивирован, иначе last_seen —
    последнее ПОДТВЕРЖДЁННОЕ наблюдение, не wall-clock now(): now()
    менялся бы при каждом вызове, last_seen — зафиксированный факт).
    Испорченные (end < start) сегменты пропускаются, а не считаются
    отрицательной длительностью."""
    first_seen = listing.get("first_seen")
    if first_seen is None:
        return []

    history = sorted(
        (h for h in archive_history if h.get("archived_at") is not None and h.get("reactivated_at") is not None),
        key=lambda h: (h["archived_at"], h["reactivated_at"]),
    )

    segments: list[tuple[datetime, datetime]] = []
    cursor = first_seen
    for h in history:
        end = h["archived_at"]
        if end >= cursor:
            segments.append((cursor, end))
        if h["reactivated_at"] > cursor:
            cursor = h["reactivated_at"]

    final_end = listing.get("archived_at") or listing.get("last_seen")
    if final_end is not None and final_end >= cursor:
        segments.append((cursor, final_end))
    return segments


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Стандартный union пересекающихся/смежных интервалов — concurrent
    duplicate listings одной property дают ПЕРЕСЕКАЮЩИЕСЯ сегменты, здесь
    они схлопываются в один, не суммируются дважды (задача, явно, п.4)."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def _observed_market_days(listings: list[dict], archive_history_by_listing: dict[str, list[dict]]) -> float | None:
    intervals: list[tuple[datetime, datetime]] = []
    for listing in listings:
        intervals.extend(_listing_intervals(listing, archive_history_by_listing.get(listing["listing_id"], [])))
    merged = _merge_intervals(intervals)
    if not merged:
        return None
    total_seconds = sum((end - start).total_seconds() for start, end in merged)
    return round(total_seconds / 86400, 2)


# ── унифицированная price trajectory (для initial/latest/min/max/pct) ───

def _price_trajectory(listings: list[dict], price_history_by_listing: dict[str, list[dict]]) -> list[tuple[datetime, int]]:
    """Один хронологический список (timestamp, price) точек по ВСЕЙ
    property — НЕ raw snapshot'ы разных listing'ов вперемешку (задача,
    явно предостерегает от этого для volatility, тот же принцип и здесь
    для initial/latest/min/max): на listing начальная точка — старейшая
    известная цена (первый price_history.old_price, если есть история
    изменений; иначе текущая apartment_listings.price как единственное,
    что вообще известно — честная оговорка, не выдуманная "историческая"
    цена), дальше — каждое реальное изменение (price_history.new_price)."""
    points: list[tuple[datetime, int]] = []
    for listing in listings:
        first_seen = listing.get("first_seen")
        if first_seen is None:
            continue
        history = price_history_by_listing.get(listing["listing_id"], [])
        initial_price = history[0]["old_price"] if history else listing.get("price")
        if initial_price is not None:
            points.append((first_seen, initial_price))
        for h in history:
            if h.get("changed_at") is not None and h.get("new_price") is not None:
                points.append((h["changed_at"], h["new_price"]))
    points.sort(key=lambda p: p[0])
    return points


def _compute_metrics(listings: list[dict], price_history_by_listing: dict[str, list[dict]],
                      archive_history_by_listing: dict[str, list[dict]]) -> dict:
    first_seens = [l["first_seen"] for l in listings if l.get("first_seen") is not None]
    last_observed = [l.get("archived_at") or l.get("last_seen") for l in listings]
    last_observed = [d for d in last_observed if d is not None]

    first_seen_at = min(first_seens) if first_seens else None
    last_seen_at = max(last_observed) if last_observed else None
    observed_span_days = round((last_seen_at - first_seen_at).total_seconds() / 86400, 2) \
        if first_seen_at is not None and last_seen_at is not None else None

    seller_names = {_normalize_seller_name(l.get("seller_name")) for l in listings}
    seller_names.discard(None)

    trajectory = _price_trajectory(listings, price_history_by_listing)
    initial_price = trajectory[0][1] if trajectory else None
    latest_price = trajectory[-1][1] if trajectory else None
    min_price = min(p for _, p in trajectory) if trajectory else None
    max_price = max(p for _, p in trajectory) if trajectory else None
    total_price_change_pct = round((latest_price - initial_price) / initial_price * 100, 2) \
        if initial_price else None
    price_change_count = sum(len(v) for v in price_history_by_listing.values())

    return {
        "first_seen_at": _iso(first_seen_at),
        "last_seen_at": _iso(last_seen_at),
        "observed_span_days": observed_span_days,
        "listing_count": len(listings),
        "relist_count": max(len(listings) - 1, 0),
        "unique_observed_seller_names": len(seller_names),
        "initial_price": initial_price,
        "latest_price": latest_price,
        "min_price": min_price,
        "max_price": max_price,
        "total_price_change_pct": total_price_change_pct,
        "price_change_count": price_change_count,
        "observed_market_days": _observed_market_days(listings, archive_history_by_listing),
    }


# ── events ────────────────────────────────────────────────────────────

# Фиксированный порядок типов — вторичный ключ сортировки событий,
# делает порядок ДЕТЕРМИНИРОВАННЫМ на событиях с одинаковым timestamp
# (тест, явно требуется задачей п.9): без него порядок двух событий с
# одним и тем же timestamp зависел бы от порядка append() ниже, который
# сам по себе не гарантирован между разными источниками (price_history/
# archive_history/listings читаются отдельными запросами).
_EVENT_TYPE_ORDER = {
    "property_identity_link": 0,
    "new_listing_linked": 1,
    "listing_first_seen": 2,
    "listing_relist": 3,
    "seller_observed_change": 4,
    "price_change": 5,
    "listing_archived": 6,
    "listing_reactivated": 7,
    "photo_evidence_observed": 8,
}


def _event_sort_key(event: dict) -> tuple:
    return (
        event["timestamp"] or "",
        _EVENT_TYPE_ORDER.get(event["type"], 99),
        event.get("listing_id") or "",
    )


def _build_events(listings: list[dict], price_history_by_listing: dict[str, list[dict]],
                   archive_history_by_listing: dict[str, list[dict]],
                   photo_evidence_rows: list[dict]) -> list[dict]:
    events: list[dict] = []

    # property_identity_link — факт СИСТЕМЫ ИДЕНТИЧНОСТИ, на linked_at.
    for listing in listings:
        events.append({
            "timestamp": _iso(listing.get("linked_at")),
            "type": "property_identity_link",
            "listing_id": listing["listing_id"],
            "before": None,
            "after": {"property_id": listing.get("property_id")},
            "evidence": {
                "link_method": listing.get("link_method"),
                "confidence": float(listing["confidence"]) if listing.get("confidence") is not None else None,
                "matcher_version": listing.get("matcher_version"),
            },
        })

    # listing_first_seen / new_listing_linked / listing_relist — РЫНОЧНЫЕ
    # факты, на apartment_listings.first_seen. Порядок "кто первый" —
    # по first_seen (listings уже отсортированы вызывающим кодом).
    by_first_seen = [l for l in listings if l.get("first_seen") is not None]
    prev_listing_id = None
    for i, listing in enumerate(by_first_seen):
        events.append({
            "timestamp": _iso(listing["first_seen"]),
            "type": "listing_first_seen",
            "listing_id": listing["listing_id"],
            "before": None,
            "after": {"price": listing.get("price"), "seller_name": listing.get("seller_name")},
            "evidence": {"address": listing.get("address"), "complex_name": listing.get("complex_name")},
        })
        if i == 0:
            events.append({
                "timestamp": _iso(listing["first_seen"]),
                "type": "new_listing_linked",
                "listing_id": listing["listing_id"],
                "before": None,
                "after": listing["listing_id"],
                "evidence": {"note": "первый по времени listing, наблюдаемый под этой property"},
            })
        else:
            events.append({
                "timestamp": _iso(listing["first_seen"]),
                "type": "listing_relist",
                "listing_id": listing["listing_id"],
                "before": prev_listing_id,
                "after": listing["listing_id"],
                "evidence": {"previous_listing_id": prev_listing_id},
            })
        prev_listing_id = listing["listing_id"]

    # listing_archived — на archived_at (текущий период архивации).
    for listing in listings:
        if listing.get("archived_at") is not None:
            events.append({
                "timestamp": _iso(listing["archived_at"]),
                "type": "listing_archived",
                "listing_id": listing["listing_id"],
                "before": "active",
                "after": "archived",
                "evidence": {"archive_reason": listing.get("archive_reason")},
            })

    # listing_reactivated — из listing_archive_history, на reactivated_at.
    for listing_id, rows in archive_history_by_listing.items():
        for h in rows:
            events.append({
                "timestamp": _iso(h.get("reactivated_at")),
                "type": "listing_reactivated",
                "listing_id": listing_id,
                "before": {"archived_at": _iso(h.get("archived_at")), "archive_reason": h.get("archive_reason")},
                "after": "active",
                "evidence": {},
            })

    # price_change — из price_history, по КАЖДОМУ реальному изменению.
    for listing_id, rows in price_history_by_listing.items():
        for h in rows:
            events.append({
                "timestamp": _iso(h.get("changed_at")),
                "type": "price_change",
                "listing_id": listing_id,
                "before": h.get("old_price"),
                "after": h.get("new_price"),
                "evidence": {},
            })

    # seller_observed_change — ТОЛЬКО между разными listing'ами (см.
    # докстринг модуля — нет history-лога seller_name внутри listing_id).
    prev_seller_raw, prev_seller_norm, prev_listing_id = None, None, None
    for listing in by_first_seen:
        seller_raw = listing.get("seller_name")
        seller_norm = _normalize_seller_name(seller_raw)
        if seller_norm is not None:
            if prev_seller_norm is not None and seller_norm != prev_seller_norm:
                events.append({
                    "timestamp": _iso(listing["first_seen"]),
                    "type": "seller_observed_change",
                    "listing_id": listing["listing_id"],
                    "before": prev_seller_raw,
                    "after": seller_raw,
                    "evidence": {"previous_listing_id": prev_listing_id, "listing_id": listing["listing_id"]},
                })
            prev_seller_raw, prev_seller_norm = seller_raw, seller_norm
        prev_listing_id = listing["listing_id"]

    # photo_evidence_observed — evidence event, НЕ raw фото-строки (задача,
    # явно: "не вставлять тысячи photo rows в timeline"). Только УЖЕ
    # посчитанные ('ok') evidence-строки.
    for row in photo_evidence_rows:
        if row.get("processing_status") != "ok":
            continue
        events.append({
            "timestamp": _iso(row.get("computed_at")),
            "type": "photo_evidence_observed",
            "listing_id": row.get("listing_id"),
            "before": None,
            "after": None,
            "evidence": {
                "candidate_id": row.get("candidate_id"),
                "match_method": row.get("match_method"),
                "relationship_type": row.get("relationship_type"),
                "candidate_status": row.get("status"),
                "exact_shared_count": row.get("exact_shared_count"),
                "perceptual_shared_count": row.get("perceptual_shared_count"),
                "ai_similar_count": row.get("ai_similar_count"),
                "shared_unit_specific_count": row.get("shared_unit_specific_count"),
                "shared_common_count": row.get("shared_common_count"),
                "max_similarity": float(row["max_similarity"]) if row.get("max_similarity") is not None else None,
                "model_version": row.get("model_version"),
            },
        })

    events = [e for e in events if e["timestamp"] is not None]
    events.sort(key=_event_sort_key)
    return events


# ── identity safety (п.5 задачи) ─────────────────────────────────────

async def _identity_section(property_row: dict, listings: list[dict], property_id: int) -> dict:
    from bot.db.pg import fetch

    listing_ids = [l["listing_id"] for l in listings]
    candidate_counts = {"pending": 0, "accepted": 0, "rejected": 0}
    photo_evidence_available = False
    if listing_ids or property_id is not None:
        rows = await fetch(
            """
            SELECT pmc.status, count(*) AS n
            FROM property_match_candidates pmc
            WHERE pmc.candidate_property_id = $1 OR pmc.listing_id = ANY($2::text[])
            GROUP BY pmc.status
            """,
            property_id, listing_ids,
        )
        for r in rows:
            if r["status"] in candidate_counts:
                candidate_counts[r["status"]] = r["n"]
        photo_evidence_available = bool(await fetch(
            """
            SELECT 1
            FROM property_match_candidates pmc
            JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id
            WHERE (pmc.candidate_property_id = $1 OR pmc.listing_id = ANY($2::text[]))
              AND pcpe.processing_status = 'ok'
            LIMIT 1
            """,
            property_id, listing_ids,
        ))

    confidences = [float(l["confidence"]) for l in listings if l.get("confidence") is not None]
    # Самая слабая связь определяет надёжность ВСЕЙ property (задача,
    # п.5: "Timeline provisional property не должен выглядеть как
    # подтверждённая физическая квартира") — консервативный выбор, не avg.
    min_confidence = min(confidences) if confidences else None

    return {
        "status": property_row["identity_status"],
        "confidence": min_confidence,
        "linked_listing_count": len(listings),
        "candidate_counts": candidate_counts,
        "photo_evidence_available": photo_evidence_available,
    }


# ── entry point ──────────────────────────────────────────────────────

async def build_property_timeline(property_id: int) -> dict | None:
    """Собирает честную единую timeline для ОДНОЙ физической квартиры
    (properties.property_id). None, если property_id не существует —
    вызывающий код (API endpoint) сам решает, как это представить (404).

    Никаких schema change, никаких production writes — read-only агрегация
    уже существующих таблиц (см. докстринг модуля — полный список
    источников и обоснование, почему они честны)."""
    from bot.db.pg import fetch, fetchrow

    property_row = await fetchrow(
        """
        SELECT property_id, complex_id, address_hash, floor, area_sqm, rooms,
               first_seen_at, last_seen_at, created_at, identity_status
        FROM properties WHERE property_id = $1
        """,
        property_id,
    )
    if property_row is None:
        return None
    property_row = dict(property_row)

    listing_rows = await fetch(
        """
        SELECT pl.property_id, pl.listing_id, pl.linked_at, pl.link_method, pl.confidence,
               pl.matcher_version,
               al.url, al.address, al.complex_name, al.floor, al.area, al.rooms, al.price,
               al.seller_name, al.is_owner, al.is_active, al.first_seen, al.last_seen,
               al.archived_at, al.archive_reason
        FROM property_listings pl
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = $1
        ORDER BY al.first_seen ASC NULLS LAST, pl.listing_id ASC
        """,
        property_id,
    )
    listings = [dict(r) for r in listing_rows]
    listing_ids = [l["listing_id"] for l in listings]

    price_history_by_listing: dict[str, list[dict]] = {lid: [] for lid in listing_ids}
    archive_history_by_listing: dict[str, list[dict]] = {lid: [] for lid in listing_ids}
    photo_evidence_rows: list[dict] = []

    if listing_ids:
        price_rows = await fetch(
            """
            SELECT listing_id, old_price, new_price, changed_at
            FROM price_history
            WHERE listing_id = ANY($1::text[])
            ORDER BY changed_at ASC, id ASC
            """,
            listing_ids,
        )
        for r in price_rows:
            price_history_by_listing.setdefault(r["listing_id"], []).append(dict(r))

        archive_rows = await fetch(
            """
            SELECT listing_id, archived_at, archive_reason, reactivated_at
            FROM listing_archive_history
            WHERE listing_id = ANY($1::text[])
            ORDER BY reactivated_at ASC, id ASC
            """,
            listing_ids,
        )
        for r in archive_rows:
            archive_history_by_listing.setdefault(r["listing_id"], []).append(dict(r))

        photo_rows = await fetch(
            """
            SELECT pmc.candidate_id, pmc.listing_id, pmc.candidate_property_id, pmc.match_method,
                   pmc.relationship_type, pmc.status,
                   pcpe.exact_shared_count, pcpe.perceptual_shared_count, pcpe.ai_similar_count,
                   pcpe.shared_unit_specific_count, pcpe.shared_common_count, pcpe.max_similarity,
                   pcpe.processing_status, pcpe.computed_at, pcpe.model_version
            FROM property_match_candidates pmc
            JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id
            WHERE pmc.candidate_property_id = $1 OR pmc.listing_id = ANY($2::text[])
            ORDER BY pcpe.computed_at ASC, pmc.candidate_id ASC
            """,
            property_id, listing_ids,
        )
        photo_evidence_rows = [dict(r) for r in photo_rows]

    metrics = _compute_metrics(listings, price_history_by_listing, archive_history_by_listing)
    events = _build_events(listings, price_history_by_listing, archive_history_by_listing, photo_evidence_rows)
    identity = await _identity_section(property_row, listings, property_id)

    listings_out = [{
        "listing_id": l["listing_id"],
        "url": l.get("url"),
        "address": l.get("address"),
        "complex_name": l.get("complex_name"),
        "floor": l.get("floor"),
        "area": l.get("area"),
        "rooms": l.get("rooms"),
        "price": l.get("price"),
        "observed_seller_name": l.get("seller_name"),
        "is_owner": l.get("is_owner"),
        "is_active": l.get("is_active"),
        "first_seen": _iso(l.get("first_seen")),
        "last_seen": _iso(l.get("last_seen")),
        "archived_at": _iso(l.get("archived_at")),
        "archive_reason": l.get("archive_reason"),
        "link_method": l.get("link_method"),
        "link_confidence": float(l["confidence"]) if l.get("confidence") is not None else None,
        "linked_at": _iso(l.get("linked_at")),
    } for l in listings]

    return {
        "property_id": property_id,
        "identity_status": property_row["identity_status"],
        "confidence": identity["confidence"],
        "identity": identity,
        "metrics": metrics,
        "listings": listings_out,
        "events": events,
    }
