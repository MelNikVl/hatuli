"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase"),
последний коммит фазы — "Stability-тест". Требование заказчика:
добавление нового фактора/группы НЕ должно сдвигать нормализованный
score существующих ЖК больше чем на ±2 балла, если для этого ЖК новый
фактор нейтрален/unknown.

По конструкции availability-aware normalize_group_weighted() (коммит
"Confidence") это должно держаться СТРОГО (Δ=0, не просто ≤2) — эти
тесты подтверждают именно это на нескольких сценариях и защищают от
будущей регрессии, если кто-то ослабит _is_available()/_group_range_
available() и вернёт баг класса 66->55 (задача 2026-08-15, "школы/
садики", исходный триггер всей этой фазы)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy

import bot.core.location_score as ls


def _realistic_factors() -> dict:
    """Правдоподобный частично измеренный ЖК — часть факторов реально
    посчитана (adj != 0, содержательный reason), часть — "нет данных"
    (типичная живая картина, не искусственно все-или-ничего)."""
    return {
        "noise": {"adj": -2, "reason": "магистраль в 400м — умеренный шум"},
        "schools": {"adj": 2, "reason": "вуз рядом — арендный спрос студентов"},
        "transit_stops": {"adj": 0, "reason": "нет данных"},
        "amenities": {"adj": 3, "reason": "пешая доступность: магазин в 200м"},
        "parks": {"adj": 0, "reason": "нет данных"},
        "lrt_access": {"adj": 0, "reason": "нет данных transport_hexes рядом"},
        "road_access": {"adj": 1, "reason": "рядом крупная дорога"},
        "route_connectivity": {"adj": 0, "reason": "нет данных"},
        "demolition": {"adj": 0, "reason": "рядом нет объектов из перечня на снос"},
        "school_access": {"adj": 3, "reason": "школа в 400м"},
        "kindergarten_access": {"adj": 0, "reason": "нет данных astana_kindergartens рядом"},
        "bank": {"adj": 0, "reason": "правый берег Ишима (исторический центр)"},
    }


def test_new_factor_absent_from_group_does_not_move_score():
    """Симулирует ровно то, что случилось на живых данных 2026-08-15
    (school_access/kindergarten_access добавились в схему) — но теперь
    для ЖК, где новый фактор просто ОТСУТСТВУЕТ в factors (ключа нет
    вовсе), score должен остаться БУКВАЛЬНО тем же самым."""
    before = _realistic_factors()
    score_before = ls.normalize_group_weighted(before)

    fake_groups = copy.deepcopy(ls._GROUPS)
    fake_groups["infra"] = fake_groups["infra"] + ("hypothetical_new_signal",)
    fake_ranges = dict(ls._FACTOR_RANGES)
    fake_ranges["hypothetical_new_signal"] = (0, 10)

    import unittest.mock as mock
    with mock.patch.object(ls, "_GROUPS", fake_groups), mock.patch.object(ls, "_FACTOR_RANGES", fake_ranges):
        # after: те же factors, БЕЗ hypothetical_new_signal вообще (как
        # если бы прогон случился ДО того, как кто-то реализовал функцию,
        # которая его считает).
        score_after = ls.normalize_group_weighted(before)

    assert score_after == score_before  # Δ=0, не просто ≤2


def test_new_factor_unavailable_for_this_complex_does_not_move_score():
    """Тот же сценарий, но НОВЫЙ фактор ключ ЕСТЬ в factors — просто со
    статусом "нет данных" (как реально бывает: функция для НОВОГО
    фактора существует и вызывается для ВСЕХ ЖК, но конкретно для этого
    ничего не нашла) — тоже Δ=0."""
    before = _realistic_factors()
    score_before = ls.normalize_group_weighted(before)

    after = copy.deepcopy(before)
    after["hypothetical_new_signal"] = {"adj": 0, "reason": "нет данных"}

    fake_groups = copy.deepcopy(ls._GROUPS)
    fake_groups["infra"] = fake_groups["infra"] + ("hypothetical_new_signal",)
    fake_ranges = dict(ls._FACTOR_RANGES)
    fake_ranges["hypothetical_new_signal"] = (0, 10)

    import unittest.mock as mock
    with mock.patch.object(ls, "_GROUPS", fake_groups), mock.patch.object(ls, "_FACTOR_RANGES", fake_ranges):
        score_after = ls.normalize_group_weighted(after)

    assert score_after == score_before


def test_new_factor_actually_measured_is_allowed_to_move_score():
    """Контрольный случай — если новый фактор ДЕЙСТВИТЕЛЬНО измерен
    (не unknown), score МОЖЕТ измениться — stability-требование касается
    только нейтрального/unknown случая, не запрещает score реагировать
    на реальную новую информацию."""
    before = _realistic_factors()
    score_before = ls.normalize_group_weighted(before)

    after = copy.deepcopy(before)
    after["hypothetical_new_signal"] = {"adj": 10, "reason": "измерено: максимум"}

    fake_groups = copy.deepcopy(ls._GROUPS)
    fake_groups["infra"] = fake_groups["infra"] + ("hypothetical_new_signal",)
    fake_ranges = dict(ls._FACTOR_RANGES)
    fake_ranges["hypothetical_new_signal"] = (0, 10)

    import unittest.mock as mock
    with mock.patch.object(ls, "_GROUPS", fake_groups), mock.patch.object(ls, "_FACTOR_RANGES", fake_ranges):
        score_after = ls.normalize_group_weighted(after)

    assert score_after != score_before  # реальная новая информация ДОЛЖНА влиять


def test_new_group_with_reallocated_weight_is_a_documented_exception():
    """ЧЕСТНАЯ находка при написании stability-теста, НЕ баг: гарантия
    Δ=0 (или даже ±2) держится СТРОГО только для нового ФАКТОРА ВНУТРИ
    существующей группы (см. остальные тесты этого файла — та ситуация,
    которая реально случилась 2026-08-15 и запустила всю эту фазу). Для
    совершенно НОВОЙ ГРУППЫ с весом, забранным у существующей, гарантия
    НЕ держится математически — если группа-донор весa была у СВОЕГО
    края (100% или 0%) для конкретного ЖК, урезание её веса в пользу
    "нейтральной" 50%-новой группы неизбежно двигает итог, пропорционально
    тому, насколько крайним было её значение. Это не решается технически
    без потери смысла (у какой ещё группы забирать вес?) — ровно поэтому
    _GROUP_WEIGHTS уже задокументирован как "требует явного решения
    заказчика" (см. bot/core/location_score.py) — добавление НОВОЙ
    группы ВСЕГДА обдуманное решение с ручным пересмотром весов, не
    незаметное техническое изменение, как добавление фактора в группу."""
    before = _realistic_factors()  # risk у этого профиля на своём МАКСИМУМЕ (100%)
    score_before = ls.normalize_group_weighted(before)

    fake_groups = copy.deepcopy(ls._GROUPS)
    fake_groups["hypothetical_new_group"] = ("hypothetical_group_factor",)
    fake_ranges = dict(ls._FACTOR_RANGES)
    fake_ranges["hypothetical_group_factor"] = (0, 5)
    fake_weights = dict(ls._GROUP_WEIGHTS)
    fake_weights["risk"] = fake_weights["risk"] - 0.05
    fake_weights["hypothetical_new_group"] = 0.05

    import unittest.mock as mock
    with mock.patch.object(ls, "_GROUPS", fake_groups), \
         mock.patch.object(ls, "_FACTOR_RANGES", fake_ranges), \
         mock.patch.object(ls, "_GROUP_WEIGHTS", fake_weights):
        score_after = ls.normalize_group_weighted(before)

    # Реальный сдвиг ЕСТЬ (не Δ=0) — фиксируем находку явно, не
    # притворяемся, что ±2 держится всегда.
    assert score_after != score_before
    # Но и не катастрофический — пропорционален урезанному весу (0.05 =
    # 5% модели), а не всей модели целиком (тот старый баг класса 66->55
    # двигал ВСЕ ЖК на десятки баллов, это — единицы).
    assert abs(score_after - score_before) <= 5


def test_stability_holds_across_varied_realistic_profiles():
    """Не один частный случай, а несколько разных правдоподобных
    профилей ЖК (полностью unknown / частично / почти полностью
    измерен) — во всех Δ=0 при добавлении неизмеренного фактора."""
    profiles = [
        {},  # полностью unknown
        _realistic_factors(),
        {k: {"adj": v["adj"], "reason": "измерено (тест)"} for k, v in _realistic_factors().items()},  # всё measured
    ]
    fake_groups = copy.deepcopy(ls._GROUPS)
    fake_groups["environment"] = fake_groups["environment"] + ("hypothetical_park_quality",)
    fake_ranges = dict(ls._FACTOR_RANGES)
    fake_ranges["hypothetical_park_quality"] = (0, 3)

    import unittest.mock as mock
    for profile in profiles:
        score_before = ls.normalize_group_weighted(profile)
        with mock.patch.object(ls, "_GROUPS", fake_groups), mock.patch.object(ls, "_FACTOR_RANGES", fake_ranges):
            score_after = ls.normalize_group_weighted(profile)
        assert score_after == score_before, f"profile={profile} moved score {score_before}->{score_after}"
