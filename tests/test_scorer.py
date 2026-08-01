"""Tests for bot/core/scorer.py"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

import pytest
from bot.core.scorer import (
    score,
    top_positive_reasons,
    _budget_score,
    _district_score,
    _rooms_score,
    _area_score,
    _suspicious_score,
    _age_score,
)


# ── _budget_score ─────────────────────────────────────────────────────────────

def test_budget_exact_match():
    pts, reasons = _budget_score(200000, {"budget_min": 150000, "budget_max": 250000})
    assert pts == 20
    assert any("точно в бюджете" in r.lower() for r in reasons)


def test_budget_within_range():
    pts, reasons = _budget_score(220000, {"budget_min": 150000, "budget_max": 250000})
    assert pts >= 10


def test_budget_within_15pct_over():
    pts, reasons = _budget_score(270000, {"budget_max": 250000})
    assert pts == -10


def test_budget_over_15pct_30pct():
    pts, reasons = _budget_score(310000, {"budget_max": 250000})
    assert pts == -25


def test_budget_over_30pct():
    pts, reasons = _budget_score(400000, {"budget_max": 250000})
    assert pts <= -25


def test_budget_no_max():
    pts, reasons = _budget_score(500000, {"budget_min": None})
    assert pts == 0


def test_budget_no_price():
    pts, _ = _budget_score(None, {"budget_max": 200000})
    assert pts == 0


# ── _district_score ───────────────────────────────────────────────────────────

def test_district_match():
    listing = {"district": "Алмалинский район"}
    prefs = {"district": "алмалинский"}
    pts, reasons = _district_score(listing, prefs)
    assert pts == 15
    assert reasons


def test_district_mismatch():
    listing = {"district": "Бостандыкский район"}
    prefs = {"district": "алмалинский"}
    pts, _ = _district_score(listing, prefs)
    assert pts == -10


def test_district_no_pref():
    listing = {"district": "Что-то"}
    prefs = {}
    pts, _ = _district_score(listing, prefs)
    assert pts == 0


# ── _rooms_score ──────────────────────────────────────────────────────────────

def test_rooms_exact_match():
    listing = {"rooms": 2}
    prefs = {"rooms": ["2", "3"]}
    pts, reasons = _rooms_score(listing, prefs)
    assert pts == 15


def test_rooms_mismatch():
    listing = {"rooms": 1}
    prefs = {"rooms": ["2", "3"]}
    pts, _ = _rooms_score(listing, prefs)
    assert pts == -30


def test_rooms_4plus_match():
    listing = {"rooms": 5}
    prefs = {"rooms": ["4+"]}
    pts, _ = _rooms_score(listing, prefs)
    assert pts == 15


def test_rooms_4plus_no_match():
    listing = {"rooms": 2}
    prefs = {"rooms": ["4+"]}
    pts, _ = _rooms_score(listing, prefs)
    assert pts == -30


def test_rooms_no_pref():
    listing = {"rooms": 2}
    prefs = {}
    pts, _ = _rooms_score(listing, prefs)
    assert pts == 0


def test_rooms_no_listing_rooms():
    listing = {}
    prefs = {"rooms": ["2"]}
    pts, _ = _rooms_score(listing, prefs)
    assert pts == 0


# ── _area_score ───────────────────────────────────────────────────────────────

def test_area_meets_minimum():
    listing = {"area": 60.0}
    prefs = {"area_min": 50.0}
    pts, _ = _area_score(listing, prefs)
    assert pts == 10


def test_area_below_minimum():
    listing = {"area": 40.0}
    prefs = {"area_min": 50.0}
    pts, reasons = _area_score(listing, prefs)
    assert pts < 0
    assert any("меньше" in r.lower() for r in reasons)


def test_area_no_pref():
    listing = {"area": 60.0}
    prefs = {}
    pts, _ = _area_score(listing, prefs)
    assert pts == 0


def test_area_no_listing_area():
    listing = {}
    prefs = {"area_min": 50.0}
    pts, _ = _area_score(listing, prefs)
    assert pts == 0


# ── _suspicious_score ─────────────────────────────────────────────────────────

def test_suspicious_no_photo():
    listing = {"photo_url": None}
    pts, reasons = _suspicious_score(listing)
    assert pts < 0
    assert any("фото" in r.lower() for r in reasons)


def test_suspicious_has_photo():
    listing = {"photo_url": "https://example.com/img.jpg"}
    pts, _ = _suspicious_score(listing)
    assert pts == 0


def test_suspicious_very_low_price():
    listing = {"photo_url": "https://x.com/img.jpg", "price": 10000, "area": 60.0}
    pts, reasons = _suspicious_score(listing)
    assert pts <= -25


# ── _age_score ────────────────────────────────────────────────────────────────

def test_age_score_old_listing():
    old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    pts, reasons = _age_score({"published_at": old_date})
    assert pts == -5
    assert reasons


def test_age_score_fresh_listing():
    fresh_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    pts, reasons = _age_score({"published_at": fresh_date})
    assert pts == 0
    assert not reasons


def test_age_score_no_date():
    pts, reasons = _age_score({})
    assert pts == 0


# ── score (integration) ───────────────────────────────────────────────────────

def test_score_perfect_listing():
    listing = {
        "price": 200000,
        "area": 65.0,
        "rooms": 2,
        "district": "Алмалинский",
        "photo_url": "https://example.com/img.jpg",
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    prefs = {
        "budget_min": 150000,
        "budget_max": 250000,
        "district": "алмалинский",
        "rooms": ["2"],
        "area_min": 55.0,
    }
    total, reasons = score(listing, prefs)
    assert total > 40
    assert len(reasons) > 0


def test_score_bad_listing():
    listing = {
        "price": 500000,  # way over budget
        "area": 30.0,     # below area min
        "rooms": 1,       # wrong rooms
        "district": "Другой",
        "photo_url": None,
    }
    prefs = {
        "budget_max": 200000,
        "district": "алмалинский",
        "rooms": ["2", "3"],
        "area_min": 55.0,
    }
    total, reasons = score(listing, prefs)
    assert total < 0


def test_score_handles_none_gracefully():
    """score() should never raise even with completely empty dicts."""
    total, reasons = score({}, {})
    assert isinstance(total, int)
    assert isinstance(reasons, list)


def test_score_returns_reasons_list():
    listing = {"price": 200000, "rooms": 2, "area": 60.0}
    prefs = {"budget_max": 200000, "rooms": ["2"], "area_min": 50.0}
    total, reasons = score(listing, prefs)
    assert isinstance(reasons, list)
    for r in reasons:
        assert isinstance(r, str)


# ── top_positive_reasons ──────────────────────────────────────────────────────

def test_top_positive_reasons_filters_negatives():
    reasons = [
        "Цена в бюджете",
        "Не подходит по комнатам",
        "Район совпадает",
        "Значительно выше бюджета",
        "Площадь соответствует",
    ]
    result = top_positive_reasons(reasons, n=3)
    assert len(result) <= 3
    assert all("Не подход" not in r and "Значительно" not in r for r in result)


def test_top_positive_reasons_respects_n():
    reasons = ["Причина 1", "Причина 2", "Причина 3", "Причина 4"]
    assert len(top_positive_reasons(reasons, n=2)) == 2


def test_top_positive_reasons_empty():
    assert top_positive_reasons([]) == []
