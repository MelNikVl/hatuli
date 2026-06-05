"""Tests for bot/core/dedup.py"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bot.core.dedup import (
    normalize_address,
    deduplicate,
    _canonical_fields,
    _fields_match_count,
    _hash_distance,
)


# ── normalize_address ─────────────────────────────────────────────────────────

def test_normalize_strips_prefixes():
    result = normalize_address("ул. Ленина, д. 5")
    assert "ул" not in result
    assert "д" not in result
    assert "ленина" in result
    assert "5" in result


def test_normalize_lowercases():
    assert normalize_address("Проспект Достык") == normalize_address("проспект достык")


def test_normalize_punctuation():
    result = normalize_address("Алматы, пр. Достык, 100")
    assert "," not in result


def test_normalize_empty():
    assert normalize_address("") == ""
    assert normalize_address(None) == ""  # type: ignore[arg-type]


def test_normalize_microdistrict():
    result = normalize_address("мкр Алатау, д. 12")
    assert "мкр" not in result
    assert "алатау" in result


# ── _hash_distance ────────────────────────────────────────────────────────────

def test_hash_distance_identical():
    assert _hash_distance("abcd1234", "abcd1234") == 0


def test_hash_distance_one_diff():
    assert _hash_distance("abcd1234", "abcd1235") == 1


def test_hash_distance_none_on_missing():
    assert _hash_distance(None, "abcd") is None
    assert _hash_distance("abcd", None) is None


def test_hash_distance_none_on_length_mismatch():
    assert _hash_distance("abc", "abcd") is None


# ── _fields_match_count ───────────────────────────────────────────────────────

def test_fields_match_count_all_match():
    a = {"phone": "7771234567", "area": 55.0, "price": 200000, "floor": 5, "rooms": 2, "address": "ленина 5", "complex_name": ""}
    b = dict(a)
    count = _fields_match_count(a, b)
    assert count >= 4  # phone, area, price, floor, rooms, address


def test_fields_match_count_none_ignored():
    a = {"phone": None, "area": None, "price": 200000, "floor": 5, "rooms": 2, "address": None, "complex_name": None}
    b = {"phone": None, "area": None, "price": 200000, "floor": 5, "rooms": 2, "address": None, "complex_name": None}
    count = _fields_match_count(a, b)
    assert count == 3  # price, floor, rooms


def test_fields_match_count_no_match():
    a = {"phone": "111", "area": 40.0, "price": 100000}
    b = {"phone": "222", "area": 60.0, "price": 200000}
    assert _fields_match_count(a, b) == 0


# ── deduplicate ───────────────────────────────────────────────────────────────

def _listing(**kwargs):
    defaults = {
        "id": "1",
        "url": "https://example.com/1",
        "price": 200000,
        "area": 55.0,
        "rooms": 2,
        "floor": 5,
        "floors_total": 12,
        "address": "Ленина 5",
        "district": "Центр",
        "phone": "7771234567",
        "complex_name": "",
        "photo_url": None,
        "photo_hash": None,
        "sources": ["krisha.kz"],
    }
    defaults.update(kwargs)
    return defaults


def test_deduplicate_empty():
    assert deduplicate([]) == []


def test_deduplicate_single():
    listings = [_listing(id="1")]
    result = deduplicate(listings)
    assert len(result) == 1
    assert result[0]["sources"] == ["krisha.kz"]


def test_deduplicate_exact_duplicate():
    l1 = _listing(id="1", sources=["krisha.kz"])
    l2 = _listing(id="2", sources=["olx.kz"])  # same data, different id/source
    result = deduplicate([l1, l2])
    assert len(result) == 1
    assert set(result[0]["sources"]) == {"krisha.kz", "olx.kz"}


def test_deduplicate_no_duplicates():
    l1 = _listing(id="1", price=200000, phone="7771111111")
    l2 = _listing(id="2", price=350000, phone="7772222222", area=80.0, rooms=3, floor=8)
    result = deduplicate([l1, l2])
    assert len(result) == 2


def test_deduplicate_image_hash_close():
    l1 = _listing(id="1", phone="0000000001", photo_hash="a" * 16)
    l2 = _listing(id="2", phone="0000000002", photo_hash="a" * 15 + "b")  # distance=1
    result = deduplicate([l1, l2])
    assert len(result) == 1


def test_deduplicate_image_hash_far():
    # Use distinct values so fewer than 3 fields match
    l1 = _listing(id="1", phone=None, area=None, floor=None, rooms=1,
                  price=100000, address="Уникальный адрес 1", photo_hash="0" * 16)
    l2 = _listing(id="2", phone=None, area=None, floor=None, rooms=3,
                  price=999999, address="Уникальный адрес 2", photo_hash="f" * 16)  # max distance
    result = deduplicate([l1, l2])
    # Only complex_name="" matches (→ None after normalization, so skipped) → 0 fields → not a dup
    assert len(result) == 2


def test_deduplicate_merges_sources_dedup():
    l1 = _listing(id="1", sources=["krisha.kz", "olx.kz"])
    l2 = _listing(id="2", sources=["krisha.kz"])  # duplicate source
    result = deduplicate([l1, l2])
    assert len(result) == 1
    # sources deduplicated
    assert result[0]["sources"].count("krisha.kz") == 1


def test_deduplicate_three_listings_two_dups():
    l1 = _listing(id="1", sources=["a"])
    l2 = _listing(id="2", sources=["b"])  # dup of l1
    l3 = _listing(id="3", price=999999, phone="0000000099", rooms=3, area=100.0, floor=10, sources=["c"])  # unique
    result = deduplicate([l1, l2, l3])
    assert len(result) == 2
    sources_combined = {s for r in result for s in r["sources"]}
    assert "a" in sources_combined
    assert "b" in sources_combined
    assert "c" in sources_combined
