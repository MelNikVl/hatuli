"""tests/test_market_dashboards.py — «Обзор рынка» + «Поглощение и
ликвидность» (задача 2026-08-21, /admin/analytics/market-overview и
/admin/analytics/market-absorption). Synthetic fixtures only — тот же
паттерн, что tests/test_property_identity_dashboard.py: реальная Postgres
test DB (DATABASE_URL), никакой зависимости от прод-данных.

Изоляция от прод-данных, которые уже лежат в той же таблице
apartment_listings: НЕ префикс id (KPI/графики читают ВСЮ таблицу, не
по id), а уникальный синтетический complex_id/complex_name на тест —
все проверяемые запросы фильтруются по нему (`complex_id=...` в filters),
поэтому прод-строки в агрегаты не попадают. district/rooms/market_type
внутри одного теста подбираются так, чтобы не пересекаться с чужими
синтетическими комплексами других тестов, запущенных параллельно (в CI
тесты этого файла идут последовательно в одном процессе pytest, но имя
ЖК всё равно уникализируется через uuid на случай retry/параллели)."""
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
_NOW = datetime.now(timezone.utc)


def _days_ago(n: float) -> datetime:
    return _NOW - timedelta(days=n)


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


class _Scenario:
    """Один изолированный синтетический ЖК на тест — id листингов
    генерируются на его основе, всё чистится в finally по complex_id."""

    def __init__(self, tag: str):
        self.tag = tag
        self.suffix = uuid.uuid4().hex[:8]
        self.complex_name = f"__TEST_MKT_{tag}_{self.suffix}__"
        self.complex_id: int | None = None
        self.listing_ids: list[str] = []

    def lid(self, n: int) -> str:
        return f"__test_mkt_{self.tag}_{self.suffix}_{n}__"


@pytest_asyncio.fixture
async def scenario(db, request):
    from bot.db.pg import execute, fetchval
    sc = _Scenario(request.node.name[:20])
    sc.complex_id = await fetchval(
        "INSERT INTO complexes (name, district, housing_class) VALUES ($1, $2, $3) RETURNING id",
        sc.complex_name, "Есильский р-н", "комфорт",
    )
    yield sc
    # Cleanup — сперва зависимые таблицы, потом сам listing/complex.
    for lid in sc.listing_ids:
        await execute("DELETE FROM outcome_labels WHERE listing_id = $1", lid)
        await execute("DELETE FROM price_history WHERE listing_id = $1", lid)
        await execute("DELETE FROM listing_archive_history WHERE listing_id = $1", lid)
        await execute("DELETE FROM property_listings WHERE listing_id = $1", lid)
    await execute("DELETE FROM properties WHERE address_hash LIKE $1", f"__test_mkt_{sc.tag}_{sc.suffix}%")
    for lid in sc.listing_ids:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
    await execute("DELETE FROM complexes WHERE id = $1", sc.complex_id)


async def _insert(sc: _Scenario, n: int, *, price=30_000_000, area=50.0, rooms=2,
                   district="Есильский р-н", market_type="secondary",
                   first_seen=None, last_seen=None, archived_at=None, archive_reason=None,
                   is_active=True):
    from bot.db.pg import execute
    lid = sc.lid(n)
    sc.listing_ids.append(lid)
    await execute(
        """
        INSERT INTO apartment_listings
            (id, url, price, area, rooms, district, complex_name, market_type,
             is_active, first_seen, last_seen, archived_at, archive_reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (id) DO UPDATE SET price=$3, area=$4, rooms=$5, district=$6,
            complex_name=$7, market_type=$8, is_active=$9, first_seen=$10, last_seen=$11,
            archived_at=$12, archive_reason=$13
        """,
        lid, f"https://krisha.kz/test/{lid}", price, area, rooms, district, sc.complex_name,
        market_type, is_active, first_seen or _days_ago(60), last_seen or _NOW,
        archived_at, archive_reason,
    )
    return lid


async def _price_change(lid, old_price, new_price, changed_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        lid, old_price, new_price, changed_at,
    )


async def _outcome(lid, *, time_on_market=None):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO outcome_labels (listing_id, time_on_market) VALUES ($1,$2) "
        "ON CONFLICT (listing_id) DO UPDATE SET time_on_market=$2",
        lid, time_on_market,
    )


async def _property(sc: _Scenario, lid, *, first_seen_at, floor=5, area_sqm=50.0):
    from bot.db.pg import execute, fetchval
    addr_hash = f"__test_mkt_{sc.tag}_{sc.suffix}_{lid}__"
    pid = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm, complex_id, first_seen_at) "
        "VALUES ($1,$2,$3,$4,$5) RETURNING property_id",
        addr_hash, floor, area_sqm, sc.complex_id, first_seen_at,
    )
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1,$2,'auto',1.0)", pid, lid,
    )
    return pid


def _base_filters(complex_id: int, **overrides) -> dict:
    from bot.analytics import market_dashboards as md
    raw = {"period": "30", "complex_id": str(complex_id)}
    raw.update(overrides)
    return md.normalize_filters(raw)


# ══════════════════════════════════════════════════════════════════════
# 1-2. Доступ администратора + пункты меню
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_market_pages_require_admin_auth(db):
    from bot.admin_web import create_admin_app
    from bot.db.compat import BotDB
    from httpx import AsyncClient, ASGITransport
    app = create_admin_app(BotDB("/tmp/__test_mkt_admin.db"), admin_password="x", bot_version="test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        for path in ("/admin/analytics/market-overview", "/admin/analytics/market-absorption"):
            r = await client.get(path)
            assert r.status_code == 302
            assert r.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_market_pages_render_when_authed(db):
    from bot.admin_web import create_admin_app
    from bot.db.compat import BotDB
    from httpx import AsyncClient, ASGITransport
    app = create_admin_app(BotDB("/tmp/__test_mkt_admin.db"), admin_password="x", bot_version="test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            cookies={"admin_auth": "1"}) as client:
        for path in ("/admin/analytics/market-overview", "/admin/analytics/market-absorption"):
            r = await client.get(path)
            assert r.status_code == 200


def test_menu_has_two_market_entries():
    tabs_html = open(os.path.join(os.path.dirname(__file__), "..", "bot", "templates",
                                   "_analytics_tabs.html"), encoding="utf-8").read()
    assert tabs_html.count('href="/admin/analytics/market-overview"') == 1
    assert tabs_html.count('href="/admin/analytics/market-absorption"') == 1
    assert "Рыночная аналитика" in tabs_html


def test_menu_active_state_uses_atab_variable():
    tabs_html = open(os.path.join(os.path.dirname(__file__), "..", "bot", "templates",
                                   "_analytics_tabs.html"), encoding="utf-8").read()
    assert "atab == 'market_overview'" in tabs_html
    assert "atab == 'market_absorption'" in tabs_html


# ══════════════════════════════════════════════════════════════════════
# 3. Фильтры
# ══════════════════════════════════════════════════════════════════════

def test_normalize_filters_rejects_invalid_values():
    from bot.analytics import market_dashboards as md
    f = md.normalize_filters({
        "period": "bogus", "district": "Совершенно другой район",
        "klass": "not-a-class", "rooms": "99", "market_type": "rental", "status": "??",
    })
    assert f["period"] == "30"
    assert f["district"] == ""
    assert f["klass"] == ""
    assert f["rooms"] == ""
    assert f["market_type"] == ""
    assert f["status"] == "active"


def test_normalize_filters_accepts_valid_values():
    from bot.analytics import market_dashboards as md
    f = md.normalize_filters({
        "period": "90", "district": "Алматы р-н", "klass": "бизнес",
        "rooms": "4+", "market_type": "primary", "status": "archived",
    })
    assert f["period_days"] == 90
    assert f["district"] == "Алматы р-н"
    assert f["klass"] == "бизнес"
    assert f["rooms"] == "4+"
    assert f["market_type"] == "primary"
    assert f["status"] == "archived"


# ══════════════════════════════════════════════════════════════════════
# 4-8. Формулы KPI: медиана цены, цена/м², новое предложение,
#      подтверждённое выбывание, деление на ноль
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_median_price_and_ppm2(scenario):
    from bot.analytics import market_dashboards as md
    # 3 активных: 20/30/50 м², цены дают ppm2 = 500k каждая -> медиана точна.
    await _insert(scenario, 1, price=10_000_000, area=20.0)
    await _insert(scenario, 2, price=15_000_000, area=30.0)
    await _insert(scenario, 3, price=25_000_000, area=50.0)
    filters = _base_filters(scenario.complex_id)
    kpis = await md.overview_kpis(filters)
    assert kpis["median_price"]["value"] == pytest.approx(15_000_000)
    assert kpis["median_ppm2"]["value"] == pytest.approx(500_000)
    assert kpis["active_listings"]["value"] == 3


@pytest.mark.asyncio
async def test_new_supply_counts_only_within_period(scenario):
    from bot.analytics import market_dashboards as md
    await _insert(scenario, 1, first_seen=_days_ago(5))   # внутри 30-дневного периода
    await _insert(scenario, 2, first_seen=_days_ago(45))  # старше периода
    filters = _base_filters(scenario.complex_id, period="30")
    kpis = await md.overview_kpis(filters)
    assert kpis["new_supply"]["value"] == 1


@pytest.mark.asyncio
async def test_confirmed_exits_counts_archived_in_period(scenario):
    from bot.analytics import market_dashboards as md
    await _insert(scenario, 1, first_seen=_days_ago(50), archived_at=_days_ago(5),
                   archive_reason="confirmed_gone", is_active=False)
    await _insert(scenario, 2, first_seen=_days_ago(50), archived_at=_days_ago(45),
                   archive_reason="confirmed_gone", is_active=False)  # архивирован ДО периода
    await _insert(scenario, 3, first_seen=_days_ago(50))  # всё ещё активен
    filters = _base_filters(scenario.complex_id, period="30")
    kpis = await md.overview_kpis(filters)
    assert kpis["confirmed_exits"]["value"] == 1


@pytest.mark.asyncio
async def test_confirmed_exits_includes_reactivated_within_period(scenario):
    """Ключевая находка аудита (см. модульный докстринг
    _confirmed_exits_in_period): листинг, ушедший в архив И
    реактивированный ВНУТРИ одного периода, должен всё равно посчитаться
    как событие выбывания — иначе воронка не сходится."""
    from bot.analytics import market_dashboards as md
    from bot.db.pg import execute
    lid = await _insert(scenario, 1, first_seen=_days_ago(50), is_active=True, archived_at=None)
    await execute(
        "INSERT INTO listing_archive_history (listing_id, archived_at, archive_reason, reactivated_at) "
        "VALUES ($1,$2,'confirmed_gone',$3)", lid, _days_ago(10), _days_ago(3),
    )
    filters = _base_filters(scenario.complex_id, period="30")
    exits = await md._confirmed_exits_in_period(_days_ago(30), filters)
    assert exits == 1


@pytest.mark.asyncio
async def test_exit_rate_division_by_zero_is_insufficient(scenario):
    from bot.analytics import market_dashboards as md
    # Никаких листингов вообще -> active_at_period_start = 0.
    filters = _base_filters(scenario.complex_id, period="30")
    kpis = await md.absorption_kpis(filters)
    assert kpis["exit_rate"]["status"] == md.INSUFFICIENT
    assert kpis["exit_rate"]["value"] is None


@pytest.mark.asyncio
async def test_months_of_stock_insufficient_on_zero_exits(scenario):
    from bot.analytics import market_dashboards as md
    await _insert(scenario, 1, first_seen=_days_ago(50))  # активен, ни одного выбывания
    filters = _base_filters(scenario.complex_id, period="30")
    kpis = await md.absorption_kpis(filters)
    assert kpis["months_of_stock"]["status"] == md.INSUFFICIENT
    assert kpis["months_of_stock"]["value"] is None


@pytest.mark.asyncio
async def test_months_of_stock_insufficient_on_short_period(scenario):
    from bot.analytics import market_dashboards as md
    await _insert(scenario, 1, first_seen=_days_ago(50), archived_at=_days_ago(2),
                   archive_reason="confirmed_gone", is_active=False)
    filters = _base_filters(scenario.complex_id, period="7")  # < MIN_STOCK_PERIOD_DAYS
    kpis = await md.absorption_kpis(filters)
    assert kpis["months_of_stock"]["status"] == md.INSUFFICIENT


# ══════════════════════════════════════════════════════════════════════
# 9-11. Реактивация, повторная публикация, отсутствие утверждения о продаже
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_supply_funnel_counts_reactivated(scenario):
    from bot.analytics import market_dashboards as md
    from bot.db.pg import execute
    lid = await _insert(scenario, 1, first_seen=_days_ago(50), is_active=True, archived_at=None)
    await execute(
        "INSERT INTO listing_archive_history (listing_id, archived_at, archive_reason, reactivated_at) "
        "VALUES ($1,$2,'confirmed_gone',$3)", lid, _days_ago(10), _days_ago(3),
    )
    filters = _base_filters(scenario.complex_id, period="30")
    funnel = await md.supply_funnel(filters)
    assert funnel["reactivated"] == 1


@pytest.mark.asyncio
async def test_relist_share_detects_known_property(scenario):
    from bot.analytics import market_dashboards as md
    # Квартира уже существовала (property.first_seen_at раньше) -> новый
    # листинг в периоде — повторная публикация.
    lid_old = await _insert(scenario, 1, first_seen=_days_ago(80))
    await _property(scenario, lid_old, first_seen_at=_days_ago(80))
    lid_new = await _insert(scenario, 2, first_seen=_days_ago(5))
    # Тот же property (повторная публикация той же физ. квартиры).
    from bot.db.pg import fetchval, execute
    pid = await fetchval("SELECT property_id FROM properties WHERE complex_id=$1", scenario.complex_id)
    await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                  "VALUES ($1,$2,'auto',1.0)", pid, lid_new)
    filters = _base_filters(scenario.complex_id, period="30")
    kpis = await md.absorption_kpis(filters)
    assert kpis["relist_share"]["value"] == 100.0


def test_absorption_page_never_claims_sale():
    """Задача явно требует: НИГДЕ на странице поглощения не должно быть
    утверждения факта продажи."""
    html = open(os.path.join(os.path.dirname(__file__), "..", "bot", "templates",
                              "market_absorption.html"), encoding="utf-8").read()
    forbidden = ["подтверждена продажа", "объявление продано", "факт продажи подтверждён"]
    for phrase in forbidden:
        assert phrase not in html.lower()
    # Единственные упоминания продажи — как ОДНОЙ ИЗ возможных причин
    # выбывания, не как подтверждённый факт (задача, явно).
    assert "без предположения о факте сделки" in html.lower()
    assert "причиной может быть продажа" in html.lower()


# ══════════════════════════════════════════════════════════════════════
# 12-15. Недостаточная история/выборка, пустая выборка, согласованность воронки
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ppm2_change_insufficient_without_prior_history(scenario):
    from bot.analytics import market_dashboards as md
    # Комплекс появился ТОЛЬКО что — нет никакой истории до начала периода.
    await _insert(scenario, 1, first_seen=_days_ago(2))
    filters = _base_filters(scenario.complex_id, period="90")
    kpis = await md.overview_kpis(filters)
    assert kpis["median_ppm2_change"]["status"] == md.INSUFFICIENT


@pytest.mark.asyncio
async def test_empty_selection_returns_zero_not_error(scenario):
    from bot.analytics import market_dashboards as md
    filters = _base_filters(scenario.complex_id, period="30")
    kpis = await md.overview_kpis(filters)
    assert kpis["active_listings"]["value"] == 0
    assert kpis["median_price"]["value"] is None
    dyn = await md.market_dynamics_series(filters)
    assert dyn["active_counts"] == [0] * len(dyn["labels"]) or dyn["labels"] == []


@pytest.mark.asyncio
async def test_segment_marked_insufficient_below_min_n(scenario):
    from bot.analytics import market_dashboards as md
    for i in range(5):  # < MIN_SEGMENT_N (20)
        await _insert(scenario, i, rooms=2)
    filters = _base_filters(scenario.complex_id, period="30")
    rows = await md.segment_exit_speed(filters, "district")
    matching = [r for r in rows if r["n"] == 5]
    assert matching and matching[0]["insufficient"] is True


@pytest.mark.asyncio
async def test_supply_funnel_arithmetic_consistency(scenario):
    """active_end воронки посчитан ФОРМУЛОЙ (active_start + new − exits +
    reactivated), а не независимым запросом — при отсутствии реактиваций
    ВНУТРИ периода формула обязана точно совпасть с фактическим замером."""
    from bot.analytics import market_dashboards as md
    await _insert(scenario, 1, first_seen=_days_ago(50))  # был активен на начало периода, активен и сейчас
    await _insert(scenario, 2, first_seen=_days_ago(5))   # новый в периоде
    await _insert(scenario, 3, first_seen=_days_ago(50), archived_at=_days_ago(10),
                   archive_reason="confirmed_gone", is_active=False)  # выбыл в периоде, не реактивирован
    filters = _base_filters(scenario.complex_id, period="30")
    funnel = await md.supply_funnel(filters)
    assert funnel["active_start"] + funnel["new_supply"] - funnel["confirmed_exits"] + funnel["reactivated"] \
        == funnel["active_end"]
    assert funnel["active_end"] == funnel["active_end_actual"]
    assert funnel["reconciled"] is True


# ══════════════════════════════════════════════════════════════════════
# 16. Отсутствие N+1 — запрос страницы не растёт по числу SQL-вызовов
#     с числом строк в выборке.
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_overview_kpis_query_count_bounded(scenario):
    """COUNT() вызовов fetch/fetchrow/fetchval не зависит от количества
    строк — вставляем 30 листингов и проверяем, что число SQL-запросов
    не изменилось бы при 300 (сам факт: один и тот же фиксированный набор
    запросов, GROUP BY/агрегаты, не цикл по объявлениям)."""
    import bot.db.pg as pg
    from bot.analytics import market_dashboards as md
    for i in range(30):
        await _insert(scenario, i, first_seen=_days_ago(5 + i % 20))
    filters = _base_filters(scenario.complex_id, period="30")

    calls = {"n": 0}
    orig_fetch, orig_fetchrow, orig_fetchval = pg.fetch, pg.fetchrow, pg.fetchval

    async def counted_fetch(*a, **kw):
        calls["n"] += 1
        return await orig_fetch(*a, **kw)

    async def counted_fetchrow(*a, **kw):
        calls["n"] += 1
        return await orig_fetchrow(*a, **kw)

    async def counted_fetchval(*a, **kw):
        calls["n"] += 1
        return await orig_fetchval(*a, **kw)

    md.fetch, md.fetchrow, md.fetchval = counted_fetch, counted_fetchrow, counted_fetchval
    try:
        await md.overview_kpis(filters)
        n_for_30 = calls["n"]
    finally:
        md.fetch, md.fetchrow, md.fetchval = orig_fetch, orig_fetchrow, orig_fetchval

    # Фиксированное, небольшое число запросов (не пропорционально 30 строкам).
    assert n_for_30 < 15
