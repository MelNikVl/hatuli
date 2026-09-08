"""tests/test_ai_tools_passthrough.py — Этап 1, capabilities 2-6:
property_history / listing_analysis / listing_risks / complex_market_profile
/ location_analysis. Проверяет, что bot.ai_tools.core — ТОНКАЯ обёртка:
данные 1:1 совпадают с прямым вызовом переиспользуемой core-функции, не
теряют insufficient_data/confidence/evidence при прохождении через
envelope. Синтетические фикстуры, тот же паттерн, что остальные тесты
проекта (реальная Postgres, DATABASE_URL, префиксованные id, cleanup в
finally)."""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

pytestmark = pytest.mark.asyncio


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
        return f"8{self.numeric_suffix}{n}"

    @property
    def district(self) -> str:
        return f"__TEST_AI_PASSTHROUGH_{self.suffix}__"


@pytest_asyncio.fixture
async def scenario(db):
    sc = _Scenario()
    yield sc
    from bot.db.pg import execute
    for lid in sc.listing_ids:
        await execute("DELETE FROM property_listings WHERE listing_id = $1", lid)
    for pid in sc.property_ids:
        await execute("DELETE FROM properties WHERE property_id = $1", pid)
    for lid in sc.listing_ids:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
    for cid in sc.complex_ids:
        await execute("DELETE FROM complexes WHERE id = $1", cid)


async def _insert_listing(sc: _Scenario, n: int, **overrides):
    from bot.db.pg import execute
    lid = sc.lid(n)
    sc.listing_ids.append(lid)
    price = overrides.get("price", 25_000_000)
    area = overrides.get("area", 45.0)
    rooms = overrides.get("rooms", 2)
    floor = overrides.get("floor", 5)
    floors_total = overrides.get("floors_total", 9)
    await execute(
        """
        INSERT INTO apartment_listings (id, url, price, area, rooms, floor, floors_total,
                                         district, market_type, is_active, first_seen)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'secondary', TRUE, now())
        ON CONFLICT (id) DO NOTHING
        """,
        lid, f"https://krisha.kz/a/show/{lid}", price, area, rooms, floor, floors_total, sc.district,
    )
    return lid


async def _make_property(sc: _Scenario, complex_id=None) -> int:
    from bot.db.pg import fetchval
    address_hash = f"__test_ai_passthrough_prop_{sc.suffix}_{uuid.uuid4().hex[:6]}__"
    pid = await fetchval(
        "INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms) "
        "VALUES ($1,$2,5,45.0,2) RETURNING property_id",
        complex_id, address_hash,
    )
    sc.property_ids.append(pid)
    return pid


async def _link(pid: int, lid: str):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1,$2,'auto',0.8)", pid, lid,
    )


async def _make_complex(sc: _Scenario, name: str) -> int:
    from bot.db.pg import fetchval
    cid = await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)
    sc.complex_ids.append(cid)
    return cid


# ── property_history passthrough ─────────────────────────────────────────

async def test_property_history_matches_direct_timeline_call(scenario):
    from bot.ai_tools.core import get_property_history
    from bot.core.property_timeline import build_property_timeline

    lid = await _insert_listing(scenario, 1)
    pid = await _make_property(scenario)
    await _link(pid, lid)

    direct = await build_property_timeline(pid)
    result = await get_property_history(pid)

    assert result.data["found"] is True
    # passthrough 1:1 — все ключи direct-результата должны быть в data без
    # изменений (задача: "без новой логики").
    for key, value in direct.items():
        assert result.data[key] == value
    assert result.confidence == direct["identity"]["confidence"]


async def test_property_history_unknown_property_id(db):
    from bot.ai_tools.core import get_property_history

    result = await get_property_history(2_000_000_000)
    assert result.data == {"property_id": 2_000_000_000, "found": False}
    assert "not found" in result.warnings[0]


# ── listing_analysis ──────────────────────────────────────────────────────

async def test_listing_analysis_no_comparables_marks_insufficient_data(scenario):
    from bot.ai_tools.core import get_listing_analysis

    lid = await _insert_listing(scenario, 2)
    result = await get_listing_analysis(lid)

    assert result.data["found"] is True
    assert result.data["price"] == 25_000_000
    assert result.data["comparables_count"] == 0
    assert result.data["insufficient_data"] is True
    assert any("no comparables" in w for w in result.warnings)
    assert "dom_scenario" in result.data


async def test_listing_analysis_unknown_listing(db):
    from bot.ai_tools.core import get_listing_analysis

    result = await get_listing_analysis("999999999998")
    assert result.data == {"listing_id": "999999999998", "found": False}
    assert "not found" in result.warnings[0]


# ── listing_risks passthrough ─────────────────────────────────────────────

async def test_listing_risks_passthrough_matches_build_listing_detail(scenario):
    from bot.ai_tools.core import get_listing_risks
    from bot.core.listing_detail import build_listing_detail

    lid = await _insert_listing(scenario, 3, floor=1, floors_total=1)  # триггерит floor-риск
    detail = await build_listing_detail(lid, tier="admin")
    result = await get_listing_risks(lid)

    assert result.data["found"] is True
    assert result.data["risk_analysis"] == detail["risk_analysis"]
    assert result.methodology_version == detail["risk_analysis"]["version"]
    assert result.as_of == detail["risk_analysis"]["calculated_at"]
    # floor=1/floors_total=1 — единственный этаж, должен дать хотя бы один
    # risk item (не пустой items) — не проверяем конкретный текст (это уже
    # проверено tests/test_listing_risks.py), только что passthrough не
    # обнулил items.
    assert isinstance(result.data["risk_analysis"]["items"], list)


async def test_listing_risks_unknown_listing(db):
    from bot.ai_tools.core import get_listing_risks

    result = await get_listing_risks("999999999997")
    assert result.data == {"listing_id": "999999999997", "found": False}


# ── complex_market_profile passthrough ─────────────────────────────────────

async def test_complex_market_profile_passthrough_and_insufficient_data_preserved(scenario):
    from bot.ai_tools.core import get_complex_market_profile
    from bot.core.complex_market_profile import get_complex_market_profile as direct_profile

    cid = await _make_complex(scenario, f"__TEST_AI_CMP_{scenario.suffix}__")
    result = await get_complex_market_profile(cid)
    direct = await direct_profile(cid)

    assert result.data["found"] is True
    # as_of отличается на микросекунды между двумя независимыми вызовами
    # (каждый сам берёт datetime.now(), as_of=None) — не часть passthrough-
    # гарантии, сравнивается отдельно (обе — валидные ISO-метки "только что").
    for key, value in direct.items():
        if key == "as_of":
            continue
        assert result.data[key] == value, key
    assert result.data["as_of"] and direct["as_of"]
    # свежий ЖК без properties — price/liquidity honestly insufficient_data.
    assert result.data["price"]["insufficient_data"] is True
    assert result.data["liquidity"]["insufficient_data"] is True
    # review PR #51 п.5 — demand.insufficient_history (свежий ЖК без единой
    # строки views_history) заслуживает тот же warning, что price/liquidity.
    assert result.data["demand"]["insufficient_history"] is True
    assert any("demand" in w and "insufficient_history" in w for w in result.warnings)


async def test_complex_market_profile_unknown_complex(db):
    from bot.ai_tools.core import get_complex_market_profile

    result = await get_complex_market_profile(2_000_000_000)
    assert result.data == {"complex_id": 2_000_000_000, "found": False}


# ── location_analysis passthrough ──────────────────────────────────────────

async def test_location_analysis_no_coords(scenario):
    from bot.ai_tools.core import get_location_analysis

    cid = await _make_complex(scenario, f"__TEST_AI_LOC_{scenario.suffix}__")
    result = await get_location_analysis(cid)

    assert result.data["found"] is True
    assert result.data["has_coords"] is False
    assert result.data["has_score"] is False
    assert any("no resolved geo centroid" in w for w in result.warnings)


async def test_location_analysis_unknown_complex(db):
    from bot.ai_tools.core import get_location_analysis

    result = await get_location_analysis(2_000_000_000)
    assert result.data == {"complex_id": 2_000_000_000, "found": False}
