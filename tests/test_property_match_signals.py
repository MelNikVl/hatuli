"""Регрессия для задачи 2026-08-16 ("Property Identity v2: read-only
аудит сильных сигналов") — scripts/audit_property_match_signals.py.
Чистые тесты (синтетические dict, БЕЗ БД) — build_pair_evidence/
classify_tier не трогают сеть/БД."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _row(id, address, floor=5, area=45.0, rooms=2, seller="Продавец А", price=10_000_000,
         first_seen=None, archived_at=None, complex_id=1, description=None, photos=None,
         lat=None, lon=None, is_duplicate=False, duplicate_of=None):
    return {
        "id": id, "address": address, "floor": floor, "area": area, "rooms": rooms,
        "seller_name": seller, "price": price,
        "first_seen": first_seen or (NOW - timedelta(days=30)), "last_seen": NOW,
        "archived_at": archived_at, "complex_id": complex_id, "description": description,
        "photos": photos, "lat": lat, "lon": lon,
        "is_duplicate": is_duplicate, "duplicate_of": duplicate_of,
    }


# ── 1. Разные квартиры с одинаковым address_hash (rooms mismatch) ──────

def test_rooms_mismatch_within_same_hash_is_rejected():
    from audit_property_match_signals import build_pair_evidence, classify_tier

    a = _row("L1", "Адрес А, 1", rooms=2)
    b = _row("L2", "Адрес А, 1", rooms=3)  # тот же адрес+этаж+площадь, ДРУГОЕ число комнат
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    tier, reasons = classify_tier(ev)
    assert tier == "rejected"
    assert any("rooms" in r for r in reasons)


# ── 2. Одинаковая квартира с новым listing_id (честный relist) ─────────

def test_clean_relist_same_everything_no_overlap_is_strong_candidate():
    from audit_property_match_signals import build_pair_evidence, classify_tier

    a = _row("L1", "Адрес Б, 5", rooms=2, seller="Продавец Б", price=10_000_000,
             first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40))
    b = _row("L2", "Адрес Б, 5", rooms=2, seller="Продавец Б", price=10_000_000,
             first_seen=NOW - timedelta(days=20))
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    tier, reasons = classify_tier(ev)
    assert tier == "strong_candidate"
    assert ev["house_number_equal"] is True
    assert ev["no_temporal_overlap"] is True


# ── 3. Конфликт номера дома ──────────────────────────────────────────────

def test_house_number_mismatch_is_rejected():
    from audit_property_match_signals import build_pair_evidence, classify_tier

    a = _row("L1", "Кабанбай батыра, 15", rooms=2)
    b = _row("L2", "Кабанбай батыра, 27", rooms=2)  # разный номер дома
    ev = build_pair_evidence(a, b, "fuzzy_candidate", area_diff=0.5)
    tier, reasons = classify_tier(ev)
    assert tier == "rejected"
    assert any("номер дома" in r for r in reasons)


# ── 4. Одновременная активность ──────────────────────────────────────────

def test_simultaneously_active_with_different_seller_is_review_required():
    from audit_property_match_signals import build_pair_evidence, classify_tier

    overlap_start = NOW - timedelta(days=10)
    a = _row("L1", "Адрес В, 3", seller="Продавец А", first_seen=overlap_start, archived_at=None)
    b = _row("L2", "Адрес В, 3", seller="Продавец Б", first_seen=overlap_start, archived_at=None)
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["simultaneously_active"] is True
    assert ev["seller_equal"] is False
    tier, reasons = classify_tier(ev)
    assert tier == "review_required"
    assert any("конфликтующие" in r for r in reasons)


# ── 5. Совпадение фотографий ─────────────────────────────────────────────

def test_photo_overlap_detected_and_counts_as_positive_signal():
    from audit_property_match_signals import build_pair_evidence, classify_tier

    shared_photos = [
        "https://krisha-photos.kcdn.online/webp/aa/aaaaaaaa-1111-2222-3333-444444444444/1-full.jpg",
    ]
    a = _row("L1", "Адрес Г, 4", seller="Продавец Г", photos=shared_photos,
             first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40))
    b = _row("L2", "Адрес Г, 4", seller="Продавец Г", photos=shared_photos,
             first_seen=NOW - timedelta(days=20))
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["photo_overlap"] is True
    tier, reasons = classify_tier(ev)
    assert tier == "strong_candidate"


def test_different_photos_no_overlap():
    from audit_property_match_signals import build_pair_evidence

    a = _row("L1", "Адрес Д, 5", photos=["https://x/aaaaaaaa-1111-2222-3333-444444444444/1.jpg"])
    b = _row("L2", "Адрес Д, 5", photos=["https://x/bbbbbbbb-5555-6666-7777-888888888888/1.jpg"])
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["photo_overlap"] is False


# ── 6. Отсутствие фото ────────────────────────────────────────────────────

def test_missing_photos_gives_none_not_false():
    """Задача: "отсутствие фото" — отдельный тестовый случай. None (не
    знаем), НЕ False (это было бы ложным отрицательным сигналом —
    Unknown ≠ average)."""
    from audit_property_match_signals import build_pair_evidence

    a = _row("L1", "Адрес Е, 6", photos=None)
    b = _row("L2", "Адрес Е, 6", photos=["https://x/aaaaaaaa-1111-2222-3333-444444444444/1.jpg"])
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["photo_overlap"] is None

    ev_both_none = build_pair_evidence(a, _row("L3", "Адрес Е, 6", photos=None), "exact_hash", area_diff=0.0)
    assert ev_both_none["photo_overlap"] is None


# ── 7. Существующий unit/dedup candidate (is_duplicate cross-check) ─────

def test_existing_dedup_listings_confirmation_forces_strong_candidate():
    """bot/core/dedup_listings.py — уже ЖИВОЙ, независимый механизм
    (is_duplicate/duplicate_of). Если он УЖЕ подтвердил пару — это
    сильнее любого нашего собственного сигнала, даже при конфликтующих
    прочих полях (например разные продавцы — dedup_listings ловит
    именно "от хозяина vs от риелтора" одного и того же жилья)."""
    from audit_property_match_signals import build_pair_evidence, classify_tier

    a = _row("L1", "Адрес Ж, 7", seller="Хозяин", is_duplicate=False, duplicate_of=None)
    b = _row("L2", "Адрес Ж, 7", seller="Риелтор Иван", is_duplicate=True, duplicate_of="L1")
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["already_confirmed_by_dedup_listings"] is True
    tier, reasons = classify_tier(ev)
    assert tier == "strong_candidate"
    assert "dedup_listings" in reasons[0]


def test_no_existing_dedup_confirmation_does_not_force_strong():
    from audit_property_match_signals import build_pair_evidence

    a = _row("L1", "Адрес З, 8", is_duplicate=False, duplicate_of=None)
    b = _row("L2", "Адрес З, 8", is_duplicate=False, duplicate_of=None)
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["already_confirmed_by_dedup_listings"] is False


# ── Доп. содержательные тесты ────────────────────────────────────────────

def test_severe_price_difference_is_rejected():
    from audit_property_match_signals import build_pair_evidence, classify_tier

    a = _row("L1", "Адрес И, 9", price=10_000_000)
    b = _row("L2", "Адрес И, 9", price=20_000_000)  # +100%, далеко за severe-порогом
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["price_severely_different"] is True
    tier, reasons = classify_tier(ev)
    assert tier == "rejected"


def test_never_returns_confirmed_tier():
    """Задача, п.4: "Не использовать слово confirmed" — структурная
    проверка набора допустимых значений tier."""
    from audit_property_match_signals import classify_tier

    valid_tiers = {"rejected", "weak_candidate", "strong_candidate", "review_required"}
    samples = [
        {"rooms_equal": False, "house_number_equal": None, "price_severely_different": False,
         "already_confirmed_by_dedup_listings": False, "simultaneously_active": None, "seller_equal": None,
         "rooms_a": 1, "rooms_b": 2, "house_number_a": None, "house_number_b": None,
         "price_diff_pct": None},
        {"rooms_equal": True, "house_number_equal": True, "price_severely_different": False,
         "already_confirmed_by_dedup_listings": False, "simultaneously_active": False, "seller_equal": True,
         "description_similar": None, "photo_overlap": None, "coords_equal": None, "price_similar": True,
         "no_temporal_overlap": True},
    ]
    for ev in samples:
        for k in ("description_similar", "photo_overlap", "coords_equal", "price_similar", "no_temporal_overlap"):
            ev.setdefault(k, None)
        tier, _ = classify_tier(ev)
        assert tier in valid_tiers
        assert tier != "confirmed"


def test_missing_rooms_does_not_reject():
    """rooms неизвестны у одной из сторон -> rooms_equal=None, НЕ False
    -> не reject (Unknown ≠ average)."""
    from audit_property_match_signals import build_pair_evidence, classify_tier

    a = _row("L1", "Адрес К, 10", rooms=None, seller="Продавец К",
             first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40))
    b = _row("L2", "Адрес К, 10", rooms=2, seller="Продавец К",
             first_seen=NOW - timedelta(days=20))
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["rooms_equal"] is None
    tier, reasons = classify_tier(ev)
    assert tier != "rejected"


def test_coords_within_tolerance_is_positive_signal():
    from audit_property_match_signals import build_pair_evidence

    a = _row("L1", "Адрес Л, 11", lat=51.1801, lon=71.4460)
    b = _row("L2", "Адрес Л, 11", lat=51.1802, lon=71.4461)  # ~15м разница
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["coords_equal"] is True


def test_coords_far_apart_not_equal():
    from audit_property_match_signals import build_pair_evidence

    a = _row("L1", "Адрес М, 12", lat=51.1801, lon=71.4460)
    b = _row("L2", "Адрес М, 12", lat=51.2000, lon=71.5000)  # далеко
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["coords_equal"] is False


def test_description_similarity_computed_via_ratio():
    from audit_property_match_signals import build_pair_evidence

    a = _row("L1", "Адрес Н, 13", description="Продаётся уютная квартира с ремонтом у метро")
    b = _row("L2", "Адрес Н, 13", description="Продаётся уютная квартира с ремонтом рядом с метро")
    ev = build_pair_evidence(a, b, "exact_hash", area_diff=0.0)
    assert ev["description_similarity"] is not None
    assert ev["description_similarity"] > 0.8
    assert ev["description_similar"] is True


# ── 8. Никаких записей в БД (read-only гарантия) ────────────────────────

def test_module_has_no_write_sql():
    """Тот же AST-based guard, что в предыдущих аудитах этой ветки задач
    (см. tests/test_seller_profile_property_id_audit.py) — не substring
    по всему файлу (докстринг documented этот же принцип прозой)."""
    import ast
    import audit_property_match_signals as audit_module

    src = open(audit_module.__file__, encoding="utf-8").read()
    tree = ast.parse(src)

    call_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                call_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                call_names.add(func.attr)
        elif isinstance(node, ast.ImportFrom) and node.module == "bot.db.pg":
            call_names |= {alias.name for alias in node.names}
    assert "execute" not in call_names

    write_verbs = ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            assert not any(v in upper for v in write_verbs), node.value
