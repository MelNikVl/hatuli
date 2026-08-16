"""Регрессия для задачи 2026-08-16 ("Локальный OSM-слой") —
scripts/sync_city_poi.py + bot/score_layers/osm.py::{local_poi_near,
kinds_synced, city_poi_freshness_days} + bot/core/location_score.py::
{_apply_freshness_confidence_penalty, _effective_source_quality}.

Синтетические координаты (SYN_LAT/SYN_LON) — заведомо далеко и от
реальной Астаны (~51.1/71.4), и от Алматы-опорной точки других тестов
этого модуля (43.25/76.95, tests/test_score_layer_schools_university_
only.py) — ни один существующий тест/реальные данные не пересекутся."""
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

SYN_LAT = 10.0000
SYN_LON = 10.0000


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_poi(kind, lat=SYN_LAT, lon=SYN_LON, name=None, updated_at=None):
    from bot.db.pg import fetchval
    return await fetchval(
        """INSERT INTO city_poi (kind, name, lat, lon, updated_at)
           VALUES ($1, $2, $3, $4, COALESCE($5, now())) RETURNING id""",
        kind, name, lat, lon, updated_at)


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM city_poi WHERE id = ANY($1::int[])", list(ids))


async def _cleanup_kind(kind):
    from bot.db.pg import execute
    await execute("DELETE FROM city_poi WHERE kind = $1", kind)


# ── local_poi_near / kinds_synced (bot/score_layers/osm.py) ──────────────

@pytest.mark.asyncio
async def test_local_poi_near_returns_points_within_radius(db):
    """Правильные POI для координат ЖК: точка в 100м входит, точка в
    5км — нет, даже если kind совпадает и она "синхронизирована"."""
    from bot.score_layers.osm import local_poi_near
    near_id = await _insert_poi("__test_syn_shop__", lat=SYN_LAT, lon=SYN_LON)
    # ~5км по широте (1 градус ≈ 111км, 0.05° ≈ 5.5км)
    far_id = await _insert_poi("__test_syn_shop__", lat=SYN_LAT + 0.05, lon=SYN_LON)
    try:
        found = await local_poi_near(SYN_LAT, SYN_LON, ["__test_syn_shop__"], 700)
        assert found is not None
        assert len(found) == 1
        assert found[0]["lat"] == pytest.approx(SYN_LAT)
    finally:
        await _cleanup(near_id, far_id)


@pytest.mark.asyncio
async def test_local_poi_near_none_when_kind_never_synced(db):
    """kind нигде в city_poi вообще не встречается -> None (сигнал
    "категория не синхронизирована", вызывающий слой падает на
    overpass_cached), НЕ пустой список."""
    from bot.score_layers.osm import local_poi_near
    result = await local_poi_near(SYN_LAT, SYN_LON, ["__test_syn_never_synced__"], 700)
    assert result is None


@pytest.mark.asyncio
async def test_local_poi_near_empty_list_when_synced_but_nothing_nearby(db):
    """kind СУЩЕСТВУЕТ в city_poi (где-то далеко) -> категория считается
    синхронизированной, но рядом с этой конкретной точкой ничего нет ->
    [] (валидный "не нашли", НЕ None) — именно эту путаницу чинил
    bot/score_layers/schools.py::_from_local_table (см. git log,
    "SELECT COUNT(*) FROM city_poi" без WHERE kind)."""
    from bot.score_layers.osm import local_poi_near
    far_id = await _insert_poi("__test_syn_park__", lat=SYN_LAT + 1.0, lon=SYN_LON + 1.0)
    try:
        result = await local_poi_near(SYN_LAT, SYN_LON, ["__test_syn_park__"], 700)
        assert result == []
    finally:
        await _cleanup(far_id)


@pytest.mark.asyncio
async def test_city_poi_freshness_days_computes_age_from_updated_at(db):
    from bot.score_layers.osm import city_poi_freshness_days
    old_at = datetime.now(timezone.utc) - timedelta(days=20)
    rid = await _insert_poi("__test_syn_fresh__", updated_at=old_at)
    try:
        age = await city_poi_freshness_days(["__test_syn_fresh__"])
        assert age is not None
        assert 19.9 < age < 20.1
    finally:
        await _cleanup(rid)


@pytest.mark.asyncio
async def test_city_poi_freshness_days_none_when_kind_absent(db):
    from bot.score_layers.osm import city_poi_freshness_days
    age = await city_poi_freshness_days(["__test_syn_absent_kind__"])
    assert age is None


# ── _apply_freshness_confidence_penalty / _effective_source_quality
#    (bot/core/location_score.py) — чистая логика, БЕЗ БД (monkeypatch
#    city_poi_freshness_days) ───────────────────────────────────────────

def _factors_with_quality():
    from bot.core.location_score import _SOURCE_QUALITY, _OSM_LOCAL_FACTOR_KEYS
    return {k: {"adj": 0, "reason": "измерено", "source_quality": _SOURCE_QUALITY[k]}
            for k in _OSM_LOCAL_FACTOR_KEYS}


@pytest.mark.asyncio
async def test_freshness_penalty_none_when_never_synced_no_change(monkeypatch):
    """age_days=None (категория ни разу не синхронизирована) — другой
    кейс, не "устарела" — source_quality НЕ трогаем (эти факторы и так
    идут по live Overpass-фолбэку внутри score_layers)."""
    import bot.core.location_score as ls
    from bot.score_layers import osm as osm_module

    async def _fake_age(kinds):
        return None
    # _apply_freshness_confidence_penalty делает "from bot.score_layers.osm
    # import city_poi_freshness_days" ВНУТРИ функции (не на уровне модуля
    # location_score.py) — патчим именно исходный модуль osm, не ls.
    monkeypatch.setattr(osm_module, "city_poi_freshness_days", _fake_age)

    factors = _factors_with_quality()
    before = {k: f["source_quality"] for k, f in factors.items()}
    await ls._apply_freshness_confidence_penalty(factors)
    assert {k: f["source_quality"] for k, f in factors.items()} == before


@pytest.mark.asyncio
async def test_freshness_penalty_14_days_applies_0_8_multiplier(monkeypatch):
    import bot.core.location_score as ls
    from bot.score_layers import osm as osm_module

    async def _fake_age(kinds):
        return 20.0  # >14, <=30
    monkeypatch.setattr(osm_module, "city_poi_freshness_days", _fake_age)

    factors = _factors_with_quality()
    before = {k: f["source_quality"] for k, f in factors.items()}
    await ls._apply_freshness_confidence_penalty(factors)
    for k in ls._OSM_LOCAL_FACTOR_KEYS:
        assert factors[k]["source_quality"] == pytest.approx(before[k] * 0.8)


@pytest.mark.asyncio
async def test_freshness_penalty_30_days_applies_0_5_multiplier_not_both(monkeypatch):
    """>30 дней -> ×0.5, а НЕ ×0.8×0.5 (пороги не перемножаются, берётся
    более строгий)."""
    import bot.core.location_score as ls
    from bot.score_layers import osm as osm_module

    async def _fake_age(kinds):
        return 45.0
    monkeypatch.setattr(osm_module, "city_poi_freshness_days", _fake_age)

    factors = _factors_with_quality()
    before = {k: f["source_quality"] for k, f in factors.items()}
    await ls._apply_freshness_confidence_penalty(factors)
    for k in ls._OSM_LOCAL_FACTOR_KEYS:
        assert factors[k]["source_quality"] == pytest.approx(before[k] * 0.5)
        assert factors[k]["source_quality"] != pytest.approx(before[k] * 0.8 * 0.5)


@pytest.mark.asyncio
async def test_freshness_penalty_under_14_days_no_change(monkeypatch):
    import bot.core.location_score as ls
    from bot.score_layers import osm as osm_module

    async def _fake_age(kinds):
        return 5.0
    monkeypatch.setattr(osm_module, "city_poi_freshness_days", _fake_age)

    factors = _factors_with_quality()
    before = {k: f["source_quality"] for k, f in factors.items()}
    await ls._apply_freshness_confidence_penalty(factors)
    assert {k: f["source_quality"] for k, f in factors.items()} == before


def test_effective_source_quality_prefers_annotated_over_static():
    """Уценённое f["source_quality"] (после _apply_freshness_confidence_
    penalty) реально используется _compute_confidence/_group_confidence
    (числитель) — если бы они по-прежнему читали статический
    _SOURCE_QUALITY напрямую, устаревание НИКАК не повлияло бы на
    итоговый confidence (в точности баг, которого эта задача избегает)."""
    from bot.core.location_score import _effective_source_quality, _SOURCE_QUALITY

    factors = {"schools": {"adj": 0, "reason": "х", "source_quality": 0.3}}
    assert _effective_source_quality("schools", factors) == 0.3
    assert _effective_source_quality("schools", factors) != _SOURCE_QUALITY["schools"]


def test_effective_source_quality_falls_back_to_static_when_not_annotated():
    """factors без предварительной _annotate_factor_metadata (как в
    tests/test_location_score_group_weighted.py::_factors()) — старое
    поведение 1:1, не ломает существующие тесты confidence."""
    from bot.core.location_score import _effective_source_quality, _SOURCE_QUALITY

    factors = {"schools": {"adj": 0, "reason": "нет данных"}}
    assert _effective_source_quality("schools", factors) == _SOURCE_QUALITY["schools"]


# ── sync_city_poi.py — чистая логика (сэмплинг/дедуп), БЕЗ сети/БД ───────

def test_sample_polyline_produces_points_at_interval():
    from sync_city_poi import _sample_polyline
    # ~1км вдоль широты, шаг между узлами ~10м (много МЕНЬШЕ interval_m,
    # иначе сэмплер тривиально берёт каждый узел, ничего не проверив) —
    # 1° широты ≈ 111000м -> 1км ≈ 0.009009°, 100 шагов по ~10м.
    total_deg = 1000.0 / 111_000.0
    nodes = [{"lat": total_deg * i / 100, "lon": 0.0} for i in range(101)]
    sampled = _sample_polyline(nodes, interval_m=150.0)
    assert sampled[0] == (0.0, 0.0)
    assert 3 <= len(sampled) <= 10  # ~1000м / 150м ≈ 6-7 точек, не 1 и не сотни
    assert len(sampled) < len(nodes)


def test_sample_polyline_empty_nodes_returns_empty():
    from sync_city_poi import _sample_polyline
    assert _sample_polyline([], interval_m=150.0) == []


def test_extract_points_dedupes_by_rounded_coords():
    from sync_city_poi import _extract_points
    data = {"elements": [
        {"type": "node", "lat": 1.0, "lon": 2.0, "tags": {"name": "А"}},
        {"type": "node", "lat": 1.0000001, "lon": 2.0000001, "tags": {"name": "А-дубль"}},
        {"type": "node", "lat": 3.0, "lon": 4.0, "tags": {}},
    ]}
    points = _extract_points("shop", data)
    assert len(points) == 2  # первые два — один и тот же (округлённый) POI


# ── sync_city_poi.py — dry-run не пишет в БД ─────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_does_not_touch_city_poi(db, monkeypatch):
    import sync_city_poi as sync_module

    async def _fake_fetch_kind(kind):
        return [{"kind": kind, "lat": SYN_LAT, "lon": SYN_LON, "name": None, "address": None}]
    monkeypatch.setattr(sync_module, "fetch_kind", _fake_fetch_kind)
    sync_module.CATEGORIES = {**sync_module.CATEGORIES, "_test_cat_": ["__test_syn_dryrun__"]}

    from bot.db.pg import fetchval
    before = await fetchval("SELECT count(*) FROM city_poi WHERE kind = $1", "__test_syn_dryrun__")
    assert before == 0
    stats = await sync_module.run_sync(dry_run=True, category="_test_cat_")
    after = await fetchval("SELECT count(*) FROM city_poi WHERE kind = $1", "__test_syn_dryrun__")
    assert after == 0
    assert stats == {"__test_syn_dryrun__": 1}


@pytest.mark.asyncio
async def test_save_category_is_idempotent(db, monkeypatch):
    """DELETE+INSERT по kind — второй прогон с теми же данными не
    задваивает строки (сама суть идемпотентности TRUNCATE-подобной
    записи, см. докстринг save_category)."""
    import sync_city_poi as sync_module
    from bot.db.pg import fetchval

    points = [{"kind": "__test_syn_idem__", "lat": SYN_LAT, "lon": SYN_LON,
               "name": "т", "address": None}]
    try:
        n1 = await sync_module.save_category("__test_syn_idem__", points)
        n2 = await sync_module.save_category("__test_syn_idem__", points)
        assert n1 == n2 == 1
        cnt = await fetchval("SELECT count(*) FROM city_poi WHERE kind = $1", "__test_syn_idem__")
        assert cnt == 1
    finally:
        await _cleanup_kind("__test_syn_idem__")


@pytest.mark.asyncio
async def test_save_category_replaces_stale_rows_not_accumulates(db):
    """Второй прогон с ДРУГИМ набором точек (POI пропал из OSM) — старая
    точка исчезает из city_poi, не остаётся мусором навсегда (DELETE по
    kind перед INSERT, не просто UPSERT новых поверх старых)."""
    import sync_city_poi as sync_module
    from bot.db.pg import fetchval

    p_old = [{"kind": "__test_syn_replace__", "lat": SYN_LAT, "lon": SYN_LON,
               "name": None, "address": None}]
    p_new = [{"kind": "__test_syn_replace__", "lat": SYN_LAT + 1, "lon": SYN_LON + 1,
               "name": None, "address": None}]
    try:
        await sync_module.save_category("__test_syn_replace__", p_old)
        await sync_module.save_category("__test_syn_replace__", p_new)
        cnt = await fetchval("SELECT count(*) FROM city_poi WHERE kind = $1", "__test_syn_replace__")
        assert cnt == 1
        still_there = await fetchval(
            "SELECT count(*) FROM city_poi WHERE kind = $1 AND lat = $2",
            "__test_syn_replace__", SYN_LAT)
        assert still_there == 0
    finally:
        await _cleanup_kind("__test_syn_replace__")
