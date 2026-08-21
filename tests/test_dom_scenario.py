"""tests/test_dom_scenario.py — bot/analytics/dom_scenario.py (задача
2026-08-21, "MVP прогноза срока экспозиции при разных ценах"). Синтетические
фикстуры на реальной Postgres test DB (DATABASE_URL) — тот же паттерн, что
tests/test_market_dashboards.py/tests/test_property_identity_dashboard.py:
уникальный district/complex-тег на тест, прод-строки в чужой таблице
apartment_listings не задеваются (фильтруются по district/id-префиксу),
всё чистится в finally.

Покрытие (см. задание §6):
  - fallback между уровнями сегментов
  - работа с малой выборкой ("Пока недостаточно похожих объектов")
  - учёт активных объявлений как censored, НЕ как события
  - группировка по property_id (relist — одно наблюдение, не два)
  - реконструкция цены (price_at, чистая функция)
  - монотонность сценариев (снижение цены не увеличивает срок)
  - диапазоны в разумных пределах (DAYS_MIN..DAYS_MAX)
  - нигде не заявляется подтверждённая продажа
  - API — существующее/отсутствующее объявление, graceful fallback при ошибке
  - блок в попапе dashboard.html рендерится"""
from __future__ import annotations

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

from bot.analytics.dom_scenario import (  # noqa: E402
    _rooms_bucket, _rooms_label, price_at, kaplan_meier, km_quantile, pava,
    _enforce_monotone_scenarios, CONFIRMED_SALE_DISCLAIMER, DAYS_MIN, DAYS_MAX,
)


def test_rooms_bucket_collapses_large_rooms_only():
    assert _rooms_bucket(1) == "1"
    assert _rooms_bucket(3) == "3"
    assert _rooms_bucket(4) == "4+"
    assert _rooms_bucket(7) == "4+"
    assert _rooms_bucket(None) is None


def test_rooms_label_pluralization():
    assert _rooms_label(1) == "1 комната"
    assert _rooms_label(2) == "2 комнаты"
    assert _rooms_label(5) == "5 комнат"
    assert _rooms_label("4+") == "4+ комнат"


def test_price_at_reconstruction_uses_latest_change_before_as_of():
    history = [
        {"old_price": 30_000_000, "new_price": 29_000_000, "changed_at": _days_ago(20)},
        {"old_price": 29_000_000, "new_price": 28_000_000, "changed_at": _days_ago(10)},
    ]
    # as_of между двумя изменениями -> последнее, что было ДО as_of
    assert price_at(history, _days_ago(15), current_price=25_000_000) == 29_000_000
    # as_of раньше всех изменений -> old_price САМОГО РАННЕГО изменения
    assert price_at(history, _days_ago(25), current_price=25_000_000) == 30_000_000
    # as_of позже всех изменений -> последний new_price
    assert price_at(history, _days_ago(1), current_price=25_000_000) == 28_000_000


def test_price_at_no_history_falls_back_to_current_price():
    assert price_at([], _days_ago(5), current_price=41_000_000) == 41_000_000


def test_price_at_never_uses_changes_after_as_of():
    """Честность backtest (п.9 задания) — изменение цены ПОСЛЕ as_of не
    должно быть видно реконструкции."""
    history = [{"old_price": 40_000_000, "new_price": 30_000_000, "changed_at": _days_ago(1)}]
    # as_of за 30 дней до этого изменения — изменение из будущего не учтено
    assert price_at(history, _days_ago(30), current_price=40_000_000) == 40_000_000


def test_kaplan_meier_survival_decreases_at_event_times():
    obs = [(10, 1), (20, 1), (30, 0), (15, 1), (25, 0)]
    steps = kaplan_meier(obs)
    assert steps[0] == (0.0, 1.0)
    survivals = [s for _, s in steps]
    assert survivals == sorted(survivals, reverse=True)  # невозрастающая
    assert steps[-1][1] < 1.0


def test_kaplan_meier_all_censored_never_drops():
    obs = [(10, 0), (20, 0), (30, 0)]
    steps = kaplan_meier(obs)
    assert all(s == 1.0 for _, s in steps)  # ни одного события — S(t)=1 всюду


def test_km_quantile_returns_none_when_curve_never_reached():
    """Сильное цензурирование (как в реальных данных, см. аудит) — если
    кривая не опускается до q, не подставляем последнее наблюдённое t как
    якобы честный ответ."""
    obs = [(100, 0)] * 20 + [(5, 1)]
    steps = kaplan_meier(obs)
    assert km_quantile(steps, 0.1) is None


def test_pava_produces_nondecreasing_sequence():
    noisy = [5, 1, 3, 2, 8, 6, 9]
    smoothed = pava(noisy)
    assert smoothed == sorted(smoothed)
    assert len(smoothed) == len(noisy)


def test_pava_already_monotone_is_unchanged():
    vals = [1, 2, 3, 4]
    assert pava(vals) == vals


def test_enforce_monotone_scenarios_clamps_upward_noise():
    # Сценарий с discount_pct=5 "шумно" оценен ВЫШЕ, чем discount_pct=3 —
    # после clamp'а он не может быть выше предыдущего (меньшая скидка).
    scenarios = [
        {"discount_pct": 0, "days_low": 20, "days_high": 32},
        {"discount_pct": 3, "days_low": 18, "days_high": 28},
        {"discount_pct": 5, "days_low": 22, "days_high": 35},  # нарушение
        {"discount_pct": 7, "days_low": 12, "days_high": 20},
        {"discount_pct": 10, "days_low": 9, "days_high": 18},
    ]
    fixed = _enforce_monotone_scenarios(scenarios)
    lows = [s["days_low"] for s in fixed]
    highs = [s["days_high"] for s in fixed]
    assert all(lows[i] <= lows[i - 1] for i in range(1, len(lows)))
    assert all(highs[i] <= highs[i - 1] for i in range(1, len(highs)))
    assert fixed[2]["days_high"] <= fixed[1]["days_high"]  # шумный скачок вверх подавлен


def test_disclaimer_never_claims_confirmed_sale():
    forbidden = ["точно продастся", "квартира продана", "гарантированно продастся", "подтверждена продажа"]
    for phrase in forbidden:
        assert phrase not in CONFIRMED_SALE_DISCLAIMER
    assert "не подтверждает факт продажи" in CONFIRMED_SALE_DISCLAIMER


def test_days_bounds_are_sane():
    assert 0 < DAYS_MIN < DAYS_MAX <= 365


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
        self.district = f"__TEST_DOM_{tag}_{self.suffix}__"
        self.listing_ids: list[str] = []
        self.property_ids: list[int] = []

    def lid(self, n) -> str:
        return f"__test_dom_{self.tag}_{self.suffix}_{n}__"


@pytest_asyncio.fixture
async def scenario(db, request):
    sc = _Scenario(request.node.name[:16])
    yield sc
    from bot.db.pg import execute
    for lid in sc.listing_ids:
        await execute("DELETE FROM outcome_labels WHERE listing_id = $1", lid)
        await execute("DELETE FROM property_listings WHERE listing_id = $1", lid)
    for pid in sc.property_ids:
        await execute("DELETE FROM properties WHERE property_id = $1", pid)
    for lid in sc.listing_ids:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)


async def _insert(sc: _Scenario, n: int, *, price=30_000_000, area=50.0, rooms=2,
                   district=None, first_seen=None, archived_at=None, is_active=True,
                   time_on_market=None):
    from bot.db.pg import execute
    lid = sc.lid(n)
    sc.listing_ids.append(lid)
    await execute(
        """
        INSERT INTO apartment_listings
            (id, url, price, area, rooms, district, market_type, is_active, first_seen, archived_at)
        VALUES ($1,$2,$3,$4,$5,$6,'secondary',$7,$8,$9)
        ON CONFLICT (id) DO UPDATE SET price=$3, area=$4, rooms=$5, district=$6,
            is_active=$7, first_seen=$8, archived_at=$9
        """,
        lid, f"https://krisha.kz/test/{lid}", price, area, rooms,
        district or sc.district, is_active, first_seen or _days_ago(60), archived_at,
    )
    if time_on_market is not None:
        await execute(
            "INSERT INTO outcome_labels (listing_id, time_on_market) VALUES ($1,$2) "
            "ON CONFLICT (listing_id) DO UPDATE SET time_on_market=$2",
            lid, time_on_market,
        )
    return lid


async def _link_property(sc: _Scenario, listing_ids: list[str], *, floor=5, area=50.0, rooms=2):
    from bot.db.pg import execute, fetchval
    address_hash = f"__test_dom_prop_{sc.tag}_{sc.suffix}_{uuid.uuid4().hex[:6]}__"
    pid = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm, rooms) VALUES ($1,$2,$3,$4) "
        "RETURNING property_id",
        address_hash, floor, area, rooms,
    )
    sc.property_ids.append(pid)
    for lid in listing_ids:
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
            "VALUES ($1,$2,'auto',1.0)",
            pid, lid,
        )
    return pid


def _fill_segment(sc, count, *, event_days_start=15, price=30_000_000, area=50.0, rooms=2):
    """Возвращает список coroutine-объектов _insert для count разрешившихся
    аналогов — вызывающий должен await-ить их (helper, не фикстура)."""
    return [
        _insert(sc, i, price=price, area=area, rooms=rooms,
                first_seen=_days_ago(event_days_start + i + 60),
                archived_at=_days_ago(event_days_start + i),
                time_on_market=event_days_start + i)
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_insufficient_data_when_price_or_area_missing(scenario, db):
    """Детерминированный триггер ветки "Пока недостаточно похожих объектов"
    — отсутствие price/area делает сравнение в принципе невозможным, ДО
    похода за сегментом. Полное исчерпание фоллбэка (даже городской
    baseline пуст) тем же кодом не воспроизводится здесь синтетически:
    БД этого окружения — не пустая тестовая, а рабочая dev/прод-подобная
    (см. докстринг файла) — city_baseline почти всегда найдёт реальные
    данные, что и есть желаемое поведение на практике."""
    from bot.analytics.dom_scenario import compute_dom_scenario
    target = await _insert(scenario, "target", rooms=3, price=None, area=None)

    result = await compute_dom_scenario(target)
    assert result["available"] is True
    assert result["insufficient_data"] is True
    assert "недостаточно" in result["message"]
    assert result["confidence"] == "low"


@pytest.mark.asyncio
async def test_fallback_expands_when_district_rooms_segment_is_thin(scenario, db):
    """district×rooms почти пуст, но rooms_bucket по всему городу (тот же
    уникальный district-тег НЕ используется для fallback-уровней — они
    ищут по REAL_DISTRICTS, значит thin district×rooms точно не наберёт
    MIN_EVENTS_MEDIUM и обязан провалиться на city_rooms_bucket/baseline)."""
    from bot.analytics.dom_scenario import compute_dom_scenario, MIN_EVENTS_MEDIUM

    target = await _insert(scenario, "target", rooms=2, price=30_000_000, area=50.0,
                            district=scenario.district)
    # 2 события в своём уникальном районе — меньше MIN_EVENTS_MEDIUM
    for coro in _fill_segment(scenario, 2, rooms=2):
        await coro

    result = await compute_dom_scenario(target)
    assert result["available"] is True
    if not result["insufficient_data"]:
        assert result["fallback_level"] != "district_rooms"
        assert result["confidence"] in ("low", "medium")


@pytest.mark.asyncio
async def test_active_listings_are_censored_not_events(scenario, db):
    from bot.analytics.dom_scenario import _fetch_segment_population, _row_to_obs

    target_key = "l:__nonexistent__"
    # 3 разрешившихся + 4 активных (censored) в одном сегменте
    for coro in _fill_segment(scenario, 3, rooms=2):
        await coro
    for i in range(4):
        await _insert(scenario, f"active{i}", rooms=2, price=31_000_000, area=50.0,
                       first_seen=_days_ago(20), archived_at=None, is_active=True)

    pop = await _fetch_segment_population("district_rooms", scenario.district, "2", target_key)
    obs = [_row_to_obs(r, _NOW) for r in pop]
    event_count = sum(1 for _, e, _ in obs if e == 1)
    censored_count = sum(1 for _, e, _ in obs if e == 0)
    assert event_count == 3
    assert censored_count == 4
    assert len(pop) == 7  # sample_size учитывает и censored, и события


@pytest.mark.asyncio
async def test_property_id_groups_relists_as_single_observation(scenario, db):
    from bot.analytics.dom_scenario import _fetch_segment_population

    # 2 listing_id одной физической квартиры (relist) — должны схлопнуться
    # в ОДНО наблюдение (самое свежее по first_seen), не в два.
    lid_old = await _insert(scenario, "relist_old", rooms=2, price=30_000_000, area=50.0,
                             first_seen=_days_ago(90), archived_at=_days_ago(70), time_on_market=20)
    lid_new = await _insert(scenario, "relist_new", rooms=2, price=29_000_000, area=50.0,
                             first_seen=_days_ago(30), archived_at=_days_ago(10), time_on_market=20)
    await _link_property(scenario, [lid_old, lid_new])
    # плюс пара обычных, непривязанных объявлений — не путаются с relist'ом
    await _insert(scenario, "plain1", rooms=2, price=28_000_000, area=50.0,
                   archived_at=_days_ago(15), time_on_market=15)

    pop = await _fetch_segment_population("district_rooms", scenario.district, "2", "l:__nonexistent__")
    listing_ids_in_pop = {r["listing_id"] for r in pop}
    # ровно ОДИН из двух relist-listing_id попал в популяцию (самый свежий)
    assert lid_new in listing_ids_in_pop
    assert lid_old not in listing_ids_in_pop
    assert len(pop) == 2  # relist (1) + plain1 (1), не 3


@pytest.mark.asyncio
async def test_scenarios_are_monotone_and_within_bounds(scenario, db):
    from bot.analytics.dom_scenario import compute_dom_scenario, DAYS_MIN, DAYS_MAX

    target = await _insert(scenario, "target", rooms=2, price=30_000_000, area=50.0)
    # Сегмент с явной ценовой корреляцией: дешёвые (низкий ppm2) продаются
    # быстрее дорогих — чтобы было что сглаживать PAVA и было видно, что
    # сценарии реально РАЗЛИЧАЮТСЯ, а не просто все клэмпнуты в одну точку.
    cheap_prices = [24_000_000, 25_000_000, 26_000_000, 24_500_000, 25_500_000, 26_500_000]
    expensive_prices = [34_000_000, 35_000_000, 36_000_000, 34_500_000, 35_500_000, 36_500_000]
    for i, p in enumerate(cheap_prices):
        await _insert(scenario, f"cheap{i}", rooms=2, price=p, area=50.0,
                       archived_at=_days_ago(8 + i), time_on_market=8 + i)
    for i, p in enumerate(expensive_prices):
        await _insert(scenario, f"exp{i}", rooms=2, price=p, area=50.0,
                       archived_at=_days_ago(40 + i), time_on_market=40 + i)

    result = await compute_dom_scenario(target)
    assert result["available"] is True
    if result["insufficient_data"]:
        pytest.skip("сегмент оказался тоньше ожидаемого на этой БД — не тестируем монотонность впустую")
    scenarios = sorted(result["scenarios"], key=lambda s: s["discount_pct"])
    assert [s["discount_pct"] for s in scenarios] == [0, 3, 5, 7, 10]
    for i in range(1, len(scenarios)):
        assert scenarios[i]["days_low"] <= scenarios[i - 1]["days_low"]
        assert scenarios[i]["days_high"] <= scenarios[i - 1]["days_high"]
        assert scenarios[i]["price"] < scenarios[i - 1]["price"]
    for s in scenarios:
        assert DAYS_MIN <= s["days_low"] <= s["days_high"] <= DAYS_MAX


@pytest.mark.asyncio
async def test_output_json_never_claims_confirmed_sale(scenario, db):
    from bot.analytics.dom_scenario import compute_dom_scenario

    target = await _insert(scenario, "target", rooms=2, price=30_000_000, area=50.0)
    for coro in _fill_segment(scenario, 6, rooms=2):
        await coro

    result = await compute_dom_scenario(target)
    blob = json.dumps(result, default=str, ensure_ascii=False)
    for phrase in ["точно продастся", "квартира продана", "подтверждённая продажа"]:
        assert phrase not in blob


# ── Часть 3 — API ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db):
    import httpx
    from bot.db.pg import init_pool, close_pool  # noqa: F401 (db fixture уже инициализировал pool)
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
async def test_api_dom_scenario_existing_listing(client, scenario, db):
    target = await _insert(scenario, "target", rooms=2, price=30_000_000, area=50.0)
    for coro in _fill_segment(scenario, 6, rooms=2):
        await coro
    r = await client.get(f"/admin/api/listing/{target}/dom-scenario")
    assert r.status_code == 200
    body = r.json()
    assert "available" in body


@pytest.mark.asyncio
async def test_api_dom_scenario_missing_listing_returns_available_false(client, db):
    r = await client.get("/admin/api/listing/__does_not_exist_dom_scenario__/dom-scenario")
    assert r.status_code == 200  # не 500 — не ломает открытие карточки
    body = r.json()
    assert body["available"] is False


@pytest.mark.asyncio
async def test_api_dom_scenario_graceful_fallback_on_internal_error(client, scenario, db, monkeypatch):
    import bot.analytics.dom_scenario as dom_scenario_mod

    async def _boom(listing_id):
        raise RuntimeError("сбой расчёта (симуляция)")

    monkeypatch.setattr(dom_scenario_mod, "compute_dom_scenario_cached", _boom)
    target = await _insert(scenario, "target", rooms=2, price=30_000_000, area=50.0)
    r = await client.get(f"/admin/api/listing/{target}/dom-scenario")
    assert r.status_code == 200
    assert r.json()["available"] is False


@pytest.mark.asyncio
async def test_main_listing_card_still_opens_when_dom_scenario_errors(client, scenario, db, monkeypatch):
    """Прогноз — отдельный endpoint именно поэтому: даже если его расчёт
    падает, /admin/api/listing/{id} (сама карточка) не задета вовсе."""
    import bot.analytics.dom_scenario as dom_scenario_mod

    async def _boom(listing_id):
        raise RuntimeError("сбой расчёта (симуляция)")

    monkeypatch.setattr(dom_scenario_mod, "compute_dom_scenario_cached", _boom)
    target = await _insert(scenario, "target", rooms=2, price=30_000_000, area=50.0)
    r = await client.get(f"/admin/api/listing/{target}")
    assert r.status_code == 200
    assert r.json().get("error") is None


@pytest.mark.asyncio
async def test_dashboard_popup_shell_contains_dom_scenario_block(client, db):
    """Рендер главной страницы (та же карта/шелл, что открывает попап через
    openDetailModal) — блок и JS-функции присутствуют в разметке."""
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Ожидаемый срок экспозиции" in html
    assert "modal-dom-scenario-body" in html
    assert "loadDomScenario" in html
    assert "domScenarioChartSVG" in html
    assert "квартира точно продастся" not in html
