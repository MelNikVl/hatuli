"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase" v2,
коммит "Семантика + якоря + иерархическая модель") —
bot/core/location_score.py::normalize_group_weighted()/_group_pct() +
_is_available()/_group_range_available()/_compute_confidence()/
_annotate_factor_metadata(). Чистые функции (без сети/БД), тестируются
напрямую на синтетических factors-словарях.

Таксономия v2 — пять latent-свойств (transport=Accessibility,
infra=Everyday infrastructure, environment=Environment [было ДВЕ группы
green+noise, теперь ОДНА], risk=Location risk, urban_quality=Urban
quality/desirability, НОВОЕ и пока ПУСТОЕ — ни одного фактора)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.location_score import (
    normalize_group_weighted, _group_pct, _GROUPS, _GROUP_WEIGHTS, _group_range, _FACTOR_RANGES,
    _is_available, _group_range_available, _compute_confidence, _annotate_factor_metadata,
)


def _factors(available=(), **overrides) -> dict:
    """`available` — ключи, которым выставляется reason БЕЗ "нет данных"
    (т.е. реально измеренные) — ПО УМОЛЧАНИЮ ВСЕ факторы "нет данных"
    (реалистичный дефолт после задачи "Confidence": ничего не измерено,
    пока явно не указано в `available`). `overrides` — adj для конкретных
    ключей, НЕЗАВИСИМО от `available`."""
    keys = [k for g in _GROUPS.values() for k in g] + ["bank"]
    base = {k: {"adj": 0, "reason": "нет данных"} for k in keys}
    for k in available:
        base[k]["reason"] = "измерено (тест)"
    for k, v in overrides.items():
        base[k]["adj"] = v
    return base


def test_group_weights_sum_to_one():
    assert abs(sum(_GROUP_WEIGHTS.values()) - 1.0) < 1e-9


def test_five_latent_properties_taxonomy():
    """Задача 2026-08-15 v2 — таксономия ровно из пяти свойств с этими
    именами и весами (структурная константа продукта)."""
    assert set(_GROUPS.keys()) == {"transport", "infra", "environment", "risk", "urban_quality"}
    assert _GROUP_WEIGHTS == {
        "transport": 0.25, "infra": 0.25, "environment": 0.20,
        "risk": 0.15, "urban_quality": 0.15,
    }


def test_environment_merges_old_green_and_noise_plus_air_quality():
    """environment = бывшие green(parks) + noise(noise) + air_quality
    (переехал сюда ИЗ risk, задача 2026-08-15 v2) — ОДНО свойство, не два."""
    assert set(_GROUPS["environment"]) == {"noise", "parks", "air_quality"}


def test_air_quality_moved_out_of_risk():
    """air_quality был в risk (задача "воздух в location_score"),
    v2 переносит его в environment — он УТОЧНЯЕТ среду, не является
    отдельным риском."""
    assert "air_quality" not in _GROUPS["risk"]
    assert _GROUPS["risk"] == ("demolition",)


def test_urban_quality_is_new_and_empty():
    """Задача 2026-08-15 v2 — новое свойство, СЕЙЧАС пустое (ни одного
    измеримого фактора). Unknown ≠ average: НЕ подменяем нейтральной
    оценкой молча — confidence этого свойства всегда 0 (см.
    test_compute_confidence в этом же файле и _group_confidence
    в будущем коммите "Confidence" той же фазы)."""
    assert _GROUPS["urban_quality"] == ()


def test_building_age_not_in_any_group():
    """building_age — качество ЗДАНИЯ, не локации — не входит НИКУДА,
    в т.ч. НЕ в urban_quality несмотря на смысловую близость названия
    (сама _building_age_factor() сохранена в коде, просто не
    вызывается из compute_complex_location_score())."""
    all_keys = {k for keys in _GROUPS.values() for k in keys}
    assert "building_age" not in all_keys
    assert "building_age" not in _FACTOR_RANGES


# ── _is_available ────────────────────────────────────────────────────────

def test_is_available_detects_no_data_and_error():
    assert _is_available({"reason": "нет данных"}) is False
    assert _is_available({"reason": "нет данных astana_schools рядом"}) is False
    assert _is_available({"reason": "ошибка слоя: timeout"}) is False
    assert _is_available({"reason": "школа в 300м"}) is True
    assert _is_available({}) is True  # нет reason вовсе -> осторожный дефолт "доступен"


# ── normalize_group_weighted / _group_pct ───────────────────────────────

def test_all_unavailable_gives_exactly_fifty():
    """ЗАКРЫВАЕТ баг, задокументированный в предыдущих коммитах фазы:
    unknown-фактор в свойствах с неотрицательным диапазоном раньше
    читался как МИНИМУМ (0%), не середина. Теперь — свойство БЕЗ
    единого доступного фактора вносит РОВНО 50 (= "нормальный городской
    уровень Астаны", не "не знаем"), и так для всех пяти -> взвешенный
    итог тоже РОВНО 50."""
    f = _factors()  # ничего не available
    assert normalize_group_weighted(f) == 50


def test_urban_quality_always_fifty_percent_structurally():
    """urban_quality пуст СТРУКТУРНО (не просто "не измерено сейчас") —
    _group_pct() для него ВСЕГДА 50, при любых factors, потому что в
    _GROUPS["urban_quality"] нет ни одного ключа для перебора."""
    assert _group_pct("urban_quality", _factors()) == 50.0
    assert _group_pct("urban_quality", _factors(available=("noise", "schools"), noise=-6, schools=2)) == 50.0


def test_theoretical_bounds_are_not_0_100_while_urban_quality_empty():
    """Честное следствие пустого urban_quality (15% веса): пока в нём
    нет ни одного фактора, итоговый score НИКОГДА не достигает
    буквальных 0/100 — urban_quality всегда тянет к своим 50% на эту
    долю веса. Это НЕ баг: акт признания "15% картины нам неизвестны"
    не должен звучать как "уверенно средне" (0/100 были бы ложной
    точностью). Теоретические границы сейчас — round(0.85*0 + 0.15*50)
    = 8 и round(0.85*100 + 0.15*50) = 92."""
    all_available = ("noise", "demolition", "route_connectivity", "schools")
    worst = _factors(available=all_available, noise=-6, demolition=-2)
    best = _factors(available=all_available + ("parks",),
                     route_connectivity=2, schools=2, parks=2)
    assert normalize_group_weighted(worst) == 8
    assert normalize_group_weighted(best) == 92


def test_group_ranges_match_factor_range_sums():
    """_group_range() — СТАТИЧЕСКИЙ теоретический диапазон (для
    документации/отображения), normalize_group_weighted() его больше не
    использует напрямую (см. _group_range_available() ниже)."""
    assert _group_range("transport") == (0, 11)
    # infra: schools(0,2)+amenities(0,4)+school_access(0,4)+
    # kindergarten_access(0,2) — schools сжат с 0..5 до 0..2 (задача
    # "двойные школы", 2026-08-15).
    assert _group_range("infra") == (0, 12)
    # environment: noise(-6,0)+parks(0,2)+air_quality(-3,0) — задача
    # 2026-08-15 v2 объединила green+noise и перенесла сюда air_quality.
    assert _group_range("environment") == (-9, 2)
    # risk: только demolition — air_quality уехал в environment, v2.
    assert _group_range("risk") == (-2, 0)
    # urban_quality: пустая группа -> (0, 0).
    assert _group_range("urban_quality") == (0, 0)


def test_group_range_available_none_when_nothing_measured():
    assert _group_range_available("transport", _factors()) is None


def test_group_range_available_always_none_for_urban_quality():
    """Пустая группа -> [k for k in () if ...] всегда [], значит
    _group_range_available() всегда None, при ЛЮБЫХ factors."""
    assert _group_range_available("urban_quality", _factors()) is None
    assert _group_range_available("urban_quality", _factors(available=("noise",), noise=-3)) is None


def test_group_range_available_only_counts_available_keys():
    f = _factors(available=("schools",), schools=1)
    assert _group_range_available("infra", f) == (0, 2)  # только schools, не остальные 3 infra-фактора


def test_missing_group_untouched_by_change_in_another_group():
    """Ключевое свойство групповой модели (vs старый единый диапазон,
    задача-триггер коммита "Семантика + групповая модель"): изменение
    ОДНОГО свойства не должно менять нормализованный вклад ДРУГИХ."""
    base = _factors(available=("schools", "amenities"), schools=2, amenities=4)
    more_transport = _factors(available=("schools", "amenities", "lrt_access"),
                               schools=2, amenities=4, lrt_access=4)
    base_score = normalize_group_weighted(base)
    more_score = normalize_group_weighted(more_transport)
    # infra идентична в обоих случаях (schools=2/2 max + amenities=4/4 max
    # -> 100% в обоих) — вся разница из transport: 50% (ничего не
    # измерено) -> 100% (lrt_access=4 из своего же диапазона 0..4).
    expected_delta = round(_GROUP_WEIGHTS["transport"] * 50)
    assert abs((more_score - base_score) - expected_delta) <= 1  # округление


def test_symmetric_extremes_environment_and_risk():
    lo_env = _factors(available=("noise",), noise=-6)
    hi_env = _factors(available=("noise",), noise=0)
    assert normalize_group_weighted(lo_env) < normalize_group_weighted(hi_env)
    lo_risk = _factors(available=("demolition",), demolition=-2)
    hi_risk = _factors(available=("demolition",), demolition=0)
    assert normalize_group_weighted(lo_risk) < normalize_group_weighted(hi_risk)


def test_new_factor_neutral_for_existing_complex_does_not_move_score():
    """Прямая демонстрация свойства, которое проверяет stability-тест:
    "ЖК", у которого появился новый гипотетический фактор внутри infra
    остался НЕ измерен (unavailable) — его score НЕ ДОЛЖЕН измениться."""
    before = _factors(available=("schools",), schools=1)
    after_with_unmeasured_new_key = dict(before)
    after_with_unmeasured_new_key["kindergarten_access"] = {"adj": 0, "reason": "нет данных"}
    assert normalize_group_weighted(before) == normalize_group_weighted(after_with_unmeasured_new_key)


# ── _compute_confidence ──────────────────────────────────────────────────

def test_compute_confidence_weighted_by_source_quality():
    """Один доступный фактор из точного реестра (school_access,
    source_quality=0.8) даёт БОЛЬШЕ confidence, чем один доступный
    фактор из OSM (schools, 0.6) при равном общем числе факторов схемы."""
    f_precise = _factors(available=("school_access",))
    f_osm = _factors(available=("schools",))
    assert _compute_confidence(f_precise) > _compute_confidence(f_osm)


def test_compute_confidence_nothing_available_is_zero():
    assert _compute_confidence(_factors()) == 0


def test_compute_confidence_everything_available_is_hundred():
    all_keys = [k for g in _GROUPS.values() for k in g] + ["bank"]
    f = _factors(available=tuple(all_keys))
    assert _compute_confidence(f) == 100


# ── _annotate_factor_metadata ────────────────────────────────────────────

def test_annotate_factor_metadata_sets_all_four_dimensions():
    f = _factors(available=("noise",), noise=-3)
    _annotate_factor_metadata(f)
    assert f["noise"]["available"] is True
    assert 0 < f["noise"]["source_quality"] <= 1
    assert f["noise"]["freshness"] in ("live", "periodic", "manual")
    assert f["noise"]["precision"] in ("exact", "presence", "heuristic")
    assert f["schools"]["available"] is False  # не в available -> "нет данных"
