"""Регрессия для Фазы L1 продуктового трека «Локация» (docs/location_
product_design.md §7, задача 2026-08-14), коммит 5 —
complex_location_score_snapshot.py: append-only снимок в
complex_location_scores, нормализация 0-100, группировка breakdown.

compute_complex_location_score() подменяется фейком через monkeypatch —
НЕ на объекте complex_location_score_snapshot (там этого имени на
уровне модуля нет — run_snapshot() делает `from bot.core.location_score
import compute_complex_location_score` ВНУТРИ функции, отложенный
импорт, тот же паттерн, что everywhere в проекте), а на источнике
bot.core.location_score — то же самое, что уже сделано для
tests/test_osm_healthcheck.py по той же причине. Тест не бьёт в
реальный Overpass. Реальная БД для complexes/apartment_listings/
complex_location_scores (тот же паттерн, что tests/test_complex_stats_
snapshot.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _fake_factors(**overrides) -> dict:
    """Полный набор факторов с нулевыми adj по умолчанию (нейтральная
    середина диапазона) — overrides переопределяют конкретные."""
    # building_age сюда НЕ входит — с задачи "двойные школы + building_age"
    # (2026-08-15) он не в _GROUPS и не влияет на score (см. tests/test_
    # location_score_group_weighted.py::test_building_age_not_in_any_group).
    base = {
        "noise": {"adj": 0, "label": "🔇", "reason": "тихо"},
        "schools": {"adj": 0, "label": "🏫", "reason": "нет данных"},
        "transit_stops": {"adj": 0, "label": "🚏", "reason": "нет данных"},
        "amenities": {"adj": 0, "label": "🛒", "reason": "нет данных"},
        "school_access": {"adj": 0, "label": "🏫", "reason": "нет данных astana_schools рядом"},
        "kindergarten_access": {"adj": 0, "label": "🧸", "reason": "нет данных astana_kindergartens рядом"},
        "parks": {"adj": 0, "label": "🌳", "reason": "нет данных"},
        "lrt_access": {"adj": 0, "label": "🚈", "reason": "нет данных"},
        "road_access": {"adj": 0, "label": "🚗", "reason": "нет данных"},
        "route_connectivity": {"adj": 0, "label": "🔀", "reason": "нет данных"},
        "demolition": {"adj": 0, "label": "🚧", "reason": "нет объектов"},
        "bank": {"adj": 0, "label": "🌉", "reason": "район не определён"},
    }
    for k, v in overrides.items():
        base[k]["adj"] = v
    return base


async def _insert_complex_with_listing(name, lat, lon, year_built=None, district=None):
    """ЖК + 1 активное объявление с координатами — resolve_complex_geo_
    centroid() находит центроид только через реальные apartment_listings,
    самих complexes.lat/lon не читает (см. bot/core/house_resolution.py)."""
    from bot.db.pg import fetchval, execute
    cid = await fetchval(
        "INSERT INTO complexes (name, year_built, district) VALUES ($1, $2, $3) RETURNING id",
        name, year_built, district)
    lid = f"__test_cls_listing_{cid}__"
    await execute(
        "INSERT INTO apartment_listings (id, complex_name, lat, lon, price, area, rooms, is_active) "
        "VALUES ($1, $2, $3, $4, 30000000, 60.0, 2, TRUE)",
        lid, name, lat, lon)
    return cid, lid


async def _cleanup(cid, lid):
    from bot.db.pg import execute
    await execute("DELETE FROM complex_location_scores WHERE complex_id=$1", cid)
    await execute("DELETE FROM apartment_listings WHERE id=$1", lid)
    await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_snapshot_writes_row_with_normalized_score_and_groups(db, monkeypatch):
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_compute(lat, lon, year_built=None, district=None):
        # schools=2, не 5 — с задачи "двойные школы" (2026-08-15) это
        # реальный максимум фактора "schools" в location_score (OSM
        # university_only=True в обычном случае, школьно-садиковая часть
        # переехала в school_access/kindergarten_access).
        factors = _fake_factors(schools=2, amenities=4, parks=2, lrt_access=4, demolition=-2)
        return {"total": sum(f["adj"] for f in factors.values()), "factors": factors, "confidence": 90}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    cid, lid = await _insert_complex_with_listing(
        "__test_cls_full__", 51.15, 71.45, year_built=2022, district="Есиль")
    try:
        result = await snap.run_snapshot(complex_ids=[cid])
        assert result["written"] == 1
        assert result["no_coords"] == 0

        from bot.db.pg import fetchrow
        row = await fetchrow("SELECT * FROM complex_location_scores WHERE complex_id=$1", cid)
        assert row is not None
        # Групповая модель (задача 2026-08-15, "Location Reliability
        # Phase", normalize_group_weighted()) — взвешенное среднее по
        # группам, НЕ линейный total/диапазон (тот убран). По группам:
        #   transport: lrt_access=4 из диапазона (0,11) -> 36.36%
        #   infra: schools=2+amenities=4=6 из (0,12)     -> 50%
        #   noise: 0 из (-6,0)                            -> 100%
        #   green: parks=2 из (0,2)                       -> 100%
        #   risk: demolition=-2 из (-2,0)                 -> 0%
        # 0.25*36.36 + 0.25*50 + 0.15*100 + 0.20*100 + 0.15*0 = 56.59 -> 57
        assert row["score"] == 57
        assert row["confidence"] == 90
        assert row["infra_score"] == 6      # schools(2)+amenities(4)
        assert row["transport_score"] == 4  # lrt_access(4), остальные 0
        assert row["green_score"] == 2
        assert row["risk_score"] == -2      # demolition(-2), building_age больше не в группе
        assert row["noise_score"] == 0
        assert row["score_version"] == "loc_v1"
        assert row["git_commit"]
        assert round(row["lat"], 2) == 51.15
        breakdown = row["breakdown"]
        breakdown = json.loads(breakdown) if isinstance(breakdown, str) else breakdown
        assert breakdown["infra"]["schools"]["adj"] == 2
        assert breakdown["risk"]["demolition"]["adj"] == -2
        assert "building_age" not in breakdown["risk"]  # убран из группы (задача "двойные школы")
        assert breakdown["informational"]["bank"]["adj"] == 0
        assert "bank" not in breakdown.get("risk", {})  # bank вне групп
    finally:
        await _cleanup(cid, lid)


@pytest.mark.asyncio
async def test_snapshot_all_groups_at_min_or_max_gives_0_or_100(db, monkeypatch):
    """Групповая модель (задача 2026-08-15) — 0/100 достигаются, когда
    КАЖДАЯ группа одновременно на своём мин/макс (не один общий total,
    как раньше с _TOTAL_ADJ_MIN/MAX, убранными в этом же коммите)."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_min(lat, lon, year_built=None, district=None):
        # noise=-6 (мин noise-группы), demolition=-2 (мин risk-группы —
        # единственный член с задачи "двойные школы", building_age из неё
        # убран); transport/infra/green уже на нуле = их минимум по умолчанию.
        factors = _fake_factors(noise=-6, demolition=-2)
        return {"total": -8, "factors": factors, "confidence": 50}

    async def _fake_max(lat, lon, year_built=None, district=None):
        # Максимум КАЖДОЙ группы одновременно: transport (transit_stops+
        # lrt_access+road_access+route_connectivity=11), infra (schools=2+
        # amenities+school_access+kindergarten_access=12 — schools сжат до
        # 0..2 задачей "двойные школы"), green (parks=2), risk (demolition
        # уже на 0 = её максимум, building_age в группе больше нет вовсе).
        # noise остаётся на дефолтном 0 — это и есть максимум noise-группы.
        factors = _fake_factors(schools=2, transit_stops=3, amenities=4, parks=2,
                                 lrt_access=4, road_access=2, route_connectivity=2,
                                 school_access=4, kindergarten_access=2)
        return {"total": 25, "factors": factors, "confidence": 100}

    cid1, lid1 = await _insert_complex_with_listing("__test_cls_min__", 51.10, 71.40)
    cid2, lid2 = await _insert_complex_with_listing("__test_cls_max__", 51.11, 71.41)
    try:
        monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_min)
        await snap.run_snapshot(complex_ids=[cid1])
        monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_max)
        await snap.run_snapshot(complex_ids=[cid2])

        from bot.db.pg import fetchrow
        row1 = await fetchrow("SELECT score FROM complex_location_scores WHERE complex_id=$1", cid1)
        row2 = await fetchrow("SELECT score FROM complex_location_scores WHERE complex_id=$1", cid2)
        assert row1["score"] == 0
        assert row2["score"] == 100
    finally:
        await _cleanup(cid1, lid1)
        await _cleanup(cid2, lid2)


@pytest.mark.asyncio
async def test_snapshot_low_confidence_still_written_not_skipped(db, monkeypatch):
    """Требование заказчика: полный отказ Overpass -> низкий confidence,
    НЕ пропуск ЖК. Строка пишется как есть (Unknown != average)."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_low_confidence(lat, lon, year_built=None, district=None):
        factors = _fake_factors()  # все нули/нет-данных
        return {"total": 0, "factors": factors, "confidence": 18}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_low_confidence)

    cid, lid = await _insert_complex_with_listing("__test_cls_lowconf__", 51.12, 71.42)
    try:
        result = await snap.run_snapshot(complex_ids=[cid])
        assert result["written"] == 1  # НЕ пропущен несмотря на низкий confidence

        from bot.db.pg import fetchrow
        row = await fetchrow("SELECT confidence, score FROM complex_location_scores WHERE complex_id=$1", cid)
        assert row["confidence"] == 18
        assert row["score"] is not None  # строка реально записана
    finally:
        await _cleanup(cid, lid)


@pytest.mark.asyncio
async def test_snapshot_skips_complex_without_resolvable_coords(db, monkeypatch):
    """Единственная причина пропустить ЖК — нет координат вовсе
    (resolve_complex_geo_centroid() -> None, нет объявлений с lat/lon)."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module
    from bot.db.pg import fetchval, execute

    called = {"n": 0}

    async def _fake_should_not_be_called(*a, **kw):
        called["n"] += 1
        return {"total": 0, "factors": _fake_factors(), "confidence": 50}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_should_not_be_called)

    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_cls_nocoords__') RETURNING id")
    try:
        result = await snap.run_snapshot(complex_ids=[cid])
        assert result["written"] == 0
        assert result["no_coords"] == 1
        assert called["n"] == 0  # compute_complex_location_score даже не вызывалась

        from bot.db.pg import fetch
        rows = await fetch("SELECT * FROM complex_location_scores WHERE complex_id=$1", cid)
        assert rows == []
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_snapshot_append_only_two_runs_same_complex(db, monkeypatch):
    """Повторный прогон -> ВТОРАЯ строка (append-only, PRIMARY KEY
    (complex_id, computed_at)), не перезапись (в отличие от complex_
    stats_history/hex_market_stats, которые UPSERT по дню)."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_compute(lat, lon, year_built=None, district=None):
        factors = _fake_factors(schools=3)
        return {"total": 3, "factors": factors, "confidence": 70}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    cid, lid = await _insert_complex_with_listing("__test_cls_append__", 51.13, 71.43)
    try:
        await snap.run_snapshot(complex_ids=[cid])
        await snap.run_snapshot(complex_ids=[cid])

        from bot.db.pg import fetch
        rows = await fetch("SELECT computed_at FROM complex_location_scores WHERE complex_id=$1", cid)
        assert len(rows) == 2
    finally:
        await _cleanup(cid, lid)


@pytest.mark.asyncio
async def test_snapshot_processes_multiple_complexes_concurrently_without_mixing_rows(db, monkeypatch):
    """_CONCURRENCY-ограниченный asyncio.gather() (живая находка при
    разработке — строго последовательный проход на холодном osm_cache
    был непрактично медленным на ~2000+ ЖК) не должен путать
    complex_id между параллельными задачами — каждый ЖК получает СВОЮ
    строку с правильными значениями."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_compute(lat, lon, year_built=None, district=None):
        # Разный total для разных координат — если конкурентные задачи
        # перепутают complex_id/результат, тест это поймает.
        adj = 5 if round(lat, 2) == 51.20 else 2
        factors = _fake_factors(schools=adj)
        return {"total": adj, "factors": factors, "confidence": 80}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    pairs = [
        await _insert_complex_with_listing("__test_cls_conc_a__", 51.20, 71.20),
        await _insert_complex_with_listing("__test_cls_conc_b__", 51.21, 71.21),
        await _insert_complex_with_listing("__test_cls_conc_c__", 51.22, 71.22),
    ]
    cids = [p[0] for p in pairs]
    try:
        result = await snap.run_snapshot(complex_ids=cids)
        assert result["written"] == 3

        from bot.db.pg import fetch
        rows = await fetch(
            "SELECT complex_id, score FROM complex_location_scores WHERE complex_id = ANY($1::int[])", cids)
        by_id = {r["complex_id"]: r["score"] for r in rows}
        assert len(by_id) == 3
        assert by_id[cids[0]] != by_id[cids[1]]  # 51.20 (adj=5) != 51.21 (adj=2)
        assert by_id[cids[1]] == by_id[cids[2]]  # оба adj=2 -> одинаковый score
    finally:
        for cid, lid in pairs:
            await _cleanup(cid, lid)
