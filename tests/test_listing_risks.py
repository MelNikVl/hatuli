"""tests/test_listing_risks.py — bot/core/listing_risks.py (задача
2026-08-21, "Риски объекта" — единый паспорт рисков объявления).

Часть 1 — чистые функции (floor/seller/kzk/building/location/valuation
сигналы) работают на голых dict'ах, БД не нужна. Часть 2 — интеграция
(price_history/property siblings/dom_forecast/demolition — реальные
запросы) на синтетических фикстурах реальной Postgres test DB, тот же
паттерн, что tests/test_dom_scenario.py. Часть 3 — API. Часть 4 —
реальный браузер (мобильная вёрстка блока)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
_NOW = datetime.now(timezone.utc)


def _days_ago(n: float) -> datetime:
    return _NOW - timedelta(days=n)


# ── Часть 1 — чистые функции, без БД ────────────────────────────────────

from bot.core.listing_risks import (  # noqa: E402
    _floor_signals, _building_signals, _seller_signals, _kzk_signals,
    _valuation_signals, _location_signals, _overall_level, _build_summary,
    SEVERITY_RANK, _ALWAYS_UNKNOWN_GROUPS, FALLBACK_RISK_ANALYSIS,
)


def _codes(items):
    return {it["code"] for it in items}


# ── Этаж ──────────────────────────────────────────────────────────────

def test_first_floor_signal():
    items = _floor_signals(1, 9)
    assert "FIRST_FLOOR" in _codes(items)
    assert items[0]["severity"] == "medium"


def test_last_floor_signal():
    items = _floor_signals(9, 9)
    assert "LAST_FLOOR" in _codes(items)
    assert items[0]["severity"] == "medium"


def test_middle_floor_no_signal():
    items = _floor_signals(5, 9)
    assert items == []


def test_floor_unknown_signal():
    items = _floor_signals(None, 9)
    assert "FLOOR_UNKNOWN" in _codes(items)
    items2 = _floor_signals(5, None)
    assert "FLOOR_UNKNOWN" in _codes(items2)


# ── Продавец ──────────────────────────────────────────────────────────

def test_seller_realtor_signal_is_info_not_high_risk():
    """задание: "риелтор — информационный сигнал... не автоматически
    высокий риск"."""
    items, unknowns = _seller_signals({"seller_type": "realtor", "active_listings_count": 1,
                                        "total_listings_count": 1, "is_large_agency": False,
                                        "is_high_relist_rate": False, "is_ambiguous": False})
    realtor_items = [it for it in items if it["code"] == "SELLER_REALTOR"]
    assert len(realtor_items) == 1
    assert realtor_items[0]["severity"] == "info"


def test_seller_large_agency_signal():
    items, _ = _seller_signals({"seller_type": "realtor", "active_listings_count": 62,
                                 "total_listings_count": 90, "is_large_agency": True,
                                 "is_high_relist_rate": False, "is_ambiguous": False})
    assert "SELLER_LARGE_AGENCY" in _codes(items)


def test_seller_high_relist_rate_signal():
    items, _ = _seller_signals({"seller_type": "owner", "active_listings_count": 2,
                                 "total_listings_count": 5, "is_large_agency": False,
                                 "is_high_relist_rate": True, "is_ambiguous": False})
    assert "SELLER_HIGH_RELIST_RATE" in _codes(items)
    assert next(it for it in items if it["code"] == "SELLER_HIGH_RELIST_RATE")["severity"] == "medium"


def test_seller_ambiguous_name_is_unknown_not_item():
    items, unknowns = _seller_signals({"seller_type": "owner", "active_listings_count": 1,
                                        "total_listings_count": 1, "is_large_agency": False,
                                        "is_high_relist_rate": False, "is_ambiguous": True})
    assert "SELLER_AMBIGUOUS_NAME" in {u["code"] for u in unknowns}
    assert "SELLER_AMBIGUOUS_NAME" not in _codes(items)


def test_seller_type_unknown_when_no_profile():
    items, unknowns = _seller_signals(None)
    assert items == []
    assert "SELLER_TYPE_UNKNOWN" in {u["code"] for u in unknowns}


# ── КЖК / защита дольщика ────────────────────────────────────────────

def test_kzk_blacklisted_is_critical():
    items, protective = _kzk_signals({"is_blacklisted": True, "warranty_scheme": None}, "primary")
    assert protective == []
    assert items[0]["code"] == "KZK_BLACKLISTED"
    assert items[0]["severity"] == "critical"


def test_kzk_no_protection_is_high():
    items, protective = _kzk_signals({"is_blacklisted": False, "warranty_scheme": None}, "primary")
    assert "KZK_NO_PROTECTION" in _codes(items)
    assert next(it for it in items if it["code"] == "KZK_NO_PROTECTION")["severity"] == "high"
    assert protective == []


def test_kzk_mio_permit_is_moderate_item_not_protective():
    items, protective = _kzk_signals({"is_blacklisted": False, "warranty_scheme": "Разрешение МИО"}, "primary")
    assert "KZK_MIO_PERMIT" in _codes(items)
    assert protective == []


def test_kzk_guarantee_is_protective_green():
    items, protective = _kzk_signals({"is_blacklisted": False, "warranty_scheme": "Гарантия КЖК"}, "primary")
    assert items == []
    assert protective[0]["code"] == "KZK_GUARANTEE"


def test_kzk_bvu_is_protective_green():
    items, protective = _kzk_signals({"is_blacklisted": False, "warranty_scheme": "Участие БВУ"}, "primary")
    assert items == []
    assert protective[0]["code"] == "KZK_BVU"


def test_kzk_skipped_for_secondary_market():
    """kzk_badge относится только к первичке — на вторичке сигнал не
    выдаётся, даже если badge почему-то передан."""
    items, protective = _kzk_signals({"is_blacklisted": True, "warranty_scheme": None}, "secondary")
    assert items == []
    assert protective == []


def test_kzk_skipped_when_no_badge():
    items, protective = _kzk_signals(None, "primary")
    assert items == [] and protective == []


# ── Оценка цены / valuation ──────────────────────────────────────────

def test_valuation_price_above_market_high():
    items, _ = _valuation_signals({"di": 0.7, "sources": "тот же дом/ЖК", "confidence": 80, "flags": []})
    assert "PRICE_ABOVE_MARKET" in _codes(items)
    assert next(it for it in items if it["code"] == "PRICE_ABOVE_MARKET")["severity"] == "high"


def test_valuation_price_at_market_no_signal():
    items, _ = _valuation_signals({"di": 1.0, "sources": "тот же дом/ЖК", "confidence": 80, "flags": []})
    assert "PRICE_ABOVE_MARKET" not in _codes(items)


def test_valuation_few_comparables_is_unknown():
    items, unknowns = _valuation_signals({"di": 1.0, "sources": "только город", "confidence": 80, "flags": []})
    assert "VALUATION_CITY_ONLY" in {u["code"] for u in unknowns}
    assert items == [] or "VALUATION_CITY_ONLY" not in _codes(items)


def test_valuation_unavailable_when_no_hex_details():
    items, unknowns = _valuation_signals(None)
    assert items == []
    assert "VALUATION_UNAVAILABLE" in {u["code"] for u in unknowns}


def test_valuation_low_confidence_is_unknown():
    items, unknowns = _valuation_signals({"di": 1.0, "sources": "тот же дом/ЖК", "confidence": 20, "flags": []})
    assert "VALUATION_LOW_CONFIDENCE" in {u["code"] for u in unknowns}


# ── Характеристики объекта/дома ──────────────────────────────────────

def test_old_building_signal_neutral_wording():
    items, _ = _building_signals(1985, None, "комфорт")
    old = next(it for it in items if it["code"] == "OLD_BUILDING")
    assert old["severity"] == "low"
    for forbidden in ["аварий", "незакон", "опасн"]:
        assert forbidden not in old["description"].lower()


def test_year_built_unknown_signal():
    items, _ = _building_signals(None, None, "комфорт")
    assert "YEAR_BUILT_UNKNOWN" in _codes(items)


def test_complex_class_unknown_is_data_limitation_not_object_item():
    """Регрессия: раньше "класс ЖК не известен" дублировался и как item
    (В), и как unknown (А) — задание прямо требует не путать нехватку
    данных с риском самого объекта, теперь это ТОЛЬКО unknown."""
    items, unknowns = _building_signals(2015, None, None)
    assert "COMPLEX_CLASS_UNKNOWN" not in _codes(items)
    assert "COMPLEX_CLASS_UNKNOWN" in {u["code"] for u in unknowns}


def test_relayout_unconfirmed_legality_signal():
    items, _ = _building_signals(2015, {"is_relayout": True, "is_relayout_legal": False,
                                         "is_free_layout": False}, "комфорт")
    assert "RELAYOUT_LEGALITY_UNCONFIRMED" in _codes(items)
    assert next(it for it in items if it["code"] == "RELAYOUT_LEGALITY_UNCONFIRMED")["severity"] == "medium"


def test_relayout_confirmed_legal_no_signal():
    items, _ = _building_signals(2015, {"is_relayout": True, "is_relayout_legal": True,
                                         "is_free_layout": False}, "комфорт")
    assert "RELAYOUT_LEGALITY_UNCONFIRMED" not in _codes(items)


def test_free_layout_signal():
    items, _ = _building_signals(2015, {"is_relayout": False, "is_relayout_legal": None,
                                         "is_free_layout": True}, "комфорт")
    assert "FREE_LAYOUT" in _codes(items)


# ── Локация ───────────────────────────────────────────────────────────

def test_noise_major_road_signal():
    items, _ = _location_signals({"noise": {"adj": -6, "reason": "магистраль в 80м"},
                                   "transit": {"adj": 3, "reason": "остановка рядом"}}, None)
    assert "NOISE_MAJOR_ROAD" in _codes(items)
    assert next(it for it in items if it["code"] == "NOISE_MAJOR_ROAD")["severity"] == "medium"


def test_no_noise_no_signal():
    items, _ = _location_signals({"noise": {"adj": 0, "reason": "тихо"},
                                   "transit": {"adj": 3, "reason": "остановка рядом"}}, None)
    assert "NOISE_MAJOR_ROAD" not in _codes(items)


def test_weak_transit_is_unknown():
    items, unknowns = _location_signals({"noise": {"adj": 0, "reason": "тихо"},
                                          "transit": {"adj": 0, "reason": "нет остановок рядом"}}, None)
    assert "TRANSIT_WEAK" in {u["code"] for u in unknowns}


def test_location_layers_unavailable_is_unknown():
    items, unknowns = _location_signals(None, None)
    assert items == []
    assert "LOCATION_LAYERS_UNAVAILABLE" in {u["code"] for u in unknowns}


def test_demolition_nearby_signal():
    items, _ = _location_signals({"noise": {"adj": 0, "reason": "тихо"}, "transit": {"adj": 3, "reason": "ок"}},
                                  {"adj": -2, "reason": "рядом дом из перечня на снос (120м)"})
    assert "NEAR_DEMOLITION_LIST" in _codes(items)


def test_no_poi_is_not_automatically_negative():
    """"не превращать отсутствие POI в доказанный негативный риск" —
    отсутствие demolition-совпадения не создаёт сигнала вообще."""
    items, _ = _location_signals({"noise": {"adj": 0, "reason": "тихо"}, "transit": {"adj": 3, "reason": "ок"}},
                                  {"adj": 0, "reason": "рядом нет объектов из перечня на снос"})
    assert items == []


# ── overall_level / summary ──────────────────────────────────────────

def test_overall_level_is_max_not_average():
    """"overall_level определяется по самому серьёзному подтверждённому
    сигналу, а не непрозрачной средней арифметикой" — куча low-сигналов
    не должна "усредниться" ниже единственного critical."""
    items = [{"severity": "low"}] * 10 + [{"severity": "critical"}]
    assert _overall_level(items) == "critical"


def test_overall_level_info_when_no_items():
    assert _overall_level([]) == "info"


def test_overall_level_medium_for_medium_only():
    items = [{"severity": "info"}, {"severity": "medium"}, {"severity": "low"}]
    assert _overall_level(items) == "medium"


def test_summary_mentions_significant_and_unknowns():
    items = [{"severity": "high"}, {"severity": "medium"}]
    s = _build_summary(items, 3)
    assert "2" in s and "3" in s


def test_always_unknown_groups_are_compact_three():
    """задание §4: "нельзя автоматически показывать огромный одинаковый
    список" — компактные группы, не десяток отдельных строк."""
    assert len(_ALWAYS_UNKNOWN_GROUPS) == 3
    codes = {u["code"] for u in _ALWAYS_UNKNOWN_GROUPS}
    assert codes == {"DOCUMENTS_UNKNOWN", "TECHNICAL_UNKNOWN", "DEAL_HISTORY_UNKNOWN"}


def test_fallback_shape_matches_task_example():
    assert FALLBACK_RISK_ANALYSIS["overall_level"] == "unknown"
    assert FALLBACK_RISK_ANALYSIS["items"] == []
    assert FALLBACK_RISK_ANALYSIS["unknowns"] == []
    assert "не рассчитаны" in FALLBACK_RISK_ANALYSIS["summary"]


def test_no_confirmed_sale_or_object_not_selling_claims():
    """"не писать, что объект «не продаётся»" — грепаем весь модуль
    исходников на характерные формулировки."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot", "core", "listing_risks.py"),
                encoding="utf-8").read()
    for forbidden in ["не продаётся", "точно продастся", "подтверждена продажа", "аварийный дом", "незаконная перепланировка"]:
        assert forbidden not in src


def test_no_stale_5pct_risk_weight_text_anywhere():
    """задание §7: убрать устаревшее "риск (5%)" в весах Deal Score."""
    for path in ["bot/templates/dashboard.html", "README.md", "bot/templates/info.html"]:
        full = os.path.join(os.path.dirname(__file__), "..", path)
        text = open(full, encoding="utf-8").read()
        assert "риск (5%)" not in text, f"устаревший текст найден в {path}"


# ── Часть 2 — интеграция с БД (синтетические фикстуры) ──────────────────

@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


class _Scenario:
    def __init__(self, tag: str):
        self.tag = tag
        self.suffix = uuid.uuid4().hex[:8]
        self.district = f"__TEST_RISK_{tag}_{self.suffix}__"
        self.listing_ids: list[str] = []
        self.property_ids: list[int] = []
        self.seller_names: list[str] = []

    def lid(self, n) -> str:
        return f"__test_risk_{self.tag}_{self.suffix}_{n}__"


@pytest_asyncio.fixture
async def scenario(db, request):
    sc = _Scenario(request.node.name[:16])
    yield sc
    from bot.db.pg import execute
    for name in sc.seller_names:
        await execute("DELETE FROM seller_profiles WHERE seller_name = $1", name)
    for lid in sc.listing_ids:
        await execute("DELETE FROM outcome_labels WHERE listing_id = $1", lid)
        await execute("DELETE FROM price_history WHERE listing_id = $1", lid)
        await execute("DELETE FROM property_listings WHERE listing_id = $1", lid)
    for pid in sc.property_ids:
        await execute("DELETE FROM properties WHERE property_id = $1", pid)
    for lid in sc.listing_ids:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)


async def _insert(sc: _Scenario, n: int, *, price=30_000_000, area=50.0, rooms=2,
                   floor=5, floors_total=9, district=None, first_seen=None,
                   is_active=True, seller_name=None, market_type="secondary",
                   lat=None, lon=None):
    from bot.db.pg import execute
    lid = sc.lid(n)
    sc.listing_ids.append(lid)
    await execute(
        """
        INSERT INTO apartment_listings
            (id, url, price, area, rooms, floor, floors_total, district, market_type,
             is_active, first_seen, seller_name, lat, lon)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        ON CONFLICT (id) DO UPDATE SET price=$3, area=$4, rooms=$5, floor=$6,
            floors_total=$7, district=$8, is_active=$10, seller_name=$12
        """,
        lid, f"https://krisha.kz/test/{lid}", price, area, rooms, floor, floors_total,
        district or sc.district, market_type, is_active, first_seen or _days_ago(20),
        seller_name, lat, lon,
    )
    return lid


async def _price_change(lid, old_price, new_price, changed_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        lid, old_price, new_price, changed_at,
    )


async def _link_property(sc: _Scenario, listing_ids: list[str]):
    from bot.db.pg import execute, fetchval
    address_hash = f"__test_risk_prop_{sc.tag}_{sc.suffix}_{uuid.uuid4().hex[:6]}__"
    pid = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm, rooms) VALUES ($1,5,50.0,2) "
        "RETURNING property_id",
        address_hash,
    )
    sc.property_ids.append(pid)
    for lid in listing_ids:
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
            "VALUES ($1,$2,'auto',1.0)", pid, lid,
        )
    return pid


@pytest.mark.asyncio
async def test_price_history_stats_counts_decreases(scenario, db):
    from bot.core.listing_risks import _price_history_stats
    lid = await _insert(scenario, "target", price=28_000_000)
    await _price_change(lid, 31_000_000, 30_000_000, _days_ago(15))
    await _price_change(lid, 30_000_000, 28_000_000, _days_ago(5))
    stats = await _price_history_stats(lid)
    assert stats["decreases"] == 2
    assert stats["changes"] == 2


@pytest.mark.asyncio
async def test_property_siblings_detects_relist(scenario, db):
    from bot.core.listing_risks import _property_siblings
    lid_a = await _insert(scenario, "a")
    lid_b = await _insert(scenario, "b")
    await _link_property(scenario, [lid_a, lid_b])
    siblings = await _property_siblings(lid_a)
    assert {s["id"] for s in siblings} == {lid_b}
    assert await _property_siblings(await _insert(scenario, "solo")) == []


@pytest.mark.asyncio
async def test_compute_listing_risks_price_cut_still_active(scenario, db):
    from bot.core.listing_risks import compute_listing_risks
    lid = await _insert(scenario, "target", price=28_000_000, is_active=True)
    await _price_change(lid, 30_000_000, 28_000_000, _days_ago(5))
    l = {"id": lid, "price": 28_000_000, "area": 50.0, "rooms": 2, "floor": 5,
         "floors_total": 9, "district": scenario.district, "market_type": "secondary",
         "is_active": True, "first_seen": _days_ago(20), "year_built": 2015,
         "lat": None, "lon": None, "hex_details": None}
    ra = await compute_listing_risks(lid, l, kzk_badge=None, seller_profile=None,
                                      layers=None, ai_analysis=None, complex_housing_class="комфорт")
    codes = {it["code"] for it in ra["items"]}
    assert "PRICE_CUT_STILL_ACTIVE" in codes


@pytest.mark.asyncio
async def test_compute_listing_risks_property_relisted(scenario, db):
    from bot.core.listing_risks import compute_listing_risks
    lid_a = await _insert(scenario, "a")
    lid_b = await _insert(scenario, "b")
    await _link_property(scenario, [lid_a, lid_b])
    l = {"id": lid_a, "price": 30_000_000, "area": 50.0, "rooms": 2, "floor": 5,
         "floors_total": 9, "district": scenario.district, "market_type": "secondary",
         "is_active": True, "first_seen": _days_ago(20), "year_built": 2015,
         "lat": None, "lon": None, "hex_details": None}
    ra = await compute_listing_risks(lid_a, l, kzk_badge=None, seller_profile=None,
                                      layers=None, ai_analysis=None, complex_housing_class="комфорт")
    codes = {it["code"] for it in ra["items"]}
    assert "PROPERTY_RELISTED" in codes


@pytest.mark.asyncio
async def test_compute_listing_risks_no_confirmed_risks_clean_listing(scenario, db):
    """Средний этаж, известный год, приватный продавец без флагов,
    вторичка (KZK неприменим), нет истории снижений — ноль items."""
    from bot.core.listing_risks import compute_listing_risks
    lid = await _insert(scenario, "target", price=30_000_000, floor=5, floors_total=9)
    l = {"id": lid, "price": 30_000_000, "area": 50.0, "rooms": 2, "floor": 5,
         "floors_total": 9, "district": scenario.district, "market_type": "secondary",
         "is_active": True, "first_seen": _days_ago(5), "year_built": 2018,
         "lat": None, "lon": None, "hex_details": None}
    ra = await compute_listing_risks(lid, l, kzk_badge=None, seller_profile=None,
                                      layers=None, ai_analysis=None, complex_housing_class="комфорт")
    assert ra["items"] == []
    assert ra["overall_level"] == "info"
    assert len(ra["unknowns"]) >= 3  # минимум фиксированные 3 группы + valuation/location


@pytest.mark.asyncio
async def test_compute_listing_risks_safe_graceful_fallback(scenario, db, monkeypatch):
    import bot.core.listing_risks as risks_mod

    async def _boom(*a, **kw):
        raise RuntimeError("симуляция сбоя")

    monkeypatch.setattr(risks_mod, "_price_history_stats", _boom)
    lid = await _insert(scenario, "target")
    l = {"id": lid, "price": 30_000_000, "area": 50.0, "rooms": 2, "floor": 5,
         "floors_total": 9, "district": scenario.district, "market_type": "secondary",
         "is_active": True, "first_seen": _days_ago(5), "year_built": 2018,
         "lat": None, "lon": None, "hex_details": None}
    ra = await risks_mod.compute_listing_risks_safe(
        lid, l, kzk_badge=None, seller_profile=None, layers=None,
        ai_analysis=None, complex_housing_class="комфорт")
    assert ra["overall_level"] == "unknown"
    assert ra["items"] == []


# ── Часть 3 — API ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db):
    import httpx
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    bdb = BotDB(DB_PATH)
    await bdb.init()
    app = create_admin_app(bdb, ADMIN_PASSWORD, "test", DB_PATH)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                  cookies={"admin_auth": "1"}) as c:
        yield c


@pytest.mark.asyncio
async def test_api_listing_detail_includes_risk_analysis(client, scenario, db):
    lid = await _insert(scenario, "target", floor=1)
    r = await client.get(f"/admin/api/listing/{lid}")
    assert r.status_code == 200
    body = r.json()
    assert "risk_analysis" in body
    ra = body["risk_analysis"]
    assert ra["overall_level"] in ("critical", "high", "medium", "low", "info", "unknown")
    codes = {it["code"] for it in ra["items"]}
    assert "FIRST_FLOOR" in codes  # floor=1 задан явно выше


@pytest.mark.asyncio
async def test_api_graceful_fallback_does_not_break_listing_card(client, scenario, db, monkeypatch):
    """"ошибка расчёта рисков не должна ломать карточку объявления" —
    даже если весь risk-модуль падает, /admin/api/listing/{id} — 200."""
    import bot.core.listing_risks as risks_mod

    async def _boom(*a, **kw):
        raise RuntimeError("симуляция сбоя")

    monkeypatch.setattr(risks_mod, "compute_listing_risks", _boom)
    lid = await _insert(scenario, "target")
    r = await client.get(f"/admin/api/listing/{lid}")
    assert r.status_code == 200
    body = r.json()
    assert body.get("error") is None
    assert body["risk_analysis"]["overall_level"] == "unknown"


@pytest.mark.asyncio
async def test_dashboard_popup_shell_contains_risk_analysis_block(client, db):
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Риски объекта" in html
    assert "modal-risk-analysis-body" in html
    assert "renderRiskAnalysisBody" in html
    assert "риск (5%)" not in html


# ── Часть 4 — реальный браузер (Playwright), мобильная ширина ───────────

_MOBILE_PORT = 8097

_FAKE_RISK_RESPONSE = json.dumps({
    "overall_level": "high",
    "summary": "Обнаружено 2 значимых риска и 3 ограничения данных",
    "items": [
        {"code": "PRICE_ABOVE_MARKET", "category": "valuation", "severity": "high",
         "title": "Цена заметно выше ожидаемой рыночной",
         "description": "По сравнению с похожими объектами рядом цена выглядит завышенной примерно на 20%.",
         "source": "Deal Score — оценка по локальным аналогам",
         "recommendation": "Сравнить с 2-3 похожими объявлениями.", "verified": True},
        {"code": "KZK_NO_PROTECTION", "category": "legal", "severity": "high",
         "title": "Нет подтверждённой официальной схемы защиты дольщика",
         "description": "В реестре КЖК не нашлось подтверждённой схемы защиты дольщика.",
         "source": "Реестр КЖК", "recommendation": "Уточнить у застройщика напрямую.", "verified": True},
        {"code": "FIRST_FLOOR", "category": "object", "severity": "medium",
         "title": "Первый этаж", "description": "Квартира на первом этаже.",
         "source": "Данные объявления", "recommendation": None, "verified": True},
        {"code": "SELLER_REALTOR", "category": "seller", "severity": "info",
         "title": "Продавец — риелтор", "description": "Возможна комиссия.",
         "source": "Профиль продавца", "recommendation": None, "verified": True},
    ],
    "protective": [
        {"code": "KZK_BVU", "category": "legal", "title": "Участие БВУ",
         "description": "Подтверждено участие банка в схеме финансирования.", "source": "Реестр КЖК"},
    ],
    "unknowns": [
        {"code": "DOCUMENTS_UNKNOWN", "title": "Документы не проверены", "description": "Право собственности и обременения не проверяются."},
        {"code": "TECHNICAL_UNKNOWN", "title": "Техническое состояние не проверено", "description": "Кровля и коммуникации не осматривались."},
        {"code": "DEAL_HISTORY_UNKNOWN", "title": "История сделки неизвестна", "description": "Причина продажи неизвестна."},
    ],
    "calculated_at": "2026-08-22T12:00:00Z",
    "version": "listing_risk_v1",
}, ensure_ascii=False)


@pytest_asyncio.fixture
async def mobile_live_server():
    import uvicorn
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    from bot.admin_web import create_admin_app
    from bot.db.compat import BotDB
    db = BotDB("/tmp/__test_listing_risks_mobile_admin.db")
    await db.init()
    app = create_admin_app(db, admin_password="x", bot_version="test")
    config = uvicorn.Config(app=app, host="127.0.0.1", port=_MOBILE_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{_MOBILE_PORT}"
    server.should_exit = True
    await task
    await close_pool()


@pytest.mark.asyncio
async def test_risk_analysis_block_fits_on_mobile_width(mobile_live_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 360, "height": 720})
        await page.goto(mobile_live_server + "/", wait_until="networkidle", timeout=25000)
        await page.wait_for_function("typeof renderRiskAnalysisBody === 'function'", timeout=10000)

        metrics = await page.evaluate(
            """(fakeJson) => {
                const ra = JSON.parse(fakeJson);
                const container = document.createElement('div');
                container.style.cssText = 'width:328px;box-sizing:border-box;';
                document.body.appendChild(container);
                container.innerHTML = renderRiskAnalysisBody(ra, 'test123');
                return {
                    scrollWidth: container.scrollWidth,
                    clientWidth: container.clientWidth,
                    hasHeader: container.innerHTML.includes('Обнаружены значимые риски'),
                    hasNotFullyCheckedNote: container.innerHTML.includes('не означает, что объект полностью проверен'),
                    hasShowAllButton: container.innerHTML.includes('Показать все'),
                    hasProtective: container.innerHTML.includes('Участие БВУ'),
                    hasUnknownGroup: container.innerHTML.includes('Документы не проверены'),
                };
            }""",
            _FAKE_RISK_RESPONSE,
        )
        await browser.close()

    assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1, (
        f"блок рисков создаёт собственный горизонтальный оверфлоу на мобильной ширине: "
        f"scrollWidth={metrics['scrollWidth']} > clientWidth={metrics['clientWidth']}"
    )
    assert metrics["hasHeader"] is True
    assert metrics["hasNotFullyCheckedNote"] is True
    assert metrics["hasShowAllButton"] is True  # 4 items > 3 -> кнопка "Показать все"
    assert metrics["hasProtective"] is True
    assert metrics["hasUnknownGroup"] is True
