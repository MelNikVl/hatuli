"""bot/analytics/dom_scenario.py — консервативный эмпирический сценарный
калькулятор "ожидаемый срок экспозиции при разных ценах" для попапа
объявления (задача 2026-08-21, "MVP прогноза срока экспозиции").

## Почему не ML

См. docs/dom_forecast_audit.md — log-normal AFT на этих же данных даёт
MAE 229.6 дня против 15.1 у сегментного baseline (в 10-17 раз хуже) и
нарушает действующую "Паузу по ML" (docs/verdict_strategy.md). Этот
модуль НЕ обучает модель и НЕ пытается снова параметрическую AFT —
непараметрический расчёт (Kaplan–Meier по сегменту + PAVA-сглаживание
ценовых корзин), тот же класс метода, что аудит указал как непроверенную,
но менее чувствительную к короткому окну наблюдения альтернативу (§5
аудита, пункт 4 "что нужно, чтобы пересмотреть решение").

## Единица наблюдения

property_id (Property Identity), когда объявление к ней привязано —
повторные публикации одной физической квартиры НЕ считаются независимыми
объектами. Если Property Identity ещё не привязала listing (пока не
покрыла всю базу — см. docs/dom_forecast_audit.md §1), unit = сам
listing_id (уже не "повторная публикация", просто нет данных о её
существовании — не гадаем).

## Право-цензурирование

Активные объявления (is_active) входят как right-censored наблюдения:
T = сегодня − first_seen, event=0. Архивированные — event=1, T =
outcome_labels.time_on_market, если он уже посчитан, иначе
archived_at − first_seen (тот же исходный факт, посчитанный на лету —
не ждём отдельного backfill, чтобы не терять наблюдения).
archived_at НЕ интерпretируется как подтверждённая продажа — см.
CONFIRMED_SALE_DISCLAIMER ниже, тот же текст, что и в property_identity_
dashboard.py.

## Fallback-сегменты

1. district × rooms (точная комнатность)
2. district × rooms_bucket (укрупнённая — 4+ вместо 4/5/6...; для 1-3
   комнат совпадает с уровнем 1, это ожидаемо — расширение имеет смысл
   только для редких больших квартир)
3. rooms_bucket по всему городу (без района)
4. общий городской baseline (без района и комнатности)

Выбирается первый уровень, где event_count (число РАЗРЕШИВШИХСЯ
наблюдений, не просто размер выборки — с 90%+ цензурированием размер
выборки почти ничего не говорит о том, можно ли надёжно оценить медиану)
достигает MIN_EVENTS_MEDIUM; если ни один уровень не достигает — берётся
последний (городской baseline) с тем, что есть, и уровень надёжности
принудительно "low".

## Если Kaplan-Meier не оценивается

Возможна ситуация, когда даже у выбранного сегмента кривая KM ни разу не
опускается до 0.5 (типично при сильном цензурировании — большинство ещё
активно) — тогда медианный "хвостовой" прогноз недостоверен. В этом
случае используется тот же метод, что уже проверен в scripts/dom_forecast_
baseline_backtest.py::_baseline_segment_median — медиана T среди РАЗРЕШИВШИХСЯ
(event=1) наблюдений сегмента, с явно принудительной низкой надёжностью
(п.10 задания)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

CONFIRMED_SALE_DISCLAIMER = (
    "Оценка основана на сроках активности похожих объявлений. Снятие "
    "объявления с публикации не подтверждает факт продажи."
)

# Те же 6 реальных районов Астаны, что market_dashboards.DISTRICT_OPTIONS —
# остальные значения apartment_listings.district единичны/шумны (см. тот
# же комментарий там) и не образуют содержательного сегмента.
REAL_DISTRICTS: list[str] = [
    "Есильский р-н", "Алматы р-н", "Сарыарка р-н",
    "Нура р-н", "Сарайшык р-н", "р-н Байконур",
]

ROOMS_BUCKET_SQL = "CASE WHEN a.rooms >= 4 THEN '4+' ELSE a.rooms::text END"

DISCOUNT_SCENARIOS: list[int] = [0, 3, 5, 7, 10]

# ── пороги/ограничения — намеренно консервативные (п.8 задания: "не
# выдавать точность выше реальной") ─────────────────────────────────────
MIN_EVENTS_FOR_KM = 8       # меньше — KM-квантиль слишком шумный
MIN_EVENTS_SUFFICIENT = 15  # событий на самом специфичном уровне -> "достаточно"
MIN_EVENTS_MEDIUM = 5       # порог для расширения фоллбэка/уровня "средний"
MIN_EVENTS_FOR_PRICE_CURVE = 6  # меньше — ценовая чувствительность не оценивается
                                  # (сценарии совпадают с текущей ценой, не выдумываем наклон)
MIN_SAMPLE_ANY = 3          # меньше — "недостаточно данных", блок без чисел

DAYS_MIN = 3     # разумный нижний предел диапазона
DAYS_MAX = 180   # разумный верхний предел — не даём AFT-подобной экстраполяции
                  # в сотни/тысячи дней (см. аудит §4: AFT давал до 2336)

_CACHE_TTL = 900.0  # 15 минут — попап открывают часто, цена объявления не
                     # меняется поминутно; тот же паттерн TTL-кэша, что уже
                     # использует bot/analytics/market_dashboards.py (там 300с
                     # для более лёгких агрегатов, здесь чуть шире — расчёт
                     # на объявление тяжелее: KM + PAVA по сегменту)
_cache: dict[str, tuple[float, dict]] = {}


def _rooms_bucket(rooms: int | str | None) -> str | None:
    if rooms is None:
        return None
    try:
        n = int(rooms)
    except (TypeError, ValueError):
        return None
    return "4+" if n >= 4 else str(n)


def _rooms_label(rooms: int | str) -> str:
    """Человекочитаемое склонение — '1 комната'/'2 комнаты'/'5 комнат'."""
    if isinstance(rooms, str) and rooms == "4+":
        return "4+ комнат"
    n = int(rooms)
    if n % 10 == 1 and n % 100 != 11:
        word = "комната"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "комнаты"
    else:
        word = "комнат"
    return f"{n} {word}"


# ── реконструкция цены на дату (та же формула, что bot/analytics/
# market_dashboards.py::_median_ppm2_reconstructed_at и scripts/dom_forecast_
# baseline_backtest.py — не переизобретается, вынесена сюда как чистая
# функция, чтобы её можно было тестировать и переиспользовать из backtest-
# скрипта без похода в БД) ───────────────────────────────────────────────

def price_at(price_history: list[dict], as_of: datetime, current_price: float | None) -> float | None:
    """price_history — список {"old_price","new_price","changed_at"},
    ЛЮБОЙ порядок. Правило: последний new_price с changed_at<=as_of; иначе
    old_price САМОГО РАННЕГО изменения (цена ДО первого изменения, валидна
    до его даты); иначе current_price (изменений вообще не было).
    НЕ использует изменения после as_of — обязательное условие честного
    backtest (п.9 задания, "не использовать будущие данные")."""
    changes = sorted(
        (h for h in price_history if h.get("changed_at") is not None),
        key=lambda h: h["changed_at"],
    )
    past = [h for h in changes if h["changed_at"] <= as_of]
    if past:
        return past[-1]["new_price"]
    if changes:
        return changes[0]["old_price"]
    return current_price


# ── Kaplan-Meier (без внешних тяжёлых зависимостей — lifelines/scipy.stats
# survival в проекте нет и не добавляется, п. "Пауза по ML" запрещает
# новые ML-зависимости, а обычная выборочная KM — не ML-модель, а
# описательная непараметрическая статистика) ────────────────────────────

def kaplan_meier(observations: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """observations — [(T, event)], event=1 разрешилось / 0 censored.
    Возвращает шаги (t, S(t)) начиная с (0.0, 1.0), S невозрастающая."""
    if not observations:
        return [(0.0, 1.0)]
    event_times = sorted({t for t, e in observations if e == 1})
    steps = [(0.0, 1.0)]
    survival = 1.0
    for t in event_times:
        n_at_risk = sum(1 for tt, _ in observations if tt >= t)
        if n_at_risk <= 0:
            continue
        d = sum(1 for tt, e in observations if tt == t and e == 1)
        survival *= (1 - d / n_at_risk)
        steps.append((t, survival))
    return steps


def km_quantile(steps: list[tuple[float, float]], q: float) -> float | None:
    """Первое время t, где S(t) <= q (стандартная KM-квантиль). None, если
    кривая ни разу не опускается до q в пределах наблюдённых данных —
    сигнал "оценке нельзя доверять", не подставляем последнее t как якобы
    известный ответ (п.10 задания)."""
    for t, s in steps:
        if s <= q:
            return t
    return None


# ── PAVA — pool adjacent violators, изотоническая (неубывающая)
# регрессия ────────────────────────────────────────────────────────────

def pava(values: list[float], weights: list[float] | None = None) -> list[float]:
    """values уже упорядочены по x (в этом модуле — по price_dev
    возрастанию). Возвращает неубывающую по индексу сглаженную версию —
    п.7 задания ("применить монотонное сглаживание/PAVA", если ценовые
    корзины шумные)."""
    n = len(values)
    if n == 0:
        return []
    if weights is None:
        weights = [1.0] * n
    blocks = [[float(values[i]), float(weights[i]), 1] for i in range(n)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-12:
            v1, w1, c1 = blocks[i]
            v2, w2, c2 = blocks[i + 1]
            merged_w = w1 + w2
            merged_v = (v1 * w1 + v2 * w2) / merged_w
            blocks[i:i + 2] = [[merged_v, merged_w, c1 + c2]]
            i = max(i - 1, 0)
        else:
            i += 1
    result: list[float] = []
    for v, _w, c in blocks:
        result.extend([v] * c)
    return result


def _clamp_days(x: float) -> float:
    return max(DAYS_MIN, min(DAYS_MAX, x))


def _enforce_monotone_scenarios(scenarios: list[dict]) -> list[dict]:
    """Финальный защитный проход (п. "снижение цены не может увеличивать
    ожидаемый срок"): scenarios уже отсортированы по discount_pct
    возрастанию (0%, 3%, 5%...) — цена по сценарию строго не растёт, значит
    days_low/days_high тоже не должны расти. Это НЕ замена PAVA-сглаживания
    ценовых корзин (оно уже применено раньше и обычно достаточно), а
    дешёвая гарантия на выходе — если апстрим-оценка всё же шумная,
    результат пользователю всё равно останется монотонным."""
    out = []
    prev_low, prev_high = None, None
    for sc in scenarios:
        low, high = sc["days_low"], sc["days_high"]
        if prev_low is not None:
            low = min(low, prev_low)
            high = min(high, prev_high)
        if low > high:
            low = high
        prev_low, prev_high = low, high
        out.append({**sc, "days_low": int(round(low)), "days_high": int(round(high))})
    return out


# ── ценовая чувствительность: корзины price_dev внутри сегмента + PAVA ──

def _price_sensitivity_curve(events: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """events — [(price_dev, T)] ТОЛЬКО среди разрешившихся (event=1)
    наблюдений сегмента. Возвращает список (bucket_price_dev, smoothed_days)
    отсортированный по price_dev, монотонно неубывающий по days — или
    None, если событий мало для содержательной оценки наклона (п.8:
    "не выдавать точность выше реальной" — в этом случае сценарии просто
    совпадут с текущим прогнозом, кроме финального монотонного клэмпа)."""
    if len(events) < MIN_EVENTS_FOR_PRICE_CURVE:
        return None
    ordered = sorted(events, key=lambda x: x[0])
    n = len(ordered)
    n_buckets = min(5, max(2, n // 5))
    bucket_size = max(1, n // n_buckets)
    buckets: list[list[tuple[float, float]]] = []
    for i in range(0, n, bucket_size):
        chunk = ordered[i:i + bucket_size]
        if not chunk:
            continue
        # последний неполный остаток — приклеиваем к предыдущей корзине,
        # а не оставляем корзину из 1 точки (шумно).
        if buckets and len(chunk) < bucket_size / 2:
            buckets[-1].extend(chunk)
        else:
            buckets.append(chunk)
    if len(buckets) < 2:
        return None
    xs = [sorted(b, key=lambda x: x[0])[len(b) // 2][0] for b in buckets]  # медиана price_dev корзины
    ys = [sorted(t for _, t in b)[len(b) // 2] for b in buckets]           # медиана T корзины
    ws = [float(len(b)) for b in buckets]
    smoothed = pava(ys, ws)
    return list(zip(xs, smoothed))


def _interp_curve(curve: list[tuple[float, float]], x: float) -> float:
    if len(curve) == 1:
        return curve[0][1]
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return curve[-1][1]


# ── сборка популяции сегмента из БД ──────────────────────────────────────

async def _fetch_segment_population(
    tier: str, district: str | None, rooms_filter: str | None, exclude_key: str,
) -> list[dict]:
    """Один SQL-запрос на уровень (не в цикле по объявлениям — п. "исключить
    N+1"). Дедупликация по property_id, где она известна (COALESCE на
    'l:'||listing_id — тем самым listing без Property Identity остаётся
    отдельным наблюдением, не теряется и не задваивается)."""
    from bot.db.pg import fetch

    conditions = ["a.price > 0", "a.area > 0", "a.rooms IS NOT NULL", "a.first_seen IS NOT NULL"]
    params: list[Any] = []
    if district is not None:
        params.append(district)
        conditions.append(f"a.district = ${len(params)}")
    else:
        params.append(REAL_DISTRICTS)
        conditions.append(f"a.district = ANY(${len(params)}::text[])")
    if rooms_filter is not None:
        if tier == "district_rooms":
            params.append(int(rooms_filter))
            conditions.append(f"a.rooms = ${len(params)}")
        else:
            params.append(rooms_filter)
            conditions.append(f"({ROOMS_BUCKET_SQL}) = ${len(params)}")
    where = " AND ".join(conditions)
    sql = f"""
        WITH ranked AS (
            SELECT a.id AS listing_id, a.price, a.area, a.rooms, a.district,
                   a.first_seen, a.archived_at, a.is_active, pl.property_id,
                   row_number() OVER (
                       PARTITION BY COALESCE(pl.property_id::text, 'l:' || a.id)
                       ORDER BY a.first_seen DESC
                   ) AS rn
            FROM apartment_listings a
            LEFT JOIN property_listings pl ON pl.listing_id = a.id
            WHERE {where}
        )
        SELECT r.listing_id, r.property_id, r.price, r.area, r.rooms, r.district,
               r.first_seen, r.archived_at, r.is_active, ol.time_on_market
        FROM ranked r
        LEFT JOIN outcome_labels ol ON ol.listing_id = r.listing_id
        WHERE r.rn = 1
          AND COALESCE(r.property_id::text, 'l:' || r.listing_id) != ${len(params) + 1}
    """
    params.append(exclude_key)
    rows = await fetch(sql, *params)
    return [dict(r) for r in rows]


def _row_to_obs(row: dict, now: datetime) -> tuple[float, int, float]:
    """Возвращает (T, event, ppm2) для одной property/listing-строки."""
    ppm2 = float(row["price"]) / float(row["area"])
    archived_at = row.get("archived_at")
    if archived_at is not None:
        tom = row.get("time_on_market")
        if tom is None:
            fs = row["first_seen"]
            tom = (archived_at - fs).total_seconds() / 86400.0
        T = max(float(tom), 0.5)
        event = 1
    else:
        fs = row["first_seen"]
        T = max((now - fs).total_seconds() / 86400.0, 0.5)
        event = 0
    return T, event, ppm2


_SEGMENT_LEVELS = ["district_rooms", "district_rooms_bucket", "city_rooms_bucket", "city_baseline"]


async def _pick_segment(district: str | None, rooms: int | None, exclude_key: str, now: datetime):
    """Идёт по уровням фоллбэка (п.2 задания) и возвращает первый, где
    event_count достигает MIN_EVENTS_MEDIUM. Если ни один не достигает —
    возвращает последний (городской baseline) с тем, что набралось."""
    rooms_bucket = _rooms_bucket(rooms)
    best = None
    for tier in _SEGMENT_LEVELS:
        if tier == "district_rooms":
            if district is None or rooms is None:
                continue
            pop = await _fetch_segment_population(tier, district, str(rooms), exclude_key)
        elif tier == "district_rooms_bucket":
            if district is None or rooms_bucket is None:
                continue
            pop = await _fetch_segment_population(tier, district, rooms_bucket, exclude_key)
        elif tier == "city_rooms_bucket":
            if rooms_bucket is None:
                continue
            pop = await _fetch_segment_population(tier, None, rooms_bucket, exclude_key)
        else:  # city_baseline
            pop = await _fetch_segment_population(tier, None, None, exclude_key)

        obs = [_row_to_obs(r, now) for r in pop]
        event_count = sum(1 for _, e, _ in obs if e == 1)
        candidate = {"tier": tier, "population": pop, "obs": obs, "event_count": event_count,
                     "sample_size": len(pop)}
        best = candidate  # всегда есть, даже если событий 0 — на случай, что дальше расширяться некуда
        if event_count >= MIN_EVENTS_MEDIUM:
            return candidate
    return best


def _segment_label(tier: str, district: str | None, rooms: int | None) -> str:
    rooms_bucket = _rooms_bucket(rooms)
    if tier == "district_rooms" and district and rooms is not None:
        return f"{district} · {_rooms_label(rooms)}"
    if tier == "district_rooms_bucket" and district and rooms_bucket:
        return f"{district} · {_rooms_label(rooms_bucket)}"
    if tier == "city_rooms_bucket" and rooms_bucket:
        return f"Астана · {_rooms_label(rooms_bucket)}"
    return "Астана · весь рынок"


def _confidence_level(tier: str, event_count: int, km_ok: bool) -> str:
    if tier == "district_rooms" and event_count >= MIN_EVENTS_SUFFICIENT and km_ok:
        return "sufficient"
    if tier in ("district_rooms", "district_rooms_bucket") and event_count >= MIN_EVENTS_MEDIUM:
        return "medium"
    if event_count >= 1:
        return "low"
    return "low"


async def compute_dom_scenario(listing_id: str) -> dict:
    """Главная точка входа — один расчёт на объявление (кэшируется на
    уровне compute_dom_scenario_cached, см. ниже)."""
    from bot.db.pg import fetchrow

    now = datetime.now(timezone.utc)
    listing = await fetchrow(
        "SELECT a.id, a.price, a.area, a.rooms, a.district, a.market_type, "
        "       a.first_seen, pl.property_id "
        "FROM apartment_listings a "
        "LEFT JOIN property_listings pl ON pl.listing_id = a.id "
        "WHERE a.id = $1",
        listing_id,
    )
    if listing is None:
        return {"available": False, "reason": "listing_not_found"}

    price = listing["price"]
    area = listing["area"]
    rooms = listing["rooms"]
    district = listing["district"]
    calculated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not price or not area or area <= 0:
        return {
            "available": True, "insufficient_data": True,
            "message": "Пока недостаточно похожих объектов для расчёта.",
            "disclaimer": CONFIRMED_SALE_DISCLAIMER, "confidence": "low",
            "sample_size": 0, "event_count": 0, "segment": None,
            "calculated_at": calculated_at,
        }

    exclude_key = f"l:{listing_id}" if listing["property_id"] is None else str(listing["property_id"])
    segment = await _pick_segment(district, rooms, exclude_key, now)

    if segment is None or segment["sample_size"] < MIN_SAMPLE_ANY or segment["event_count"] == 0:
        return {
            "available": True, "insufficient_data": True,
            "message": "Пока недостаточно похожих объектов для расчёта.",
            "disclaimer": CONFIRMED_SALE_DISCLAIMER, "confidence": "low",
            "sample_size": segment["sample_size"] if segment else 0,
            "event_count": segment["event_count"] if segment else 0,
            "segment": _segment_label(segment["tier"], district, rooms) if segment else None,
            "calculated_at": calculated_at,
        }

    obs_TE = [(t, e) for t, e, _ppm2 in segment["obs"]]
    steps = kaplan_meier(obs_TE)
    days_low_km = km_quantile(steps, 0.75)
    days_high_km = km_quantile(steps, 0.25)
    km_ok = (segment["event_count"] >= MIN_EVENTS_FOR_KM and days_low_km is not None
             and days_high_km is not None)

    if km_ok:
        days_low_base = _clamp_days(days_low_km)
        days_high_base = _clamp_days(max(days_high_km, days_low_km))
        method = "kaplan_meier"
    else:
        # KM недостоверна (п. "если Kaplan-Meier невозможно корректно
        # оценить...") — тот же проверенный baseline, что и в
        # scripts/dom_forecast_baseline_backtest.py::_baseline_segment_median,
        # с явным принуждением к низкой надёжности ниже.
        event_Ts = sorted(t for t, e in obs_TE if e == 1)
        mid = event_Ts[len(event_Ts) // 2]
        days_low_base = _clamp_days(mid * 0.75)
        days_high_base = _clamp_days(mid * 1.35)
        method = "segment_median_baseline"

    # ── ценовая чувствительность ─────────────────────────────────────────
    seg_ppm2 = [ppm2 for _t, _e, ppm2 in segment["obs"]]
    seg_ppm2_sorted = sorted(seg_ppm2)
    seg_median_ppm2 = seg_ppm2_sorted[len(seg_ppm2_sorted) // 2] if seg_ppm2_sorted else (price / area)

    import math

    def dev_from_ppm2(ppm2: float) -> float:
        return math.log(max(ppm2, 1e-6)) - math.log(max(seg_median_ppm2, 1e-6))

    def price_dev(p: float) -> float:
        # только для сценариев ТЕКУЩЕГО объявления (площадь не меняется
        # между сценариями, меняется только цена) — для строк популяции
        # своя площадь у каждой, см. dev_from_ppm2 + ppm2 из _row_to_obs.
        return dev_from_ppm2(p / area)

    events_dev = [(dev_from_ppm2(ppm2), t) for (t, e, ppm2) in segment["obs"] if e == 1]
    curve = _price_sensitivity_curve(events_dev)
    current_dev = price_dev(price)
    baseline_curve_val = _interp_curve(curve, current_dev) if curve else None

    scenarios = []
    for pct in DISCOUNT_SCENARIOS:
        scenario_price = round(price * (1 - pct / 100))
        if curve is not None and baseline_curve_val and baseline_curve_val > 0:
            target_dev = price_dev(scenario_price)
            multiplier = _interp_curve(curve, target_dev) / baseline_curve_val
        else:
            multiplier = 1.0
        scenarios.append({
            "discount_pct": pct,
            "price": scenario_price,
            "days_low": _clamp_days(days_low_base * multiplier),
            "days_high": _clamp_days(days_high_base * multiplier),
        })
    scenarios = _enforce_monotone_scenarios(scenarios)

    confidence = _confidence_level(segment["tier"], segment["event_count"], km_ok)
    if method == "segment_median_baseline":
        confidence = "low"  # п.10: baseline-фоллбэк ВСЕГДА низкая надёжность

    current_scenario = scenarios[0]
    ppm2_current = round(price / area)

    return {
        "available": True,
        "insufficient_data": False,
        "current": {
            "price": price,
            "price_per_m2": ppm2_current,
            "days_low": current_scenario["days_low"],
            "days_high": current_scenario["days_high"],
        },
        "scenarios": scenarios,
        "sample_size": segment["sample_size"],
        "event_count": segment["event_count"],
        "segment": _segment_label(segment["tier"], district, rooms),
        "fallback_level": segment["tier"],
        "confidence": confidence,
        "method": method,
        "disclaimer": CONFIRMED_SALE_DISCLAIMER,
        "calculated_at": calculated_at,
    }


async def compute_dom_scenario_cached(listing_id: str) -> dict:
    now = time.monotonic()
    hit = _cache.get(listing_id)
    if hit is not None and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = await compute_dom_scenario(listing_id)
    _cache[listing_id] = (now, value)
    return value
