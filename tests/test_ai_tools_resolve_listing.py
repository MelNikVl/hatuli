"""tests/test_ai_tools_resolve_listing.py — Этап 1, capability resolve_listing.
Синтетические фикстуры, тот же паттерн, что tests/test_listing_risks.py /
tests/test_complex_market_profile.py: реальная Postgres (DATABASE_URL),
id с префиксом '__test_ai_resolve_...__', удаляются в finally."""
import os
import sys
import uuid
from datetime import datetime, timezone

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
        # numeric_suffix — ЧИСТО цифровой (resolve_listing извлекает id из URL
        # регэкспом r"/(\d{5,})", тем же, что bot/core/parser.py использует
        # для настоящих карточек Крыши — буквы в id сломали бы этот матч).
        self.numeric_suffix = str(uuid.uuid4().int)[:9]
        self.listing_ids: list[str] = []
        self.property_ids: list[int] = []
        self.complex_ids: list[int] = []

    def lid(self, n) -> str:
        return f"9{self.numeric_suffix}{n}"  # выглядит как настоящий numeric krisha id


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


async def _insert_listing(sc: _Scenario, n: int, *, complex_name=None, price=25_000_000,
                           address="ул. Тестовая, 1", district="Есильский"):
    from bot.db.pg import execute
    lid = sc.lid(n)
    sc.listing_ids.append(lid)
    await execute(
        """
        INSERT INTO apartment_listings (id, url, price, area, rooms, address, district,
                                         complex_name, market_type, is_active, first_seen)
        VALUES ($1,$2,$3,45.0,2,$4,$5,$6,'secondary', TRUE, now())
        ON CONFLICT (id) DO NOTHING
        """,
        lid, f"https://krisha.kz/a/show/{lid}", price, address, district, complex_name,
    )
    return lid


async def _make_complex(sc: _Scenario, name: str) -> int:
    from bot.db.pg import fetchval
    cid = await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)
    sc.complex_ids.append(cid)
    return cid


async def _link_property(sc: _Scenario, lid: str, complex_id: int | None, *,
                          confidence=0.92, link_method="auto") -> int:
    from bot.db.pg import execute, fetchval
    address_hash = f"__test_ai_resolve_prop_{sc.suffix}_{uuid.uuid4().hex[:6]}__"
    pid = await fetchval(
        "INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms) "
        "VALUES ($1,$2,5,45.0,2) RETURNING property_id",
        complex_id, address_hash,
    )
    sc.property_ids.append(pid)
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1,$2,$3,$4)",
        pid, lid, link_method, confidence,
    )
    return pid


# ── URL -> listing resolution ────────────────────────────────────────────

async def test_resolve_by_full_url_with_property_identity(scenario):
    from bot.ai_tools.core import resolve_listing

    complex_name = f"__TEST_AI_RESOLVE_CX_{scenario.suffix}__"
    cid = await _make_complex(scenario, complex_name)
    lid = await _insert_listing(scenario, 1, complex_name=complex_name)
    await _link_property(scenario, lid, cid, confidence=0.87, link_method="auto")

    result = await resolve_listing(f"https://krisha.kz/a/show/{lid}")
    assert result.data["found"] is True
    assert result.data["listing_id"] == lid
    assert result.data["complex_id"] == cid
    assert result.data["complex_id_resolution"] == "property_identity"
    assert result.data["property_id"] is not None
    assert result.confidence == pytest.approx(0.87)
    assert result.warnings == []
    assert result.methodology_version


async def test_resolve_by_bare_id(scenario):
    from bot.ai_tools.core import resolve_listing

    lid = await _insert_listing(scenario, 2)
    result = await resolve_listing(lid)
    assert result.data["found"] is True
    assert result.data["listing_id"] == lid


async def test_resolve_complex_id_fallback_via_name_match_when_not_linked(scenario):
    """Property Identity ещё не связала объявление — complex_id всё равно
    резолвится через complex_name text-match, но ЯВНО помечен как менее
    надёжный fallback (не молча выдан за property_identity)."""
    from bot.ai_tools.core import resolve_listing

    complex_name = f"__TEST_AI_RESOLVE_CX_FALLBACK_{scenario.suffix}__"
    cid = await _make_complex(scenario, complex_name)
    lid = await _insert_listing(scenario, 3, complex_name=complex_name)

    result = await resolve_listing(f"https://krisha.kz/a/show/{lid}")
    assert result.data["found"] is True
    assert result.data["complex_id"] == cid
    assert result.data["complex_id_resolution"] == "complex_name_match"
    assert result.data["property_id"] is None
    assert any("Property Identity" in w for w in result.warnings)
    assert any("complex_name text match" in w for w in result.warnings)


# ── unknown listing ───────────────────────────────────────────────────────

async def test_resolve_unknown_listing_id(db):
    from bot.ai_tools.core import resolve_listing

    result = await resolve_listing("999999999999")
    assert result.data["found"] is False
    assert result.data["listing_id"] == "999999999999"
    assert "not found" in result.warnings[0]


async def test_resolve_unparsable_input(db):
    from bot.ai_tools.core import resolve_listing

    result = await resolve_listing("not a krisha url at all")
    assert result.data["found"] is False
    assert result.data["listing_id"] is None
    assert "could not extract" in result.warnings[0]
