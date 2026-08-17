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
    """Полный набор факторов — БЕЗ overrides большинство "нет данных"
    (реалистичный дефолт: astana_schools/transport_hexes/OSM ничего не
    нашли), noise/demolition/bank — реалистично ВСЕГДА доступны (тихо/
    нет объектов на снос/район не определён — валидные измеренные
    результаты, не "нет данных").

    **overrides = int** переопределяет adj И одновременно меняет reason
    на "измерено (тест)" — задача 2026-08-15 ("Location Reliability
    Phase", коммит "Confidence"): _is_available() смотрит на reason, не
    на adj, так что override без смены reason остался бы "неизмеренным"
    для normalize_group_weighted() (тест был бы неправдоподобным —
    реальный измеренный фактор никогда не пишет "нет данных" в reason
    вместе с содержательным adj)."""
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
        "air_quality": {"adj": 0, "label": "💨", "reason": "нет данных air_stations рядом"},
        "bank": {"adj": 0, "label": "🌉", "reason": "район не определён"},
    }
    for k, v in overrides.items():
        base[k]["adj"] = v
        base[k]["reason"] = "измерено (тест)"
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

    async def _fake_compute(lat, lon, year_built=None, district=None, complex_id=None):
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
        # Пять latent-свойств + availability-aware диапазоны (задача
        # 2026-08-15, "Location Reliability Phase" v2, normalize_group_
        # weighted()) — только измеренные (override -> "измерено") факторы
        # участвуют в диапазоне СВОЙСТВА, не статическая схема. По свойствам
        # (в скобках — какие факторы AVAILABLE и их динамический диапазон):
        #   transport: только lrt_access(измерено)=4 из диапазона (0,4)
        #     — остальные 3 "нет данных", исключены целиком -> 100%
        #   infra: schools(измерено)=2 из (0,2) + amenities(измерено)=4
        #     из (0,4) -> раздельно оба на своём максимуме -> 100%
        #   environment: noise=0 дефолтно ДОСТУПЕН ("тихо") + parks
        #     (измерено)=2 -> raw=2 из объединённого (-6,2) -> 100%
        #     (air_quality "нет данных", исключён)
        #   risk: demolition(измерено)=-2 из (-2,0)          -> 0%
        #   urban_quality: пусто структурно -> ВСЕГДА 50% (см. tests/
        #     test_location_score_group_weighted.py::
        #     test_urban_quality_always_fifty_percent_structurally)
        # 0.25*100+0.25*100+0.20*100+0.15*0+0.15*50 = 77.5 -> round -> 78
        assert row["score"] == 78
        # confidence теперь взвешен по source_quality, не "доля посчитанных"
        # (см. bot/core/location_score.py::_compute_confidence()) —
        # передаётся из fake напрямую (90), run_snapshot() не пересчитывает.
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
        # _group_scores — задача 2026-08-15 v2, коммит "Confidence":
        # пара score/confidence на каждое из пяти свойств. confidence =
        # source_quality доступных / source_quality ВСЕХ факторов группы
        # (напр. transport: только lrt_access(0.8) доступен из 4 членов
        # с суммарным весом 3.0 -> round(100*0.8/3.0)=27).
        assert breakdown["_group_scores"]["transport"] == {"score": 100, "confidence": 27}
        assert breakdown["_group_scores"]["infra"] == {"score": 100, "confidence": 43}
        assert breakdown["_group_scores"]["risk"] == {"score": 0, "confidence": 100}
        assert breakdown["_group_scores"]["urban_quality"] == {"score": 50, "confidence": 0}
    finally:
        await _cleanup(cid, lid)


@pytest.mark.asyncio
async def test_snapshot_all_measurable_groups_at_min_or_max(db, monkeypatch):
    """Пять latent-свойств + availability-aware диапазоны (задача
    2026-08-15 v2) — НЕ 0/100 больше (urban_quality пусто структурно,
    всегда тянет к своим 50% на 15% веса — см. tests/test_location_
    score_group_weighted.py::test_theoretical_bounds_are_not_0_100_
    while_urban_quality_empty), а 8/92: round(0.85*0+0.15*50)=8,
    round(0.85*100+0.15*50)=92. Честное следствие пустого свойства —
    не притворяемся, что можем быть уверены в абсолютном 0 или 100,
    пока 15% картины (urban_quality) в принципе неизмеримы."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_min(lat, lon, year_built=None, district=None, complex_id=None):
        # По одному измеренному фактору на измеримое свойство, каждый —
        # на своём минимуме: route_connectivity=0 (transport), schools=0
        # (infra), noise=-6 (environment), demolition=-2 (risk).
        factors = _fake_factors(route_connectivity=0, schools=0, noise=-6, demolition=-2)
        return {"total": -8, "factors": factors, "confidence": 50}

    async def _fake_max(lat, lon, year_built=None, district=None, complex_id=None):
        # На максимуме: route_connectivity=2, schools=2, parks=2 —
        # noise/demolition уже на максимуме СВОИМИ дефолтами (0 — и
        # "тихо", и "нет объектов на снос", оба ДОСТУПНЫ без override).
        factors = _fake_factors(route_connectivity=2, schools=2, parks=2)
        return {"total": 4, "factors": factors, "confidence": 100}

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
        assert row1["score"] == 8
        assert row2["score"] == 92
    finally:
        await _cleanup(cid1, lid1)
        await _cleanup(cid2, lid2)


@pytest.mark.asyncio
async def test_snapshot_low_confidence_still_written_not_skipped(db, monkeypatch):
    """Требование заказчика: полный отказ Overpass -> низкий confidence,
    НЕ пропуск ЖК. Строка пишется как есть (Unknown != average)."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_low_confidence(lat, lon, year_built=None, district=None, complex_id=None):
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

    async def _fake_compute(lat, lon, year_built=None, district=None, complex_id=None):
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

    async def _fake_compute(lat, lon, year_built=None, district=None, complex_id=None):
        # Разный total для разных координат — если конкурентные задачи
        # перепутают complex_id/результат, тест это поймает. schools
        # (не 5, а 2 — новый максимум фактора с задачи "двойные школы",
        # 2026-08-15) vs 0 — заведомо разные измеренные значения.
        adj = 2 if round(lat, 2) == 51.20 else 0
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
        assert by_id[cids[0]] != by_id[cids[1]]  # 51.20 (adj=2) != 51.21 (adj=0)
        assert by_id[cids[1]] == by_id[cids[2]]  # оба adj=0 -> одинаковый score
    finally:
        for cid, lid in pairs:
            await _cleanup(cid, lid)


# ── Задача 2026-08-17: canary-режим (--complex-ids/--limit/--dry-run,
#    изоляция ошибок по ЖК, processed/succeeded/failed/skipped) ─────────

@pytest.mark.asyncio
async def test_dry_run_does_not_write(db, monkeypatch):
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_compute(lat, lon, year_built=None, district=None, complex_id=None):
        factors = _fake_factors(schools=2)
        return {"total": 2, "factors": factors, "confidence": 80}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    cid, lid = await _insert_complex_with_listing("__test_cls_dryrun__", 51.16, 71.46)
    try:
        result = await snap.run_snapshot(complex_ids=[cid], dry_run=True)
        assert result["dry_run"] is True
        assert result["written"] == 1  # посчитал бы, но не вставил
        assert result["succeeded"] == 1

        from bot.db.pg import fetchval
        count = await fetchval("SELECT count(*) FROM complex_location_scores WHERE complex_id=$1", cid)
        assert count == 0
    finally:
        await _cleanup(cid, lid)


@pytest.mark.asyncio
async def test_one_complex_failure_does_not_abort_batch(db, monkeypatch):
    """Задача: "ошибка одного ЖК не должна прекращать весь batch" —
    раньше asyncio.gather() без return_exceptions=True роняло ВЕСЬ
    прогон на первом же исключении."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _flaky_compute(lat, lon, year_built=None, district=None, complex_id=None):
        if round(lat, 2) == 51.30:
            raise RuntimeError("симулированный сбой Overpass")
        factors = _fake_factors(schools=2)
        return {"total": 2, "factors": factors, "confidence": 80}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _flaky_compute)

    pairs = [
        await _insert_complex_with_listing("__test_cls_fail_a__", 51.30, 71.30),  # упадёт
        await _insert_complex_with_listing("__test_cls_fail_b__", 51.31, 71.31),  # ок
        await _insert_complex_with_listing("__test_cls_fail_c__", 51.32, 71.32),  # ок
    ]
    cids = [p[0] for p in pairs]
    try:
        result = await snap.run_snapshot(complex_ids=cids)
        assert result["failed"] == 1
        assert result["succeeded"] == 2
        assert result["processed"] == 3
        assert len(result["failed_ids"]) == 1
        assert result["failed_ids"][0]["complex_id"] == cids[0]

        from bot.db.pg import fetchval
        written_count = await fetchval(
            "SELECT count(*) FROM complex_location_scores WHERE complex_id = ANY($1::int[])", cids)
        assert written_count == 2  # b и c записались несмотря на падение a
    finally:
        for cid, lid in pairs:
            await _cleanup(cid, lid)


@pytest.mark.asyncio
async def test_limit_caps_scope(db, monkeypatch):
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    async def _fake_compute(lat, lon, year_built=None, district=None, complex_id=None):
        return {"total": 0, "factors": _fake_factors(), "confidence": 50}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    pairs = [
        await _insert_complex_with_listing("__test_cls_limit_a__", 51.40, 71.40),
        await _insert_complex_with_listing("__test_cls_limit_b__", 51.41, 71.41),
    ]
    cids = [p[0] for p in pairs]
    try:
        result = await snap.run_snapshot(complex_ids=cids, limit=1)
        assert result["processed"] == 1
    finally:
        for cid, lid in pairs:
            await _cleanup(cid, lid)


def test_cli_has_canary_flags():
    import inspect
    import complex_location_score_snapshot as snap
    src = inspect.getsource(snap.main)
    for flag in ("--complex-ids", "--limit", "--dry-run", "--only-missing"):
        assert flag in src


@pytest.mark.asyncio
async def test_only_missing_skips_complex_with_todays_row(db, monkeypatch):
    """Задача 2026-08-17 ("завершение Location Score без повторного
    пересчёта уже свежих") — ЖК, у которого уже есть строка с computed_at
    сегодня, --only-missing пропускает целиком (даже не вызывает
    compute_complex_location_score), другой (без сегодняшней строки) —
    обрабатывает как обычно."""
    import complex_location_score_snapshot as snap
    import bot.core.location_score as location_score_module

    called_for = []

    async def _fake_compute(lat, lon, year_built=None, district=None, complex_id=None):
        called_for.append(complex_id)
        return {"total": 0, "factors": _fake_factors(), "confidence": 50}

    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    already_done = await _insert_complex_with_listing("__test_cls_om_done__", 51.50, 71.50)
    still_missing = await _insert_complex_with_listing("__test_cls_om_missing__", 51.51, 71.51)
    cid_done, lid_done = already_done
    cid_missing, lid_missing = still_missing
    try:
        # симулируем "уже посчитан сегодня" — прямая запись, без прогона через snap
        from bot.db.pg import execute
        await execute("""
            INSERT INTO complex_location_scores
                (complex_id, score, confidence, transport_score, infra_score,
                 noise_score, green_score, risk_score, lat, lon, breakdown,
                 score_version, git_commit)
            VALUES ($1, 50, 50, 0, 0, 0, 0, 0, 51.50, 71.50, '{}'::jsonb, 'loc_v1', 'test')
        """, cid_done)

        result = await snap.run_snapshot(complex_ids=[cid_done, cid_missing], only_missing=True)
        assert result["processed"] == 1
        assert cid_missing in called_for
        assert cid_done not in called_for
    finally:
        await _cleanup(cid_done, lid_done)
        await _cleanup(cid_missing, lid_missing)
