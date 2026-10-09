"""tests/test_ai_tools_location_readonly.py — fix/ai-tools-strict-read-only
(review PR #51 п.1/п.2): get_location_analysis() должен работать на
листинге С координатами (has_coords=True) БЕЗ единого живого запроса к
Overpass и БЕЗ единой записи в osm_cache — предыдущий test suite
(tests/test_ai_tools_read_only.py, tests/test_ai_tools_passthrough.py)
проверял только has_coords=False, что и позволило дыре из review PR #51
остаться незамеченной.

Детерминированность (не полагаемся на реальное содержимое прод city_poi/
osm_cache — оно меняется во времени и от машины к машине): единственная
развилка "локально засинкано или нет" — bot.score_layers.osm.kinds_synced,
патчим её напрямую (local_poi_near вызывает её через global lookup своего
же модуля на каждый вызов — патч действует независимо от того, как
fetch_poi/fetch_schools_poi её импортировали). Координаты выбраны заведомо
абсурдными (открытый океан), чтобы bbox-запрос к РЕАЛЬНОЙ city_poi таблице
гарантированно не нашёл ничего — даже без патча это было бы так, патч
нужен только для ветки "ещё не синхронизировано вообще" (kinds_synced=False),
которую на реальных данных (city_poi уже наполнена) не воспроизвести
честно."""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from tests.admin_auth_helpers import admin_cookies
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

pytestmark = pytest.mark.asyncio

# Открытый океан у экватора — гарантированно вне зоны реального покрытия
# city_poi/osm_cache (весь остальной проект — Астана, ~51°с.ш./71°в.д.).
_OCEAN_LAT, _OCEAN_LON = 1.234, 2.345


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
        self.complex_ids: list[int] = []

    def lid(self, n) -> str:
        return f"6{self.numeric_suffix}{n}"

    @property
    def complex_name(self) -> str:
        return f"__TEST_AI_LOC_RO_{self.suffix}__"


@pytest_asyncio.fixture
async def scenario(db):
    sc = _Scenario()
    yield sc
    from bot.db.pg import execute
    for lid in sc.listing_ids:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
    for cid in sc.complex_ids:
        await execute("DELETE FROM complexes WHERE id = $1", cid)


async def _seed_complex_with_coords(sc: _Scenario) -> int:
    from bot.db.pg import execute, fetchval

    lid = sc.lid(1)
    sc.listing_ids.append(lid)
    await execute(
        """
        INSERT INTO apartment_listings (id, url, price, area, rooms, complex_name,
                                         market_type, is_active, first_seen, lat, lon)
        VALUES ($1,$2,25000000,45.0,2,$3,'secondary', TRUE, now(), $4, $5)
        ON CONFLICT (id) DO NOTHING
        """,
        lid, f"https://krisha.kz/a/show/{lid}", sc.complex_name, _OCEAN_LAT, _OCEAN_LON,
    )
    cid = await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", sc.complex_name)
    sc.complex_ids.append(cid)
    return cid


def _guard_no_network_no_write(monkeypatch):
    """Общий guard для обоих сценариев: если бы фикс не работал, эти два
    вызова были бы единственным способом реально сходить в Overpass и
    записать в osm_cache (bot/score_layers/osm.py::overpass_cached) —
    оба должны остаться недостижимыми при allow_live_fetch=False."""
    import bot.score_layers.osm as osm

    async def _boom_overpass(*a, **kw):
        raise AssertionError("live Overpass request must not happen from the AI read-only path")

    async def _boom_execute(*a, **kw):
        raise AssertionError("osm_cache write must not happen from the AI read-only path")

    monkeypatch.setattr(osm, "overpass_request", _boom_overpass)
    monkeypatch.setattr(osm, "execute", _boom_execute)
    # Дополнительный, более широкий guard — на случай, если что-то вообще
    # не по цепочке osm.py попробует писать в БД.
    import bot.db.pg as pg

    async def _boom_pg_execute(*a, **kw):
        raise AssertionError("no DB write of any kind must happen from the AI read-only path")

    monkeypatch.setattr(pg, "execute", _boom_pg_execute)


async def test_location_analysis_with_coords_local_available_no_live_fetch(scenario, monkeypatch):
    """kinds_synced=True — local_poi_near честно проверяет реальную
    (пустую для океана) city_poi и возвращает [] (не None) -> available=True,
    поведение "проверили, рядом ничего нет", НЕ "не знаем". Overpass/write
    не трогаются вообще (не только не нужны — недостижимы физически)."""
    import bot.score_layers.osm as osm

    async def _always_synced(kinds):
        return True

    monkeypatch.setattr(osm, "kinds_synced", _always_synced)
    cid = await _seed_complex_with_coords(scenario)
    _guard_no_network_no_write(monkeypatch)

    from bot.ai_tools.core import get_location_analysis
    result = await get_location_analysis(cid)

    assert result.data["found"] is True
    assert result.data["has_coords"] is True
    assert result.data["poi_source"] == {"mode": "local_cache_only", "available": True}
    assert result.data["poi"] == {"bus_stop": [], "shop": [], "health": [], "food": [],
                                   "service": [], "park": [], "school": []}
    assert not any("POI data unavailable" in w for w in result.warnings)


async def test_location_analysis_with_coords_no_local_data_returns_honest_warning(scenario, monkeypatch):
    """kinds_synced=False — ни local sync, ни (гарантированно отсутствующий
    для этих координат) osm_cache не покрывают точку -> available=False,
    честный insufficient_data warning с точной формулировкой из задачи,
    НЕ молчаливый fallback на 0/среднее. Endpoint при этом остаётся
    рабочим (found=True, has_coords=True) — недостающий сигнал не роняет
    весь ответ."""
    import bot.score_layers.osm as osm

    async def _never_synced(kinds):
        return False

    monkeypatch.setattr(osm, "kinds_synced", _never_synced)
    cid = await _seed_complex_with_coords(scenario)
    _guard_no_network_no_write(monkeypatch)

    from bot.ai_tools.core import get_location_analysis
    result = await get_location_analysis(cid)

    assert result.data["found"] is True
    assert result.data["has_coords"] is True
    assert result.data["poi_source"] == {"mode": "local_cache_only", "available": False}
    assert result.data["poi"] == {"bus_stop": [], "shop": [], "health": [], "food": [],
                                   "service": [], "park": [], "school": []}
    assert any(
        w == ("POI data unavailable in local/cache sources; live external fetch disabled "
              "for AI read-only API")
        for w in result.warnings
    )


async def test_location_analysis_endpoint_stays_200_with_coords(scenario, monkeypatch):
    """Тот же 'available=False' сценарий, но через настоящий HTTP endpoint
    (router.py) — доказывает, что отсутствие POI-данных не превращается в
    500/ошибку, а честно приходит 200 + warning в envelope."""
    import bot.score_layers.osm as osm

    async def _never_synced(kinds):
        return False

    monkeypatch.setattr(osm, "kinds_synced", _never_synced)
    cid = await _seed_complex_with_coords(scenario)
    _guard_no_network_no_write(monkeypatch)

    # httpx.AsyncClient(transport=ASGITransport(...)) — не starlette
    # TestClient: тот гоняет запрос в СВОЁМ потоке/event loop через anyio
    # portal, что ломает уже открытый в этом же процессе asyncpg pool
    # (pytest-asyncio-фикстура db инициализировала его на СВОЁМ loop) —
    # asyncpg.InterfaceError "another operation is in progress". ASGI-
    # транспорт выполняет запрос в ТОМ ЖЕ event loop, что и сам тест —
    # тот же pool, никакого конфликта, всё ещё реальный ASGI-проход через
    # router.py, не прямой вызов core.py в обход HTTP-слоя.
    import httpx
    from bot.admin_web import create_admin_app
    from bot.db.compat import BotDB

    db = BotDB.__new__(BotDB)
    app = create_admin_app(db, "x", "1.0")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                  cookies=await admin_cookies()) as client:
        resp = await client.get(f"/admin/api/ai/v1/complex/{cid}/location-analysis")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["found"] is True
    assert body["data"]["poi_source"]["available"] is False
    assert any("POI data unavailable" in w for w in body["warnings"])
