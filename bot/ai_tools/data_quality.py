"""bot/ai_tools/data_quality.py — read-only collector health snapshot,
Этап 3 задачи "Hatuli API v1 для AI tools" (foundation под будущий Data
Quality Agent). Никакого LLM, никаких autonomous writes — этот модуль
ТОЛЬКО читает уже существующие временные таблицы и раскладывает факты по
структуре {status, findings}. Ничего не чинит и не пишет.

## Что проверяется и как (честно, без выдумывания)

  1. Freshness — по каждому "коллектору" из реестра `COLLECTORS` ниже:
     максимальный timestamp в его ключевой temporal-таблице против
     ожидаемого максимального разрыва (`expected_max_gap_minutes` —
     ЭВРИСТИКА на основе циклов systemd-таймеров, см. комментарий у
     каждой записи, НЕ SLA и не научно калиброванное число).
  2. Sudden volume drop — количество строк, тронутых за последние 24ч, vs
     предыдущие 24ч, по той же timestamp-колонке. Порог падения (по
     умолчанию >=50% при baseline >=5 строк) — тоже эвристика, явно
     помечена как таковая в docstring `_evaluate_volume_drop`.
  3. Coverage degradation (NULL rate) — ТОЛЬКО там, где это уже честно
     измеримо одним запросом (`COVERAGE_CHECKS`): доля NULL в колонке,
     обычно проставляемой enrichment-шагом (координаты после geobind/coord
     backfill), у когорты объявлений "первый раз увиденных" в последние
     24ч vs предыдущие 24ч.

## Что СОЗНАТЕЛЬНО НЕ проверяется в этом фундаменте

  - Состояние systemd (`systemctl status`/journalctl) — потребовало бы
    subprocess/root-доступа из веб-процесса, ломает переносимость и
    тестируемость (нет реального systemd в CI). Симптом "коллектор не
    пишет" уже честно виден через freshness/volume-drop по данным в БД —
    этого достаточно для read-only foundation. Прямая интеграция с
    systemd — естественное расширение, не сделано здесь намеренно (см.
    финальный отчёт задачи).
  - Логи/трейсбеки (`*.log` на диске) — тот же принцип: файлы логов не
    универсальны между средами (dev/prod/CI), путь неустойчив, а
    прочитанный текст ошибки всё равно не заменяет структурированный факт
    "данные не обновляются" — не добавлено ради количества, которое
    выглядело бы как анализ логов, но им не является.
  - NULL/coverage деградация для ЛЮБОЙ колонки любой таблицы — только два
    случая ниже, где когорта ("первый раз увидели в последние 24ч") и
    колонка (координаты) осмысленны и уже используются в проде похожим
    образом (bot/core/coord_backfill.py и др.) — не изобретаем произвольные
    пороги для колонок, где "процент NULL" ничего не значит без контекста.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

SEVERITY_RANK = {"ok": 0, "warning": 1, "critical": 2}


def _assert_safe_identifier(name: str) -> None:
    """Реестр ниже — статическая конфигурация в этом файле, не пользовательский
    ввод, но имена таблиц/колонок всё равно интерполируются в SQL (asyncpg
    не параметризует идентификаторы) — этот guard документирует и
    проверяет допущение "только буквы/цифры/подчёркивание", а не
    полагается на него молча."""
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"unsafe SQL identifier in data-quality registry: {name!r}")


# ── реестр "какие коллекторы должны писать данные" ──────────────────────
# expected_max_gap_minutes — цикл systemd-таймера (см. systemctl list-timers
# krisha-*, README "Синхронизация из Krisha") + запас (для дневных задач —
# 24ч + 2ч запаса = 1560 мин, для недельной — 7 дней + запас).
COLLECTORS: list[dict] = [
    {
        "component": "krisha-apartments",
        "table": "apartment_listings", "timestamp_column": "last_seen",
        "expected_max_gap_minutes": 120,
        "description": "Основной парсер продаж (цикл 30-120 мин)",
    },
    {
        "component": "krisha-rental",
        "table": "rental_listings", "timestamp_column": "last_seen",
        "expected_max_gap_minutes": 15,
        "description": "Парсер аренды (цикл 5-15 мин)",
    },
    {
        "component": "krisha-viewcount",
        "table": "views_history", "timestamp_column": "observed_at",
        "expected_max_gap_minutes": 90,
        "description": "Playwright-сборщик реальных просмотров (цикл ~1ч)",
    },
    {
        "component": "krisha-property-identity-incremental",
        "table": "property_listings", "timestamp_column": "linked_at",
        "expected_max_gap_minutes": 30,
        "description": "Инкрементальное поддержание Property Identity (таймер 15 мин)",
    },
    {
        "component": "krisha-listing-snapshot",
        "table": "listing_snapshots", "timestamp_column": "observed_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный снимок объявлений (вердикт-стратегия, Фаза A)",
    },
    {
        "component": "krisha-deal-score-snapshot",
        "table": "deal_score_snapshots", "timestamp_column": "observed_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный снимок Deal Score",
    },
    {
        "component": "krisha-complex-stats",
        "table": "complex_stats_history", "timestamp_column": "computed_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный снимок статистики ЖК",
    },
    {
        "component": "krisha-hex-market-stats",
        "table": "hex_market_stats", "timestamp_column": "computed_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный снимок плотности предложения по гексагону",
    },
    {
        "component": "krisha-outcome-labels",
        "table": "outcome_labels", "timestamp_column": "computed_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный пересчёт outcome_labels",
    },
    {
        "component": "krisha-seller-profile",
        "table": "seller_profiles", "timestamp_column": "computed_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный снимок профилей продавцов",
    },
    {
        "component": "krisha-reviews-collect",
        "table": "reviews_raw", "timestamp_column": "fetched_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный сбор отзывов из всех источников",
    },
    {
        "component": "krisha-crime",
        "table": "crime_incidents", "timestamp_column": "fetched_at",
        "expected_max_gap_minutes": 1560,
        "description": "Ежедневный инкрементальный сбор КПСиСУ (известно нестабилен "
                        "при истёкшем TLS-сертификате источника — намеренно оставлен "
                        "в реестре, чтобы snapshot честно это показывал)",
    },
    {
        "component": "krisha-newbuild",
        "table": "newbuild_units", "timestamp_column": "last_seen_at",
        "expected_max_gap_minutes": 7 * 24 * 60 + 24 * 60,  # неделя + сутки запаса
        "description": "Еженедельный скан новостроек (все импортёры застройщиков)",
    },
]

# ── coverage-деградация (NULL rate) — только там, где когорта и колонка
# однозначны ──────────────────────────────────────────────────────────
COVERAGE_CHECKS: list[dict] = [
    {
        "component": "krisha-geobind (apartment coverage)",
        "table": "apartment_listings", "cohort_column": "first_seen", "null_column": "lat",
        "description": "Доля новых объявлений продажи без координат (geobind должен "
                        "проставлять их вскоре после появления)",
    },
    {
        "component": "krisha-rental (coord coverage)",
        "table": "rental_listings", "cohort_column": "found_at", "null_column": "lat",
        "description": "Доля новых объявлений аренды без координат",
    },
]


async def _fetch_component_counts(table: str, ts_col: str, now: datetime) -> dict:
    from bot.db.pg import fetchrow

    _assert_safe_identifier(table)
    _assert_safe_identifier(ts_col)
    row = await fetchrow(
        f"SELECT max({ts_col}) AS latest, "
        f"count(*) FILTER (WHERE {ts_col} > $1) AS recent_24h, "
        f"count(*) FILTER (WHERE {ts_col} > $2 AND {ts_col} <= $1) AS prior_24h "
        f"FROM {table}",
        now - timedelta(hours=24), now - timedelta(hours=48),
    )
    return dict(row) if row else {"latest": None, "recent_24h": 0, "prior_24h": 0}


async def _fetch_null_rate(table: str, cohort_col: str, null_col: str,
                            since: datetime, until: datetime | None = None) -> dict:
    from bot.db.pg import fetchrow

    _assert_safe_identifier(table)
    _assert_safe_identifier(cohort_col)
    _assert_safe_identifier(null_col)
    if until is None:
        row = await fetchrow(
            f"SELECT count(*) AS n, count(*) FILTER (WHERE {null_col} IS NULL) AS n_null "
            f"FROM {table} WHERE {cohort_col} > $1",
            since,
        )
    else:
        row = await fetchrow(
            f"SELECT count(*) AS n, count(*) FILTER (WHERE {null_col} IS NULL) AS n_null "
            f"FROM {table} WHERE {cohort_col} > $1 AND {cohort_col} <= $2",
            since, until,
        )
    return dict(row) if row else {"n": 0, "n_null": 0}


# ── pure evaluators (никакого DB/IO — юнит-тестируемые синтетическими
# входами на "нормальное" и "stale" состояние без похода в реальную БД) ──

def _evaluate_freshness(component: str, table: str, ts_col: str, latest: datetime | None,
                         now: datetime, expected_max_gap_minutes: float) -> dict | None:
    if latest is None:
        return {
            "component": component, "severity": "critical",
            "fact": f"{table}.{ts_col} has no rows at all — collector has never written data "
                    f"(or table is empty)",
            "evidence": {"table": table, "timestamp_column": ts_col, "latest": None},
            "recommended_action": f"check that the {component} collector/service is running and has DB access",
        }
    gap_minutes = (now - latest).total_seconds() / 60.0
    if gap_minutes <= expected_max_gap_minutes:
        return None
    ratio = gap_minutes / expected_max_gap_minutes if expected_max_gap_minutes else float("inf")
    severity = "critical" if ratio >= 5 else "warning"
    return {
        "component": component, "severity": severity,
        "fact": f"{table}.{ts_col} last updated {round(gap_minutes, 1)} min ago "
                f"(expected max gap {expected_max_gap_minutes} min)",
        "evidence": {
            "table": table, "timestamp_column": ts_col, "latest": latest.isoformat(),
            "gap_minutes": round(gap_minutes, 1), "expected_max_gap_minutes": expected_max_gap_minutes,
        },
        "recommended_action": f"check systemd status/logs for the {component} collector "
                               f"(stopped, crashing, or blocked upstream — e.g. expired TLS cert, "
                               f"changed source schema, rate-limited)",
    }


def _evaluate_volume_drop(component: str, table: str, recent: int, prior: int,
                           min_baseline: int = 5, drop_ratio: float = 0.5) -> dict | None:
    if prior < min_baseline:
        return None  # baseline слишком мал, чтобы честно назвать это "падением"
    if recent >= prior * (1 - drop_ratio):
        return None
    pct = round((1 - recent / prior) * 100, 1) if prior else 100.0
    severity = "critical" if recent == 0 else "warning"
    return {
        "component": component, "severity": severity,
        "fact": f"{table}: {recent} rows touched in the last 24h vs {prior} in the previous 24h "
                f"({pct}% drop)",
        "evidence": {"table": table, "recent_24h": recent, "prior_24h": prior, "drop_pct": pct},
        "recommended_action": f"check whether the {component} collector is running at its usual "
                               f"cadence and whether the upstream source is blocking/rate-limiting it",
    }


def _evaluate_coverage(component: str, table: str, column: str,
                        recent_n: int, recent_null: int, prior_n: int, prior_null: int,
                        min_rows: int = 20, jump_threshold_pp: float = 15.0) -> dict | None:
    if recent_n < min_rows or prior_n < min_rows:
        return None  # выборка слишком мала, чтобы честно измерить сдвиг
    recent_rate = recent_null / recent_n * 100
    prior_rate = prior_null / prior_n * 100
    jump = recent_rate - prior_rate
    if jump < jump_threshold_pp:
        return None
    severity = "critical" if recent_rate >= 50 else "warning"
    return {
        "component": component, "severity": severity,
        "fact": f"{table}.{column} NULL rate jumped from {round(prior_rate, 1)}% to "
                f"{round(recent_rate, 1)}% (cohorts: rows first observed in the respective 24h window)",
        "evidence": {
            "table": table, "column": column,
            "recent_n": recent_n, "recent_null": recent_null,
            "prior_n": prior_n, "prior_null": prior_null,
            "recent_null_rate_pct": round(recent_rate, 1), "prior_null_rate_pct": round(prior_rate, 1),
        },
        "recommended_action": f"check the enrichment step responsible for {table}.{column} "
                               f"— coverage regression suggests it stopped running or started failing silently",
    }


async def build_data_quality_snapshot(now: datetime | None = None) -> dict:
    """Единственная точка входа. Read-only — только SELECT'ы (через
    fetchrow), никаких execute()/INSERT/UPDATE. `now` — параметр только
    для тестов (детерминированные synthetic-сценарии); в проде всегда
    вызывается без аргумента."""
    now = now or datetime.now(timezone.utc)
    findings: list[dict] = []

    for c in COLLECTORS:
        counts = await _fetch_component_counts(c["table"], c["timestamp_column"], now)
        f = _evaluate_freshness(c["component"], c["table"], c["timestamp_column"],
                                 counts["latest"], now, c["expected_max_gap_minutes"])
        if f:
            findings.append(f)
        vf = _evaluate_volume_drop(c["component"], c["table"], counts["recent_24h"], counts["prior_24h"])
        if vf:
            findings.append(vf)

    for cc in COVERAGE_CHECKS:
        recent = await _fetch_null_rate(cc["table"], cc["cohort_column"], cc["null_column"],
                                         now - timedelta(hours=24))
        prior = await _fetch_null_rate(cc["table"], cc["cohort_column"], cc["null_column"],
                                        now - timedelta(hours=48), now - timedelta(hours=24))
        cf = _evaluate_coverage(cc["component"], cc["table"], cc["null_column"],
                                 recent["n"], recent["n_null"], prior["n"], prior["n_null"])
        if cf:
            findings.append(cf)

    findings.sort(key=lambda f: SEVERITY_RANK.get(f["severity"], 0), reverse=True)
    status = "ok"
    for f in findings:
        if SEVERITY_RANK.get(f["severity"], 0) > SEVERITY_RANK[status]:
            status = f["severity"]

    return {
        "status": status,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collectors_checked": len(COLLECTORS),
        "coverage_checks_run": len(COVERAGE_CHECKS),
        "findings": findings,
    }
