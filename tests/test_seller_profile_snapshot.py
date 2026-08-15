"""Юнит-тесты агрегационной логики seller_profile_snapshot.py (§2.7
docs/liquidity_model_design.md, задача 2026-08-15) — чистые функции
(_normalize_name/_aggregate), без БД. Схема таблицы/UPSERT-контракт —
tests/test_seller_profiles_schema.py."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from seller_profile_snapshot import _aggregate, _normalize_name, _GENERIC_NAME_STOPLIST

NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


def _listing(id, seller_name, seller_type="owner", is_active=True, last_seen=NOW,
             bargain_discount_pct=None, relisted_within_60d=None, time_on_market=None):
    return {
        "id": id, "seller_name": seller_name, "seller_type": seller_type,
        "is_active": is_active, "last_seen": last_seen,
        "bargain_discount_pct": bargain_discount_pct,
        "relisted_within_60d": relisted_within_60d, "time_on_market": time_on_market,
    }


def test_normalize_name_trims_lowers_collapses_spaces():
    assert _normalize_name("  Ультаракова   Асемгуль ") == "ультаракова асемгуль"
    assert _normalize_name("Айгуль") == "айгуль"


def test_generic_names_excluded_from_aggregation():
    """'Хозяин'/'Продавец' и т.п. — роль-заглушка тысяч разных людей на
    живых данных (1120/219 объявлений под одной строкой) — НЕ один
    продавец, не должны попадать в seller_profiles вовсе."""
    listings = [_listing(str(i), "Хозяин") for i in range(5)] + [_listing("real1", "Айгуль")]
    profiles = _aggregate(listings, {}, NOW)
    names = {p["seller_name"] for p in profiles}
    assert "хозяин" not in names
    assert "айгуль" in names
    assert all(n not in _GENERIC_NAME_STOPLIST for n in names)


def test_relist_rate_and_price_cut_rate():
    listings = [
        _listing("1", "Иван Риелтор", relisted_within_60d=True),
        _listing("2", "Иван Риелтор", relisted_within_60d=False),
        _listing("3", "Иван Риелтор", relisted_within_60d=None),
        _listing("4", "Иван Риелтор", relisted_within_60d=False),
    ]
    cuts = {"2": [{"old_price": 10_000_000, "new_price": 9_500_000, "changed_at": NOW}]}
    profiles = _aggregate(listings, cuts, NOW)
    p = profiles[0]
    assert p["total_listings_count"] == 4
    assert p["relist_count"] == 1
    assert p["relist_rate"] == pytest.approx(0.25)
    assert p["price_cut_count"] == 1
    assert p["price_cut_rate"] == pytest.approx(0.25)


def test_median_discount_pct_uses_statistics_median():
    listings = [
        _listing("1", "Продавец Тест", bargain_discount_pct=2.0),
        _listing("2", "Продавец Тест", bargain_discount_pct=8.0),
        _listing("3", "Продавец Тест", bargain_discount_pct=20.0),
    ]
    profiles = _aggregate(listings, {}, NOW)
    assert profiles[0]["median_discount_pct"] == pytest.approx(8.0)


def test_avg_days_to_sell_only_counts_resolved_time_on_market():
    listings = [
        _listing("1", "Продавец Тест", time_on_market=10),
        _listing("2", "Продавец Тест", time_on_market=30),
        _listing("3", "Продавец Тест", time_on_market=None),  # ещё активно — не censored, не в среднем
    ]
    profiles = _aggregate(listings, {}, NOW)
    assert profiles[0]["avg_days_to_sell"] == pytest.approx(20.0)


def test_is_high_relist_rate_needs_min_sample_size():
    """relist_rate>0.3 на выборке из 1 объявления — шум одного случая, не
    паттерн (анти-шумовой порог total_listings_count>=3, см. докстринг
    migrations/077_seller_profiles.sql)."""
    one_listing = [_listing("1", "Одиночка", relisted_within_60d=True)]
    assert _aggregate(one_listing, {}, NOW)[0]["is_high_relist_rate"] is False

    three_listings = [
        _listing("1", "Частый Релистер", relisted_within_60d=True),
        _listing("2", "Частый Релистер", relisted_within_60d=True),
        _listing("3", "Частый Релистер", relisted_within_60d=False),
    ]
    assert _aggregate(three_listings, {}, NOW)[0]["is_high_relist_rate"] is True


def test_is_motivated_seller_needs_two_cuts_within_30_days():
    old_cut = NOW - timedelta(days=45)
    recent_cut = NOW - timedelta(days=5)
    listings = [_listing("1", "Мотивированный"), _listing("2", "Мотивированный")]

    cuts_one_recent = {
        "1": [{"old_price": 10_000_000, "new_price": 9_000_000, "changed_at": recent_cut}],
        "2": [{"old_price": 10_000_000, "new_price": 9_000_000, "changed_at": old_cut}],
    }
    assert _aggregate(listings, cuts_one_recent, NOW)[0]["is_motivated_seller"] is False

    cuts_two_recent = {
        "1": [{"old_price": 10_000_000, "new_price": 9_000_000, "changed_at": recent_cut}],
        "2": [{"old_price": 10_000_000, "new_price": 9_000_000, "changed_at": recent_cut}],
    }
    assert _aggregate(listings, cuts_two_recent, NOW)[0]["is_motivated_seller"] is True


def test_active_listings_count_treats_null_as_active():
    """is_active=NULL (не проставлен) — считаем активным, тот же принцип
    'IS NOT FALSE', что уже используется в остальном проекте (например
    complex_walkability_snapshot.py при выборе complexes)."""
    listings = [
        _listing("1", "Тест Активность", is_active=True),
        _listing("2", "Тест Активность", is_active=None),
        _listing("3", "Тест Активность", is_active=False),
    ]
    assert _aggregate(listings, {}, NOW)[0]["active_listings_count"] == 2
