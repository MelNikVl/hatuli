"""Регрессия merge_translit_dups.py::phase_conflict() — найдено ЖИВЫМ багом
при первом гейте (--limit 10, 2026-08-12): #4272 'ЖК "Abai Joly" (3 очередь)'
и #1400 'Abai Joly' схлопнулись в один транслит-ключ ('abai joly') потому
что norm_name() стирает "(3 очередь)" ДО группировки, а продуктовый токен
эту дыру не ловил (фаза — не линейка продукта). merge_translit_dups.py
подхватил бы это как auto-merge (гео/застройщик совпадали) и отменил
легитимный split, сделанный unravel_blobs.py тем же днём. Требует живой
Postgres — phase_conflict() зовёт name_similarity() (pg_trgm).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

from merge_translit_dups import phase_conflict, split_provenance_conflict, developer_conflict

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_abai_joly_explicit_phase_vs_implicit_base_conflicts(db):
    """Живой баг гейта: голая база 'Abai Joly' (implicit-1) против явной
    '(3 очередь)' — конфликт, НЕ должно мерджиться."""
    assert await phase_conflict('Abai Joly', 'ЖК "Abai Joly" (3 очередь)') is True


@pytest.mark.asyncio
async def test_same_explicit_phase_no_conflict(db):
    """Обе стороны — одна и та же явная очередь: не конфликт."""
    assert await phase_conflict('ЖК "Abai Joly" (3 очередь)', 'Abai Joly 3') is False


@pytest.mark.asyncio
async def test_different_explicit_phases_conflict(db):
    assert await phase_conflict('ЖК "Abai Joly" (2 очередь)', 'ЖК "Abai Joly" (3 очередь)') is True


@pytest.mark.asyncio
async def test_no_phase_tokens_no_conflict(db):
    """Ни у одной стороны явного токена фазы — не должно ложно блокировать
    обычный транслит-дубль (Tandau/Тандау и подобные)."""
    assert await phase_conflict("Tandau", "Тандау") is False


@pytest.mark.asyncio
async def test_implicit_phase_conflict_cross_script(db):
    """Живой баг: 'Алтын Саулет' (голая, implicit-1, кириллица) vs
    'Altyn Saulet (2 очередь)' (явная фаза 2, латиница) — без транслита
    base_sim('алтын саулет','altyn saulet')~0, implicit-конфликт молчал
    и union-find склеивал явно разные очереди через кириллический
    'мост'. С транслитом base'ы — конфликт (1 неявная vs 2 явная)."""
    assert await phase_conflict("Алтын Саулет", 'ЖК "Altyn Saulet" (2 очередь)') is True


@pytest.mark.asyncio
async def test_position_marker_recognized_as_queue_synonym(db):
    """'позиция N' — синоним 'очередь N' (Bagystan). Разные позиции с
    обеих сторон — конфликт, как у обычной очереди."""
    assert await phase_conflict('ЖК "Bagystan" (очередь 3)', 'ЖК "Bagystan" (позиция 12)') is True


# ── split_provenance_conflict(): второй, независимый предохранитель —
#    ВТОРОЙ живой баг гейта (#311/#4339 'Времена Года (Лето)') нашёл
#    дыру именно в phase_conflict(): "(Лето)" между базой и суффиксом
#    блока сбивает pg_trgm base_sim ниже 0.8, сигнал молчит. provenance
#    надёжнее — не зависит от текста имени вовсе.

def test_split_provenance_conflict_blocks_direct_parent_child():
    parent_of = {4339: 311}
    assert split_provenance_conflict(311, 4339, parent_of) is True
    assert split_provenance_conflict(4339, 311, parent_of) is True  # порядок аргументов не важен


def test_split_provenance_conflict_blocks_two_hop_lineage():
    """Живой баг: #4339 -> #4266 -> #311 (два хопа, не прямая связь)."""
    parent_of = {4339: 4266, 4266: 311}
    assert split_provenance_conflict(311, 4339, parent_of) is True


def test_split_provenance_conflict_blocks_siblings_of_same_split():
    """Два ребёнка ОДНОГО родителя (разные явные блоки одного blob'а) —
    тоже общий предок в ветке родословной, тоже блокируем."""
    parent_of = {4339: 4266, 4340: 4266}
    assert split_provenance_conflict(4339, 4340, parent_of) is True


def test_split_provenance_conflict_false_for_unrelated_pair():
    parent_of = {200: 999}
    assert split_provenance_conflict(100, 200, parent_of) is False


# ── developer_conflict(): явное расхождение застройщика — минус-сигнал,
#    не просто "не участвует". Живой случай: 'samruk towers' (#230,
#    developer_id=210) / 'Самрук Towers' (#435, developer_id=11) — 93 м
#    друг от друга, проходили в auto чисто по гео, хотя застройщик
#    известен с обеих сторон и разный.

def test_samruk_towers_developer_conflict_blocks_geo_only_match():
    a = {"developer_id": 210, "developer": None}
    b = {"developer_id": 11, "developer": None}
    assert developer_conflict(a, b) is True


def test_developer_conflict_false_when_missing_on_one_side():
    """Нет данных с одной стороны — не конфликт, просто сигнал не
    участвует (тот же принцип, что address_match/остальные сигналы)."""
    a = {"developer_id": 210, "developer": None}
    b = {"developer_id": None, "developer": None}
    assert developer_conflict(a, b) is False


def test_developer_conflict_false_when_equal():
    a = {"developer_id": 210, "developer": None}
    b = {"developer_id": 210, "developer": None}
    assert developer_conflict(a, b) is False


def test_developer_conflict_falls_back_to_text_field():
    a = {"developer_id": None, "developer": "BI Group"}
    b = {"developer_id": None, "developer": "NAK"}
    assert developer_conflict(a, b) is True
