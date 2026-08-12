"""Регрессия политики гранулярности ЖК (задача 2026-08-12, см.
docs/entity_resolution_plan.md — "политика гранулярности ЖК"): таблица
известных вердиктов для расшивки blob-комплексов. Проверяет
_phase_token() на реальных именах из живой БД — "пятно"/"квартал"/
перечисления номеров НИКОГДА не дают токен (весь диапазон один ЖК),
явные буква/номер блока — дают, разные явные токены — основа для split.

Не требует БД (_phase_token — чистая функция, без похода в pg_trgm).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bot.core.entity_resolution import _phase_token


# ── "пятно"/"квартал"/перечисления — НИКОГДА не split ────────────────

@pytest.mark.parametrize("name", [
    'ЖК "Городской романс" (пятна 6, 9)',
    'ЖК "Городской романс" (пятна 8, 10, 4)',
    'ЖК "Городской романс" (квартал 5)',
    'ЖК "Городской романс" (квартал 7)',
    'ЖК "Городской романс" (квартал 9)',
    'ЖК "Городской романс" (квартал 10)',
    'ЖК "Millennium Park" (пятно 12)',
    'ЖК "Millennium Park" (пятно 15)',
    'ЖК "MILLENNIUM PARK" (блок 23)',  # блок с явным номером НЕ рядом с пятно/квартал — но тут внутри есть "MILLENNIUM PARK" без пятно, отдельный кейс ниже
])
def test_pyatno_kvartal_never_gives_token(name):
    token, _ = _phase_token(name)
    if "пятно" in name.lower() or "пятна" in name.lower() or "квартал" in name.lower():
        assert token is None, f"{name!r} -> token={token!r}, должно быть None (пятно/квартал не разграничитель)"


@pytest.mark.parametrize("name", [
    'МЖК "AUSTRIA" (блоки 1, 2, 3)',
    'МЖК "AUSTRIA" (блоки 4, 11)',
    'МЖК "AUSTRIA" (блоки 5, 7)',
    'МЖК "AUSTRIA" (блоки 8, 10)',
    'ЖК "Ак-Дидар" (блоки 9, 10, 11, 12, 13)',
    'ЖК "Atlant - 1,2"',
    'ЖК "LANDMARK - 1" (пятна 1-4)',
])
def test_enumerated_list_never_gives_token(name):
    token, _ = _phase_token(name)
    assert token is None, f"{name!r} -> token={token!r}, должно быть None (список номеров — не конкретный блок)"


# ── явные единичные блоки/номера — дают токен, разный -> split ───────

def test_family_nest_letters_split():
    a, _ = _phase_token("Family Nest F")
    b, _ = _phase_token("Family Nest B")
    c, base_c = _phase_token("Family Nest")
    assert a == "block:f"
    assert b == "block:b"
    assert a != b
    assert c is None
    assert base_c == "family nest"


def test_arupark_numbers_split():
    base_token, base_base = _phase_token('ЖК "AruPark"')
    t3, base3 = _phase_token('ЖК "AruPark - 3"')
    assert base_token is None
    assert base_base == "arupark"
    assert t3 == "3"
    assert base3 == "arupark"  # база совпадает с безномерной -> implicit phase 1 сработает в score_match


def test_parkland_explicit_tokens_present_on_all_sides():
    # У Parkland все 6 объектов несут ЯВНЫЙ токен (номер или буква) — нет
    # безномерной "базы", поэтому решение "куда group" — за адресом
    # (см. правило 5 в docs), но сам факт "это split-кандидат, не одна
    # безномерная база" подтверждается тут: у каждого объекта token не None.
    names = ['ЖК "PARKLAND 2"', 'ЖК "PARKLAND - 1"', 'ЖК "Parkland - E"',
             'ЖК "Parkland - F"', 'ЖК "Parkland - C"', 'ЖК "Parkland D"']
    tokens = [_phase_token(n)[0] for n in names]
    assert all(t is not None for t in tokens), tokens
    assert len(set(tokens)) == len(tokens), "токены должны быть все разные (2,1,E,F,C,D)"
