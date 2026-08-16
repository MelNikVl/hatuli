"""Регрессия для scripts/audit_address_hash_exact.py (задача 2026-08-16,
"безопасный exact-only property linker", п.1) — чистые тесты на
group_by_exact_hash/audit_exact_clusters, без БД."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _row(id, address, floor, area, rooms=2, complex_name="ЖК Тест", seller_name="Продавец А",
         is_active=True, first_seen=None, archived_at=None):
    return {
        "id": id, "address": address, "floor": floor, "area": area, "rooms": rooms,
        "complex_name": complex_name, "seller_name": seller_name, "is_active": is_active,
        "first_seen": first_seen or (NOW - timedelta(days=10)), "last_seen": NOW,
        "archived_at": archived_at, "price": 10_000_000, "lat": None, "lon": None,
    }


def test_group_by_exact_hash_groups_identical_address_floor_area():
    from audit_address_hash_exact import group_by_exact_hash

    rows = [
        _row("L1", "Адрес А, 1", 5, 45.0),
        _row("L2", "Адрес А, 1", 5, 45.0),
        _row("L3", "Адрес Б, 2", 5, 45.0),
    ]
    by_hash = group_by_exact_hash(rows)
    assert len(by_hash) == 2
    sizes = sorted(len(v) for v in by_hash.values())
    assert sizes == [1, 2]


def test_rooms_differ_flagged_within_same_exact_hash():
    """Прямое доказательство задачи п.1: rooms НЕ входит в хэш -> два
    listing с разным rooms МОГУТ получить один address_hash."""
    from audit_address_hash_exact import group_by_exact_hash, audit_exact_clusters

    rows = [
        _row("L1", "Адрес А, 1", 5, 45.0, rooms=2),
        _row("L2", "Адрес А, 1", 5, 45.0, rooms=3),  # ДРУГОЕ число комнат, ТОТ ЖЕ hash
    ]
    result = audit_exact_clusters(rows)
    assert result["rooms_differ_clusters"] == 1
    assert result["hashes_with_2plus_listings"] == 1


def test_simultaneously_active_and_different_seller_flagged_high_risk():
    from audit_address_hash_exact import audit_exact_clusters

    rows = [
        _row("L1", "Адрес А, 1", 5, 45.0, seller_name="Продавец А", is_active=True,
             first_seen=NOW - timedelta(days=5), archived_at=None),
        _row("L2", "Адрес А, 1", 5, 45.0, seller_name="Продавец Б", is_active=True,
             first_seen=NOW - timedelta(days=5), archived_at=None),
    ]
    result = audit_exact_clusters(rows)
    top = result["top50_suspicious_exact_clusters"][0]
    assert top["risk"] == "high"
    assert top["distinct_seller_identities"] == 2
    assert top["simultaneously_active"] is True


def test_single_listing_hashes_not_counted_as_clusters():
    from audit_address_hash_exact import audit_exact_clusters

    rows = [_row("L1", "Уникальный адрес", 5, 45.0)]
    result = audit_exact_clusters(rows)
    assert result["hashes_with_2plus_listings"] == 0
    assert result["total_distinct_hashes"] == 1
    assert result["top50_suspicious_exact_clusters"] == []


def test_low_risk_when_relist_sequential_same_seller():
    from audit_address_hash_exact import audit_exact_clusters

    rows = [
        _row("L1", "Адрес А, 1", 5, 45.0, seller_name="Продавец А",
             first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40)),
        _row("L2", "Адрес А, 1", 5, 45.0, seller_name="Продавец А",
             first_seen=NOW - timedelta(days=20), archived_at=None),
    ]
    result = audit_exact_clusters(rows)
    top = result["top50_suspicious_exact_clusters"][0]
    assert top["risk"] == "low"


def test_cluster_size_distribution_and_max():
    from audit_address_hash_exact import audit_exact_clusters

    rows = [_row(f"L{i}", "Адрес А, 1", 5, 45.0) for i in range(6)]  # 1 кластер, 6 listing
    result = audit_exact_clusters(rows)
    assert result["max_cluster_size"] == 6
    assert result["cluster_size_distribution"] == {"6-10": 1}
