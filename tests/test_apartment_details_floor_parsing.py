"""Регрессия для bot/core/apartment_details.py::fetch_apartment_details —
парсинг этажа (задача 2026-08-17, "Missing floor + orphan audit").

Найдено на canary scripts/backfill_listing_floors.py (40 реальных
объявлений backlog'а, floor_filled=0/40): существенная доля страниц
krisha.kz пишет этаж как отдельный info-item "Этаж N" (label, ПОТОМ
число, БЕЗ "N из M") — ни существующий "N из M" regex, ни существующий
"N этаж" (число, ПОТОМ label) фолбэк его не ловили. Добавлен третий
фолбэк — эти тесты его закрепляют (и защищают два старых пути от
регрессии тем же PR)."""
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _html(info_items: list[str], title: str = "1-комнатная квартира") -> str:
    items_html = "".join(f'<div class="offer__info-item">{t}</div>' for t in info_items)
    return f'<html><body><h1>{title}</h1>{items_html}</body></html>'


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        pass


async def _fetch_with_html(html: str):
    from bot.core.apartment_details import fetch_apartment_details

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_FakeResponse(html))):
        with patch("asyncio.sleep", new=AsyncMock()):  # не ждать реальные 3-6с в тесте
            return await fetch_apartment_details("https://krisha.kz/a/show/__test__")


@pytest.mark.asyncio
async def test_floor_n_of_m_format_unaffected():
    html = _html(["Этаж 7 из 9", "Площадь 45 м²"])
    d = await _fetch_with_html(html)
    assert d["floor"] == 7
    assert d["floors_total"] == 9
    assert d["floor_position"] == "middle"


@pytest.mark.asyncio
async def test_floor_label_then_number_format_new_fallback():
    """РЕГРЕССИЯ на находку этой задачи: "Этаж 7" без "из M" — раньше
    floor оставался None, теперь фолбэк 1 его ловит."""
    html = _html(["Этаж 7", "Площадь 35.14 м²", "Высота потолков 3 м"])
    d = await _fetch_with_html(html)
    assert d["floor"] == 7
    assert d.get("floors_total") is None


@pytest.mark.asyncio
async def test_floor_number_then_word_format_still_works():
    """Фолбэк 2 (существовавший до этой задачи) — "3 этаж" в заголовке,
    без структурированного info-item вообще."""
    html = _html(["Площадь 45 м²"], title="· 3 этаж, Жирентаева 13/1")
    d = await _fetch_with_html(html)
    assert d["floor"] == 3


@pytest.mark.asyncio
async def test_etazhnost_does_not_false_match_new_fallback():
    """"Этажность дома 12" — про ЗДАНИЕ, не про юнит. Новый regex
    (?<!\\w)этаж\\s+(\\d+) НЕ должен спутать его с "Этаж N": сразу после
    "этаж" идёт "ность", не пробел+цифра — не матчит вовсе."""
    html = _html(["Этажность дома 12", "Площадь 45 м²"])
    d = await _fetch_with_html(html)
    assert d.get("floor") is None


@pytest.mark.asyncio
async def test_no_floor_info_anywhere_stays_none():
    """Настоящий floor_not_found — задача, п.1: реальные развёрстки
    krisha.kz, где этаж не указан продавцом вовсе (не ошибка парсинга)."""
    html = _html(["Тип дома кирпичный", "Площадь 57.2 м²", "Год постройки 2023"])
    d = await _fetch_with_html(html)
    assert d.get("floor") is None
