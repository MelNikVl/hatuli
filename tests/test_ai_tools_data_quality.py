"""tests/test_ai_tools_data_quality.py — Этап 3 задачи "Hatuli API v1 для
AI tools": data quality snapshot. Часть 1 — pure evaluators на
синтетических datetime (нормальное И stale состояние, без похода в
реальную БД — реальные production-таблицы нельзя надёжно сделать "stale"
фикстурой, не тронув живые данные). Часть 2 — один smoke-тест реального
build_data_quality_snapshot() против настоящей БД (форма ответа, не
конкретный статус — тот зависит от текущего состояния прод-коллекторов)."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

from bot.ai_tools.data_quality import (
    _evaluate_freshness, _evaluate_volume_drop, _evaluate_coverage,
    build_data_quality_snapshot, COLLECTORS, COVERAGE_CHECKS,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


# ── freshness: normal state ──────────────────────────────────────────────

def test_freshness_ok_within_expected_gap():
    latest = NOW - timedelta(minutes=30)
    finding = _evaluate_freshness("krisha-apartments", "apartment_listings", "last_seen",
                                   latest, NOW, expected_max_gap_minutes=120)
    assert finding is None


# ── freshness: stale state ───────────────────────────────────────────────

def test_freshness_warning_when_gap_exceeded_moderately():
    latest = NOW - timedelta(minutes=300)  # 2.5x expected
    finding = _evaluate_freshness("krisha-apartments", "apartment_listings", "last_seen",
                                   latest, NOW, expected_max_gap_minutes=120)
    assert finding is not None
    assert finding["severity"] == "warning"
    assert finding["component"] == "krisha-apartments"
    assert "gap_minutes" in finding["evidence"]


def test_freshness_critical_when_gap_far_exceeded():
    latest = NOW - timedelta(hours=20)  # >>5x expected (120 min)
    finding = _evaluate_freshness("krisha-crime", "crime_incidents", "fetched_at",
                                   latest, NOW, expected_max_gap_minutes=120)
    assert finding is not None
    assert finding["severity"] == "critical"


def test_freshness_critical_when_never_observed():
    finding = _evaluate_freshness("krisha-newbuild", "newbuild_units", "last_seen_at",
                                   None, NOW, expected_max_gap_minutes=1440)
    assert finding is not None
    assert finding["severity"] == "critical"
    assert finding["evidence"]["latest"] is None


# ── volume drop ───────────────────────────────────────────────────────────

def test_volume_drop_none_when_stable():
    assert _evaluate_volume_drop("krisha-rental", "rental_listings", recent=100, prior=95) is None


def test_volume_drop_flagged_on_sudden_drop():
    finding = _evaluate_volume_drop("krisha-rental", "rental_listings", recent=5, prior=100)
    assert finding is not None
    assert finding["severity"] == "warning"


def test_volume_drop_critical_when_zero():
    finding = _evaluate_volume_drop("krisha-rental", "rental_listings", recent=0, prior=100)
    assert finding is not None
    assert finding["severity"] == "critical"


def test_volume_drop_ignored_below_baseline():
    """prior < min_baseline — слишком мало данных, чтобы честно назвать
    это падением (не выдумываем сигнал на шуме)."""
    assert _evaluate_volume_drop("x", "t", recent=0, prior=2) is None


# ── coverage degradation ───────────────────────────────────────────────────

def test_coverage_ok_when_stable():
    finding = _evaluate_coverage("krisha-geobind", "apartment_listings", "lat",
                                  recent_n=100, recent_null=5, prior_n=100, prior_null=4)
    assert finding is None


def test_coverage_flagged_on_null_rate_jump():
    finding = _evaluate_coverage("krisha-geobind", "apartment_listings", "lat",
                                  recent_n=100, recent_null=60, prior_n=100, prior_null=5)
    assert finding is not None
    assert finding["severity"] == "critical"  # 60% >= 50 threshold
    assert finding["evidence"]["recent_null_rate_pct"] == 60.0


def test_coverage_ignored_below_min_rows():
    finding = _evaluate_coverage("x", "t", "c", recent_n=3, recent_null=3, prior_n=3, prior_null=0)
    assert finding is None


# ── registry sanity ────────────────────────────────────────────────────────

def test_registry_has_expected_shape():
    assert len(COLLECTORS) >= 10
    for c in COLLECTORS:
        assert {"component", "table", "timestamp_column", "expected_max_gap_minutes"} <= c.keys()
    for cc in COVERAGE_CHECKS:
        assert {"component", "table", "cohort_column", "null_column"} <= cc.keys()


# ── integration smoke test against the real DB ──────────────────────────

@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_build_data_quality_snapshot_shape(db):
    snapshot = await build_data_quality_snapshot()
    assert snapshot["status"] in ("ok", "warning", "critical")
    assert isinstance(snapshot["findings"], list)
    assert snapshot["collectors_checked"] == len(COLLECTORS)
    for f in snapshot["findings"]:
        assert set(f.keys()) == {"component", "severity", "fact", "evidence", "recommended_action"}
        assert f["severity"] in ("warning", "critical")
    # status — максимум severity среди findings (тот же принцип "max, не
    # среднее", что и overall_level в bot/core/listing_risks.py).
    if any(f["severity"] == "critical" for f in snapshot["findings"]):
        assert snapshot["status"] == "critical"
    elif any(f["severity"] == "warning" for f in snapshot["findings"]):
        assert snapshot["status"] == "warning"
    else:
        assert snapshot["status"] == "ok"


@pytest.mark.asyncio
async def test_build_data_quality_snapshot_is_read_only(db, monkeypatch):
    import bot.db.pg as pg

    async def _boom(*a, **kw):
        raise AssertionError("data_quality snapshot must not write to the DB")

    monkeypatch.setattr(pg, "execute", _boom)
    await build_data_quality_snapshot()  # не должно поднять AssertionError
