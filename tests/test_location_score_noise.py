"""Регрессия для задачи 2026-08-15 ("noise.py в location score") —
bot/score_layers/noise.py уже был подключён в compute_complex_location_
score() (_OSM_LAYERS/_GROUPS['environment']/_FACTOR_RANGES/_SOURCE_
QUALITY, см. git log "Location Reliability Phase v2") к моменту этой
задачи — тест закрывает реальный пробел: регрессии на это не было
вообще. Проверяет то, что просили: фактор появляется в factors dict
с полной metadata (_annotate_factor_metadata), и confidence честно
реагирует на наличие/отсутствие сигнала.

noise/transit/amenities/parks/schools — фейки через monkeypatch (тот же
приём, что tests/test_location_score_no_double_school_count.py) — тест
не бьёт в реальный Overpass. REF_LAT/REF_LON — Алматы, заведомо далеко
от Астаны (та же опорная точка, что tests/test_location_score_air_
quality.py) — держит demolition/transport_hexes/astana_schools/
kindergartens/air_stations в одном и том же "далеко, не измерено"
состоянии в ОБОИХ сравниваемых прогонах, единственная переменная —
доступность noise.

НАХОДКА (не в скоупе этой задачи, см. финальный отчёт): noise.py (и
transit/amenities/parks — тот же паттерн) при сбое Overpass возвращает
reason "OSM недоступен"/"нет координат", а _is_available() в location_
score.py ищет только "нет данных"/"ошибка" — эти два failure-текста НЕ
детектируются как "недоступно". Тест ниже поэтому не проверяет noise.
compute()'s СОБСТВЕННЫЙ failure-текст напрямую (это подтвердило бы
существующий баг, а не поведение задачи) — вместо этого использует
исключение в noise.compute() (путь _run_layer() -> "ошибка слоя: ...",
который _is_available() распознаёт корректно уже сейчас) как честный
способ смоделировать "сигнал недоступен" для проверки confidence."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REF_LAT = 43.1500
REF_LON = 76.8000


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _mock_sibling_osm_layers(monkeypatch):
    """transit/amenities/parks/schools -> нейтральный нулевой фейк, poi ->
    пустой список (прогрев кэша не бьёт в сеть). noise НЕ трогает — его
    подменяет каждый тест отдельно, это и есть предмет проверки."""
    import bot.score_layers.transit as transit_module
    import bot.score_layers.amenities as amenities_module
    import bot.score_layers.parks as parks_module
    import bot.score_layers.schools as schools_module
    import bot.score_layers.poi as poi_module

    async def _fake_simple(listing):
        return 0, "фейк"

    async def _fake_schools_compute(listing, university_only=False):
        return 0, "фейк-школы"

    async def _fake_fetch_poi(lat, lon):
        return []

    monkeypatch.setattr(transit_module, "compute", _fake_simple)
    monkeypatch.setattr(amenities_module, "compute", _fake_simple)
    monkeypatch.setattr(parks_module, "compute", _fake_simple)
    monkeypatch.setattr(schools_module, "compute", _fake_schools_compute)
    monkeypatch.setattr(poi_module, "fetch_poi", _fake_fetch_poi)


@pytest.mark.asyncio
async def test_noise_factor_present_with_full_metadata(db, monkeypatch):
    """Пункт 1+2 задачи: noise реально зовётся параллельно с остальными
    OSM-слоями (через _OSM_LAYERS/asyncio.gather) и получает ту же
    metadata (available/source_quality/freshness/precision), что и
    остальные факторы — не только присутствует в dict, но и размечен."""
    import bot.score_layers.noise as noise_module
    from bot.core.location_score import compute_complex_location_score

    _mock_sibling_osm_layers(monkeypatch)

    async def _fake_noise(listing):
        return -4, "магистраль в <250м"

    monkeypatch.setattr(noise_module, "compute", _fake_noise)

    result = await compute_complex_location_score(REF_LAT, REF_LON)
    assert result is not None
    factors = result["factors"]
    assert "noise" in factors
    f = factors["noise"]
    assert f["adj"] == -4
    assert f["reason"] == "магистраль в <250м"
    assert f["label"] == "🔇 Шум (магистрали)"
    assert f["available"] is True
    assert f["source_quality"] == pytest.approx(0.6)
    assert f["freshness"] == "live"
    assert f["precision"] == "presence"
    # noise входит в группу environment (_GROUPS) — участвует в её score.
    assert "environment" in result["group_scores"]
    assert "environment" in result["group_confidence"]


@pytest.mark.asyncio
async def test_noise_measured_quiet_is_available_not_unknown(db, monkeypatch):
    """'Тихо, крупных дорог рядом нет' — это ИЗМЕРЕННЫЙ ноль (реально
    искали и не нашли), не 'не смогли посчитать' (Unknown ≠ average,
    verdict_strategy.md §3.1) — available должно остаться True."""
    import bot.score_layers.noise as noise_module
    from bot.core.location_score import compute_complex_location_score

    _mock_sibling_osm_layers(monkeypatch)

    async def _fake_noise_quiet(listing):
        return 0, "крупных дорог в 250м нет — тихо"

    monkeypatch.setattr(noise_module, "compute", _fake_noise_quiet)

    result = await compute_complex_location_score(REF_LAT, REF_LON)
    f = result["factors"]["noise"]
    assert f["adj"] == 0
    assert f["available"] is True


@pytest.mark.asyncio
async def test_noise_layer_failure_marked_unavailable(db, monkeypatch):
    """Сбой слоя (Overpass упал/выбросил исключение) -> _run_layer()
    ловит и пишет 'ошибка слоя: ...' -> _is_available() корректно
    считает это НЕ измеренным (см. докстринг модуля про "ошибка"/"нет
    данных" — единственные два паттерна, которые она распознаёт)."""
    import bot.score_layers.noise as noise_module
    from bot.core.location_score import compute_complex_location_score

    _mock_sibling_osm_layers(monkeypatch)

    async def _fake_noise_broken(listing):
        raise RuntimeError("Overpass timeout")

    monkeypatch.setattr(noise_module, "compute", _fake_noise_broken)

    result = await compute_complex_location_score(REF_LAT, REF_LON)
    f = result["factors"]["noise"]
    assert f["adj"] == 0
    assert "ошибка слоя" in f["reason"]
    assert f["available"] is False


@pytest.mark.asyncio
async def test_environment_confidence_drops_when_noise_unavailable(db, monkeypatch):
    """Пункт 3 задачи, ядро проверки: group_confidence['environment']
    (noise живёт именно в этой группе, _GROUPS) должна быть ВЫШЕ, когда
    noise реально измерен, чем когда он недоступен — при прочих равных
    (parks/air_quality/схема координат не меняются между прогонами,
    единственная переменная — noise)."""
    import bot.score_layers.noise as noise_module
    from bot.core.location_score import compute_complex_location_score

    _mock_sibling_osm_layers(monkeypatch)

    async def _fake_noise_ok(listing):
        return -1, "оживлённая улица в <250м"

    monkeypatch.setattr(noise_module, "compute", _fake_noise_ok)
    result_available = await compute_complex_location_score(REF_LAT, REF_LON)

    async def _fake_noise_broken(listing):
        raise RuntimeError("Overpass timeout")

    monkeypatch.setattr(noise_module, "compute", _fake_noise_broken)
    result_unavailable = await compute_complex_location_score(REF_LAT, REF_LON)

    conf_available = result_available["group_confidence"]["environment"]
    conf_unavailable = result_unavailable["group_confidence"]["environment"]
    assert conf_available > conf_unavailable
