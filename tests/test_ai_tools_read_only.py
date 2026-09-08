"""tests/test_ai_tools_read_only.py — гарантия "no production writes"
(ограничение задачи "Hatuli API v1 для AI tools"). Два независимых
уровня проверки:

  1. Статический — роутер (bot/ai_tools/router.py) регистрирует ТОЛЬКО
     GET-роуты — ни одного write-метода (POST/PUT/PATCH/DELETE) в принципе
     недостижим ни с каким телом запроса.
  2. Динамический — monkeypatch bot.db.pg.execute() на функцию, которая
     поднимает AssertionError, затем реально вызываются все 6 read-only
     capabilities (на синтетических фикстурах, подготовленных ДО патча) и
     data_quality_snapshot — если хоть один внутри вызовет execute()
     (напрямую или через любую переиспользуемую core-функцию), тест
     упадёт. Это проверяет весь фактический call-graph, не только код,
     который мы сами написали в bot/ai_tools/."""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def test_router_registers_get_only():
    from bot.ai_tools.router import make_ai_tools_router

    router = make_ai_tools_router()
    assert len(router.routes) >= 7  # 6 capabilities + tools registry
    for route in router.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}, f"{route.path} exposes non-GET method(s): {methods}"


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


class _Scenario:
    def __init__(self):
        self.suffix = uuid.uuid4().hex[:8]
        self.numeric_suffix = str(uuid.uuid4().int)[:9]
        self.listing_ids: list[str] = []
        self.property_ids: list[int] = []
        self.complex_ids: list[int] = []

    def lid(self, n) -> str:
        return f"7{self.numeric_suffix}{n}"

    @property
    def district(self) -> str:
        return f"__TEST_AI_READONLY_{self.suffix}__"


@pytest_asyncio.fixture
async def scenario(db):
    sc = _Scenario()
    yield sc
    # cleanup выполняется ДО повторного monkeypatch (fixture teardown
    # запускается уже после того, как monkeypatch сам себя откатил —
    # pytest откатывает monkeypatch раньше yield-фикстур, объявленных
    # позже в графе, поэтому execute() здесь снова настоящий).
    from bot.db.pg import execute
    for lid in sc.listing_ids:
        await execute("DELETE FROM property_listings WHERE listing_id = $1", lid)
    for pid in sc.property_ids:
        await execute("DELETE FROM properties WHERE property_id = $1", pid)
    for lid in sc.listing_ids:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
    for cid in sc.complex_ids:
        await execute("DELETE FROM complexes WHERE id = $1", cid)


async def _seed(sc: _Scenario):
    from bot.db.pg import execute, fetchval

    complex_name = f"__TEST_AI_READONLY_CX_{sc.suffix}__"
    lid = sc.lid(1)
    sc.listing_ids.append(lid)
    await execute(
        """
        INSERT INTO apartment_listings (id, url, price, area, rooms, floor, floors_total,
                                         district, complex_name, market_type, is_active,
                                         first_seen, lat, lon)
        VALUES ($1,$2,25000000,45.0,2,5,9,$3,$4,'secondary', TRUE, now(), 1.234, 2.345)
        ON CONFLICT (id) DO NOTHING
        """,
        lid, f"https://krisha.kz/a/show/{lid}", sc.district, complex_name,
    )
    # lat/lon (океан, вне зоны реального покрытия city_poi/osm_cache) — задача
    # "fix/ai-tools-strict-read-only": этот сценарий раньше НИКОГДА не доходил
    # до has_coords=True/_build_poi_strict_read_only (review PR #51 дыра была
    # найдена именно потому, что этот тест её не покрывал).
    cid = await fetchval(
        "INSERT INTO complexes (name) VALUES ($1) RETURNING id",
        complex_name,
    )
    sc.complex_ids.append(cid)
    address_hash = f"__test_ai_readonly_prop_{sc.suffix}__"
    pid = await fetchval(
        "INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms) "
        "VALUES ($1,$2,5,45.0,2) RETURNING property_id",
        cid, address_hash,
    )
    sc.property_ids.append(pid)
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1,$2,'auto',0.8)", pid, lid,
    )
    return lid, pid, cid


@pytest.mark.asyncio
async def test_all_capabilities_never_call_execute(scenario, monkeypatch):
    lid, pid, cid = await _seed(scenario)

    import bot.db.pg as pg

    async def _boom(*a, **kw):
        raise AssertionError(f"a read-only AI-tools capability attempted a DB write: args={a!r}")

    monkeypatch.setattr(pg, "execute", _boom)

    from bot.ai_tools import core
    from bot.ai_tools.data_quality import build_data_quality_snapshot

    # Found-пути (самые "тяжёлые" — задействуют больше всего переиспользуемого
    # кода: bargain/comparables, risk passport, market profile, location).
    await core.resolve_listing(f"https://krisha.kz/a/show/{lid}")
    await core.get_property_history(pid)
    await core.get_listing_analysis(lid)
    await core.get_listing_risks(lid)
    await core.get_complex_market_profile(cid)
    await core.get_location_analysis(cid)
    await build_data_quality_snapshot()

    # Not-found-пути (другая ветка кода в каждой функции).
    await core.resolve_listing("999999999996")
    await core.get_property_history(2_000_000_001)
    await core.get_listing_analysis("999999999995")
    await core.get_listing_risks("999999999994")
    await core.get_complex_market_profile(2_000_000_002)
    await core.get_location_analysis(2_000_000_003)
