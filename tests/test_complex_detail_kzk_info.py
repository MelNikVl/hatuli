"""Регрессия для задачи 2026-08-15 ("БВУ/КЖК/МИО в карточках ЖК") —
bot/core/complex_detail.py::get_kzk_info(). Реальная БД, синтетические
строки (id/bin-скоуп через cleanup в finally, __test_ префиксы) — не
трогают 313 реальных kzk_registry/514 developers/сотни complexes."""
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


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_complex(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)


async def _insert_developer(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO developers (name) VALUES ($1) RETURNING id", name)


async def _insert_tech_specs(complex_id, developer_bin):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO complex_tech_specs (complex_id, developer_bin) VALUES ($1, $2)",
        complex_id, developer_bin)


async def _insert_kzk(bin_, warranty_scheme=None, is_blacklisted=False, developer_id=None,
                       method=None, zhk_matches=None, snapshot_date=None):
    from bot.db.pg import execute
    await execute("""
        INSERT INTO kzk_registry
            (bin, developer_legal, warranty_scheme, is_blacklisted, in_registry,
             developer_id, developer_match_method, zhk_matches, source_snapshot_date)
        VALUES ($1, 'ТОО Тест', $2, $3, TRUE, $4, $5, $6::jsonb, $7::date)
    """, bin_, warranty_scheme, is_blacklisted, developer_id, method,
        json.dumps(zhk_matches) if zhk_matches is not None else None, snapshot_date)


async def _cleanup(complex_ids=(), developer_ids=(), kzk_bins=()):
    from bot.db.pg import execute
    if kzk_bins:
        await execute("DELETE FROM kzk_registry WHERE bin = ANY($1::text[])", list(kzk_bins))
    if complex_ids:
        await execute("DELETE FROM complex_tech_specs WHERE complex_id = ANY($1::int[])", list(complex_ids))
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(complex_ids))
    if developer_ids:
        await execute("DELETE FROM developers WHERE id = ANY($1::int[])", list(developer_ids))


@pytest.mark.asyncio
async def test_bin_exact_match_wins_over_everything_else(db):
    from bot.core.complex_detail import get_kzk_info
    cid = await _insert_complex("__test_kzk_cx_bin__")
    await _insert_tech_specs(cid, "__test_bin_001__")
    # Ещё и developer/zhk-матчи есть — но bin_exact должен победить их всех.
    await _insert_kzk("__test_bin_001__", warranty_scheme="Гарантия КЖК", is_blacklisted=False)
    try:
        r = await get_kzk_info(cid, None)
        assert r is not None
        assert r["match_level"] == "bin_exact"
        assert r["warranty_scheme"] == "Гарантия КЖК"
        assert r["is_blacklisted"] is False
    finally:
        await _cleanup(complex_ids=[cid], kzk_bins=["__test_bin_001__"])


@pytest.mark.asyncio
async def test_complex_zhk_match_confidence_above_threshold(db):
    from bot.core.complex_detail import get_kzk_info
    cid = await _insert_complex("__test_kzk_cx_zhk__")
    await _insert_kzk("__test_bin_002__", warranty_scheme="Участие БВУ",
                       zhk_matches=[{"name": "ЖК Тест", "complex_id": cid, "confidence": 0.9, "method": "auto"}])
    try:
        r = await get_kzk_info(cid, None)
        assert r is not None
        assert r["match_level"] == "complex_match"
        assert r["warranty_scheme"] == "Участие БВУ"
    finally:
        await _cleanup(complex_ids=[cid], kzk_bins=["__test_bin_002__"])


@pytest.mark.asyncio
async def test_complex_zhk_match_below_threshold_ignored(db):
    from bot.core.complex_detail import get_kzk_info
    cid = await _insert_complex("__test_kzk_cx_zhk_low__")
    await _insert_kzk("__test_bin_003__", warranty_scheme="Участие БВУ",
                       zhk_matches=[{"name": "ЖК Тест", "complex_id": cid, "confidence": 0.6, "method": "review"}])
    try:
        r = await get_kzk_info(cid, None)
        assert r is None  # confidence 0.6 < 0.8 — не используем, developer_id тоже не передан
    finally:
        await _cleanup(complex_ids=[cid], kzk_bins=["__test_bin_003__"])


@pytest.mark.asyncio
async def test_developer_fallback_single_confirmed_row(db):
    from bot.core.complex_detail import get_kzk_info
    dev_id = await _insert_developer("__test_kzk_dev1__")
    await _insert_kzk("__test_bin_004__", warranty_scheme="Разрешение МИО",
                       developer_id=dev_id, method="name_fuzzy_auto")
    try:
        r = await get_kzk_info(None, dev_id)
        assert r is not None
        assert r["match_level"] == "developer_match"
        assert r["warranty_scheme"] == "Разрешение МИО"
        assert r["scheme_conflict"] is False
    finally:
        await _cleanup(developer_ids=[dev_id], kzk_bins=["__test_bin_004__"])


@pytest.mark.asyncio
async def test_developer_fallback_unanimous_scheme_across_rows(db):
    from bot.core.complex_detail import get_kzk_info
    dev_id = await _insert_developer("__test_kzk_dev2__")
    await _insert_kzk("__test_bin_005a__", warranty_scheme="Гарантия КЖК", developer_id=dev_id, method="bin")
    await _insert_kzk("__test_bin_005b__", warranty_scheme="Гарантия КЖК", developer_id=dev_id, method="manual_confirmed")
    try:
        r = await get_kzk_info(None, dev_id)
        assert r["warranty_scheme"] == "Гарантия КЖК"
        assert r["scheme_conflict"] is False
        assert set(r["matched_bins"]) == {"__test_bin_005a__", "__test_bin_005b__"}
    finally:
        await _cleanup(developer_ids=[dev_id], kzk_bins=["__test_bin_005a__", "__test_bin_005b__"])


@pytest.mark.asyncio
async def test_developer_fallback_conflicting_scheme_hides_badge(db):
    from bot.core.complex_detail import get_kzk_info
    dev_id = await _insert_developer("__test_kzk_dev3__")
    await _insert_kzk("__test_bin_006a__", warranty_scheme="Гарантия КЖК", developer_id=dev_id, method="bin")
    await _insert_kzk("__test_bin_006b__", warranty_scheme="Участие БВУ", developer_id=dev_id, method="name_fuzzy_auto")
    try:
        r = await get_kzk_info(None, dev_id)
        assert r["warranty_scheme"] is None
        assert r["scheme_conflict"] is True
        assert r["is_blacklisted"] is False
        assert r["has_signal"] is False  # нечего показать — не блокируем "🔴", т.к. это было бы неправдой
    finally:
        await _cleanup(developer_ids=[dev_id], kzk_bins=["__test_bin_006a__", "__test_bin_006b__"])


@pytest.mark.asyncio
async def test_developer_fallback_worst_case_blacklist_wins(db):
    from bot.core.complex_detail import get_kzk_info
    dev_id = await _insert_developer("__test_kzk_dev4__")
    await _insert_kzk("__test_bin_007a__", warranty_scheme="Гарантия КЖК", is_blacklisted=False, developer_id=dev_id, method="bin")
    await _insert_kzk("__test_bin_007b__", warranty_scheme="Гарантия КЖК", is_blacklisted=True, developer_id=dev_id, method="manual_confirmed")
    try:
        r = await get_kzk_info(None, dev_id)
        assert r["is_blacklisted"] is True  # "хуже побеждает"
        assert r["has_signal"] is True
    finally:
        await _cleanup(developer_ids=[dev_id], kzk_bins=["__test_bin_007a__", "__test_bin_007b__"])


@pytest.mark.asyncio
async def test_developer_fallback_excludes_unconfirmed_review_tier(db):
    from bot.core.complex_detail import get_kzk_info
    dev_id = await _insert_developer("__test_kzk_dev5__")
    await _insert_kzk("__test_bin_008__", warranty_scheme="Гарантия КЖК",
                       developer_id=dev_id, method="name_fuzzy_review")
    try:
        r = await get_kzk_info(None, dev_id)
        assert r is None  # review-tier не подтверждён — не используем на публичной странице
    finally:
        await _cleanup(developer_ids=[dev_id], kzk_bins=["__test_bin_008__"])


@pytest.mark.asyncio
async def test_no_match_anywhere_returns_none(db):
    from bot.core.complex_detail import get_kzk_info
    cid = await _insert_complex("__test_kzk_cx_none__")
    try:
        r = await get_kzk_info(cid, None)
        assert r is None
    finally:
        await _cleanup(complex_ids=[cid])


@pytest.mark.asyncio
async def test_no_signal_scheme_none_no_conflict_is_legit_red_flag(db):
    """Однозначно НЕТ схемы (не конфликт, просто пусто у всех
    подтверждённых строк) — это реальная находка, has_signal=True,
    UI должен показать "🔴 нет официальной защиты"."""
    from bot.core.complex_detail import get_kzk_info
    dev_id = await _insert_developer("__test_kzk_dev6__")
    await _insert_kzk("__test_bin_009__", warranty_scheme=None, developer_id=dev_id, method="bin")
    try:
        r = await get_kzk_info(None, dev_id)
        assert r["warranty_scheme"] is None
        assert r["scheme_conflict"] is False
        assert r["has_signal"] is True
    finally:
        await _cleanup(developer_ids=[dev_id], kzk_bins=["__test_bin_009__"])


@pytest.mark.asyncio
async def test_source_snapshot_date_returned(db):
    from bot.core.complex_detail import get_kzk_info
    cid = await _insert_complex("__test_kzk_cx_date__")
    await _insert_tech_specs(cid, "__test_bin_010__")
    await _insert_kzk("__test_bin_010__", warranty_scheme="Участие БВУ", snapshot_date=date(2026, 7, 29))
    try:
        r = await get_kzk_info(cid, None)
        assert r["source_snapshot_date"] == date(2026, 7, 29)
    finally:
        await _cleanup(complex_ids=[cid], kzk_bins=["__test_bin_010__"])
