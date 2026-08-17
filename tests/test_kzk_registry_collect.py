"""Регрессия для задачи 2026-08-15 ("Реестр КЖК"), коммит 2 —
kzk_registry_collect.py: парсинг встроенного в HTML `<script id=
"regBase">` JSON (developers.kz/market/proverit-zastroyshika), upsert
по `bin`. Тестовая разметка — минимальный синтетический фрагмент,
воспроизводящий РЕАЛЬНУЮ структуру страницы (проверено вручную curl'ом
при разведке 2026-08-15, см. migrations/074 докстринг), не полный
scrape реального сайта — не тащим в репозиторий чужой контент, только
структуру."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from datetime import date

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def _make_html(entries: list[dict], snapshot_date: str = "29.07.2026") -> str:
    """Минимальный HTML, воспроизводящий реальную разметку страницы:
    заголовок с датой снапшота + <script id="regBase"> с JSON-массивом."""
    return f"""<!doctype html><html><head><title>test</title></head>
<body>
<div class="hero-upd"><span class="dot"></span>Данные обновлены <b>{snapshot_date}</b></div>
<main><div class="reg">
<form id="regForm"></form>
<script type="application/json" id="regBase">{json.dumps(entries, ensure_ascii=False)}</script>
</div></main>
</body></html>"""


_NORMAL_ENTRY = {
    "bin": "__test_bin_normal__", "dev": 'ТОО "Тест Норм"', "brand": "TestNorm",
    "cities": ["Астана"], "objects": 5, "zhk_n": 3, "by_city": [["Астана", 5]],
    "scheme": "Участие БВУ", "flagged": False, "in_reg": True, "zhk": [], "phone": "+7(700)111-11-11",
}
_BLACKLIST_ENTRY = {
    "bin": "__test_bin_blacklist__", "dev": 'ТОО "Тест Чёрный"', "brand": None,
    "cities": ["Алматы"], "objects": 1, "zhk_n": 1, "by_city": [["Алматы", 1]],
    "scheme": "", "flagged": True, "in_reg": False, "zhk": [], "phone": None,
}
_BORDERLINE_ENTRY = {
    "bin": "__test_bin_borderline__", "dev": 'ТОО "Тест Погранично"', "brand": "Border",
    "cities": ["Астана"], "objects": 2, "zhk_n": 1, "by_city": [["Астана", 2]],
    "scheme": "Гарантия КЖК", "flagged": True, "in_reg": True, "zhk": ['ЖК "Borderline"'], "phone": "+7(700)222-22-22",
}


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _cleanup(*bins):
    from bot.db.pg import execute
    await execute("DELETE FROM kzk_registry WHERE bin = ANY($1::text[])", list(bins))


def test_parse_registry_html_extracts_entries_and_date():
    from kzk_registry_collect import parse_registry_html
    html = _make_html([_NORMAL_ENTRY, _BLACKLIST_ENTRY])
    entries, snapshot_date = parse_registry_html(html)
    assert len(entries) == 2
    assert entries[0]["bin"] == "__test_bin_normal__"
    assert snapshot_date == date(2026, 7, 29)


def test_parse_registry_html_raises_on_missing_regbase():
    from kzk_registry_collect import parse_registry_html
    with pytest.raises(ValueError):
        parse_registry_html("<html><body>нет нужного script-тега</body></html>")


def test_parse_registry_html_missing_date_returns_none():
    from kzk_registry_collect import parse_registry_html
    html = '<html><body><script type="application/json" id="regBase">[]</script></body></html>'
    entries, snapshot_date = parse_registry_html(html)
    assert entries == []
    assert snapshot_date is None


@pytest.mark.asyncio
async def test_run_collect_inserts_new_entries(db):
    from kzk_registry_collect import run_collect
    from bot.db.pg import fetchrow

    html = _make_html([_NORMAL_ENTRY])
    try:
        result = await run_collect(html=html)
        assert result["created"] == 1
        assert result["updated"] == 0
        assert result["snapshot_date"] == "2026-07-29"

        row = await fetchrow("SELECT * FROM kzk_registry WHERE bin=$1", "__test_bin_normal__")
        assert row["developer_legal"] == 'ТОО "Тест Норм"'
        assert row["developer_brand"] == "TestNorm"
        assert row["warranty_scheme"] == "Участие БВУ"
        assert row["is_blacklisted"] is False
        assert row["in_registry"] is True
        assert row["source_snapshot_date"] == date(2026, 7, 29)
    finally:
        await _cleanup("__test_bin_normal__")


@pytest.mark.asyncio
async def test_run_collect_upsert_updates_not_duplicates(db):
    from kzk_registry_collect import run_collect
    from bot.db.pg import fetch

    try:
        await run_collect(html=_make_html([_NORMAL_ENTRY]))
        changed = dict(_NORMAL_ENTRY, scheme="Гарантия КЖК", objects=99)
        result = await run_collect(html=_make_html([changed]))

        assert result["created"] == 0
        assert result["updated"] == 1
        rows = await fetch("SELECT warranty_scheme, objects_count FROM kzk_registry WHERE bin=$1",
                            "__test_bin_normal__")
        assert len(rows) == 1  # не задублировано
        assert rows[0]["warranty_scheme"] == "Гарантия КЖК"
        assert rows[0]["objects_count"] == 99
    finally:
        await _cleanup("__test_bin_normal__")


@pytest.mark.asyncio
async def test_run_collect_logs_removed_without_deleting(db):
    """Бин был у нас с прошлого прогона, пропал из свежего снапшота —
    считается в result["removed"], но строка НЕ удаляется из БД
    (см. докстринг kzk_registry_collect.py)."""
    from kzk_registry_collect import run_collect
    from bot.db.pg import fetchrow

    try:
        await run_collect(html=_make_html([_NORMAL_ENTRY, _BLACKLIST_ENTRY]))
        # Таблица не обязательно пуста (прод/другие тесты могли что-то
        # оставить) — проверяем ЧЛЕНСТВО конкретного бина в removed_bins,
        # не точное общее число (то честно зависит от всего состояния
        # таблицы на момент прогона, не только от этого теста).
        result = await run_collect(html=_make_html([_NORMAL_ENTRY]))  # blacklist-запись пропала

        assert result["removed"] >= 1
        assert "__test_bin_blacklist__" in result["removed_bins"]
        still_there = await fetchrow("SELECT bin FROM kzk_registry WHERE bin=$1", "__test_bin_blacklist__")
        assert still_there is not None  # не удалена
    finally:
        await _cleanup("__test_bin_normal__", "__test_bin_blacklist__")


@pytest.mark.asyncio
async def test_is_blacklisted_raw_flagged_not_and_with_in_registry(db):
    """Пограничный случай (flagged=true И in_reg=true разом) —
    is_blacklisted=true СОХРАНЯЕТСЯ несмотря на in_registry=true, не
    AND-схлопывается в false (см. migrations/074 докстринг)."""
    from kzk_registry_collect import run_collect
    from bot.db.pg import fetchrow

    try:
        await run_collect(html=_make_html([_BORDERLINE_ENTRY]))
        row = await fetchrow(
            "SELECT is_blacklisted, in_registry FROM kzk_registry WHERE bin=$1", "__test_bin_borderline__")
        assert row["is_blacklisted"] is True
        assert row["in_registry"] is True
    finally:
        await _cleanup("__test_bin_borderline__")


@pytest.mark.asyncio
async def test_run_collect_skips_entries_without_bin(db):
    from kzk_registry_collect import run_collect

    broken = {k: v for k, v in _NORMAL_ENTRY.items() if k != "bin"}
    result = await run_collect(html=_make_html([broken]))
    assert result["total"] == 1
    assert result["created"] == 0  # без bin — пропущена, не упала с ошибкой
    assert result["skipped"] == 1


# ── dry-run: живой парсинг, но НЕ пишет (задача 2026-08-17, "KZK
#    registry нужен... сначала dry-run/canary") ─────────────────────────

@pytest.mark.asyncio
async def test_dry_run_does_not_write_but_reports_would_be_counts(db):
    from kzk_registry_collect import run_collect
    from bot.db.pg import fetchrow

    try:
        result = await run_collect(html=_make_html([_NORMAL_ENTRY]), dry_run=True)
        assert result["dry_run"] is True
        assert result["created"] == 1
        row = await fetchrow("SELECT bin FROM kzk_registry WHERE bin=$1", "__test_bin_normal__")
        assert row is None  # ничего не записано
    finally:
        await _cleanup("__test_bin_normal__")


@pytest.mark.asyncio
async def test_dry_run_after_real_run_reports_unchanged(db):
    """dry-run поверх УЖЕ существующей, НЕ изменившейся записи ->
    unchanged, не created/updated (иначе canary перед реальным прогоном
    не отличил бы "ничего не изменилось" от "перезаписал бы всё")."""
    from kzk_registry_collect import run_collect

    try:
        await run_collect(html=_make_html([_NORMAL_ENTRY]))
        result = await run_collect(html=_make_html([_NORMAL_ENTRY]), dry_run=True)
        assert result["created"] == 0
        assert result["updated"] == 0
        assert result["unchanged"] == 1
    finally:
        await _cleanup("__test_bin_normal__")


@pytest.mark.asyncio
async def test_real_run_after_change_reports_updated_not_unchanged(db):
    from kzk_registry_collect import run_collect

    try:
        await run_collect(html=_make_html([_NORMAL_ENTRY]))
        changed = dict(_NORMAL_ENTRY, scheme="Гарантия КЖК", objects=99)
        result = await run_collect(html=_make_html([changed]), dry_run=True)
        assert result["updated"] == 1
        assert result["unchanged"] == 0
    finally:
        await _cleanup("__test_bin_normal__")


# ── ошибка одной записи не должна ронять весь прогон ──────────────────

@pytest.mark.asyncio
async def test_one_bad_entry_does_not_abort_whole_batch(db):
    """Одна запись с некорректным типом поля (objects_count — TEXT
    вместо INT/None, INSERT упадёт на этой строке) НЕ должна помешать
    остальным записям того же снапшота записаться — errors считается
    отдельно, cbatch продолжается (задача, неявно: "покажи errors" =>
    они возможны и не фатальны)."""
    from kzk_registry_collect import run_collect
    from bot.db.pg import fetchrow

    broken = dict(_NORMAL_ENTRY, bin="__test_bin_broken__", objects="не число")
    good = dict(_BLACKLIST_ENTRY)
    try:
        result = await run_collect(html=_make_html([broken, good]))
        assert result["errors"] == 1
        assert result["created"] == 1  # good всё равно записалась
        assert len(result["error_samples"]) == 1

        good_row = await fetchrow("SELECT bin FROM kzk_registry WHERE bin=$1", "__test_bin_blacklist__")
        assert good_row is not None
        broken_row = await fetchrow("SELECT bin FROM kzk_registry WHERE bin=$1", "__test_bin_broken__")
        assert broken_row is None  # кривая запись не записалась, но и не уронила остальные
    finally:
        await _cleanup("__test_bin_broken__", "__test_bin_blacklist__")


def test_cli_has_dry_run_flag():
    import inspect
    from kzk_registry_collect import main
    src = inspect.getsource(main)
    assert "--dry-run" in src
