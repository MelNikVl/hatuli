"""Регрессия сигнала токена очереди/фазы в bot/core/entity_resolution.score_match().

Калибровка 2026-08-12 (см. docs/entity_resolution_plan.md, разделы
"сигнал токена очереди/фазы" и "sibling-sweep... implicit phase 1") —
живые данные, не выдуманные числа: реальные homeportal_objects/complexes
строки из прод-БД (project krisha_bot), зафиксированные здесь как
регрессия, чтобы будущая правка весов сигналов не тихо сломала то, ради
чего сигнал вообще появился.

Требует живой Postgres (DATABASE_URL/.env, как у остальных скриптов
проекта) — score_match() считает pg_trgm similarity() в БД, не в
Python. Запуск: venv/bin/pytest tests/test_entity_resolution_phase.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

from hype_tracker.homeportal_scan import norm_name
from bot.core.entity_resolution import score_match, AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    """Функциональный scope (не module/session) — pytest-asyncio по
    умолчанию даёт каждому тесту свой event loop; общий пул на модуль
    ловит 'attached to a different loop' на втором тесте. init/close
    пула недорогие (min_size=2), заметной просадки на десяток тестов
    нет."""
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _verdict(conf: float) -> str:
    if conf >= AUTO_MATCH_THRESHOLD:
        return "auto"
    if conf >= REVIEW_QUEUE_THRESHOLD:
        return "review"
    return "skip"


# ── Дармен: homeportal-объект 1266 'ЖК "Darmen 2"' — пара, с которой всё
#    началось (калибровка 2026-08-12, живой прогон homeportal). Гео/БИН
#    застройщика — реальные значения из homeportal_objects/complexes/
#    complex_tech_specs (см. проверку в докстрингах ниже), не выдумка.
# ────────────────────────────────────────────────────────────────────
DARMEN2_RAW = 'ЖК "Darmen 2"'          # homeportal_objects.name, object_id=1266
DARMEN2_LAT, DARMEN2_LON = 51.165970, 71.443448
DARMEN2_ADDRESS = "г. Астана, р. Байқоңыр, прсч. ул.А. Иманова, Тараз и Асан Қайғы, уч. 2"
DARMEN_DEV_BIN_MATCH = True  # complex_tech_specs.developer_bin у обоих complex_id ниже == 130940009587


@pytest.mark.asyncio
async def test_darmen_2_vs_darmen_1_capped_to_review(db):
    """complexes.id=2972 'Darmen 1' (существующая связь-кандидат по
    fuzzy-имени) — оба номера явные (2 и 1), разные -> потолок 0.79,
    review, не auto. Это и есть исходная находка калибровки, из-за
    которой появился весь сигнал фазы."""
    existing_name, existing_lat, existing_lon, existing_address = "Darmen 1", 51.166836, 71.44342, None
    conf, method = await score_match(
        norm_name(DARMEN2_RAW), norm_name(existing_name),
        existing_lat=existing_lat, existing_lon=existing_lon,
        candidate_lat=DARMEN2_LAT, candidate_lon=DARMEN2_LON,
        developer_match=DARMEN_DEV_BIN_MATCH,
        existing_address=existing_address, candidate_address=DARMEN2_ADDRESS,
        name_a_full=DARMEN2_RAW, name_b_full=existing_name,
    )
    assert _verdict(conf) == "review", f"conf={conf} method={method}"
    assert "phase_mismatch(2!=1)" in method, method
    assert conf <= 0.79


@pytest.mark.asyncio
async def test_darmen_2_vs_darmen_unnumbered_capped_to_review(db):
    """complexes.id=2053 'DARMEN' (реальный, БЕЗ номера — это и есть
    неявная "первая фаза") против того же homeportal-объекта 'Darmen 2'.
    Раньше (правило "токен только с одной стороны -> нейтрально") это
    ушло бы в auto без капа — 6/7 sibling-пар из sweep именно так и
    проходили. После implicit-phase-1: голая сторона 'darmen' совпадает
    с базой номерованной ('darmen 2' минус '2' = 'darmen') -> неявная
    фаза "1" -> сравнение 1 vs 2 -> потолок 0.79, review."""
    existing_name, existing_lat, existing_lon = "DARMEN", 51.166542, 71.4433
    existing_address = "РК, г.Астана, район Байқоңыр, пересечение улиц Кенесары, Асан Қайғы и Тараз"
    conf, method = await score_match(
        norm_name(DARMEN2_RAW), norm_name(existing_name),
        existing_lat=existing_lat, existing_lon=existing_lon,
        candidate_lat=DARMEN2_LAT, candidate_lon=DARMEN2_LON,
        developer_match=DARMEN_DEV_BIN_MATCH,
        existing_address=existing_address, candidate_address=DARMEN2_ADDRESS,
        name_a_full=DARMEN2_RAW, name_b_full=existing_name,
    )
    assert _verdict(conf) == "review", f"conf={conf} method={method}"
    assert "1~implicit!=2" in method, method
    assert conf <= 0.79


@pytest.mark.asyncio
async def test_darmen_2_vs_darmen_parenthetical_queue_matches_auto(db):
    """Тот же 'Darmen 2', но кандидат называется 'Darmen (2 очередь)' —
    конструированный вариант написания (в проде такого имени нет,
    проверяем механизм: номер внутри скобок не должен теряться из-за
    norm_name(), см. докстринг _phase_token). Номера совпадают (2 и 2)
    -> бонус, а не потеря сигнала из-за скобок -> auto."""
    existing_name = "Darmen (2 очередь)"
    existing_lat, existing_lon = 51.166836, 71.44342  # гео Darmen 1 — та же площадка
    conf, method = await score_match(
        norm_name(DARMEN2_RAW), norm_name(existing_name),
        existing_lat=existing_lat, existing_lon=existing_lon,
        candidate_lat=DARMEN2_LAT, candidate_lon=DARMEN2_LON,
        developer_match=DARMEN_DEV_BIN_MATCH,
        existing_address=None, candidate_address=DARMEN2_ADDRESS,
        name_a_full=DARMEN2_RAW, name_b_full=existing_name,
    )
    assert _verdict(conf) == "auto", f"conf={conf} method={method}"
    assert "phase(2)" in method, method


# ── 7 sibling-пар из живого sweep (один застройщик, <=150 м, схожие
#    имена) — все реальные complexes-строки. 6/7 раньше проходили в
#    auto несмотря на первый фикс токена фазы (только явный vs явный
#    номер), потому что у "базовой" стороны номера нет вовсе. Asylym —
#    единственная, где номер был явным с ОБЕИХ сторон, её ловило уже
#    первое правило. ────────────────────────────────────────────────

SIBLING_PAIRS = [
    # (base_name, base_lat, base_lon, base_dev_id, base_address,
    #  numbered_name, numbered_lat, numbered_lon, numbered_dev_id, numbered_address,
    #  ожидаемый фрагмент match_method — "implicit" для 6/7, явный для Asylym)
    ("GAUHARTAS", 51.101128, 71.38272, 72, None,
     "Gauhartas 2", 51.10109, 71.383224, 72, "г. Астана, пересечение пр. Улы дала и ул. Казыбек Би",
     "1~implicit!=2"),
    ("Nur Aspan", 51.129005, 71.4948, 65, None,
     "Nur Aspan 2", 51.128483, 71.49291, 65, None,
     "1~implicit!=2"),
    ("Махаббат", 51.179035, 71.40459, 147, None,
     "Махаббат-2", 51.179398, 71.40451, 147, None,
     "1~implicit!=2"),
    ("Акерке", 51.168777, 71.39408, 10, None,
     "Акерке-2", 51.168316, 71.39549, 10, None,
     "1~implicit!=2"),
    ("Отан", 51.11771, 71.51571, 207, None,
     "Отан 2", 51.11771, 71.51571, 207, None,
     "1~implicit!=2"),
    ("Inju Arena", 51.110973, 71.40467, 246, None,
     "Inju Arena 2", 51.110058, 71.4039, 246, None,
     "1~implicit!=2"),
    ("Asylym Park 1", 51.10544, 71.43389, 73, None,
     "Asylym Park 2", 51.106342, 71.43401, 73, None,
     "1!=2"),  # явный номер с обеих сторон — ловилось ещё первым фиксом
]


@pytest.mark.asyncio
@pytest.mark.parametrize("base_name,base_lat,base_lon,base_dev,base_addr,"
                          "num_name,num_lat,num_lon,num_dev,num_addr,expect_fragment", SIBLING_PAIRS)
async def test_sibling_pair_never_auto(db, base_name, base_lat, base_lon, base_dev, base_addr,
                                        num_name, num_lat, num_lon, num_dev, num_addr, expect_fragment):
    """Ни одна из 7 живых sibling-пар не должна уйти в auto — иначе это
    молчаливое слияние двух разных ЖК одного застройщика по соседству
    (тот самый класс ошибок, что и Darmen 2 / Darmen 1)."""
    conf, method = await score_match(
        base_name, num_name,
        existing_lat=base_lat, existing_lon=base_lon,
        candidate_lat=num_lat, candidate_lon=num_lon,
        developer_match=(base_dev == num_dev),
        existing_address=base_addr, candidate_address=num_addr,
        name_a_full=base_name, name_b_full=num_name,
    )
    assert _verdict(conf) != "auto", (
        f"{base_name!r} vs {num_name!r}: conf={conf} method={method} — "
        f"ушло в auto, это WOULD-MERGE ошибка")
    assert expect_fragment in method, f"{base_name!r} vs {num_name!r}: method={method!r}"
