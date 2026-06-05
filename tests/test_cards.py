"""Tests for bot/core/cards.py"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bot.core.cards import build_card_text, build_card_keyboard


# ── build_card_text ───────────────────────────────────────────────────────────

def _listing(**kwargs):
    defaults = {
        "id": "test-1",
        "price": 250000,
        "deal_type": "rent",
        "area": 65.0,
        "rooms": 2,
        "floor": 5,
        "floors_total": 12,
        "district": "Алмалинский",
        "sources": ["krisha.kz"],
        "published_at": "сегодня",
        "photo_url": "https://example.com/photo.jpg",
        "url": "https://krisha.kz/a/show/123",
    }
    defaults.update(kwargs)
    return defaults


def test_card_text_contains_price():
    text = build_card_text(_listing(price=250000, deal_type="rent"))
    assert "250" in text
    assert "₸" in text


def test_card_text_rent_suffix():
    text = build_card_text(_listing(deal_type="rent"))
    assert "мес" in text


def test_card_text_buy_suffix():
    text = build_card_text(_listing(deal_type="buy", price=50_000_000))
    assert "мес" not in text
    assert "₸" in text


def test_card_text_contains_area():
    text = build_card_text(_listing(area=65.0))
    assert "65" in text
    assert "м²" in text


def test_card_text_contains_floor():
    text = build_card_text(_listing(floor=5, floors_total=12))
    assert "5/12" in text


def test_card_text_contains_rooms():
    text = build_card_text(_listing(rooms=2))
    assert "2" in text
    assert "комн" in text


def test_card_text_4plus_rooms():
    text = build_card_text(_listing(rooms=5))
    assert "4+" in text


def test_card_text_contains_source():
    text = build_card_text(_listing(sources=["krisha.kz"]))
    assert "krisha.kz" in text


def test_card_text_no_photo_no_crash():
    text = build_card_text(_listing(photo_url=None))
    assert isinstance(text, str)


def test_card_text_no_price():
    text = build_card_text(_listing(price=None))
    assert "не указана" in text.lower() or "₸" not in text


def test_card_text_no_district_falls_back_to_address():
    text = build_card_text(_listing(district=None, address="ул. Ленина 5"))
    assert "Ленина" in text or "ленина" in text.lower()


def test_card_text_with_prefs_shows_reasons():
    prefs = {
        "budget_min": 200000,
        "budget_max": 300000,
        "rooms": ["2"],
        "area_min": 50.0,
        "district": "алмалинский",
    }
    text = build_card_text(_listing(), prefs=prefs)
    assert "Почему подходит" in text


def test_card_text_without_prefs_no_reasons():
    text = build_card_text(_listing(), prefs=None)
    assert "Почему подходит" not in text


def test_card_text_handles_empty_listing():
    text = build_card_text({"id": "x"})
    assert isinstance(text, str)
    assert len(text) > 0


# ── build_card_keyboard ───────────────────────────────────────────────────────

def test_keyboard_has_four_buttons():
    kb = build_card_keyboard("listing-123")
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert len(buttons) == 4


def test_keyboard_favorite_callback():
    kb = build_card_keyboard("listing-123")
    data_values = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "fav:listing-123" in data_values


def test_keyboard_skip_callback():
    kb = build_card_keyboard("listing-123")
    data_values = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "skip:listing-123" in data_values


def test_keyboard_follow_callback():
    kb = build_card_keyboard("listing-123")
    data_values = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "follow:listing-123" in data_values


def test_keyboard_contact_callback():
    kb = build_card_keyboard("listing-123")
    data_values = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "contact:listing-123" in data_values


def test_keyboard_uses_listing_id():
    listing_id = "abc-xyz-789"
    kb = build_card_keyboard(listing_id)
    for row in kb.inline_keyboard:
        for btn in row:
            assert listing_id in btn.callback_data
