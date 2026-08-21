"""bot/analytics/market_dashboards.py — read-only агрегирующий query layer
для двух презентационных страниц рыночной аналитики (задача 2026-08-21,
"Обзор рынка" + "Поглощение и ликвидность", /admin/analytics/market-overview
и /admin/analytics/market-absorption). НИЧЕГО не пишет в БД, не пересчитывает
Deal Score/Property Identity/архивацию — только читает уже посчитанные
таблицы теми же формулами, что описаны в аудите задачи.

## Источники (аудит, см. коммит-сообщение/переписку)

apartment_listings — объявления, is_active/archived_at/first_seen — тот же
контракт, что bot/core/archive_check.py уже использует ("is_active=FALSE AND
archived_at IS NOT NULL" = подтверждённо проверено HTTP-чекером, НЕ просто
"пропало из последнего скрола"). price_history — событийный лог изменений
цены. outcome_labels.time_on_market — уже посчитанный DOM (first_seen →
archived_at), НЕ переизобретается здесь. properties/property_listings —
Property Identity, используется только для чтения (уникальные физ. квартиры,
признак повторной публикации).

## Почему НЕ listing_snapshots для дневных рядов

listing_snapshots хранит историю только с 2026-08-14 (см. докстринг
migrations/064) — 8 дней на момент задачи, недостаточно для рядов на
30/90 дней/весь период. Вместо неё — реконструкция по реальным меткам
apartment_listings.first_seen/archived_at (доступны с 2026-06-05) и
price_history (изменения цены, доступны с 2026-07-09, но ОТСУТСТВИЕ строки
для listing само по себе значит "цена не менялась с первой публикации" —
не пробел в данных). Это не выдуманные данные — 100% выводится из реальных
timestamp'ов, ограничения описаны в tooltip у каждого показателя:

  - `_active_at_conditions()` — "было ли объявление активно на дату D":
    first_seen<=D AND (archived_at IS NULL OR archived_at>D). НЕ учитывает
    временных разрывов при реактивации внутри истории (archived_at
    обнуляется при подтверждённой реактивации — см. archive_check.py) —
    для листинга, который был архивирован и реактивирован ПОСЛЕ даты D,
    это одна непрерывная "активность", хотя по факту был период отсутствия
    в выдаче. Разрывы это НЕ теряет полностью — они видны в
    new/exit-графике (тот считает по факту archived_at/first_seen), только
    "количество активных на дату" сглаживает короткие разрывы.
  - `_median_ppm2_reconstructed_at()` — цена/м² пула, который был активен на
    ОДНУ прошлую дату (используется только для сравнения KPI, не для
    полного дневного ряда — иначе N дней × реконструкция была бы дорогой).
    Правило "цена объявления на дату D": последнее price_history.new_price
    с changed_at<=D, иначе old_price САМОГО РАННЕГО изменения (цена ДО
    первого изменения — валидна вплоть до его даты), иначе текущая
    apartment_listings.price (изменений вообще не было).
  - Линия цены на графике "Динамика рынка" — сознательно НЕ полная
    реконструкция пула на каждый день (дорого), а медиана цены/м² СРЕДИ
    НОВЫХ объявлений периода (group by date_trunc(first_seen)) — честная,
    дешёвая, стандартная в отрасли метрика ("цена предложения новых
    объявлений"), помечена в подписи явно, не выдаётся за "цену всего
    рынка на эту дату".

## Кэш

Тяжёлые агрегаты (KPI, графики) кэшируются на TTL 300с (5 минут, нижняя
граница диапазона задачи "5-15 минут") в простом dict-кэше по ключу
(имя функции, кортеж отфильтрованных параметров) — тот же паттерн, что уже
использует bot/identity/property_identity_dashboard.py
(_cached_blocked_count) — без внешних зависимостей (Redis и т.п. в проекте
нет).

## N+1

Все сегментные показатели (структура предложения, ценовые коридоры,
таблица районов/ЖК, скорость вымывания по сегментам) — ОДИН SQL-запрос
с GROUP BY, не цикл по сегментам. Scatter "цена и ликвидность" — тоже один
GROUP BY по district/class."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.db.pg import fetch, fetchrow, fetchval

# ── общие константы фильтров ────────────────────────────────────────────

PERIOD_OPTIONS: dict[str, int | None] = {"7": 7, "30": 30, "90": 90, "all": None}
PERIOD_LABELS = {"7": "7 дней", "30": "30 дней", "90": "90 дней", "all": "весь период"}

# Реальные районы Астаны, встречающиеся в apartment_listings.district —
# остальные значения этого поля (единичные улицы/ЖК, попавшие туда по
# ошибке парсинга на источнике) сознательно НЕ включены в выпадающий
# список — они не образуют содержательного сегмента (см. аудит: до 10
# объявлений на "район").
DISTRICT_OPTIONS: list[tuple[str, str]] = [
    ("Есильский р-н", "Есильский"),
    ("Алматы р-н", "Алматы"),
    ("Сарыарка р-н", "Сарыарка"),
    ("Нура р-н", "Нура"),
    ("Сарайшык р-н", "Сарайшык"),
    ("р-н Байконур", "Байконур"),
]

ROOMS_OPTIONS: list[tuple[str, str]] = [("1", "1"), ("2", "2"), ("3", "3"), ("4+", "4 и более")]

MARKET_TYPE_OPTIONS = [("primary", "Первичка"), ("secondary", "Вторичка")]

CLASS_OPTIONS = ["эконом", "комфорт", "комфорт+", "бизнес", "бизнес+", "премиум", "элит", "не определён"]

STATUS_OPTIONS = [("active", "Активные"), ("archived", "Архивные"), ("all", "Все")]

# Каноническая классификация housing_class — исходные значения в complexes
# записаны с разным регистром/пробелами ("Комфорт+", "комфорт+", "Комфорт
# lite" и т.п., см. аудит) — единое место канонизации, переиспользуется и
# для фильтра, и для GROUP BY в сегментации "по классу".
_CLASS_EXPR = """
    CASE
        WHEN mc.housing_class IS NULL OR btrim(mc.housing_class) = '' THEN 'не определён'
        WHEN lower(btrim(mc.housing_class)) IN ('комфорт', 'комфорт lite') THEN 'комфорт'
        WHEN lower(btrim(mc.housing_class)) = 'комфорт+' THEN 'комфорт+'
        WHEN lower(btrim(mc.housing_class)) = 'бизнес' THEN 'бизнес'
        WHEN lower(btrim(mc.housing_class)) = 'бизнес+' THEN 'бизнес+'
        WHEN lower(btrim(mc.housing_class)) = 'эконом' THEN 'эконом'
        WHEN lower(btrim(mc.housing_class)) = 'премиум' THEN 'премиум'
        WHEN lower(btrim(mc.housing_class)) LIKE '%элит%' THEN 'элит'
        ELSE 'не определён'
    END
"""
# LATERAL + LIMIT 1, НЕ обычный LEFT JOIN по имени — аудит нашёл дубль в
# complexes (два разных id, схлопывающихся в один lower(btrim(name)),
# "Sunset Avenue") — обычный JOIN на этой паре даёт fan-out и завышает
# COUNT(*) для объявлений этого ЖК. LATERAL гарантирует ровно одну строку
# на объявление независимо от будущих дублей в complexes (эта таблица не
# наша, не трогаем её тут — только защищаемся от fan-out в запросах).
_COMPLEX_JOIN = """LEFT JOIN LATERAL (
        SELECT c.id, c.name, c.housing_class FROM complexes c
        WHERE lower(btrim(c.name)) = lower(btrim(a.complex_name))
        ORDER BY c.id LIMIT 1
    ) mc ON TRUE"""

# Минимальная выборка, ниже которой сегмент помечается "недостаточно
# данных" вместо того, чтобы показывать шумную метрику как надёжную
# (задача, явно: "скрывать или маркировать сегменты с недостаточной
# выборкой").
MIN_SEGMENT_N = 20
# Минимум дней в периоде для расчёта "месяцев запаса" (иначе оценка месячной
# скорости выбывания статистически неустойчива на паре дней).
MIN_STOCK_PERIOD_DAYS = 14

INSUFFICIENT = "insufficient"  # маркер "Недостаточно накопленных данных"


class _ParamBuilder:
    """Нумерует asyncpg-плейсхолдеры $1.. по мере добавления параметров —
    один и тот же параметр (например as_of) может использоваться в SQL
    несколько раз под одним и тем же номером."""

    def __init__(self) -> None:
        self.params: list[Any] = []

    def add(self, value: Any) -> str:
        self.params.append(value)
        return f"${len(self.params)}"


def normalize_filters(raw: dict[str, str]) -> dict[str, Any]:
    """Валидирует сырые query-параметры в безопасный канонический набор —
    единственное место, где строки из URL превращаются в SQL-условия
    (задача, явно: "ограничить допустимые периоды и фильтры")."""
    period = raw.get("period") or "30"
    if period not in PERIOD_OPTIONS:
        period = "30"

    district = (raw.get("district") or "").strip()
    if district not in {d[0] for d in DISTRICT_OPTIONS}:
        district = ""

    complex_id_raw = (raw.get("complex_id") or "").strip()
    try:
        complex_id = int(complex_id_raw) if complex_id_raw else None
    except ValueError:
        complex_id = None

    klass = (raw.get("klass") or "").strip()
    if klass not in CLASS_OPTIONS:
        klass = ""

    rooms = (raw.get("rooms") or "").strip()
    if rooms not in {r[0] for r in ROOMS_OPTIONS}:
        rooms = ""

    market_type = (raw.get("market_type") or "").strip()
    if market_type not in {m[0] for m in MARKET_TYPE_OPTIONS}:
        market_type = ""

    status = (raw.get("status") or "active").strip()
    if status not in {s[0] for s in STATUS_OPTIONS}:
        status = "active"

    return {
        "period": period,
        "period_days": PERIOD_OPTIONS[period],
        "district": district,
        "complex_id": complex_id,
        "klass": klass,
        "rooms": rooms,
        "market_type": market_type,
        "status": status,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _period_start(filters: dict, floor: datetime | None) -> datetime:
    days = filters["period_days"]
    if days is None:
        return floor or (_now() - timedelta(days=365 * 5))
    start = _now() - timedelta(days=days)
    return max(start, floor) if floor else start


def _base_conditions(filters: dict, pb: _ParamBuilder, alias: str = "a") -> list[str]:
    """Условия, НЕ зависящие от статуса/даты — район/ЖК/класс/комнатность/
    первичка-вторичка. Общие для всех запросов ниже (требует, чтобы alias
    был присоединён вместе с _COMPLEX_JOIN как `mc`)."""
    cond: list[str] = []
    if filters["district"]:
        cond.append(f"{alias}.district = {pb.add(filters['district'])}")
    if filters["market_type"]:
        cond.append(f"{alias}.market_type = {pb.add(filters['market_type'])}")
    if filters["rooms"]:
        if filters["rooms"] == "4+":
            cond.append(f"{alias}.rooms >= 4")
        else:
            cond.append(f"{alias}.rooms = {pb.add(int(filters['rooms']))}")
    if filters["complex_id"]:
        cond.append(f"mc.id = {pb.add(filters['complex_id'])}")
    if filters["klass"]:
        cond.append(f"({_CLASS_EXPR}) = {pb.add(filters['klass'])}")
    return cond


def _status_condition(filters: dict, alias: str = "a") -> str:
    if filters["status"] == "active":
        return f"{alias}.is_active IS NOT FALSE"
    if filters["status"] == "archived":
        return f"{alias}.is_active IS FALSE"
    return "TRUE"


# ── простой TTL-кэш вокруг тяжёлых агрегатов ────────────────────────────

_CACHE_TTL = 300.0  # 5 минут — нижняя граница диапазона задачи (5-15 мин)
_cache: dict[tuple, tuple[float, Any]] = {}


async def _cached(key: tuple, compute) -> Any:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = await compute()
    _cache[key] = (now, value)
    return value


def _filters_key(filters: dict) -> tuple:
    return tuple(sorted(filters.items()))


# ── свежесть данных ─────────────────────────────────────────────────────

async def get_data_freshness() -> dict:
    row = await fetchrow("""
        SELECT MAX(last_seen) AS max_last_seen,
               MAX(archive_checked_at) AS max_archive_checked_at,
               MIN(first_seen) AS min_first_seen
        FROM apartment_listings
    """)
    return dict(row) if row else {}


# ── реконструкция состояния пула на прошлую дату ────────────────────────

async def _confirmed_exits_in_period(period_start: datetime, filters: dict) -> int:
    """COUNT события "объявление подтверждённо ушло в архив в периоде" —
    ЕДИНСТВЕННОЕ корректное определение (не просто "сейчас archived_at в
    периоде"): apartment_listings.archived_at — ТЕКУЩЕЕ состояние, при
    реактивации оно ОБНУЛЯЕТСЯ (см. archive_check.py) — тот, кто ушёл и
    вернулся в рамках ОДНОГО периода, иначе выпал бы из счётчика вовсе
    (а его "возврат" при этом всё равно учтён в reactivated — без этого
    объединения воронка на странице 2 арифметически не сходится, найдено
    при smoke-тесте: 194 реактивации в demo-периоде ровно объясняли
    расхождение active_end на +194). Поэтому здесь — ОБЪЕДИНЕНИЕ:
    (а) сейчас всё ещё в архиве, ушёл в периоде, (б) ушёл в периоде, но
    с тех пор подтверждённо реактивирован (запись в listing_archive_
    history.archived_at — то самое СТАРОЕ значение, которое чистится при
    реактивации)."""
    pb = _ParamBuilder()
    cond = _base_conditions(filters, pb, alias="a")
    ps = pb.add(period_start)
    where = " AND ".join(cond) if cond else "TRUE"
    return await fetchval(f"""
        SELECT COUNT(*) FROM apartment_listings a
        {_COMPLEX_JOIN}
        WHERE {where} AND (
            a.archived_at >= {ps}
            OR EXISTS (
                SELECT 1 FROM listing_archive_history lah
                WHERE lah.listing_id = a.id AND lah.archived_at >= {ps}
            )
        )
    """, *pb.params) or 0


async def _active_count_at(as_of: datetime, filters: dict) -> int:
    pb = _ParamBuilder()
    cond = _base_conditions(filters, pb)
    as_of_p = pb.add(as_of)
    where = " AND ".join(cond) if cond else "TRUE"
    sql = f"""
        SELECT COUNT(*) FROM apartment_listings a
        {_COMPLEX_JOIN}
        WHERE a.first_seen <= {as_of_p}
          AND (a.archived_at IS NULL OR a.archived_at > {as_of_p})
          AND {where}
    """
    return await fetchval(sql, *pb.params) or 0


async def _median_ppm2_reconstructed_at(as_of: datetime, filters: dict) -> float | None:
    """Медиана цены/м² пула, реально активного на прошлую дату as_of —
    см. модульный докстринг, правило price_at(listing, D)."""
    pb = _ParamBuilder()
    cond = _base_conditions(filters, pb)
    as_of_p = pb.add(as_of)
    where = " AND ".join(cond) if cond else "TRUE"
    sql = f"""
        WITH pool AS (
            SELECT a.id, a.area, a.price AS current_price
            FROM apartment_listings a
            {_COMPLEX_JOIN}
            WHERE a.first_seen <= {as_of_p}
              AND (a.archived_at IS NULL OR a.archived_at > {as_of_p})
              AND a.area > 0 AND a.price > 0
              AND {where}
        ),
        last_change AS (
            SELECT DISTINCT ON (ph.listing_id) ph.listing_id, ph.new_price
            FROM price_history ph
            JOIN pool p ON p.id = ph.listing_id
            WHERE ph.changed_at <= {as_of_p}
            ORDER BY ph.listing_id, ph.changed_at DESC
        ),
        first_change AS (
            SELECT DISTINCT ON (ph.listing_id) ph.listing_id, ph.old_price
            FROM price_history ph
            JOIN pool p ON p.id = ph.listing_id
            WHERE ph.listing_id NOT IN (SELECT listing_id FROM last_change)
            ORDER BY ph.listing_id, ph.changed_at ASC
        )
        SELECT percentile_cont(0.5) WITHIN GROUP (
            ORDER BY COALESCE(lc.new_price, fc.old_price, p.current_price) / p.area
        ) AS median_ppm2
        FROM pool p
        LEFT JOIN last_change lc ON lc.listing_id = p.id
        LEFT JOIN first_change fc ON fc.listing_id = p.id
    """
    val = await fetchval(sql, *pb.params)
    return float(val) if val is not None else None


# ── KPI: «Обзор рынка» ──────────────────────────────────────────────────

def _kpi(value, unit: str, formula: str, change_pct: float | None = None,
         status: str = "ok", limitation: str = "") -> dict:
    return {
        "value": value, "unit": unit, "formula": formula,
        "change_pct": change_pct, "status": status, "limitation": limitation,
    }


async def overview_kpis(filters: dict) -> dict:
    async def compute():
        pb = _ParamBuilder()
        base_cond = _base_conditions(filters, pb)
        status_cond = _status_condition(filters)
        where_active = " AND ".join([status_cond, *base_cond]) if base_cond else status_cond

        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        period_start = _period_start(filters, floor)
        prev_period_start = period_start - (
            (_now() - period_start) if filters["period_days"] else timedelta(days=30)
        )

        row = await fetchrow(f"""
            SELECT
                COUNT(*) FILTER (WHERE {where_active}) AS active_cnt,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price)
                    FILTER (WHERE {where_active} AND a.price > 0) AS median_price,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price / a.area)
                    FILTER (WHERE {where_active} AND a.price > 0 AND a.area > 0) AS median_ppm2,
                COUNT(DISTINCT mc.id) FILTER (WHERE {where_active} AND mc.id IS NOT NULL) AS active_complexes
            FROM apartment_listings a
            {_COMPLEX_JOIN}
        """, *pb.params)

        # Уникальные физ. квартиры — только среди активных, через
        # Property Identity (property_listings.listing_id -> property_id).
        pb2 = _ParamBuilder()
        cond2 = _base_conditions(filters, pb2, alias="a")
        where2 = " AND ".join([_status_condition(filters), *cond2])
        unique_properties = await fetchval(f"""
            SELECT COUNT(DISTINCT pl.property_id)
            FROM property_listings pl
            JOIN apartment_listings a ON a.id = pl.listing_id
            {_COMPLEX_JOIN}
            WHERE {where2}
        """, *pb2.params) or 0

        # Новое предложение / подтверждённо выбывшие за период — flow-метрики,
        # НЕ фильтруются по текущему is_active (статус на конец периода не
        # значим для факта "появилось"/"подтверждённо ушло" в периоде).
        pb3 = _ParamBuilder()
        cond3 = _base_conditions(filters, pb3, alias="a")
        ps = pb3.add(period_start)
        where3 = " AND ".join(cond3) if cond3 else "TRUE"
        new_supply_val = await fetchval(f"""
            SELECT COUNT(*) FROM apartment_listings a {_COMPLEX_JOIN}
            WHERE a.first_seen >= {ps} AND {where3}
        """, *pb3.params) or 0
        confirmed_exits_val = await _confirmed_exits_in_period(period_start, filters)
        flow_row = {"new_supply": new_supply_val, "confirmed_exits": confirmed_exits_val}

        # Медианная экспозиция — среди подтверждённо выбывших В ПЕРИОДЕ,
        # переиспользует уже посчитанный outcome_labels.time_on_market
        # (НЕ last_seen — тот же принцип, что archive_check.py).
        pb4 = _ParamBuilder()
        cond4 = _base_conditions(filters, pb4, alias="a")
        ps4 = pb4.add(period_start)
        where4 = " AND ".join(cond4) if cond4 else "TRUE"
        median_dom = await fetchval(f"""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ol.time_on_market)
            FROM apartment_listings a
            JOIN outcome_labels ol ON ol.listing_id = a.id
            {_COMPLEX_JOIN}
            WHERE a.archived_at >= {ps4} AND ol.time_on_market IS NOT NULL AND {where4}
        """, *pb4.params)

        # Доля активных со снижением цены в периоде.
        pb5 = _ParamBuilder()
        cond5 = _base_conditions(filters, pb5, alias="a")
        ps5 = pb5.add(period_start)
        where5 = " AND ".join([_status_condition(filters), *cond5])
        price_drop = await fetchrow(f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM price_history ph
                       WHERE ph.listing_id = a.id AND ph.new_price < ph.old_price
                         AND ph.changed_at >= {ps5}
                   )) AS with_drop
            FROM apartment_listings a
            {_COMPLEX_JOIN}
            WHERE {where5}
        """, *pb5.params)

        # Δ медианной цены/м² к предыдущему сопоставимому периоду —
        # реконструкция пула НА prev_period_start против текущего медиан.
        prev_median_ppm2 = None
        change_status = "no_baseline"
        if filters["period_days"] and prev_period_start >= (floor or prev_period_start):
            prev_median_ppm2 = await _median_ppm2_reconstructed_at(prev_period_start, filters)
            if prev_median_ppm2:
                change_status = "ok"

        current_ppm2 = float(row["median_ppm2"]) if row and row["median_ppm2"] else None
        change_pct = None
        if change_status == "ok" and current_ppm2 and prev_median_ppm2:
            change_pct = (current_ppm2 - prev_median_ppm2) / prev_median_ppm2 * 100

        drop_share = None
        if price_drop and price_drop["total"]:
            drop_share = price_drop["with_drop"] / price_drop["total"] * 100

        return {
            "active_listings": _kpi(
                row["active_cnt"] if row else 0, "шт",
                "COUNT(*) активных объявлений по текущим фильтрам"),
            "unique_properties": _kpi(
                unique_properties, "шт",
                "COUNT(DISTINCT property_id) среди активных объявлений (Property Identity)",
                limitation="Зависит от полноты сопоставления Property Identity — не 100% объявлений привязаны."),
            "new_supply": _kpi(
                flow_row["new_supply"] if flow_row else 0, "шт",
                "COUNT(*) WHERE first_seen попадает в выбранный период"),
            "confirmed_exits": _kpi(
                flow_row["confirmed_exits"] if flow_row else 0, "шт",
                "COUNT(*) WHERE archived_at попадает в выбранный период (подтверждено HTTP-проверкой архивации)",
                limitation="Не означает продажу — см. пояснение на странице «Поглощение и ликвидность»."),
            "median_price": _kpi(
                float(row["median_price"]) if row and row["median_price"] else None, "₸",
                "percentile_cont(0.5) по цене активных объявлений"),
            "median_ppm2": _kpi(
                current_ppm2, "₸/м²",
                "percentile_cont(0.5) по цене/площади активных объявлений"),
            "median_ppm2_change": _kpi(
                round(change_pct, 1) if change_pct is not None else None, "%",
                "(медиана сейчас − медиана на начало предыдущего периода) / медиана на начало предыдущего периода",
                change_pct=change_pct,
                status="ok" if change_status == "ok" else INSUFFICIENT,
                limitation="Требует периода с историей ДО его начала — недоступно для «весь период»."),
            "median_dom": _kpi(
                float(median_dom) if median_dom is not None else None, "дней",
                "percentile_cont(0.5) по outcome_labels.time_on_market для подтверждённо выбывших в периоде",
                status="ok" if median_dom is not None else INSUFFICIENT,
                limitation="Считается только по УЖЕ выбывшим объявлениям (censored-данные по ещё активным не используются)."),
            "price_drop_share": _kpi(
                round(drop_share, 1) if drop_share is not None else None, "%",
                "доля активных объявлений с ≥1 снижением цены (price_history) за период",
                status="ok" if drop_share is not None else INSUFFICIENT),
            "active_complexes": _kpi(
                row["active_complexes"] if row else 0, "шт",
                "COUNT(DISTINCT ЖК) среди активных объявлений с распознанным ЖК"),
        }
    return await _cached(("overview_kpis", _filters_key(filters)), compute)


# ── графики: «Обзор рынка» ──────────────────────────────────────────────

def _bucket_unit(period_days: int | None) -> str:
    return "day" if period_days is not None and period_days <= 14 else "week"

# Верхний предел точек на графике (задача, явно: "верхнее ограничение
# количества точек").
MAX_CHART_POINTS = 120


async def market_dynamics_series(filters: dict) -> dict:
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        start = _period_start(filters, floor)
        bucket = _bucket_unit(filters["period_days"])
        end = _now()

        # 1) медиана цены/м² среди НОВЫХ объявлений периода — дешёвый
        #    GROUP BY, честно подписывается на графике как "новое предложение".
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        start_p = pb.add(start)
        where = " AND ".join(cond) if cond else "TRUE"
        price_rows = await fetch(f"""
            SELECT date_trunc('{bucket}', a.first_seen) AS bucket,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price / a.area) AS median_ppm2,
                   COUNT(*) AS n
            FROM apartment_listings a
            {_COMPLEX_JOIN}
            WHERE a.first_seen >= {start_p} AND a.price > 0 AND a.area > 0 AND {where}
            GROUP BY 1 ORDER BY 1
            LIMIT {MAX_CHART_POINTS}
        """, *pb.params)

        # 2) активные на конец каждого бакета — реконструкция одним
        #    запросом (generate_series JOIN), не циклом по датам. Фильтры
        #    (включая класс/ЖК, которым нужен _COMPLEX_JOIN) применяются
        #    в ОТДЕЛЬНОМ CTE `pool` ДО join с buckets — mc не сослаться
        #    из ON-условия join'а buckets<->apartment_listings напрямую,
        #    LATERAL должен идти сразу за той строкой a, к которой
        #    относится (найдено на smoke-тесте: "missing FROM-clause
        #    entry for table mc").
        pb2 = _ParamBuilder()
        cond2 = _base_conditions(filters, pb2, alias="a")
        start_p2 = pb2.add(start)
        end_p2 = pb2.add(end)
        where2 = " AND ".join(cond2) if cond2 else "TRUE"
        active_rows = await fetch(f"""
            WITH pool AS (
                SELECT a.id, a.first_seen, a.archived_at
                FROM apartment_listings a
                {_COMPLEX_JOIN}
                WHERE {where2}
            ),
            buckets AS (
                SELECT generate_series(
                    date_trunc('{bucket}', {start_p2}::timestamptz),
                    date_trunc('{bucket}', {end_p2}::timestamptz),
                    ('1 {bucket}')::interval
                ) AS d
                LIMIT {MAX_CHART_POINTS}
            )
            SELECT b.d AS bucket, COUNT(p.id) AS active_cnt
            FROM buckets b
            LEFT JOIN pool p
                ON p.first_seen <= b.d + ('1 {bucket}')::interval
               AND (p.archived_at IS NULL OR p.archived_at > b.d + ('1 {bucket}')::interval)
            GROUP BY b.d ORDER BY b.d
        """, *pb2.params)

        labels = [r["bucket"].strftime("%d.%m") for r in active_rows]
        active_by_bucket = {r["bucket"]: r["active_cnt"] for r in active_rows}
        price_by_bucket = {r["bucket"]: r["median_ppm2"] for r in price_rows}
        return {
            "bucket": bucket,
            "labels": labels,
            "active_counts": [active_by_bucket.get(r["bucket"], 0) for r in active_rows],
            "median_ppm2": [
                (float(price_by_bucket[r["bucket"]]) if price_by_bucket.get(r["bucket"]) else None)
                for r in active_rows
            ],
            "note": "Линия цены — медиана цены/м² среди НОВЫХ объявлений периода (по дате публикации), "
                    "не полного пула. Количество активных — реконструкция по first_seen/archived_at.",
        }
    return await _cached(("market_dynamics_series", _filters_key(filters)), compute)


async def new_vs_exit_series(filters: dict) -> dict:
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        start = _period_start(filters, floor)
        bucket = _bucket_unit(filters["period_days"])
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        start_p = pb.add(start)
        where = " AND ".join(cond) if cond else "TRUE"
        rows = await fetch(f"""
            WITH new_ev AS (
                SELECT date_trunc('{bucket}', a.first_seen) AS bucket, COUNT(*) AS cnt
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.first_seen >= {start_p} AND {where}
                GROUP BY 1
            ),
            exit_ev AS (
                SELECT date_trunc('{bucket}', a.archived_at) AS bucket, COUNT(*) AS cnt
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.archived_at >= {start_p} AND {where}
                GROUP BY 1
            )
            SELECT COALESCE(n.bucket, e.bucket) AS bucket,
                   COALESCE(n.cnt, 0) AS new_cnt, COALESCE(e.cnt, 0) AS exit_cnt
            FROM new_ev n FULL OUTER JOIN exit_ev e ON n.bucket = e.bucket
            ORDER BY 1
            LIMIT {MAX_CHART_POINTS}
        """, *pb.params)
        return {
            "bucket": bucket,
            "labels": [r["bucket"].strftime("%d.%m") for r in rows],
            "new_supply": [r["new_cnt"] for r in rows],
            "exits": [r["exit_cnt"] for r in rows],
            "net_change": [r["new_cnt"] - r["exit_cnt"] for r in rows],
        }
    return await _cached(("new_vs_exit_series", _filters_key(filters)), compute)


_REAL_DISTRICTS = tuple(d[0] for d in DISTRICT_OPTIONS)
_REAL_DISTRICTS_SQL = "(" + ",".join(f"'{d}'" for d in _REAL_DISTRICTS) + ")"

_STRUCTURE_DIMENSIONS = {
    "rooms": ("CASE WHEN a.rooms >= 4 THEN '4+' ELSE a.rooms::text END", "a.rooms IS NOT NULL"),
    "class": (_CLASS_EXPR, "TRUE"),
    # Ограничено реальными районами (см. аудит: остальные значения поля
    # district — единичные улицы/ЖК, попавшие туда по ошибке источника,
    # не образуют содержательного сегмента).
    "district": ("a.district", f"a.district IN {_REAL_DISTRICTS_SQL}"),
    "price_range": (
        "CASE WHEN a.price < 20000000 THEN '< 20 млн' "
        "WHEN a.price < 35000000 THEN '20-35 млн' "
        "WHEN a.price < 50000000 THEN '35-50 млн' "
        "WHEN a.price < 80000000 THEN '50-80 млн' "
        "ELSE '80 млн+' END",
        "a.price > 0",
    ),
    "area": (
        "CASE WHEN a.area < 40 THEN '< 40 м²' WHEN a.area < 60 THEN '40-60 м²' "
        "WHEN a.area < 80 THEN '60-80 м²' WHEN a.area < 110 THEN '80-110 м²' "
        "ELSE '110 м²+' END",
        "a.area > 0",
    ),
}


async def supply_structure(filters: dict, dimension: str) -> dict:
    if dimension not in _STRUCTURE_DIMENSIONS:
        dimension = "rooms"
    expr, guard = _STRUCTURE_DIMENSIONS[dimension]

    async def compute():
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        where = " AND ".join([_status_condition(filters), guard, *cond])
        rows = await fetch(f"""
            SELECT ({expr}) AS segment, COUNT(*) AS cnt
            FROM apartment_listings a {_COMPLEX_JOIN}
            WHERE {where}
            GROUP BY 1 ORDER BY cnt DESC
        """, *pb.params)
        return {"dimension": dimension, "segments": [r["segment"] for r in rows],
                "counts": [r["cnt"] for r in rows]}
    return await _cached(("supply_structure", dimension, _filters_key(filters)), compute)


_CORRIDOR_DIMENSIONS = {
    "rooms": _STRUCTURE_DIMENSIONS["rooms"],
    "class": _STRUCTURE_DIMENSIONS["class"],
}


async def price_corridors(filters: dict, dimension: str) -> dict:
    if dimension not in _CORRIDOR_DIMENSIONS:
        dimension = "rooms"
    expr, guard = _CORRIDOR_DIMENSIONS[dimension]

    async def compute():
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        where = " AND ".join([_status_condition(filters), guard, "a.price > 0", "a.area > 0", *cond])
        # Отсечение явных выбросов — p1/p99 по цене/м² внутри сегмента,
        # min/max корридора считаются УЖЕ после этого отсечения.
        rows = await fetch(f"""
            WITH base AS (
                SELECT ({expr}) AS segment, a.price / a.area AS ppm2
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE {where}
            ),
            bounds AS (
                SELECT segment,
                       percentile_cont(0.01) WITHIN GROUP (ORDER BY ppm2) AS p1,
                       percentile_cont(0.99) WITHIN GROUP (ORDER BY ppm2) AS p99
                FROM base GROUP BY segment
            )
            SELECT b.segment,
                   COUNT(*) AS n,
                   MIN(b.ppm2) AS lo,
                   percentile_cont(0.25) WITHIN GROUP (ORDER BY b.ppm2) AS p25,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY b.ppm2) AS median,
                   percentile_cont(0.75) WITHIN GROUP (ORDER BY b.ppm2) AS p75,
                   MAX(b.ppm2) AS hi
            FROM base b JOIN bounds bd ON bd.segment = b.segment
            WHERE b.ppm2 BETWEEN bd.p1 AND bd.p99
            GROUP BY b.segment
            ORDER BY median
        """, *pb.params)
        return {
            "dimension": dimension,
            "rows": [
                {"segment": r["segment"], "n": r["n"], "min": float(r["lo"]), "p25": float(r["p25"]),
                 "median": float(r["median"]), "p75": float(r["p75"]), "max": float(r["hi"])}
                for r in rows if r["n"] >= MIN_SEGMENT_N
            ],
        }
    return await _cached(("price_corridors", dimension, _filters_key(filters)), compute)


async def segment_table(filters: dict) -> list[dict]:
    """Таблица районов — замена карте гексагонов (спецификация допускает
    это при нехватке времени на безопасное переиспользование карты)."""
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        start = _period_start(filters, floor)
        prev_start = start - (_now() - start)
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        start_p = pb.add(start)
        prev_start_p = pb.add(prev_start)
        district_guard = f"a.district IN {_REAL_DISTRICTS_SQL}"
        where_active = " AND ".join([_status_condition(filters), district_guard, *cond])
        where_flow = " AND ".join([district_guard, *cond]) if cond else district_guard
        rows = await fetch(f"""
            WITH active_stats AS (
                SELECT a.district,
                       COUNT(*) AS active_cnt,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price / a.area)
                           FILTER (WHERE a.price > 0 AND a.area > 0) AS median_ppm2,
                       COUNT(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM price_history ph WHERE ph.listing_id = a.id
                           AND ph.new_price < ph.old_price AND ph.changed_at >= {start_p}
                       ))::float / NULLIF(COUNT(*), 0) AS drop_share
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE {where_active}
                GROUP BY a.district
            ),
            dom_stats AS (
                SELECT a.district, percentile_cont(0.5) WITHIN GROUP (ORDER BY ol.time_on_market) AS median_dom
                FROM apartment_listings a
                JOIN outcome_labels ol ON ol.listing_id = a.id
                {_COMPLEX_JOIN}
                WHERE a.archived_at >= {start_p} AND ol.time_on_market IS NOT NULL AND {where_flow}
                GROUP BY a.district
            ),
            -- Изменение цены — та же честная методология, что и линия цены
            -- на графике "Динамика рынка" (медиана СРЕДИ НОВЫХ объявлений,
            -- не полная реконструкция пула на дату — см. модульный
            -- докстринг): текущий период против предыдущего той же длины.
            new_price_current AS (
                SELECT a.district, percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price / a.area) AS med
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.first_seen >= {start_p} AND a.price > 0 AND a.area > 0 AND {where_flow}
                GROUP BY a.district
            ),
            new_price_prev AS (
                SELECT a.district, percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price / a.area) AS med
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.first_seen >= {prev_start_p} AND a.first_seen < {start_p}
                  AND a.price > 0 AND a.area > 0 AND {where_flow}
                GROUP BY a.district
            )
            SELECT s.district, s.active_cnt, s.median_ppm2, s.drop_share, d.median_dom,
                   np.med AS new_price_med, npp.med AS new_price_prev_med
            FROM active_stats s
            LEFT JOIN dom_stats d ON d.district = s.district
            LEFT JOIN new_price_current np ON np.district = s.district
            LEFT JOIN new_price_prev npp ON npp.district = s.district
            ORDER BY s.active_cnt DESC
        """, *pb.params)
        return [
            {
                "district": r["district"], "active_count": r["active_cnt"],
                "median_ppm2": float(r["median_ppm2"]) if r["median_ppm2"] else None,
                "price_change_pct": (
                    round((float(r["new_price_med"]) - float(r["new_price_prev_med"])) / float(r["new_price_prev_med"]) * 100, 1)
                    if r["new_price_med"] and r["new_price_prev_med"] else None
                ),
                "price_drop_share_pct": round(r["drop_share"] * 100, 1) if r["drop_share"] is not None else None,
                "median_dom_days": float(r["median_dom"]) if r["median_dom"] is not None else None,
                "insufficient": r["active_cnt"] < MIN_SEGMENT_N,
            }
            for r in rows
        ]
    return await _cached(("segment_table", _filters_key(filters)), compute)


async def list_complexes_for_filter(limit: int = 300) -> list[dict]:
    """Топ ЖК по активным объявлениям — для выпадающего списка фильтра
    (полный список ЖК ~2700 позиций непрактичен в <select>)."""
    async def compute():
        rows = await fetch(f"""
            SELECT c.id, c.name, COUNT(a.id) AS active_cnt
            FROM complexes c
            JOIN apartment_listings a ON lower(btrim(a.complex_name)) = lower(btrim(c.name))
            WHERE a.is_active IS NOT FALSE
            GROUP BY c.id, c.name
            ORDER BY active_cnt DESC
            LIMIT {limit}
        """)
        return [{"id": r["id"], "name": r["name"], "active_count": r["active_cnt"]} for r in rows]
    return await _cached(("list_complexes_for_filter",), compute)


# ══════════════════════════════════════════════════════════════════════
# «Поглощение и ликвидность»
#
# Термины (задача, явно): «выбывание» — НЕ «продажа». Ничего в этом
# разделе не утверждает факт сделки — только наблюдаемое исчезновение
# объявления (archived_at, подтверждено HTTP-проверкой).
# ══════════════════════════════════════════════════════════════════════

async def absorption_kpis(filters: dict) -> dict:
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        period_start = _period_start(filters, floor)
        period_days_actual = max((_now() - period_start).days, 1)

        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        ps = pb.add(period_start)
        where = " AND ".join(cond) if cond else "TRUE"
        new_supply = await fetchval(f"""
            SELECT COUNT(*) FROM apartment_listings a {_COMPLEX_JOIN}
            WHERE a.first_seen >= {ps} AND {where}
        """, *pb.params) or 0
        confirmed_exits = await _confirmed_exits_in_period(period_start, filters)

        active_at_start = await _active_count_at(period_start, filters)
        active_now = await _active_count_at(_now(), filters)

        exit_rate = None
        if active_at_start > 0:
            exit_rate = confirmed_exits / active_at_start * 100

        months_of_stock = None
        stock_status = "ok"
        if filters["period_days"] is not None and filters["period_days"] < MIN_STOCK_PERIOD_DAYS:
            stock_status = INSUFFICIENT
        elif confirmed_exits == 0:
            stock_status = INSUFFICIENT
        else:
            monthly_exits = confirmed_exits / period_days_actual * 30
            months_of_stock = active_now / monthly_exits if monthly_exits else None

        pb2 = _ParamBuilder()
        cond2 = _base_conditions(filters, pb2, alias="a")
        ps2 = pb2.add(period_start)
        where2 = " AND ".join(cond2) if cond2 else "TRUE"
        median_dom = await fetchval(f"""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ol.time_on_market)
            FROM apartment_listings a
            JOIN outcome_labels ol ON ol.listing_id = a.id
            {_COMPLEX_JOIN}
            WHERE a.archived_at >= {ps2} AND ol.time_on_market IS NOT NULL AND {where2}
        """, *pb2.params)

        # Доля повторных публикаций среди НОВЫХ объявлений периода —
        # property.first_seen_at раньше собственного first_seen объявления
        # значит физическая квартира уже была известна системе раньше.
        pb3 = _ParamBuilder()
        cond3 = _base_conditions(filters, pb3, alias="a")
        ps3 = pb3.add(period_start)
        where3 = " AND ".join(cond3) if cond3 else "TRUE"
        relist = await fetchrow(f"""
            WITH new_listings AS (
                SELECT a.id, a.first_seen, p.first_seen_at AS property_first_seen
                FROM apartment_listings a
                JOIN property_listings pl ON pl.listing_id = a.id
                JOIN properties p ON p.property_id = pl.property_id
                {_COMPLEX_JOIN}
                WHERE a.first_seen >= {ps3} AND {where3}
            )
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE property_first_seen < first_seen) AS relist_cnt
            FROM new_listings
        """, *pb3.params)
        relist_share = None
        if relist and relist["total"]:
            relist_share = relist["relist_cnt"] / relist["total"] * 100

        pb4 = _ParamBuilder()
        cond4 = _base_conditions(filters, pb4, alias="a")
        ps4 = pb4.add(period_start)
        where4_active = " AND ".join([_status_condition(filters), *cond4])
        price_drop = await fetchrow(f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM price_history ph WHERE ph.listing_id = a.id
                       AND ph.new_price < ph.old_price AND ph.changed_at >= {ps4}
                   )) AS with_drop
            FROM apartment_listings a {_COMPLEX_JOIN}
            WHERE {where4_active}
        """, *pb4.params)
        price_drop_share = None
        if price_drop and price_drop["total"]:
            price_drop_share = price_drop["with_drop"] / price_drop["total"] * 100

        median_drop_pct = await fetchval(f"""
            SELECT percentile_cont(0.5) WITHIN GROUP (
                ORDER BY (ph.old_price - ph.new_price)::float / NULLIF(ph.old_price, 0) * 100
            )
            FROM price_history ph
            JOIN apartment_listings a ON a.id = ph.listing_id
            {_COMPLEX_JOIN}
            WHERE ph.new_price < ph.old_price AND ph.changed_at >= {ps4} AND {' AND '.join(cond4) if cond4 else 'TRUE'}
        """, *pb4.params)

        pb5 = _ParamBuilder()
        cond5 = _base_conditions(filters, pb5, alias="a")
        ps5 = pb5.add(period_start)
        where5 = " AND ".join(cond5) if cond5 else "TRUE"
        exit_after_drop = await fetchrow(f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM price_history ph WHERE ph.listing_id = a.id
                       AND ph.new_price < ph.old_price AND ph.changed_at <= a.archived_at
                   )) AS with_drop
            FROM apartment_listings a {_COMPLEX_JOIN}
            WHERE a.archived_at >= {ps5} AND {where5}
        """, *pb5.params)
        exit_after_drop_share = None
        if exit_after_drop and exit_after_drop["total"]:
            exit_after_drop_share = exit_after_drop["with_drop"] / exit_after_drop["total"] * 100

        return {
            "new_supply": _kpi(new_supply, "шт", "COUNT(*) WHERE first_seen в периоде"),
            "confirmed_exits": _kpi(confirmed_exits, "шт",
                "COUNT(*) WHERE archived_at в периоде — подтверждено HTTP-проверкой, НЕ факт продажи"),
            "exit_rate": _kpi(
                round(exit_rate, 1) if exit_rate is not None else None, "%",
                "confirmed_exits_in_period / active_at_period_start × 100%",
                status="ok" if exit_rate is not None else INSUFFICIENT,
                limitation="Не определено при active_at_period_start = 0."),
            "net_change": _kpi(new_supply - confirmed_exits, "шт", "new_supply − confirmed_exits"),
            "months_of_stock": _kpi(
                round(months_of_stock, 1) if months_of_stock is not None else None, "мес",
                "active_now / (confirmed_exits_in_period / period_days × 30)",
                status=stock_status,
                limitation="Не считается при периоде < 14 дней или нулевом выбывании в периоде."),
            "median_time_to_exit": _kpi(
                float(median_dom) if median_dom is not None else None, "дней",
                "percentile_cont(0.5) по outcome_labels.time_on_market для подтверждённо выбывших в периоде",
                status="ok" if median_dom is not None else INSUFFICIENT),
            "relist_share": _kpi(
                round(relist_share, 1) if relist_share is not None else None, "%",
                "доля новых объявлений периода, чья физ. квартира (Property Identity) уже была известна раньше",
                status="ok" if relist_share is not None else INSUFFICIENT,
                limitation="Не проверяет длительность разрыва между публикациями, только факт «квартира уже известна»."),
            "price_drop_share": _kpi(
                round(price_drop_share, 1) if price_drop_share is not None else None, "%",
                "доля активных объявлений с ≥1 снижением цены за период",
                status="ok" if price_drop_share is not None else INSUFFICIENT),
            "median_price_drop_pct": _kpi(
                round(float(median_drop_pct), 1) if median_drop_pct is not None else None, "%",
                "медиана (old_price−new_price)/old_price по событиям снижения цены за период",
                status="ok" if median_drop_pct is not None else INSUFFICIENT),
            "exit_after_drop_share": _kpi(
                round(exit_after_drop_share, 1) if exit_after_drop_share is not None else None, "%",
                "доля подтверждённо выбывших в периоде, у кого была ≥1 фиксация снижения цены до archived_at",
                status="ok" if exit_after_drop_share is not None else INSUFFICIENT),
        }
    return await _cached(("absorption_kpis", _filters_key(filters)), compute)


async def supply_funnel(filters: dict) -> dict:
    """Воронка/waterfall предложения — арифметически согласована ПО
    ПОСТРОЕНИЮ: active_end считается формулой, не отдельным запросом (см.
    модульный докстринг про пересечение категорий — задача явно требует
    waterfall вместо воронки, если независимые числа могут разойтись)."""
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        period_start = _period_start(filters, floor)

        active_start = await _active_count_at(period_start, filters)

        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        ps = pb.add(period_start)
        where = " AND ".join(cond) if cond else "TRUE"
        new_supply = await fetchval(f"""
            SELECT COUNT(*) FROM apartment_listings a {_COMPLEX_JOIN}
            WHERE a.first_seen >= {ps} AND {where}
        """, *pb.params) or 0
        confirmed_exits = await _confirmed_exits_in_period(period_start, filters)

        pb2 = _ParamBuilder()
        cond2 = _base_conditions(filters, pb2, alias="a")
        ps2 = pb2.add(period_start)
        where2 = " AND ".join(cond2) if cond2 else "TRUE"
        reactivated = await fetchval(f"""
            SELECT COUNT(*) FROM listing_archive_history lah
            JOIN apartment_listings a ON a.id = lah.listing_id
            {_COMPLEX_JOIN}
            WHERE lah.reactivated_at >= {ps2} AND {where2}
        """, *pb2.params) or 0

        active_end_derived = active_start + new_supply - confirmed_exits + reactivated
        active_end_actual = await _active_count_at(_now(), filters)
        delta = active_end_actual - active_end_derived
        # Малый допуск (реконструкция active_start не восстанавливает
        # временные разрывы для листингов, архивированных ДО периода и
        # реактивированных ВНУТРИ него — см. модульный докстринг) — до
        # 0.5% от active_start считаем практически сошедшейся, показываем
        # точную дельту всегда, не только когда она "большая".
        reconciled = abs(delta) <= max(5, round(active_start * 0.005))

        return {
            "active_start": active_start, "new_supply": new_supply,
            "confirmed_exits": confirmed_exits, "reactivated": reactivated,
            "active_end": active_end_derived, "active_end_actual": active_end_actual,
            "delta": delta, "reconciled": reconciled,
            "note": "Активные на конец периода посчитаны по формуле (не отдельным запросом) — арифметика "
                    "согласована по построению." + (
                        "" if reconciled else
                        " Фактический замер сейчас отличается — возможны дополнительные циклы "
                        "архив/реактивация внутри периода, не разложенные на отдельные шаги воронки."),
        }
    return await _cached(("supply_funnel", _filters_key(filters)), compute)


_EXIT_SPEED_DIMENSIONS = {
    "rooms": ("CASE WHEN a.rooms >= 4 THEN '4+' ELSE a.rooms::text END", "a.rooms IS NOT NULL"),
    "class": (_CLASS_EXPR, "TRUE"),
    "district": ("a.district", f"a.district IN {_REAL_DISTRICTS_SQL}"),
    "complex": ("mc.name", "mc.id IS NOT NULL"),
}


async def segment_exit_speed(filters: dict, dimension: str, limit: int = 15) -> list[dict]:
    """Скорость вымывания по сегментам — один GROUP BY на измерение, НЕ
    цикл запросов по сегментам. metric не параметризует SQL — все три
    метрики (коэффициент выбывания/медианная экспозиция/месяцы запаса)
    считаются в одном проходе, переключение в Chart.js на клиенте."""
    if dimension not in _EXIT_SPEED_DIMENSIONS:
        dimension = "rooms"
    expr, guard = _EXIT_SPEED_DIMENSIONS[dimension]

    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        period_start = _period_start(filters, floor)
        period_days_actual = max((_now() - period_start).days, 1)

        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        ps = pb.add(period_start)
        where = " AND ".join([guard, *cond]) if cond else guard
        rows = await fetch(f"""
            WITH start_pool AS (
                SELECT ({expr}) AS segment, COUNT(*) AS n_start
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.first_seen <= {ps} AND (a.archived_at IS NULL OR a.archived_at > {ps})
                  AND {where}
                GROUP BY 1
            ),
            now_pool AS (
                SELECT ({expr}) AS segment, COUNT(*) AS n_now
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.is_active IS NOT FALSE AND {where}
                GROUP BY 1
            ),
            exits AS (
                SELECT ({expr}) AS segment, COUNT(*) AS n_exits
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.archived_at >= {ps} AND {where}
                GROUP BY 1
            ),
            dom AS (
                SELECT ({expr}) AS segment,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY ol.time_on_market) AS median_dom
                FROM apartment_listings a
                JOIN outcome_labels ol ON ol.listing_id = a.id
                {_COMPLEX_JOIN}
                WHERE a.archived_at >= {ps} AND ol.time_on_market IS NOT NULL AND {where}
                GROUP BY 1
            )
            SELECT s.segment, s.n_start, COALESCE(n.n_now, 0) AS n_now,
                   COALESCE(e.n_exits, 0) AS n_exits, d.median_dom
            FROM start_pool s
            LEFT JOIN now_pool n ON n.segment = s.segment
            LEFT JOIN exits e ON e.segment = s.segment
            LEFT JOIN dom d ON d.segment = s.segment
            ORDER BY s.n_start DESC
            LIMIT {limit}
        """, *pb.params)

        result = []
        for r in rows:
            n_start = r["n_start"] or 0
            exit_rate = (r["n_exits"] / n_start * 100) if n_start else None
            monthly_exits = (r["n_exits"] / period_days_actual * 30) if r["n_exits"] else 0
            months_stock = (r["n_now"] / monthly_exits) if monthly_exits else None
            insufficient = n_start < MIN_SEGMENT_N
            result.append({
                "segment": r["segment"], "n": n_start, "n_now": r["n_now"], "n_exits": r["n_exits"],
                "exit_rate_pct": round(exit_rate, 1) if exit_rate is not None else None,
                "median_dom_days": float(r["median_dom"]) if r["median_dom"] is not None else None,
                "months_of_stock": round(months_stock, 1) if months_stock is not None else None,
                "insufficient": insufficient,
            })
        return result
    return await _cached(("segment_exit_speed", dimension, limit, _filters_key(filters)), compute)


async def price_vs_liquidity_scatter(filters: dict) -> list[dict]:
    """Точка = (район × класс) с N >= MIN_SEGMENT_N — держит количество
    точек читаемым на скриншоте (≈30-40 при полном наборе фильтров) и
    даёт цветовую легенду по классу, как того просит задача."""
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        period_start = _period_start(filters, floor)
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        ps = pb.add(period_start)
        district_guard = f"a.district IN {_REAL_DISTRICTS_SQL}"
        where = " AND ".join([_status_condition(filters), district_guard, "a.price > 0", "a.area > 0", *cond])
        rows = await fetch(f"""
            WITH pool AS (
                SELECT a.id, a.district, ({_CLASS_EXPR}) AS klass, a.price / a.area AS ppm2
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE {where}
            ),
            agg AS (
                SELECT district, klass, COUNT(*) AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY ppm2) AS median_ppm2
                FROM pool GROUP BY district, klass HAVING COUNT(*) >= {MIN_SEGMENT_N}
            ),
            exits AS (
                SELECT a.district, ({_CLASS_EXPR}) AS klass, COUNT(*) AS n_exits
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.archived_at >= {ps} AND {district_guard} AND {' AND '.join(cond) if cond else 'TRUE'}
                GROUP BY 1, 2
            )
            SELECT ag.district, ag.klass, ag.n, ag.median_ppm2, COALESCE(ex.n_exits, 0) AS n_exits
            FROM agg ag LEFT JOIN exits ex ON ex.district = ag.district AND ex.klass = ag.klass
            ORDER BY ag.n DESC
        """, *pb.params)
        return [
            {
                "district": r["district"], "klass": r["klass"], "n": r["n"],
                "median_ppm2": float(r["median_ppm2"]),
                "exit_rate_pct": round(r["n_exits"] / r["n"] * 100, 1) if r["n"] else None,
            }
            for r in rows
        ]
    return await _cached(("price_vs_liquidity_scatter", _filters_key(filters)), compute)


_DROP_BUCKETS = [
    ("no_drop", "Без снижения"),
    ("lt_5", "Снижение до 5%"),
    ("5_10", "Снижение 5–10%"),
    ("gt_10", "Снижение более 10%"),
]


async def price_drop_buckets(filters: dict) -> list[dict]:
    """Группировка ВЫБЫВШИХ в периоде объявлений по итоговому снижению
    цены (original_price -> price на момент архивации) — original_price
    берётся по тому же правилу price_at(), что и остальной модуль."""
    async def compute():
        floor = await fetchval("SELECT MIN(first_seen) FROM apartment_listings")
        period_start = _period_start(filters, floor)
        pb = _ParamBuilder()
        cond = _base_conditions(filters, pb, alias="a")
        ps = pb.add(period_start)
        where = " AND ".join(cond) if cond else "TRUE"
        rows = await fetch(f"""
            WITH exited AS (
                SELECT a.id, a.price AS last_price, a.archived_at,
                       (SELECT ph.old_price FROM price_history ph
                        WHERE ph.listing_id = a.id ORDER BY ph.changed_at ASC LIMIT 1) AS earliest_old_price
                FROM apartment_listings a {_COMPLEX_JOIN}
                WHERE a.archived_at >= {ps} AND a.price > 0 AND {where}
            ),
            with_original AS (
                SELECT id, archived_at, last_price,
                       COALESCE(earliest_old_price, last_price) AS original_price
                FROM exited
            ),
            bucketed AS (
                SELECT id, archived_at,
                       CASE
                           WHEN last_price >= original_price THEN 'no_drop'
                           WHEN (original_price - last_price)::float / original_price < 0.05 THEN 'lt_5'
                           WHEN (original_price - last_price)::float / original_price < 0.10 THEN '5_10'
                           ELSE 'gt_10'
                       END AS bucket
                FROM with_original WHERE original_price > 0
            )
            SELECT b.bucket, COUNT(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY ol.time_on_market) AS median_dom
            FROM bucketed b
            LEFT JOIN outcome_labels ol ON ol.listing_id = b.id
            GROUP BY b.bucket
        """, *pb.params)
        by_bucket = {r["bucket"]: r for r in rows}
        total = sum(r["n"] for r in rows) if rows else 0
        result = []
        for key, label in _DROP_BUCKETS:
            r = by_bucket.get(key)
            n = r["n"] if r else 0
            result.append({
                "bucket": key, "label": label, "n": n,
                "share_pct": round(n / total * 100, 1) if total else None,
                "median_dom_days": float(r["median_dom"]) if r and r["median_dom"] is not None else None,
            })
        return result
    return await _cached(("price_drop_buckets", _filters_key(filters)), compute)
