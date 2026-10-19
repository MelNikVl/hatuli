#!/usr/bin/env python3
"""
Сервис парсинга КВАРТИР НА ПРОДАЖУ.
Каждые 10-20 минут:
  1. Парсит krisha.kz/prodazha/kvartiry/astana/
  2. Считает скор (yield из rental_index, bargain из аналогов)
  3. Сохраняет/обновляет apartment_listings в PostgreSQL
  4. Синкает в Google Sheets (вкладка Квартиры)

Запуск:  python service_apartments.py
Логи:    apartments.log
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
import logging
import os
import random

from dotenv import load_dotenv
load_dotenv()

# Только FileHandler — StreamHandler(stderr) дублировал КАЖДУЮ строку
# лога (найдено 2026-08-13 при разборе adaptive recheck, apartments.log
# показывал идентичные строки с точностью до миллисекунды): systemd-юнит
# уже делает StandardError=append:apartments.log/StandardOutput=append:
# apartments.log (тот же файл), так что stderr и без StreamHandler
# попадает в лог через сам systemd — с ним получалось дважды. Тот же
# паттерн, вероятно, у других сервисов с идентичной парой (StreamHandler
# + systemd StandardOutput/Error=append:тот же .log) — не трогал, вне
# объёма этой задачи.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("apartments.log", encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("apartment_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def extract_bargain(r: dict) -> dict:
    """Задача 2026-08-14 ("Bargain — единый источник") — живой баг:
    bot/core/apartment_parser.py считает торг через get_comparables()/
    analyze_bargain() (bot/core/bargain.py — ТА ЖЕ пара функций, что и
    попап /admin/api/listing/{id}) и кладёт результат ПЛОСКО на сам
    объект листинга — r["bargain_target"]/r["bargain_discount_pct"]/
    r["bargain_rec"] (см. apartment_parser.py, конец analyze_apartments).
    Код ниже (INSERT/UPDATE) читал r["score_data"]["bargain"]["target_price"]
    — ключ score_data парсер вообще не заполняет (это протухший путь от
    старой apartment_score_v2, см. docs/scoring_audit.md §5.2), поэтому
    bargain был всегда {} и три эти колонки не писались с 2026-07-25
    (дата, когда v2-путь убрали) — сам расчёт торга при этом всё это
    время оставался живым и правильным, просто терялся молча на записи
    в БД. Единственный источник истины остаётся один — bot/core/
    bargain.py; эта функция просто читает его результат оттуда, где
    парсер его реально кладёт, вместо мёртвого пути."""
    return {
        "target_price": r.get("bargain_target"),
        "discount_pct": r.get("bargain_discount_pct"),
        "recommendation": r.get("bargain_rec"),
        # Задача 2026-08-14 (Фаза A.5 п.2 вердикт-стратегии) — тот же
        # класс бага, что был у finish_level до волны 1 (scoring_roadmap.
        # md, Часть 1, п.3): apartment_parser.py считал s["comparables_
        # cnt"]/s["bargain_method"] каждый цикл, но INSERT/UPDATE ниже их
        # никогда не включал — apartment_listings.comparables_cnt был
        # 0/47016 заполнен (живая проверка перед фиксом). Теперь пишутся.
        "comparables_cnt": r.get("comparables_cnt", 0),
        "method": r.get("bargain_method"),
    }


# Абсолютные границы — защита от опечатки в app_settings (веб-терминал
# принимает произвольную строку): 1 минута снизу (иначе цикл длиной
# ~80-135мин, см. parser_cycle_history за 7 дней, запускался бы почти
# без паузы вообще — не "интервал", а постоянный стук по Крыше), 6 часов
# сверху (иначе парсер практически замирает). Задача 2026-08-17,
# "интервал apartment parser".
_INTERVAL_ABS_MIN_MINUTES = 1.0
_INTERVAL_ABS_MAX_MINUTES = 360.0
_INTERVAL_FALLBACK_MIN, _INTERVAL_FALLBACK_MAX = 30.0, 70.0


def _next_cycle_sleep_minutes(app_settings) -> float:
    """random.uniform(MIN, MAX) минут паузы между циклами — MIN/MAX
    читаются из app_settings.PARSE_INTERVAL_MIN/MAX (дефолт 30/70, см.
    bot/db/settings.py::DEFAULTS), настраиваются через /admin/settings
    без деплоя. Randomization СОХРАНЕНА (задача, явно) — не убираем
    "человечный" разброс между стартами цикла ради детерминизма.

    Валидация (задача, явно — "min < max и разумные границы"): каждое
    значение сначала КЛАМПится в [_INTERVAL_ABS_MIN_MINUTES,
    _INTERVAL_ABS_MAX_MINUTES] (защита от опечатки/дурацкого значения в
    одном поле), ЗАТЕМ если после клампа min >= max (оба поля указывают
    на одну и ту же дурацкую конфигурацию, например MIN=100 MAX=50) —
    откатываемся на дефолт 30/70 целиком, не пытаемся угадать "что
    хотел администратор" — Unknown ≠ average, тот же принцип, что и
    везде в скоринге проекта."""
    import random as _random

    raw_min = app_settings.get_float("PARSE_INTERVAL_MIN", _INTERVAL_FALLBACK_MIN)
    raw_max = app_settings.get_float("PARSE_INTERVAL_MAX", _INTERVAL_FALLBACK_MAX)
    lo = min(max(raw_min, _INTERVAL_ABS_MIN_MINUTES), _INTERVAL_ABS_MAX_MINUTES)
    hi = min(max(raw_max, _INTERVAL_ABS_MIN_MINUTES), _INTERVAL_ABS_MAX_MINUTES)
    if lo >= hi:
        log.warning("PARSE_INTERVAL_MIN=%s/MAX=%s некорректны (после границ %s >= %s) "
                    "— используется дефолт %s-%s мин", raw_min, raw_max, lo, hi,
                    _INTERVAL_FALLBACK_MIN, _INTERVAL_FALLBACK_MAX)
        lo, hi = _INTERVAL_FALLBACK_MIN, _INTERVAL_FALLBACK_MAX
    return _random.uniform(lo, hi)


# ── Дедлайн полного круга глубокого обхода ────────────────────────────
# Задача 2026-09-28 ("один круг парсинга не более 2 суток"). Раньше
# DEEP_SWEEP_BATCH был фиксированным, а длительность круга получалась
# побочным эффектом: ~824 страницы каталога / 40 страниц за цикл = ~21
# цикл, цикл ~120 мин (69.7 мин работы — среднее по parser_cycle_history
# за 14 дней — плюс 30-70 мин паузы) = ~42 ч в идеальных условиях.
# Идеальных условий не бывает: любой простой (DNS лёг, Крыша отдаёт 468)
# сдвигает весь круг целиком, и по apartments.log реальные круги
# занимали от 1.2 до 4.3 суток. Теперь батч планируется под дедлайн:
# отстали от графика — батч растёт, идём с запасом — остаётся базовым.
_CIRCLE_TARGET_HOURS_DEFAULT = 24.0
# Объявлений на странице выдачи Крыши (см. снимок max_deep_page ниже).
KRISHA_PAGE_SIZE = 20
# Планируем не на весь бюджет, а на 85% от него: оставшиеся 15% — запас
# на простой в самом конце круга, когда наращивать батч уже поздно.
_CIRCLE_PLAN_SAFETY = 0.85
# Потолок батча. Нужен только для ДОГОНА после простоя: на здоровом
# темпе (~2 ч/цикл, 24 цикла за 48 ч) хватает и 35-40 страниц. 160 —
# это ~10 мин лишних запросов выдачи к циклу (страница = sleep 2-5 с +
# сам запрос) и полный круг за 6 циклов в пределе, чего с запасом
# хватает, чтобы вернуться в график после суточного простоя.
_CIRCLE_BATCH_MAX_DEFAULT = 160
# Оценка периода цикла (работа + пауза) в минутах — сид для EMA, пока не
# набралось реальных замеров. 120 = ~70 мин цикл + ~50 мин средняя пауза.
_CYCLE_PERIOD_SEED_MIN = 120.0
# Границы правдоподобия ОДНОГО замера периода: короче — это рестарт
# сервиса/двойной запуск, длиннее — сервис стоял. Ни то, ни другое не
# должно утягивать EMA за собой.
_CYCLE_PERIOD_SAMPLE_MIN, _CYCLE_PERIOD_SAMPLE_MAX = 10.0, 360.0
_CYCLE_PERIOD_EMA_ALPHA = 0.3


def _parse_iso(raw: str | None) -> datetime | None:
    """ISO-строка из app_settings → aware datetime (или None). Наивное
    время трактуем как UTC — всё, что пишет этот сервис, пишется в UTC."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("не разобрал временную метку %r", raw)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plan_deep_batch(*, base_batch: int, max_batch: int, pages_left: int,
                     elapsed_min: float, period_min: float,
                     target_hours: float,
                     safety: float = _CIRCLE_PLAN_SAFETY) -> int:
    """Сколько страниц глубокого обхода взять в ЭТОМ цикле, чтобы круг
    уложился в target_hours.

    Возвращает base_batch, пока идём по графику (ниже базового не
    опускаем — незачем тормозить обход, если успеваем), и поднимает
    вплоть до max_batch, когда отстали. Чистая функция: всё состояние
    приходит аргументами — проверяется тестом без БД и без сети."""
    from bot.core.sale_sweep import plan_batch
    return plan_batch(base_batch=base_batch, max_batch=max_batch,
                      pages_left=pages_left, elapsed_min=elapsed_min,
                      period_min=period_min, target_hours=target_hours,
                      reference_batch=base_batch, safety=safety)["batch"]


async def _record_cycle_period(app_settings) -> None:
    """EMA реального периода цикла (работа + пауза, минуты) — по
    интервалу между соседними ПРОДУКТИВНЫМИ проходами глубокого обхода.
    Нужна планировщику батча: сколько ещё циклов влезет в остаток
    бюджета круга.

    Продуктивными — намеренно: цикл, где вся пачка легла на сети,
    проходит быстро (запросы падают сразу) и утянул бы оценку вниз, а
    планировать догон надо по здоровому темпу, иначе после простоя батч
    окажется занижен ровно тогда, когда его надо поднимать."""
    now = datetime.now(timezone.utc)
    prev = _parse_iso(app_settings.get("DEEP_SWEEP_PRODUCTIVE_AT"))
    await app_settings.set("DEEP_SWEEP_PRODUCTIVE_AT", now.isoformat())
    if not prev:
        return
    sample = (now - prev).total_seconds() / 60.0
    if not (_CYCLE_PERIOD_SAMPLE_MIN <= sample <= _CYCLE_PERIOD_SAMPLE_MAX):
        return
    current = app_settings.get_float("DEEP_SWEEP_CYCLE_PERIOD_MIN", _CYCLE_PERIOD_SEED_MIN)
    ema = _CYCLE_PERIOD_EMA_ALPHA * sample + (1.0 - _CYCLE_PERIOD_EMA_ALPHA) * current
    await app_settings.set("DEEP_SWEEP_CYCLE_PERIOD_MIN", f"{ema:.1f}")


async def _collect_sale_catalog(app_settings, analyze, *, now=None):
    """Return source receipts; durable advancement happens AFTER DB saves."""
    from bot.core.sale_sweep import SaleSweep, STATE_KEY, COMPLETED_KEY, plan_batch, utc
    from bot.core.sale_detail_persistence import analyze_with_attempt_capture
    now = now or datetime.now(timezone.utc)
    state = SaleSweep.loads(app_settings.get(STATE_KEY))
    current_journal_unreadable = bool(app_settings.get(STATE_KEY)) and state is None
    previous = state or SaleSweep.loads(app_settings.get(COMPLETED_KEY))
    if state and state.completed_at:
        # Recover a crash between the atomic current journal and its last
        # completed copy before beginning a new round.
        await app_settings.set(COMPLETED_KEY, state.dumps())
        state = None
    if state is None:
        state = SaleSweep.new(now)
        if previous:
            state.recent_unsaved = {i: checked for i, checked in previous.recent_unsaved.items()
                                    if utc(checked) >= now - timedelta(days=7)}
            state.presence_overflow = previous.presence_overflow
        if current_journal_unreadable:
            state.archive_hold_reason = "current_journal_unreadable"
            state.archive_hold_started_at = now.isoformat()
        await app_settings.set(STATE_KEY, state.dumps())
        await app_settings.set("DEEP_SWEEP_CIRCLE_STARTED_AT", state.started_at)
        log.info("Deep sweep: new evidence journal from page 1 (legacy cursor is not proof)")
    blocked = utc(state.blocked_until)
    if blocked and blocked > now:
        log.warning("Sale catalog paused until %s after source block", state.blocked_until)
        return [], state, [], {"blocked": True, "paused": True}, {}

    head_stats = {"_detail_budget_remaining": max(0, app_settings.get_int("DETAIL_FETCH_BATCH", 15))}
    head_pages = max(0, min(app_settings.get_int("PARSER_MAX_PAGES", 5), 20))
    results = await analyze_with_attempt_capture(analyze, "astana", max_pages=head_pages, stats=head_stats) if head_pages else []
    reports = list(head_stats.get("page_reports", []))
    provisional = SaleSweep.loads(state.dumps())
    provisional.account(reports, {i for p in reports for i in p.get("accepted_ids", [])}, now=now)
    cursor = provisional.next_page
    left = max(0, (provisional.expected_last_page or 0) - cursor + 1)
    current_counts = [p["total_count"] for p in reports if not p.get("error") and p.get("ids") and p.get("total_count")]
    if current_counts:
        current_last = (current_counts[-1] + KRISHA_PAGE_SIZE - 1) // KRISHA_PAGE_SIZE
        if cursor > current_last:
            # A smaller live catalog must not strand the cursor on an
            # out-of-range redirect. Re-read its actual last page and
            # require a fresh positive terminal receipt; never jump gaps.
            cursor, left = current_last, 1
    plan = plan_batch(
        base_batch=app_settings.get_int("DEEP_SWEEP_BATCH", 5),
        max_batch=app_settings.get_int("DEEP_SWEEP_BATCH_MAX", _CIRCLE_BATCH_MAX_DEFAULT),
        pages_left=left,
        elapsed_min=max(0, (now - utc(state.started_at)).total_seconds() / 60),
        period_min=app_settings.get_float("DEEP_SWEEP_CYCLE_PERIOD_MIN", _CYCLE_PERIOD_SEED_MIN),
        target_hours=app_settings.get_float("DEEP_SWEEP_CIRCLE_TARGET_HOURS", _CIRCLE_TARGET_HOURS_DEFAULT),
        reference_batch=app_settings.get_int("DEEP_SWEEP_LAST_BATCH", 40),
        page_seconds=app_settings.get_float("DEEP_SWEEP_PAGE_SECONDS", 5),
    )
    deep_stats = {"_detail_budget_remaining": head_stats["_detail_budget_remaining"]}
    if plan["batch"] and not provisional.closed and not head_stats.get("pages_failed"):
        # Sparse DB-save retries must not re-request hundreds of pages
        # already acknowledged after the gap. The next cycle resumes at
        # the following gap/new page; a bounded repair costs one cycle.
        following_done = [int(n) for n in provisional.pages if int(n) > cursor]
        batch = min(plan["batch"], min(following_done) - cursor) if following_done else plan["batch"]
        deep_results = await analyze_with_attempt_capture(analyze, "astana", max_pages=batch, start_page=cursor,
                                                         stats=deep_stats, prior_results=results)
        results.extend(deep_results or [])
        reports.extend(deep_stats.get("page_reports", []))
    elapsed = head_stats.get("page_elapsed_sec", 0) + deep_stats.get("page_elapsed_sec", 0)
    stats = {"blocked": bool(head_stats.get("blocked") or deep_stats.get("blocked")),
             "page_elapsed_sec": elapsed, "deep_pages_ok": deep_stats.get("pages_ok", 0)}
    log.info("Deep sweep: page %d, batch %d, remaining ETA %s h, 24h/configured deadline feasible=%s",
             cursor, plan["batch"], plan["estimated_remaining_hours"], plan["deadline_feasible"])
    return results, state, reports, stats, plan


async def _commit_sale_catalog(app_settings, state, reports, saved_ids, stats, plan, *, now=None):
    from bot.core.sale_sweep import STATE_KEY, COMPLETED_KEY, utc
    now = now or datetime.now(timezone.utc)
    state.account(reports, saved_ids, now=now)
    # One atomic settings write is authoritative. Projections below are
    # diagnostics only; consumers validate the full journal.
    await app_settings.set(STATE_KEY, state.dumps())
    await app_settings.set("DEEP_SWEEP_PAGE", str(state.next_page))
    await app_settings.set("DEEP_SWEEP_CIRCLE_MAX_PAGE", str(state.expected_last_page or 0))
    await app_settings.set("DEEP_SWEEP_CURRENT_UNIQUE_IDS", str(len(state.unique_ids)))
    await app_settings.set("DEEP_SWEEP_RETRY_PAGES", str(len(state.failures)))
    if reports:
        await app_settings.set("DEEP_SWEEP_LAST_AT", now.isoformat())
    if stats.get("deep_pages_ok"):
        await _record_cycle_period(app_settings)
        await app_settings.set("DEEP_SWEEP_LAST_BATCH", str(stats["deep_pages_ok"]))
    if reports and stats.get("page_elapsed_sec", 0) > 0:
        sample = max(2, min(60, stats["page_elapsed_sec"] / len(reports)))
        old = app_settings.get_float("DEEP_SWEEP_PAGE_SECONDS", 5)
        await app_settings.set("DEEP_SWEEP_PAGE_SECONDS", str(round(.3 * sample + .7 * old, 3)))
    if plan:
        await app_settings.set("DEEP_SWEEP_PLAN_V1", json.dumps({"at": now.isoformat(), **plan}))
    if state.completed_at:
        await app_settings.set(COMPLETED_KEY, state.dumps())
        duration = max(0, (now - utc(state.started_at)).total_seconds())
        for key, value in {
            "COMPLETED_AT": state.completed_at, "DURATION_SEC": duration,
            "COMPLETED_PAGES": sum(bool(p["ids"]) for p in state.pages.values()),
            "COMPLETED_TOTAL": state.denominator, "COMPLETED_UNIQUE_IDS": len(state.unique_ids),
            "COMPLETED_VERIFIED": "1", "MAX_PAGE": "0",
        }.items():
            await app_settings.set("DEEP_SWEEP_CIRCLE_" + key, str(value))
        log.info("Deep sweep closed: %.2f h, %d source IDs, %d saved IDs, coverage=%s%%, failed attempts=%d",
                 duration / 3600, len(state.unique_ids), len(state.saved_ids), state.coverage_pct, state.failed_attempts)
    return state


async def run_cycle():
    from bot.core.apartment_parser import analyze_apartments
    from bot.core.sheets_sync import sync_apartments_to_sheets_pg
    from bot.db.pg import execute as pg_exec, fetchrow as pg_get
    from bot.db import settings as app_settings

    log.info("=== Apartment cycle start ===")

    # Подтягиваем настройки, выставленные через веб-терминал (/admin/settings)
    await app_settings.load()
    results, sweep, page_reports, collection_stats, sweep_plan = await _collect_sale_catalog(
        app_settings, analyze_apartments)
    if collection_stats.get("paused"):
        return
    from bot.core import apartment_parser as _ap
    if _ap.LAST_TOTAL_FOUND is not None:
        await app_settings.set("KRISHA_TOTAL_FOUND", str(_ap.LAST_TOTAL_FOUND))

    # ── Отсев «ЖК-улиц»: названия, помеченные аудитом как улицы, не принимаем ──
    try:
        from bot.core.complex_audit import street_names as _street_names
        import re as _re_s
        _streets = await _street_names()
        from bot.core.complex_audit import _JUNK_NAMES
        _streets |= set(_JUNK_NAMES)
        if _streets:
            def _norm_s(s):
                s = (s or "").lower()
                s = _re_s.sub(r"^\s*(жк|кг)\.?\s+", "", s)
                s = _re_s.sub(r"[«»\"'()]", " ", s)
                return _re_s.sub(r"\s+", " ", s).strip()
            cleared = 0
            for r in results:
                if r.get("complex_name"):
                    _n = _norm_s(r["complex_name"])
                    if _n in _streets or len(_n) < 4:
                        r["complex_name"] = None
                        r["complex_url"] = None
                        cleared += 1
            if cleared:
                log.info("street filter: убрано %d привязок к ЖК-улицам", cleared)
    except Exception as e:
        log.warning("street filter failed: %s", e)

    new_cnt = upd_cnt = 0
    saved_ids = set()
    from bot.core.sale_detail_persistence import catalog_only_row, save_source_details, persist_actual_attempts

    for source_row in results:
        r = catalog_only_row(source_row)
        sd = r.get("score_data", {})
        bd = sd.get("breakdown", {})
        bargain = extract_bargain(r)
        reasons_json = json.dumps(sd.get("reasons", []), ensure_ascii=False)

        exists = await pg_get("SELECT id, price FROM apartment_listings WHERE id=$1", r["id"])

        try:
            if not exists:
                write_result = await pg_exec("""
                    INSERT INTO apartment_listings
                        (id, url, title, price, area, rooms, address, district, complex_name,
                         est_rent, yield_pct, net_yield_pct, payback_years,
                         score_total, score_yield, score_price_market, score_location,
                         score_apt_type, score_floor, score_complex, score_supply,
                         reasons, description, floor, floors_total,
                         year_built, building_type, renovation, furniture,
                         is_new_build, developer_name, seller_type, is_owner, trust_score,
                         rent_source, bargain_target, bargain_discount_pct, bargain_rec,
                         comparables_cnt, bargain_method,
                         details_fetched, ceiling_height, kitchen_area, photo_url,
                         first_seen, last_seen, notified)
                    VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                         $14,$15,$16,$17,$18,$19,$20,$21,
                         $22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,
                         $35,$36,$37,$38,$39,$40,
                         $41,$42,$43,$44,NOW(),NOW(),FALSE)
                    ON CONFLICT (id) DO NOTHING
                """,
                    r["id"], r.get("url"), r.get("title"), r.get("price"), r.get("area"),
                    r.get("rooms"), r.get("address"), r.get("district"), r.get("complex_name"),
                    r.get("est_rent", 0), r.get("yield_pct", 0), r.get("net_yield_pct", 0), r.get("payback_years"),
                    sd.get("total_score", 0), bd.get("yield", 0), bd.get("price_market", 0),
                    bd.get("location", 0), bd.get("apt_type", 0), bd.get("floor", 0),
                    bd.get("complex", 0), bd.get("supply", 0),
                    reasons_json, r.get("description", ""),
                    r.get("floor"), r.get("floors_total"),
                    r.get("year_built"), r.get("building_type"),
                    r.get("renovation"), r.get("furniture"),
                    r.get("is_new_build", False), r.get("developer_name"),
                    r.get("seller_type"), r.get("is_owner"), r.get("trust_score"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    bargain.get("comparables_cnt", 0), bargain.get("method"),
                    r.get("details_fetched", False), r.get("ceiling_height"),
                    r.get("kitchen_area"), r.get("photo_url"),
                )
                if write_result == "INSERT 0 1":
                    new_cnt += 1
            else:
                # История цен: фиксируем изменение — сигнал для алертов о торге
                old_price = exists["price"]
                new_price = r.get("price")
                if new_price and old_price and new_price != old_price:
                    try:
                        await pg_exec(
                            "INSERT INTO price_history (listing_id, old_price, new_price) "
                            "VALUES ($1, $2, $3)",
                            r["id"], old_price, new_price,
                        )
                        log.info("price change %s: %s -> %s", r["id"], old_price, new_price)
                    except Exception as e:
                        log.warning("price_history insert failed %s: %s", r["id"], e)
                    if new_price < old_price:
                        # Мотивированный продавец: 1-е снижение без бонуса,
                        # 2-е +5, 3-е +10 и т.д. — прибавляем к скору.
                        try:
                            from bot.db.pg import fetchval as _pg_fv2
                            drops = await _pg_fv2(
                                "SELECT COUNT(*) FROM price_history "
                                "WHERE listing_id=$1 AND new_price < old_price",
                                r["id"])
                            bonus = max(0, (int(drops or 0) - 1) * 5)
                            await pg_exec(
                                "UPDATE apartment_listings SET price_drop_bonus=$2 WHERE id=$1",
                                r["id"], bonus)
                        except Exception as e:
                            log.warning("price_drop_bonus update failed %s: %s", r["id"], e)
                # score_total и breakdown НЕ трогаем здесь — реальный скор
                # (Deal Score v3) считается отдельно в deal_score.apply_deal_scores()
                # для всей базы; перезапись их плейсхолдером 0 на каждый re-parse
                # обнуляла бы уже посчитанный скор до следующего прохода v3.
                # ВАЖНО: title/rooms/area/address/district тоже обновляем —
                # продавец может отредактировать объявление (сменить площадь,
                # число комнат и т.п.), оставив тот же URL/ID. Раньше эти поля
                # писались только при первой вставке и потом никогда не
                # синхронизировались, из-за чего карточка навсегда застревала
                # на комнатности/площади с момента первого скана (баг с
                # расхождением комнатности между Крышей и нашей аналитикой).
                write_result = await pg_exec("""
                    UPDATE apartment_listings SET
                        price=$2, est_rent=$3, yield_pct=$4, payback_years=$5,
                        reasons=$6,
                        floor=COALESCE($7, floor), floors_total=COALESCE($8, floors_total), year_built=COALESCE($9, year_built),
                        complex_name=COALESCE(NULLIF($10,''), complex_name), seller_type=COALESCE($11, seller_type), is_owner=COALESCE($12, is_owner),
                        rent_source=$13, bargain_target=$14,
                        bargain_discount_pct=$15, bargain_rec=$16,
                        details_fetched=CASE WHEN $17::boolean THEN TRUE ELSE details_fetched END, ceiling_height=COALESCE($18, ceiling_height),
                        title=COALESCE(NULLIF($19,''),title), rooms=COALESCE($20,rooms), area=COALESCE($21,area),
                        address=COALESCE(NULLIF($22,''),address), district=COALESCE(NULLIF($23,''),district),
                        net_yield_pct=$24, kitchen_area=COALESCE($25, kitchen_area),
                        photo_url=COALESCE(photo_url, $26), trust_score=$27,
                        comparables_cnt=$28, bargain_method=$29,
                        last_seen=NOW()
                    WHERE id=$1
                """,
                    r["id"], r.get("price"), r.get("est_rent", 0),
                    r.get("yield_pct", 0), r.get("payback_years"),
                    reasons_json,
                    r.get("floor"), r.get("floors_total"), r.get("year_built"),
                    r.get("complex_name"), r.get("seller_type"), r.get("is_owner"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    r.get("details_fetched", False), r.get("ceiling_height"),
                    r.get("title"), r.get("rooms"), r.get("area"),
                    r.get("address"), r.get("district"),
                    r.get("net_yield_pct", 0), r.get("kitchen_area"), r.get("photo_url"),
                    r.get("trust_score"),
                    bargain.get("comparables_cnt", 0), bargain.get("method"),
                )
                if write_result == "UPDATE 1":
                    upd_cnt += 1

            if write_result not in ("INSERT 0 1", "UPDATE 1"):
                log.warning("Sale row %s not acknowledged (%s); page remains queued", r["id"], write_result)
                await persist_actual_attempts({r['id']: source_row.get('_source_details_attempted_at')}, execute=pg_exec)
                continue
            # Detail attributes and the success timestamp are one atomic
            # write; feed placeholders cannot overwrite the previous detail.
            await save_source_details(source_row, execute=pg_exec)
            saved_ids.add(str(r["id"]))
        except Exception as e:
            log.warning("DB error %s: %s", r["id"], e)
            await persist_actual_attempts({r['id']: source_row.get('_source_details_attempted_at')}, execute=pg_exec)

    log.info("DB: +%d new, ~%d updated", new_cnt, upd_cnt)

    await _commit_sale_catalog(app_settings, sweep, page_reports, saved_ids,
                               collection_stats, sweep_plan)
    if collection_stats.get("blocked"):
        log.warning("Sale source blocked: saved successful prefix; remaining source work deferred")
        return

    # ── House-resolution (задача 2026-08-13, "применять и при первичном
    # матчинге новых объявлений") — если ЖК объявления оказался зонтиком
    # (есть дома), пробуем определить конкретный дом по адресу/токену/гео,
    # см. bot/core/house_resolution.py. Кэш "имя ЖК -> дети" на батч —
    # десятки объявлений часто на одном и том же ЖК, не долбим БД
    # заново на каждое (умбрелл почти всегда 0 детей — самый частый путь
    # дешёвый: один SELECT на уникальное имя, не на объявление).
    try:
        from bot.core.house_resolution import get_umbrella_children, resolve_house
        _umbrella_cache: dict[str, tuple[int, list[dict]] | None] = {}
        house_resolved = 0
        for r in results:
            cname = (r.get("complex_name") or "").strip()
            if not cname:
                continue
            key = cname.lower()
            if key not in _umbrella_cache:
                cx = await pg_get("SELECT id FROM complexes WHERE lower(trim(name))=lower(trim($1))", cname)
                if cx:
                    children = await get_umbrella_children(cx["id"])
                    _umbrella_cache[key] = (cx["id"], children) if children else None
                else:
                    _umbrella_cache[key] = None
            cached = _umbrella_cache[key]
            if not cached:
                continue
            umbrella_id, children = cached
            res = await resolve_house(
                umbrella_id=umbrella_id, umbrella_name=cname,
                listing_address=r.get("address"), listing_title=r.get("title"),
                listing_description=r.get("description"),
                listing_lat=r.get("lat"), listing_lon=r.get("lon"), children=children)
            if res:
                await pg_exec(
                    "UPDATE apartment_listings SET resolved_house_id=$2, house_attribution=$3, "
                    "house_attribution_detail=$4 WHERE id=$1",
                    r["id"], res["house_id"], res["method"], res["detail"])
                house_resolved += 1
        if house_resolved:
            log.info("house-resolution: %d объявлений привязано к домам зонтиков", house_resolved)
    except Exception as e:
        log.warning("house-resolution pass failed: %s", e)

    # ── Отделка: определяем по тексту, пишем finish_level ──────────────────
    # Задача 2026-08-14 ("finish_level: убрать тихий инкремент") — раньше
    # тут же правили score_total напрямую (+= adj), в обход весов модели
    # и без всякого эффекта на деле: apply_deal_scores() в этом же цикле
    # чуть ниже полностью перезаписывает score_total = deal, инкремент
    # почти всегда стирался (см. docs/scoring_audit.md §5.2 — "живой код,
    # мёртвый эффект"). Теперь finish_level — только классификация;
    # реальный вклад в скор — небольшой субкомпонент quality в Deal Score
    # v4 (см. bot/core/deal_score.py, _FINISH_QUALITY_SCORE), виден в
    # breakdown вместо невидимой прибавки к total.
    from bot.core.listing_intel import detect_finish_level
    for r in results:
        code, adj, label = detect_finish_level(r.get("title"), r.get("description"))
        if code:
            try:
                await pg_exec(
                    "UPDATE apartment_listings SET finish_level=$2 "
                    "WHERE id=$1 AND (finish_level IS DISTINCT FROM $2)",
                    r["id"], code,
                )
            except Exception as e:
                log.warning("finish update failed %s: %s", r["id"], e)

    # ── Координаты + свежепойманная архивность из детального парсера ──────
    try:
        await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_url TEXT")
    except Exception:
        pass
    for r in results:
        try:
            if r.get("lat") and r.get("lon"):
                await pg_exec(
                    "UPDATE apartment_listings SET lat=$2, lon=$3, geo_source='krisha' WHERE id=$1",
                    r["id"], r["lat"], r["lon"],
                )
            if r.get("complex_url"):
                await pg_exec(
                    "UPDATE apartment_listings SET complex_url=$2 WHERE id=$1",
                    r["id"], r["complex_url"],
                )
            if r.get("photos"):
                await pg_exec(
                    "UPDATE apartment_listings SET photos=$2::jsonb WHERE id=$1",
                    r["id"], json.dumps(r["photos"]),
                )
                # Прогреваем кэш первого фото сразу при парсинге, а не по
                # факту первого открытия попапа пользователем — раньше это
                # давало 2-3с задержку на каждое ранее непросмотренное фото
                # (см. img-proxy/prewarm_photo_cache в terminal_extras.py).
                try:
                    from terminal_extras import prewarm_photo_cache
                    asyncio.create_task(prewarm_photo_cache(r["photos"][0]))
                except Exception:
                    pass
            if r.get("seller_name"):
                await pg_exec(
                    "UPDATE apartment_listings SET seller_name=$2 WHERE id=$1",
                    r["id"], r["seller_name"],
                )
            if r.get("is_archived"):
                await pg_exec(
                    "UPDATE apartment_listings SET is_active=FALSE, archived_at=now() WHERE id=$1",
                    r["id"],
                )
        except Exception as e:
            log.warning("coords/archive update failed %s: %s", r["id"], e)

    # ── БЭКФИЛЛ координат по ВСЕЙ базе (не только текущий цикл) ────────────
    # Баг, который это чинит: раньше детали докачивались только для
    # объявлений, увиденных в ЭТОМ цикле парсинга страниц. Объявление,
    # вставленное неделю назад и не попавшее тогда в случайную выборку на
    # докачку, больше никогда не получало координат — отсюда целые районы
    # без единой точки на карте, сколько бы времени ни прошло. Этот блок
    # работает НАПРЯМУЮ с БД: берёт любые активные объявления без lat/lon,
    # независимо от того, встретились ли они в сегодняшних страницах.
    try:
        from bot.db import settings as _app_settings2
        backfill_batch = _app_settings2.get_int("COORD_BACKFILL_BATCH", 80)
        if backfill_batch > 0:
            from bot.core.coord_backfill import backfill_coords_and_complex
            res = await backfill_coords_and_complex(backfill_batch)
            if res["attempted"]:
                log.info("coord/complex backfill: координаты %d/%d, ЖК %d (батч %d)",
                         res["got_coords"], res["attempted"], res["got_complex"], backfill_batch)
    except Exception as e:
        log.warning("coord backfill failed: %s", e)

    # ── Гео по адресу: объявления без координат ставим на карту по адресу ──
    # Если у объявления нет координат с Крыши, но такой же адрес встречается
    # у других объявлений С координатами — берём центроид этих координат.
    # Помечаем geo_source='address' (приблизительная привязка, видно в попапе).
    # В качестве источника используем только "настоящие" координаты
    # (geo_source != 'address'), чтобы не было самоподкрепления.
    try:
        await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS geo_source TEXT")
        geo_cnt = await pg_exec("""
            UPDATE apartment_listings t
            SET lat = s.lat, lon = s.lon, geo_source = 'address'
            FROM (
                SELECT lower(btrim(address)) AS addr, AVG(lat) AS lat, AVG(lon) AS lon
                FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND address IS NOT NULL AND btrim(address) <> ''
                  AND COALESCE(geo_source, '') <> 'address'
                  -- Sanity-пределы Астаны: без этого один "отравленный" геокод
                  -- (координаты за сотни км от города — см. фикс в rebind.py)
                  -- усреднялся сюда и разъезжался по ВСЕМ объявлениям с тем
                  -- же текстом адреса, превращая один плохой геокод в десятки.
                  AND lat BETWEEN 50.0 AND 53.0 AND lon BETWEEN 69.0 AND 73.0
                GROUP BY 1
            ) s
            WHERE t.lat IS NULL
              AND t.address IS NOT NULL
              AND lower(btrim(t.address)) = s.addr
        """)
        try:
            n = int(str(geo_cnt).split()[-1])
        except (ValueError, IndexError):
            n = 0
        if n:
            log.info("geo by address: привязано %d объявлений по адресу", n)
    except Exception as e:
        log.warning("geo-by-address failed: %s", e)

    # ── Зоны приоритета: пересчёт бонусов для всех объявлений с координатами ──
    # ВАЖНО: раньше это блок пропускался целиком, если zones пуст (все зоны
    # удалены) — из-за этого старые zone_bonus от УЖЕ УДАЛЁННЫХ зон навсегда
    # застревали на объявлениях и продолжали влиять на скор. Теперь строки
    # без активных зон явно обнуляются (bonus=0, zone_name=None) вместо
    # того чтобы просто не трогаться.
    try:
        from bot.core.zones import load_zones, zone_bonus_for
        zones = await load_zones()
        from bot.db.pg import fetch as pg_fetch
        coords = await pg_fetch(
            "SELECT id, lat, lon, COALESCE(zone_bonus,0) AS zb FROM apartment_listings "
            "WHERE lat IS NOT NULL AND lon IS NOT NULL"
        )
        zcnt = 0
        for c in coords:
            if zones:
                # Зоны только ПОВЫШАЮТ скор (или не делают ничего, если зона
                # сама отрицательная/анти-зона) — вне любых зон бонус 0,
                # никакого штрафа за то, что объявление просто не попало
                # в нарисованную область.
                bonus, zname = zone_bonus_for(c["lat"], c["lon"], zones)
            else:
                # Зоны не нарисованы / все удалены — бонус не применяется.
                bonus, zname = 0, None
            if bonus != c["zb"]:
                await pg_exec(
                    "UPDATE apartment_listings SET zone_bonus=$2, zone_name=$3 WHERE id=$1",
                    c["id"], bonus, zname,
                )
                zcnt += 1
        if zcnt:
            log.info("zones: updated bonus for %d listings", zcnt)
    except Exception as e:
        log.warning("zone recompute failed: %s", e)

    # ── Первичка/вторичка ─────────────────────────────────────────────────
    # Правило: ВСЁ, что в стройке — первичка. Признаки:
    #   • год постройки >= текущего (дом ещё не сдан / сдаётся)
    #   • "от застройщика", "сдача в <будущий год/квартал>"
    #   • явные маркеры стадии: "на этапе строительства", "котлован", "строится"
    import re as _re_mt
    from datetime import date as _date_mt
    _cur_year = _date_mt.today().year

    def _detect_primary(title: str, desc: str, year) -> bool:
        blob = f"{title} {desc}".lower()
        if year and year >= _cur_year:
            return True
        if "от застройщика" in blob:
            return True
        # "сдача в 2026", "сдача в 4 кв. 2027", "срок сдачи 2026"
        if _re_mt.search(r"(?:срок\s+)?сдач[аи]\s+(?:в\s+)?(?:\d\s*кв\.?\s*)?20(?:2[6-9]|3\d)", blob):
            return True
        if _re_mt.search(r"на\s+этапе\s+строительств|стади[яи]\s+строительств|котлован|дом\s+строится", blob):
            return True
        return False

    for r in results:
        is_primary = _detect_primary(r.get("title", "") or "", r.get("description", "") or "",
                                     r.get("year_built"))
        try:
            # NULL → выставляем всегда; secondary → повышаем до primary, если
            # появились явные признаки стройки (обратно primary→secondary не
            # понижаем автоматически — сданный дом уточняется вручную/годом)
            await pg_exec(
                "UPDATE apartment_listings SET market_type=$2 WHERE id=$1 "
                "AND (market_type IS NULL OR (market_type='secondary' AND $2='primary'))",
                r["id"], "primary" if is_primary else "secondary",
            )
        except Exception as e:
            log.warning("market_type failed %s: %s", r["id"], e)

    # ── Отдельная модель скоринга ПЕРВИЧКИ ─────────────────────────────────
    # (developer + стадия + дисконт к вторичке + локация — вместо базовой
    # модели, где 20% веса это фиктивный для первички yield)
    try:
        from bot.core.primary_score import compute_primary_score
        from bot.db.pg import fetch as pg_fetch3

        primary_rows = await pg_fetch3("""
            SELECT id, title, description, year_built, price, area, district,
                   complex_name
            FROM apartment_listings
            WHERE market_type = 'primary' AND is_active IS NOT FALSE
              AND (primary_score_total IS NULL
                   OR last_seen > now() - interval '1 day')
            ORDER BY last_seen DESC NULLS LAST
            LIMIT 30
        """)
        for r in primary_rows:
            r = dict(r)
            developer = None
            cx = await pg_fetch3(
                "SELECT source_info FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
                r.get("complex_name") or "",
            ) if r.get("complex_name") else []
            if cx and cx[0]["source_info"]:
                si = cx[0]["source_info"]
                if isinstance(si, str):
                    si = json.loads(si)
                developer = (si.get("korter") or si.get("homsters") or {}).get("developer")

            secondary_median = None
            if r.get("district") and r.get("area"):
                med_row = await pg_fetch3("""
                    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price/NULLIF(area,0)) AS m
                    FROM apartment_listings
                    WHERE market_type = 'secondary' AND district = $1
                      AND area BETWEEN $2 * 0.7 AND $2 * 1.3
                      AND COALESCE(is_duplicate, FALSE) = FALSE AND price > 500000
                """, r["district"], r["area"])
                secondary_median = med_row[0]["m"] if med_row else None

            score, details = compute_primary_score(r, developer, secondary_median)
            await pg_exec(
                "UPDATE apartment_listings SET primary_score_total=$2, "
                "primary_score_details=$3::jsonb, score_total=$2 WHERE id=$1",
                r["id"], score, json.dumps(details, ensure_ascii=False),
            )
        if primary_rows:
            log.info("primary score: computed for %d listings", len(primary_rows))
    except Exception as e:
        log.warning("primary score failed: %s", e)

    # ── Слои скоринга (шум, школы — OSM с кешем) ──────────────────────────
    if app_settings.get_bool("OSM_LAYERS", True):
        try:
            from bot.score_layers import compute_all_layers, details_to_json
            from bot.db.pg import fetch as pg_fetch2
            # LIMIT 10 (найдено при расследовании "у объявления в разборе
            # локации есть только распашонка, остальных слоёв нет"): при
            # десятках тысяч активных объявлений и цикле раз в 50-80 мин это
            # ~220/день — реально почти никто не успевает получить шум/школы/
            # транспорт/поблизости/парки/банки, layer_details застревает с
            # одной "layout" записью (её пишет отдельный, более быстрый AI-
            # проход). Клетки сетки Overpass кешируются на 60 дней
            # (osm_cache, ~110м клетка) — в плотной застройке Астаны большая
            # часть объявлений внутри уже прогретой клетки не бьёт по
            # внешнему API вообще, sleep(1.5) ниже — доминирующая стоимость
            # прохода, а не сам запрос. Подняли лимит вчетверо; в худшем
            # случае (все клетки холодные) это +60с к циклу — не критично на
            # фоне 50-80 минут между циклами.
            candidates = await pg_fetch2("""
                SELECT id, lat, lon FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND is_active IS NOT FALSE
                  AND (layers_computed_at IS NULL
                       OR layers_computed_at < now() - interval '30 days')
                ORDER BY score_total DESC NULLS LAST
                LIMIT 40
            """)
            for c in candidates:
                adj, details = await compute_all_layers(dict(c))
                await pg_exec(
                    "UPDATE apartment_listings SET layer_bonus=$2, "
                    "layer_details=$3::jsonb, layers_computed_at=now() WHERE id=$1",
                    c["id"], adj, details_to_json(details),
                )
                await asyncio.sleep(1.5)  # вежливость к Overpass
            if candidates:
                log.info("layers: computed for %d listings", len(candidates))
        except Exception as e:
            log.warning("layers failed: %s", e)

    # ── AI-анализ текста (DeepSeek, включается настройкой) ────────────────
    # Не трогает Крышу вообще (только DeepSeek, ~$0.0001/объявление) — лимит
    # держим щедрым, чтобы разобрать весь бэклог за разумное число дней, а
    # не годы (было 10/цикл ~= 240/день на 23к+ объявлений).
    if app_settings.get_bool("AI_TEXT_ANALYSIS", False):
        try:
            from bot.core.ai_text_analysis import analyze_top_listings
            await analyze_top_listings(limit=150)
        except Exception as e:
            log.warning("ai text analysis failed: %s", e)

    # ── Живая статистика ЖК: активные в продаже / продано / аренда ────────
    # (раньше listings_count был заморожен со времён миграции — отсюда
    # "рейтинг 1" у большинства ЖК; теперь пересчитывается каждый цикл)
    try:
        from bot.core.complex_ingest_identity import RESOLVER_VERSION
        from bot.core.listing_complex_ingest import seed_listing_complexes
        from bot.core.complex_metrics import CURRENT_METRICS_SQL, CENTROID_SQL, IDENTITY_CTES
        seed_stats = await seed_listing_complexes()
        log.info("complex catalog ingest (%s): %s", RESOLVER_VERSION, seed_stats)
        await pg_exec(CURRENT_METRICS_SQL)
        await pg_exec(CENTROID_SQL)
        await pg_exec(IDENTITY_CTES + """
            UPDATE complexes c SET photo_url = s.photo
            FROM (
                SELECT DISTINCT ON (cid) cid, photos->>0 AS photo
                FROM listings WHERE cid IS NOT NULL AND photos IS NOT NULL
                ORDER BY cid, is_active DESC NULLS LAST, first_seen DESC
            ) s WHERE c.id = s.cid AND c.photo_url IS NULL
        """)
    except Exception as e:
        log.warning("complex stats refresh failed: %s", e)

    # ── Проверка архивности объявлений ─────────────────────────────────────
    # Было limit=15/цикл — при ~22k+ активных объявлений и цикле ~60-90 мин
    # это давало охват ~15-20/час, то есть полный проход по базе занимал бы
    # больше месяца. Подняли дефолт на порядок; настраивается в /admin/settings.
    try:
        from bot.core.archive_check import check_archived
        archive_batch = app_settings.get_int("ARCHIVE_CHECK_BATCH", 150)
        res = await check_archived(limit=archive_batch)
        log.info("archive check: %s", res)
    except Exception as e:
        log.warning("archive check failed: %s", e)

    # ── Снимок для графика "объявления без фото во времени" (/admin/analytics) ──
    try:
        from bot.db.pg import execute as _pg_exec3, fetchval as _pg_fv3
        _total_active = await _pg_fv3(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _no_photo = await _pg_fv3(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND (photos IS NULL OR photos::text IN ('[]', 'null'))") or 0
        await _pg_exec3(
            "INSERT INTO no_photo_stats_history (total_active, no_photo) VALUES ($1, $2)",
            _total_active, _no_photo)
    except Exception as e:
        log.warning("no_photo snapshot failed: %s", e)

    try:
        from bot.db.pg import execute as _pg_exec4, fetchval as _pg_fv4
        _total_active2 = await _pg_fv4(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_floor = await _pg_fv4(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND floor IS NOT NULL") or 0
        await _pg_exec4(
            "INSERT INTO floor_stats_history (total_active, with_floor) VALUES ($1, $2)",
            _total_active2, _with_floor)
    except Exception as e:
        log.warning("floor snapshot failed: %s", e)

    # Снимок для графика "покрытие данными о высоте потолка во времени"
    # (/admin/analytics/ceiling) — тот же принцип, что и floor_stats_history:
    # потолок тоже приходит только с детальной страницы объявления.
    try:
        from bot.db.pg import execute as _pg_exec_ceil, fetchval as _pg_fv_ceil
        _total_active3 = await _pg_fv_ceil(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_ceiling = await _pg_fv_ceil(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND ceiling_height IS NOT NULL") or 0
        await _pg_exec_ceil(
            "INSERT INTO ceiling_stats_history (total_active, with_ceiling) VALUES ($1, $2)",
            _total_active3, _with_ceiling)
    except Exception as e:
        log.warning("ceiling snapshot failed: %s", e)

    # Снимок для графика "покрытие данными о годе постройки во времени"
    # (/admin/analytics/year) — год берём с самого объявления, а если там
    # пусто — с его ЖК (a.year_built чаще пусто, чем у самого ЖК, который
    # заполняется вручную/через Korter/Homsters).
    try:
        from bot.db.pg import execute as _pg_exec_year, fetchval as _pg_fv_year
        _total_active_y = await _pg_fv_year(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_year = await _pg_fv_year("""
            SELECT COUNT(*) FROM apartment_listings a
            LEFT JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
            WHERE a.is_active IS NOT FALSE
              AND COALESCE(a.year_built, c.year_built) IS NOT NULL
        """) or 0
        await _pg_exec_year(
            "INSERT INTO year_stats_history (total_active, with_year) VALUES ($1, $2)",
            _total_active_y, _with_year)
    except Exception as e:
        log.warning("year snapshot failed: %s", e)

    # Снимок просмотров по комнатности во времени (/admin/analytics/views) —
    # views_count накопительный с даты публикации, поэтому график по факту
    # показывает суммарный накопленный интерес по типам квартир на срезах
    # раз в цикл, а не прирост за конкретный день.
    try:
        from bot.db.pg import execute as _pg_exec5, fetchval as _pg_fv5
        _v1 = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms = 1") or 0
        _v2 = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms = 2") or 0
        _v3 = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms = 3") or 0
        _v4p = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms >= 4") or 0
        await _pg_exec5(
            "INSERT INTO views_stats_history (views_1, views_2, views_3, views_4p) VALUES ($1, $2, $3, $4)",
            _v1, _v2, _v3, _v4p)
    except Exception as e:
        log.warning("views snapshot failed: %s", e)

    # Снимок покрытия данными о просмотрах во времени (/admin/analytics/views)
    # — сколько активных объявлений вообще имеют известный views_count,
    # тот же принцип, что и ceiling_stats_history выше. views_count качает
    # отдельный медленный микросервис krisha-viewcount (Playwright).
    try:
        from bot.db.pg import execute as _pg_exec_vc, fetchval as _pg_fv_vc
        _total_active_vc = await _pg_fv_vc(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_views = await _pg_fv_vc(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND views_count IS NOT NULL") or 0
        await _pg_exec_vc(
            "INSERT INTO views_coverage_history (total_active, with_views) VALUES ($1, $2)",
            _total_active_vc, _with_views)
    except Exception as e:
        log.warning("views coverage snapshot failed: %s", e)

    # === Мониторинг сервера/проекта (CPU/память/диск/размер) — снимок раз
    # в цикл, см. /admin/settings ===
    try:
        from bot.core.system_stats import snapshot_system_stats
        await snapshot_system_stats()
    except Exception as e:
        log.warning("system stats snapshot failed: %s", e)

    # Finish/dedup -> current rental inputs -> Deal Score -> Google Sheets.
    try:
        # === Отделка (черновая/с отделкой/с мебелью) — бесплатная эвристика
        # по тексту, см. bot/core/finish_classify.py ===
        if app_settings.get_bool("AI_FINISH_CLASSIFY", True):
            try:
                from bot.core.finish_classify import apply_finish_classification
                await apply_finish_classification()
            except Exception as e:
                log.warning("finish classification failed: %s", e)

        # === Дедупликация (приоритет объявлений от хозяина) ===
        try:
            from bot.core.dedup_listings import deduplicate_apartment_listings
            dup_count = await deduplicate_apartment_listings()
            if dup_count:
                log.info("Deduplicated %d apartment listings", dup_count)
        except Exception as e:
            log.warning("Apartment deduplication failed: %s", e)

        try:
            from bot.core.rental_economics_refresh import refresh_all, rebuild_benchmarks
            snapshot_result = await refresh_all()
            log.info("Rental scenario snapshots: %s", snapshot_result)
            await rebuild_benchmarks()
        except Exception as exc:
            log.warning("Rental scenario refresh unavailable (%s)", type(exc).__name__)

        # === Гексагональный анализ цены (микролокальный Deal Index) ===
        try:
            from bot.core.deal_score import apply_deal_scores
            await apply_deal_scores()
            # Топ-10 по скору пересчитывается каждый цикл парсера квартир
            # (см. app_settings.PARSE_INTERVAL_*/random.uniform(50,80) мин) —
            # штамп нужен только чтобы показать "когда в последний раз" в аналитике.
            await app_settings.set("DEAL_SCORE_LAST_RUN_AT",
                                    datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning("hex price failed: %s", e)

        # Раньше синкали КАЖДЫЙ цикл парсера (~50-80 мин, т.е. десятки раз в
        # сутки) — Sheets не нужен настолько часто, а лишняя нагрузка на API
        # Google (и на сам цикл) того не стоит. Раз в сутки достаточно.
        # (datetime/timezone уже импортированы на уровне модуля — этот
        # повторный локальный импорт делал имя "datetime" локальным для ВСЕЙ
        # функции run_cycle, включая код глубокого обхода ВЫШЕ по тексту,
        # который выполняется раньше этой строки: там падало
        # UnboundLocalError на каждом завершении круга обхода, из-за чего
        # DEEP_SWEEP_PAGE никогда не сохранялся и курсор навсегда застревал
        # на последней странице вместо сброса на страницу 9 — обход
        # фактически не работал.)
        last_sync = app_settings.get("SHEETS_APARTMENTS_SYNCED_AT", "")
        needs_sync = True
        if last_sync:
            try:
                last_dt = datetime.fromisoformat(last_sync)
                needs_sync = (datetime.now(timezone.utc) - last_dt).total_seconds() > 20 * 3600
            except ValueError:
                needs_sync = True
        if needs_sync:
            await sync_apartments_to_sheets_pg()
            log.info("Google Sheets: Квартиры synced")
            await app_settings.set("SHEETS_APARTMENTS_SYNCED_AT",
                                   datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.warning("Sheets sync failed: %s", e)

    log.info("=== Apartment cycle done ===\n")


async def _run_cycle_timed():
    """run_cycle() обёрнутый таймингом + счётчиком реальных HTTP-запросов
    к Крыше за цикл — снимок в parser_cycle_history для графиков
    "сколько идёт парсинг" и "нагрузка на Крышу" на /admin/parser.
    Плюс (см. задачу "оптимизация работы парсеров" — пропуск detail-fetch
    для объявлений без изменений) — эффективность этой оптимизации за
    цикл: total_seen/needs_detail_fetch/skipped_no_change, см.
    apartment_parser.DETAIL_FETCH_STATS. Показывается на
    /admin/parsers?tab=recheck, секция "Нагрузка на Крышу"."""
    import time as _time
    from bot.core.apartment_parser import (
        REQUEST_COUNTS as _search_counts, DETAIL_FETCH_STATS as _df_stats)
    from bot.core.apartment_details import REQUEST_COUNTS as _detail_counts
    from bot.core.archive_check import REQUEST_COUNTS as _archive_counts, LAST_CHECK_RESULT as _archive_result
    _search_counts["search"] = 0
    _detail_counts["detail"] = 0
    _df_stats["total_seen"] = 0
    _df_stats["needs_fetch"] = 0
    _df_stats["skipped"] = 0
    _archive_counts["archive_check"] = 0
    _archive_result.clear()
    started = _time.monotonic()
    try:
        await run_cycle()
    finally:
        duration_sec = _time.monotonic() - started
        try:
            from bot.db.pg import execute as _pg_exec_cycle
            await _pg_exec_cycle(
                "INSERT INTO parser_cycle_history "
                "(duration_sec, search_requests, detail_requests, "
                " total_seen, needs_detail_fetch, skipped_no_change, "
                " archive_check_requests, archive_hot_checked, "
                " archive_cold_confirm_checked, archive_backlog_checked) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                round(duration_sec), _search_counts["search"], _detail_counts["detail"],
                _df_stats["total_seen"], _df_stats["needs_fetch"], _df_stats["skipped"],
                _archive_counts["archive_check"], _archive_result.get("hot_checked", 0),
                _archive_result.get("cold_confirm_checked", 0), _archive_result.get("backlog_checked", 0))
        except Exception as e:
            log.warning("parser_cycle_history snapshot failed: %s", e)


async def main():
    from bot.db.pg import init_pool, execute as _pg_exec_init
    await init_pool(DATABASE_URL)
    await _pg_exec_init("""
        CREATE TABLE IF NOT EXISTS parser_cycle_history (
            id SERIAL PRIMARY KEY,
            at TIMESTAMPTZ DEFAULT now(),
            duration_sec INT,
            search_requests INT,
            detail_requests INT,
            total_seen INT,
            needs_detail_fetch INT,
            skipped_no_change INT
        )
    """)
    # Бэкфилл колонок для инсталляций, где таблица уже существовала до
    # оптимизации detail-fetch (см. migrations/034_parser_cycle_detail_fetch.sql).
    # archive_check_* — задача "adaptive recheck", 2026-08-13 (см.
    # docs/adaptive_recheck_plan.md): раньше archive_check.py вообще не
    # учитывался в "запросы/сутки" — свой httpx.AsyncClient, отдельный от
    # apartment_parser/apartment_details, чей REQUEST_COUNTS уже снимался.
    for _col in ("total_seen", "needs_detail_fetch", "skipped_no_change",
                 "archive_check_requests", "archive_hot_checked",
                 "archive_cold_confirm_checked", "archive_backlog_checked"):
        try:
            await _pg_exec_init(f"ALTER TABLE parser_cycle_history ADD COLUMN IF NOT EXISTS {_col} INT")
        except Exception as e:
            log.warning("parser_cycle_history ALTER (%s) failed: %s", _col, e)
    from bot.db import settings as app_settings
    from bot.git_info import git_hash as _git_hash
    git_hash = _git_hash()
    await app_settings.load()
    await app_settings.set("APARTMENT_SERVICE_GIT_HASH", git_hash)
    await app_settings.set("APARTMENT_SERVICE_STARTED_AT", datetime.now(timezone.utc).isoformat())
    log.info("=== Apartment service started (git=%s) ===", git_hash)

    # Первый цикл сразу
    try:
        await _run_cycle_timed()
    except Exception as e:
        log.error("First cycle error: %s", e, exc_info=True)

    while True:
        # Свежий load() именно ЗДЕСЬ (не полагаемся на load() внутри уже
        # завершившегося run_cycle()) — тот кеш снят в НАЧАЛЕ цикла, а
        # цикл (см. parser_cycle_history) идёт 80-135 минут; изменение
        # PARSE_INTERVAL_MIN/MAX через /admin/settings посреди цикла
        # иначе подхватилось бы только со следующего после него.
        await app_settings.load()
        sleep_min = _next_cycle_sleep_minutes(app_settings)
        log.info("Next cycle in %.0f min...", sleep_min)
        await asyncio.sleep(sleep_min * 60)
        try:
            await _run_cycle_timed()
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
