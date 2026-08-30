"""bot/core/complex_market_profile.py — Complex Market Profile (задача
2026-08-30, "ЖК должен стать полноценной сущностью с собственным Market
Profile", Phase 5 — минимальная read-only реализация поверх Phase 1-4
аудита, см. отчёт в PR description / чате).

НЕ materialized table, НЕ ML, НЕ score, НЕ UI. Единственная точка входа —
`get_complex_market_profile(complex_id, as_of=None)`: считает всё запросами
по уже существующим таблицам (properties/property_listings/apartment_
listings/price_history/views_history/listing_archive_history) на каждый
вызов. Объём на самый большой сегодняшний ЖК (сотни properties) делает
materialization преждевременной оптимизацией — если это изменится,
кэш/таблица снимков — отдельное решение, не в этом модуле.

## Почему НЕ через complex_stats_history / outcome_labels

Оба уже существуют (complex_stats_snapshot.py, миграция 072; outcome_
labels) и частично пересекаются по смыслу с тем, что считает этот модуль
— но НЕ параметризованы `as_of`: complex_stats_history — ежедневный
снимок "на сегодня" на момент записи (за прошлые даты можно прочитать
СОХРАНЁННЫЙ снимок, но не пересчитать заново под произвольный as_of);
outcome_labels.computed_at отражает, когда МЫ посчитали лейбл, а не то,
что было бы честно известно на конкретный as_of в прошлом (там могут
использоваться данные, накопленные уже ПОСЛЕ as_of — то самое "future
leakage", которое задача явно запрещает). Этот модуль поэтому считает
каждую метрику напрямую из time-stamped таблиц с явным cutoff по as_of,
а не переиспользует эти два precomputed источника. Они остаются лучшим
выбором там, где не нужен произвольный as_of (напр. админ-дашборд "сейчас")
— не заменены, не задублированы намеренно.

## "active at as_of" — как считается без будущего листика

apartment_listings хранит только ТЕКУЩЕЕ состояние (is_active/archived_at)
— для объявления с историей нескольких архивации/реактивации ТОЛЬКО
листинг_archive_history хранит ЗАКРЫТЫЕ прошлые циклы (строка пишется В
МОМЕНТ реактивации, archived_at/reactivated_at — обе стороны интервала
"был архивирован тогда-то, вернулся тогда-то", см. bot/core/archive_
check.py). Текущий (возможно ещё открытый) цикл — только в apartment_
listings.archived_at (open-ended, если is_active=FALSE и объявление ещё
ни разу не реактивировалось после этого). Объединение обоих источников
даёт полный набор "архивных интервалов" на объявление; "активен на as_of"
= first_seen <= as_of И as_of не попадает ни в один такой интервал.

## DOM (observed market days) — ограничение

Для КАЖДОГО property мержатся интервалы [first_seen, effective_end) ВСЕХ
его listing'ов (см. `_merge_intervals` — не double-count, если два listing'а
одной property существовали в одно и то же время). effective_end для
неактивного на as_of листинга берётся из ТЕКУЩЕГО apartment_listings.
archived_at (не из точного исторического интервала, закрывшего его) —
приближение: если у листинга было несколько циклов архивации И as_of
приходится на прошлый ЗАКРЫТЫЙ цикл, конец интервала может быть не
абсолютно точным (задача Phase 4, "честно, не идеально" — ограничение
явно задокументировано, не скрыто)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

_MIN_SAMPLE = 5          # ниже этого агрегат (медиана цены и т.п.) -> insufficient_data
_MIN_SAMPLE_ROOMS = 3    # ниже этого per-room breakdown для этой комнатности -> insufficient_data
_MIN_DEMAND_ROWS = 5
_MIN_DEMAND_SPAN_DAYS = 7
_STALE_THRESHOLDS = (30, 60, 90)
_DISAPPEAR_WINDOWS = (30, 60, 90)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> float:
    """Суммарные дни в ОБЪЕДИНЕНИИ (не сумме) интервалов — два listing'а
    одной property, активных ОДНОВРЕМЕННО (concurrent duplicates), не
    должны задвоить DOM. Возвращает дни (float)."""
    if not intervals:
        return 0.0
    s = sorted(intervals, key=lambda t: t[0])
    merged: list[list[datetime]] = [list(s[0])]
    for start, end in s[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    total = sum((end - start).total_seconds() for start, end in merged)
    return total / 86400.0


async def get_complex_market_profile(complex_id: int, as_of: datetime | None = None) -> dict | None:
    """Единственная точка входа. Возвращает None, если complex_id не
    существует. `as_of` — naive трактуется как UTC; в будущем -> ValueError
    (задача, явно: "нельзя использовать сегодняшнее состояние для
    исторического as_of" — здесь симметрично: нельзя заглянуть в будущее
    вообще). Детерминированно на фиксированных входных данных (никакого
    random/now() без явного as_of внутри агрегатов)."""
    from bot.db.pg import fetch, fetchrow

    now = datetime.now(timezone.utc)
    if as_of is None:
        as_of = now
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    if as_of > now:
        raise ValueError(f"as_of ({as_of.isoformat()}) is in the future (now={now.isoformat()})")

    complex_row = await fetchrow("SELECT * FROM complexes WHERE id = $1", complex_id)
    if complex_row is None:
        return None
    complex_row = dict(complex_row)

    base_rows = await fetch(
        """
        WITH archive_intervals AS (
            SELECT listing_id, archived_at AS start_ts, reactivated_at AS end_ts
            FROM listing_archive_history
            UNION ALL
            SELECT id, archived_at, NULL
            FROM apartment_listings
            WHERE is_active IS FALSE AND archived_at IS NOT NULL
        )
        SELECT
            p.property_id, al.id AS listing_id, al.price, al.area, al.rooms,
            al.first_seen, al.archived_at AS current_archived_at,
            NOT EXISTS (
                SELECT 1 FROM archive_intervals ai
                WHERE ai.listing_id = al.id AND ai.start_ts <= $2
                  AND (ai.end_ts IS NULL OR $2 < ai.end_ts)
            ) AS active_at_as_of
        FROM properties p
        JOIN property_listings pl ON pl.property_id = p.property_id
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE p.complex_id = $1
          AND al.first_seen <= $2
          AND COALESCE(al.is_duplicate, FALSE) = FALSE
        """,
        complex_id, as_of,
    )
    base = [dict(r) for r in base_rows]
    listing_ids = [r["listing_id"] for r in base]

    price_history_rows: list[dict] = []
    if listing_ids:
        rows = await fetch(
            "SELECT listing_id, old_price, new_price, changed_at FROM price_history "
            "WHERE listing_id = ANY($1::text[]) AND changed_at <= $2 ORDER BY listing_id, changed_at",
            listing_ids, as_of,
        )
        price_history_rows = [dict(r) for r in rows]

    views_rows: list[dict] = []
    if listing_ids:
        rows = await fetch(
            "SELECT listing_id, views_count, observed_at FROM views_history "
            "WHERE listing_id = ANY($1::text[]) AND observed_at <= $2 ORDER BY listing_id, observed_at",
            listing_ids, as_of,
        )
        views_rows = [dict(r) for r in rows]

    identity = _build_identity(complex_row)
    physical = _build_physical(complex_row)
    supply = _build_supply(base, as_of)
    price = _build_price(base)
    liquidity = _build_liquidity(base, as_of)
    demand = _build_demand(base, views_rows, as_of)
    data_quality = _build_data_quality(complex_row, base, price_history_rows, views_rows, as_of, now)

    return {
        "complex_id": complex_id,
        "as_of": _iso(as_of),
        "identity": identity,
        "physical": physical,
        "supply": supply,
        "price": price,
        "liquidity": liquidity,
        "demand": demand,
        "data_quality": data_quality,
    }


# ── identity / physical — прямые поля complexes, Unknown != bad ─────────

def _build_identity(c: dict) -> dict:
    return {
        "complex_id": c["id"],
        "canonical_name": c["name"],
        "is_umbrella": bool(c.get("is_umbrella")),
        "parent_complex_id": c.get("parent_complex_id"),
        "address": c.get("address"),
        "district": c.get("district"),
        "lat": c.get("lat"),
        "lon": c.get("lon"),
        "developer_id": c.get("developer_id"),
        "developer_name_raw": c.get("developer"),  # свободный текст, НЕ FK — legacy поле
        "is_garbage": bool(c.get("is_garbage")),
    }


def _build_physical(c: dict) -> dict:
    return {
        "year_built": c.get("year_built"),
        "housing_class_manual": c.get("housing_class"),
        "housing_class_estimate": c.get("housing_class_estimate"),
        "predicted_housing_class": c.get("predicted_housing_class"),
        "predicted_housing_class_probability": c.get("predicted_housing_class_probability"),
        "material": None,  # complex_materials — отдельная таблица, join не добавлен
        # в этом минимальном foundation (задача явно: "только если есть
        # источник" — таблица есть, но только 45/3072 ЖК её заполнили,
        # см. Phase 1 отчёт; не подключено здесь, чтобы не плодить
        # полу-готовый join ради поля, которое почти всегда будет NULL —
        # следующий шаг, не блокирует остальной профиль).
    }


# ── supply ────────────────────────────────────────────────────────────

def _build_supply(base: list[dict], as_of: datetime) -> dict:
    unique_properties = {r["property_id"] for r in base}
    unique_listings = {r["listing_id"] for r in base}
    active_rows = [r for r in base if r["active_at_as_of"]]
    active_properties = {r["property_id"] for r in active_rows}
    active_listings = {r["listing_id"] for r in active_rows}

    new_counts = {}
    for days in (7, 30, 90):
        cutoff = as_of - timedelta(days=days)
        new_counts[f"new_listings_{days}d"] = sum(1 for r in base if r["first_seen"] >= cutoff)

    by_room: dict[str, int] = defaultdict(int)
    for r in active_rows:
        key = str(r["rooms"]) if r["rooms"] is not None else "unknown"
        by_room[key] += 1

    return {
        "observed_unique_listings": len(unique_listings),
        "observed_unique_properties": len(unique_properties),
        "active_listings_now": len(active_listings),
        "active_properties_now": len(active_properties),
        **new_counts,
        "active_supply_per_room_count": dict(sorted(by_room.items())),
    }


# ── price ─────────────────────────────────────────────────────────────

def _build_price(base: list[dict]) -> dict:
    active = [r for r in base if r["active_at_as_of"] and r["price"] and r["price"] > 0]
    prices = [float(r["price"]) for r in active]
    price_m2 = [float(r["price"]) / r["area"] for r in active if r.get("area") and r["area"] > 0]

    result: dict = {
        "sample_size": len(active),
        "median_asking_price": _median(prices) if len(active) >= _MIN_SAMPLE else None,
        "median_price_m2": _median(price_m2) if len(price_m2) >= _MIN_SAMPLE else None,
        "p25_price_m2": _percentile(price_m2, 0.25) if len(price_m2) >= _MIN_SAMPLE else None,
        "p75_price_m2": _percentile(price_m2, 0.75) if len(price_m2) >= _MIN_SAMPLE else None,
        "insufficient_data": len(active) < _MIN_SAMPLE,
    }

    by_room: dict[str, dict] = {}
    room_buckets: dict[str, list[float]] = defaultdict(list)
    for r in active:
        if r.get("area") and r["area"] > 0:
            key = str(r["rooms"]) if r["rooms"] is not None else "unknown"
            room_buckets[key].append(float(r["price"]) / r["area"])
    for key, vals in room_buckets.items():
        by_room[key] = {
            "sample_size": len(vals),
            "median_price_m2": _median(vals) if len(vals) >= _MIN_SAMPLE_ROOMS else None,
            "insufficient_data": len(vals) < _MIN_SAMPLE_ROOMS,
        }
    result["by_rooms"] = dict(sorted(by_room.items()))
    return result


# ── liquidity (Property Identity, не listing IDs) ────────────────────

def _build_liquidity(base: list[dict], as_of: datetime) -> dict:
    by_property: dict[int, list[dict]] = defaultdict(list)
    for r in base:
        by_property[r["property_id"]].append(r)

    n_properties = len(by_property)
    n_relisted = 0
    dom_days: list[float] = []
    stale_counts = {t: 0 for t in _STALE_THRESHOLDS}
    disappeared_counts = {w: 0 for w in _DISAPPEAR_WINDOWS}
    disappeared_denominator = {w: 0 for w in _DISAPPEAR_WINDOWS}

    for property_id, rows in by_property.items():
        if len(rows) >= 2:
            n_relisted += 1

        intervals: list[tuple[datetime, datetime]] = []
        any_active = False
        earliest_first_seen = min(r["first_seen"] for r in rows)
        for r in rows:
            start = r["first_seen"]
            if r["active_at_as_of"]:
                end = as_of
                any_active = True
            else:
                end = r["current_archived_at"] if r["current_archived_at"] is not None else as_of
                if end < start:
                    end = start
            intervals.append((start, end))
        merged_days = _merge_intervals(intervals)
        dom_days.append(merged_days)

        if any_active:
            for t in _STALE_THRESHOLDS:
                if merged_days >= t:
                    stale_counts[t] += 1

        for w in _DISAPPEAR_WINDOWS:
            window_end = earliest_first_seen + timedelta(days=w)
            if window_end > as_of:
                continue  # ещё не прошло w дней от первого наблюдения -> не в знаменателе
            disappeared_denominator[w] += 1
            if not any_active:
                disappeared_counts[w] += 1

    active_property_count = sum(1 for rows in by_property.values() if any(r["active_at_as_of"] for r in rows))

    result = {
        "sample_size_properties": n_properties,
        "true_relist_count": n_relisted,
        "true_relist_rate": round(n_relisted / n_properties, 4) if n_properties >= _MIN_SAMPLE else None,
        "median_observed_dom_days": round(_median(dom_days), 1) if len(dom_days) >= _MIN_SAMPLE and dom_days else None,
        "insufficient_data": n_properties < _MIN_SAMPLE,
        "stale_inventory": {
            f"gt_{t}d": stale_counts[t] for t in _STALE_THRESHOLDS
        },
        "active_property_count_for_stale": active_property_count,
        "fraction_disappearing": {
            f"within_{w}d": {
                "fraction": round(disappeared_counts[w] / disappeared_denominator[w], 4)
                            if disappeared_denominator[w] >= _MIN_SAMPLE else None,
                "sample_size": disappeared_denominator[w],
                "insufficient_data": disappeared_denominator[w] < _MIN_SAMPLE,
            }
            for w in _DISAPPEAR_WINDOWS
        },
        "note": "disappearance is NOT sale confirmation — no sale-date ground truth exists in this system "
                "(a listing/property may disappear for reasons other than a completed sale).",
    }
    return result


# ── demand (views) — insufficient history explicit, не выдумываем ──────

def _build_demand(base: list[dict], views_rows: list[dict], as_of: datetime) -> dict:
    if not views_rows:
        return {"insufficient_history": True, "reason": "no views_history rows for this complex up to as_of"}

    observed_ats = [r["observed_at"] for r in views_rows]
    span_days = (max(observed_ats) - min(observed_ats)).total_seconds() / 86400.0
    if len(views_rows) < _MIN_DEMAND_ROWS or span_days < _MIN_DEMAND_SPAN_DAYS:
        return {
            "insufficient_history": True,
            "reason": f"views_history span={round(span_days, 1)}d, rows={len(views_rows)} "
                      f"(need >= {_MIN_DEMAND_SPAN_DAYS}d and >= {_MIN_DEMAND_ROWS} rows)",
        }

    latest_by_listing: dict[str, int] = {}
    for r in views_rows:
        latest_by_listing[r["listing_id"]] = r["views_count"]  # rows ordered by observed_at asc

    active_listing_ids = {r["listing_id"] for r in base if r["active_at_as_of"]}
    active_views = [v for lid, v in latest_by_listing.items() if lid in active_listing_ids]

    return {
        "insufficient_history": False,
        "history_span_days": round(span_days, 1),
        "sample_size": len(active_views),
        "median_views": _median([float(v) for v in active_views]) if len(active_views) >= _MIN_SAMPLE else None,
        "views_per_active_listing": round(sum(active_views) / len(active_views), 1) if len(active_views) >= _MIN_SAMPLE else None,
        "insufficient_data": len(active_views) < _MIN_SAMPLE,
    }


# ── data_quality ─────────────────────────────────────────────────────

def _build_data_quality(c: dict, base: list[dict], price_history_rows: list[dict],
                         views_rows: list[dict], as_of: datetime, now: datetime) -> dict:
    n = len(base)
    freshness = None
    if base:
        latest_seen = max(r["first_seen"] for r in base)
        freshness = round((as_of - latest_seen).total_seconds() / 86400.0, 2)
    return {
        "sample_size_listing_rows": n,
        "complex_marked_garbage": bool(c.get("is_garbage")),
        "has_year_built": c.get("year_built") is not None,
        "has_developer_id": c.get("developer_id") is not None,
        "has_coordinates": c.get("lat") is not None and c.get("lon") is not None,
        "has_manual_housing_class": c.get("housing_class") is not None,
        "has_price_history": len(price_history_rows) > 0,
        "has_views_history": len(views_rows) > 0,
        "freshness_days_since_latest_first_seen": freshness,
        "as_of_is_now": as_of == now,
    }
