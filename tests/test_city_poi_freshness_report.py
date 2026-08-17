"""Тесты для scripts/city_poi_freshness_report.py — задача 2026-08-17
("City POI timer", п.3: "count/last_updated/freshness_days/stale
>14/30 дней"). Чистая логика (_status) без сети/БД + одна интеграция
с БД на синтетических данных (namespaced test kind, НЕ реальные kind —
см. инцидент этой же задачи с test_city_poi_park_area.py про то, почему
это критично)."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def test_status_fresh_under_14_days():
    from city_poi_freshness_report import _status
    assert _status(5.0) == "fresh"
    assert _status(14.0) == "fresh"


def test_status_stale_14_between_14_and_30():
    from city_poi_freshness_report import _status
    assert _status(14.1) == "stale_14"
    assert _status(30.0) == "stale_14"


def test_status_stale_30_over_30():
    from city_poi_freshness_report import _status
    assert _status(30.1) == "stale_30"


def test_status_never_synced_when_none():
    from city_poi_freshness_report import _status
    assert _status(None) == "never_synced"


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_build_report_reflects_real_kinds(db, monkeypatch):
    """_ALL_SYNC_KINDS подменён на синтетический набор (НЕ трогаем
    реальные production kind вроде 'park'/'school' в этом тесте) —
    проверяем саму логику построения отчёта на контролируемых данных."""
    import city_poi_freshness_report as report_module
    from bot.db.pg import execute

    monkeypatch.setattr(report_module, "_ALL_SYNC_KINDS",
                         ["__test_fresh__", "__test_stale__", "__test_never__"])

    fresh_at = datetime.now(timezone.utc) - timedelta(days=1)
    stale_at = datetime.now(timezone.utc) - timedelta(days=20)
    try:
        await execute(
            "INSERT INTO city_poi (kind, lat, lon, updated_at) VALUES ($1, $2, $3, $4)",
            "__test_fresh__", 1.0, 1.0, fresh_at)
        await execute(
            "INSERT INTO city_poi (kind, lat, lon, updated_at) VALUES ($1, $2, $3, $4)",
            "__test_stale__", 2.0, 2.0, stale_at)

        report = await report_module.build_report()
        by_kind = {r["kind"]: r for r in report}

        assert by_kind["__test_fresh__"]["status"] == "fresh"
        assert by_kind["__test_fresh__"]["count"] == 1
        assert by_kind["__test_stale__"]["status"] == "stale_14"
        assert by_kind["__test_never__"]["status"] == "never_synced"
        assert by_kind["__test_never__"]["count"] == 0
    finally:
        await execute("DELETE FROM city_poi WHERE kind = ANY($1::text[])",
                       ["__test_fresh__", "__test_stale__", "__test_never__"])
