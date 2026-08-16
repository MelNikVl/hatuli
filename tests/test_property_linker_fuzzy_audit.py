"""Регрессия для задачи 2026-08-16 ("аудит property linker fuzzy
matching") — scripts/audit_property_linker_fuzzy.py. Все тесты ЧИСТЫЕ
(синтетические списки dict, БЕЗ БД/сети) — simulate_linking() полностью
в памяти, тот же приём, что tests/test_seller_profile_property_id_audit.py
для аналогичного аудита."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _row(id, address, floor, area, rooms=2, complex_id=1, seller_name="Продавец А",
         is_active=True, first_seen=None, archived_at=None, price=10_000_000):
    return {
        "id": id, "address": address, "floor": floor, "area": area, "rooms": rooms,
        "complex_id": complex_id, "seller_name": seller_name, "is_active": is_active,
        "first_seen": first_seen or (NOW - timedelta(days=30)),
        "last_seen": NOW, "archived_at": archived_at,
        "price": price, "lat": None, "lon": None,
    }


# ── 1. Две разные квартиры одинаковой площади на одном этаже ───────────

def test_two_different_apartments_same_area_same_floor_get_fuzzy_merged():
    """Демонстрирует ИМЕННО тот риск, ради которого аудит затеян: разный
    адрес (разные квартиры реально), одинаковый этаж+ЖК+похожая площадь
    -> текущий алгоритм (RULES["A_baseline"]) их СКЛЕИВАЕТ."""
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Кабанбай батыра, 15", 5, 45.0),
        _row("L2", "Момышулы, 22", 5, 45.5),  # ДРУГОЙ адрес, тот же этаж/ЖК/похожая площадь
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    assert sim["stats"]["auto_new"] == 1
    assert sim["stats"]["fuzzy"] == 1
    assert sim["assignments"]["L1"] == sim["assignments"]["L2"]  # склеены — вот риск


def test_rule_C_house_number_separates_them_when_address_parseable():
    """Тот же сценарий, но с правилом C (номер дома должен совпадать) —
    "15" != "22" -> НЕ склеиваются, каждая получает свою property."""
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Кабанбай батыра, 15", 5, 45.0),
        _row("L2", "Момышулы, 22", 5, 45.5),
    ]
    sim = simulate_linking(rows, RULES["C_house_number"])
    assert sim["stats"]["auto_new"] == 2
    assert sim["stats"]["fuzzy"] == 0
    assert sim["assignments"]["L1"] != sim["assignments"]["L2"]


# ── 2. Несколько возможных кандидатов ───────────────────────────────────

def test_multiple_candidates_counted_as_ambiguous():
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Адрес А", 5, 45.0),   # создаёт кластер 1 (anchor 45.0)
        _row("L2", "Адрес Б", 5, 46.1),   # diff от L1 = 1.1 > tolerance(1.0) -> создаёт кластер 2 (anchor 46.1)
        _row("L3", "Адрес В", 5, 45.5),   # в допуске ОТ ОБОИХ: |45.5-45.0|=0.5, |45.5-46.1|=0.6 -> 2 кандидата
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    assert sim["ambiguous_candidate_attempts"] == 1
    l3_event = next(e for e in sim["fuzzy_events"] if e["listing_id"] == "L3")
    assert l3_event["candidates_count"] == 2


def test_rule_E_single_candidate_only_rejects_ambiguous_match():
    """Правило E: 2+ кандидата -> НЕ матчим вовсе (создаём новую
    property), не выбираем "ближайшего" молча."""
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Адрес А", 5, 45.0),
        _row("L2", "Адрес Б", 5, 46.1),
        _row("L3", "Адрес В", 5, 45.5),
    ]
    sim = simulate_linking(rows, RULES["E_single_candidate"])
    assert sim["assignments"]["L3"] not in (sim["assignments"]["L1"], sim["assignments"]["L2"])
    assert sim["stats"]["auto_new"] == 3  # L3 тоже стала новой property, не fuzzy


# ── 3. Rooms mismatch ────────────────────────────────────────────────────

def test_rooms_mismatch_flagged_high_risk():
    from audit_property_linker_fuzzy import simulate_linking, RULES, score_risk

    rows = [
        _row("L1", "Адрес А", 5, 45.0, rooms=2),
        _row("L2", "Адрес Б", 5, 45.5, rooms=3),  # ДРУГОЕ число комнат
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    event = sim["fuzzy_events"][0]
    assert "rooms" in event["differing_features"]
    risk, reasons = score_risk(event)
    assert risk == "high"
    assert any("rooms" in r for r in reasons)


def test_rule_B_rooms_match_required_rejects_mismatch():
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Адрес А", 5, 45.0, rooms=2),
        _row("L2", "Адрес Б", 5, 45.5, rooms=3),
    ]
    sim = simulate_linking(rows, RULES["B_rooms"])
    assert sim["stats"]["fuzzy"] == 0
    assert sim["stats"]["auto_new"] == 2


# ── 4. Address/building mismatch ────────────────────────────────────────

def test_house_number_mismatch_flagged_and_extracted():
    from audit_property_linker_fuzzy import simulate_linking, RULES, extract_house_number

    assert extract_house_number("Кабанбай батыра, 15") == "15"
    assert extract_house_number("Момышулы 10/2") == "10/2"
    assert extract_house_number(None) is None
    assert extract_house_number("без номера") is None

    rows = [
        _row("L1", "Кабанбай батыра, 15", 5, 45.0),
        _row("L2", "Кабанбай батыра, 27", 5, 45.5),  # та же улица, ДРУГОЙ номер дома
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    event = sim["fuzzy_events"][0]
    assert "house_number" in event["differing_features"]
    assert event["house_number"] == "27"
    assert event["anchor_house_number"] == "15"


# ── 5. Order-dependent case ──────────────────────────────────────────────

def test_order_dependence_reassigns_middle_listing_to_different_anchor():
    """Классический order-dependency: L2(45.5) ближе к ОБОИМ L1(45.0) и
    L3(46.0)-ish в разном порядке решает, кто "анкер" первым. Собираем
    сценарий, где порядок ОБРАБОТКИ меняет итоговую кластеризацию —
    ровно риск, который просила проверить задача (п.3)."""
    from audit_property_linker_fuzzy import simulate_linking, RULES

    l1 = _row("L1", "Адрес А", 5, 45.0)
    l2 = _row("L2", "Адрес Б", 5, 45.9)   # в допуске от L1 (0.9)
    l3 = _row("L3", "Адрес В", 5, 46.8)   # в допуске от L2 (0.9), НЕ от L1 (1.8)

    forward = simulate_linking([l1, l2, l3], RULES["A_baseline"])
    backward = simulate_linking([l3, l2, l1], RULES["A_baseline"])

    # Прямой порядок: L1 создаёт anchor 45.0, L2 фаззи-матчится к L1,
    # L3 создаёт СВОЙ anchor 46.8 (слишком далеко от 45.0).
    assert forward["assignments"]["L1"] == forward["assignments"]["L2"]
    assert forward["assignments"]["L1"] != forward["assignments"]["L3"]

    # Обратный порядок: L3 создаёт anchor 46.8, L2 фаззи-матчится к L3,
    # L1 создаёт СВОЙ anchor 45.0 (слишком далеко от 46.8) — L1/L2
    # оказываются в РАЗНЫХ кластерах, хотя в прямом порядке были в одном.
    assert backward["assignments"]["L2"] == backward["assignments"]["L3"]
    assert backward["assignments"]["L1"] != backward["assignments"]["L2"]

    # Итог: L2 меняет "соклиента" в зависимости от порядка обработки —
    # ровно то нестабильное поведение, которое ищет compare_order_
    # sensitivity() на реальных данных.
    assert (forward["assignments"]["L1"] == forward["assignments"]["L2"]) != \
           (backward["assignments"]["L1"] == backward["assignments"]["L2"])


def test_compare_order_sensitivity_detects_changed_assignments():
    from audit_property_linker_fuzzy import compare_order_sensitivity

    rows = [
        _row("L1", "Адрес А", 5, 45.0),
        _row("L2", "Адрес Б", 5, 45.9),
        _row("L3", "Адрес В", 5, 46.8),
    ]
    result = compare_order_sensitivity(rows)
    assert "current" in result
    assert result["current"]["assignments_changed_vs_current"] == 0  # сам с собой — 0
    # Хотя бы один альтернативный порядок должен показать изменения
    # (listing_id_desc обрабатывает L3 раньше L1 — тот же эффект, что
    # test_order_dependence_reassigns_middle_listing_to_different_anchor).
    assert any(v["assignments_changed_vs_current"] > 0
               for k, v in result.items() if k != "current")


# ── 6. Транзитивная цепочка ──────────────────────────────────────────────

def test_transitive_chain_detected_when_span_exceeds_direct_tolerance():
    """50.0 (anchor) -> 50.9 (в допуске от anchor, 0.9) -> 49.6 (в допуске
    от anchor, 0.6) — сами 50.9 и 49.6 отличаются на 1.3 > 1.0 (прямой
    tolerance), но оба фаззи-матчатся к ОБЩЕМУ anchor независимо (задача,
    п.6, "50.0 -> 50.9 -> 51.8")."""
    from audit_property_linker_fuzzy import simulate_linking, RULES, find_transitive_chains

    rows = [
        _row("L1", "Адрес А", 5, 50.0),
        _row("L2", "Адрес Б", 5, 50.9),
        _row("L3", "Адрес В", 5, 49.6),
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    chains = find_transitive_chains(sim, RULES["A_baseline"])
    assert len(chains) == 1
    assert chains[0]["area_span"] == pytest.approx(1.3)
    assert chains[0]["member_count"] == 3
    assert set(chains[0]["member_listing_ids"]) == {"L1", "L2", "L3"}


def test_no_transitive_chain_when_span_within_tolerance():
    from audit_property_linker_fuzzy import simulate_linking, RULES, find_transitive_chains

    rows = [
        _row("L1", "Адрес А", 5, 45.0),
        _row("L2", "Адрес Б", 5, 45.5),
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    chains = find_transitive_chains(sim, RULES["A_baseline"])
    assert chains == []


# ── 7. Одновременно активные объявления ─────────────────────────────────

def test_simultaneously_active_listings_flagged():
    from audit_property_linker_fuzzy import simulate_linking, RULES, score_risk

    overlapping_start = NOW - timedelta(days=10)
    rows = [
        _row("L1", "Адрес А", 5, 45.0, seller_name="Продавец А",
             first_seen=overlapping_start, archived_at=None, is_active=True),
        _row("L2", "Адрес Б", 5, 45.5, seller_name="Продавец Б",  # ДРУГОЙ продавец
             first_seen=overlapping_start, archived_at=None, is_active=True),
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    event = sim["fuzzy_events"][0]
    assert event["simultaneously_active"] is True
    risk, reasons = score_risk(event)
    assert risk == "high"  # одновременная активность + разные продавцы = high
    assert any("пересекались по времени" in r for r in reasons)


def test_sequential_non_overlapping_listings_not_flagged_as_simultaneous():
    """Честный relist: первое объявление снято ДО появления второго —
    НЕ одновременная активность, низкий риск при прочих совпадениях."""
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Адрес А", 5, 45.0, seller_name="Продавец А",
             first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40)),
        _row("L2", "Адрес А", 5, 45.0, seller_name="Продавец А",  # тот же адрес — точный хэш, не fuzzy
             first_seen=NOW - timedelta(days=20), archived_at=None),
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    # Точный адрес -> auto_existing, не fuzzy — но интервалы всё равно не пересекаются
    assert sim["stats"]["fuzzy"] == 0
    from audit_property_linker_fuzzy import _active_overlap
    assert _active_overlap(rows[0], rows[1]) is False


# ── 8. Отсутствие необязательных полей ──────────────────────────────────

def test_missing_optional_fields_do_not_crash_and_are_flagged_unknown():
    """rooms/address без номера/seller_name — None у одной или обеих
    сторон: не падаем, честно помечаем "неизвестно", не считаем это ни
    совпадением, ни расхождением."""
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [
        _row("L1", "Без номера дома", 5, 45.0, rooms=None, seller_name=None),
        _row("L2", "Тоже без номера", 5, 45.5, rooms=None, seller_name=None),
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    assert sim["stats"]["fuzzy"] == 1
    event = sim["fuzzy_events"][0]
    assert "неизвестно" in " ".join(event["differing_features"]) or \
           "неизвестен" in " ".join(event["differing_features"])
    # rooms/house_number "неизвестно" НЕ считаются в rooms_mismatch/
    # address_mismatch агрегатах (это не подтверждённое расхождение).
    from audit_property_linker_fuzzy import aggregate_ambiguity
    agg = aggregate_ambiguity(sim)
    assert agg["rooms_mismatch_count"] == 0
    assert agg["address_house_number_mismatch_count"] == 0


def test_missing_area_or_floor_falls_to_skipped_not_crash():
    from audit_property_linker_fuzzy import simulate_linking, RULES

    rows = [_row("L1", "Адрес А", None, None)]
    sim = simulate_linking(rows, RULES["A_baseline"])
    assert sim["stats"]["skipped"] == 1
    assert sim["assignments"]["L1"] is None


# ── Доп. содержательные тесты: risk scoring, rule comparison, sample ────

def test_low_risk_when_everything_aligns_and_sequential():
    from audit_property_linker_fuzzy import simulate_linking, RULES, score_risk

    rows = [
        _row("L1", "Кабанбай батыра, 15", 5, 45.0, rooms=2, seller_name="Продавец А",
             first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40)),
        _row("L2", "Кабанбай батыра, 15", 5, 45.1, rooms=2, seller_name="Продавец А",
             first_seen=NOW - timedelta(days=20), archived_at=None),
    ]
    sim = simulate_linking(rows, RULES["A_baseline"])
    event = sim["fuzzy_events"][0]
    risk, reasons = score_risk(event)
    assert risk == "low"


def test_compare_rules_baseline_has_more_fuzzy_than_stricter_variants():
    from audit_property_linker_fuzzy import simulate_linking, compare_rules, RULES

    rows = [
        _row("L1", "Адрес А", 5, 45.0, rooms=2),
        _row("L2", "Адрес Б", 5, 45.9, rooms=3),  # rooms mismatch — B/F отсекут
    ]
    comparison = compare_rules(rows)
    assert comparison["A_baseline"]["fuzzy"] >= comparison["B_rooms"]["fuzzy"]
    assert comparison["A_baseline"]["fuzzy"] >= comparison["F_combined"]["fuzzy"]
    assert "delta_vs_baseline" in comparison["B_rooms"]


def test_build_review_sample_produces_four_buckets_of_up_to_50():
    from audit_property_linker_fuzzy import simulate_linking, RULES, build_review_sample

    rows = []
    for i in range(60):
        rows.append(_row(f"anchor{i}", f"Адрес {i}", 5, 40.0 + i * 3, complex_id=i))
        rows.append(_row(f"match{i}", f"Другой адрес {i}", 5, 40.0 + i * 3 + 0.3, complex_id=i))
    sim = simulate_linking(rows, RULES["A_baseline"])
    sample = build_review_sample(sim["fuzzy_events"])
    assert len(sample["sample"]["most_confident"]) == 50
    assert len(sample["sample"]["near_boundary"]) == 50
    assert len(sample["top50_most_suspicious"]) == 50
    for e in sample["top50_most_suspicious"]:
        assert e["risk"] in ("low", "medium", "high")
        assert e["risk_reasons"]  # непусто — явное объяснение всегда есть


def test_score_risk_never_claims_match_is_true_or_false():
    """Явное требование задачи: "без объявления результата истинным
    совпадением" — ни в возвращаемом risk, ни в reasons не должно быть
    слов, утверждающих правильность/ошибочность как факт."""
    from audit_property_linker_fuzzy import score_risk

    forbidden = ("верное совпадение", "ошибочное совпадение", "true match", "false match",
                 "это ошибка", "это правильно")
    e = {
        "candidates_count": 1, "differing_features": [], "simultaneously_active": False,
        "area_diff": 0.1, "rooms": 2, "anchor_rooms": 2,
        "house_number": "15", "anchor_house_number": "15",
    }
    risk, reasons = score_risk(e)
    text = risk + " " + " ".join(reasons)
    assert not any(f in text.lower() for f in forbidden)


def test_order_variants_produces_seven_named_orderings():
    from audit_property_linker_fuzzy import order_variants

    rows = [_row(f"L{i}", f"Адрес {i}", 5, 45.0) for i in range(5)]
    variants = order_variants(rows)
    assert set(variants.keys()) == {
        "current", "listing_id_asc", "listing_id_desc",
        "listed_at_asc", "listed_at_desc", "shuffled_seed_42", "shuffled_seed_1337",
    }
    for name, ordered in variants.items():
        assert len(ordered) == 5  # ни одна строка не потеряна/задвоена


# ── Кросс-проверка верности симуляции против РЕАЛЬНОГО линковщика ──────
# (не "не менять production linker" — ЗВАТЬ его read-only в dry_run=True,
# сверить с simulate_linking на ТЕХ ЖЕ данных В ТОМ ЖЕ порядке — единственный
# способ быть уверенным, что вся аналитика этого аудита не разошлась с
# тем, что реально делает bot/identity/property_linker.py.)

@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_complex(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)


async def _insert_listing(lid, address, floor, area, rooms, complex_name):
    from bot.db.pg import execute
    await execute("""
        INSERT INTO apartment_listings (id, address, floor, area, rooms, complex_name, price, first_seen, last_seen)
        VALUES ($1, $2, $3, $4, $5, $6, 10000000, now(), now())
        ON CONFLICT (id) DO UPDATE SET address=EXCLUDED.address, floor=EXCLUDED.floor,
            area=EXCLUDED.area, rooms=EXCLUDED.rooms, complex_name=EXCLUDED.complex_name
    """, lid, address, floor, area, rooms, complex_name)


async def _cleanup(complex_id, *listing_ids):
    from bot.db.pg import execute
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))
    if complex_id is not None:
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)


@pytest.mark.asyncio
async def test_simulation_matches_real_linker_dry_run(db):
    """simulate_linking(RULES["A_baseline"]) на listing_id ASC должно
    дать ТЕ ЖЕ auto_new/fuzzy/auto_existing/skipped, что реальный
    bot.identity.property_linker.link_listing_to_property(dry_run=True,
    match_mode="fuzzy") — единственная гарантия, что весь остальной
    аудит (агрегаты/risk/rule-сравнение) не строится на разошедшейся
    копии ИМЕННО LEGACY fuzzy-алгоритма (RULES["A_baseline"] здесь =
    старое greedy fuzzy-поведение, задача "аудит property linker fuzzy
    matching"). match_mode ЯВНО "fuzzy" — задача 2026-08-16 ("безопасный
    exact-only property linker") сменила ДЕФОЛТ линковщика на
    "exact_only"; без явного match_mode этот тест сравнивал бы
    RULES["A_baseline"] (fuzzy-алгоритм) с ДРУГИМ, безопасным режимом —
    несопоставимые вещи, тест был бы не про то, что заявлен (найдено
    при разборе падения CI PR #5, задача "минимальный интеграционный
    фикс")."""
    from audit_property_linker_fuzzy import simulate_linking, RULES
    from bot.identity.property_linker import link_listing_to_property, DryRunCache

    complex_name = "__test_fuzzy_audit_complex__"
    complex_id = await _insert_complex(complex_name)
    lids = ["__test_fa_1__", "__test_fa_2__", "__test_fa_3__", "__test_fa_4__"]
    try:
        await _insert_listing(lids[0], "Адрес А, 1", 5, 45.0, 2, complex_name)
        await _insert_listing(lids[1], "Адрес А, 1", 5, 45.0, 2, complex_name)  # точный дубль -> auto_existing
        await _insert_listing(lids[2], "Адрес Б, 2", 5, 45.9, 2, complex_name)  # fuzzy к первому
        await _insert_listing(lids[3], "Адрес В, 3", 9, 70.0, 3, complex_name)  # свой этаж -> новая

        rows = [
            {"id": lids[0], "address": "Адрес А, 1", "floor": 5, "area": 45.0, "rooms": 2,
             "complex_id": complex_id, "seller_name": None, "is_active": True,
             "first_seen": NOW, "last_seen": NOW, "archived_at": None, "price": None, "lat": None, "lon": None},
            {"id": lids[1], "address": "Адрес А, 1", "floor": 5, "area": 45.0, "rooms": 2,
             "complex_id": complex_id, "seller_name": None, "is_active": True,
             "first_seen": NOW, "last_seen": NOW, "archived_at": None, "price": None, "lat": None, "lon": None},
            {"id": lids[2], "address": "Адрес Б, 2", "floor": 5, "area": 45.9, "rooms": 2,
             "complex_id": complex_id, "seller_name": None, "is_active": True,
             "first_seen": NOW, "last_seen": NOW, "archived_at": None, "price": None, "lat": None, "lon": None},
            {"id": lids[3], "address": "Адрес В, 3", "floor": 9, "area": 70.0, "rooms": 3,
             "complex_id": complex_id, "seller_name": None, "is_active": True,
             "first_seen": NOW, "last_seen": NOW, "archived_at": None, "price": None, "lat": None, "lon": None},
        ]
        sim = simulate_linking(rows, RULES["A_baseline"])

        dry_run_cache = DryRunCache()
        real_stats = {"auto_new": 0, "auto_existing": 0, "fuzzy": 0, "skipped": 0}
        for lid, address, floor, area, rooms in [
            (lids[0], "Адрес А, 1", 5, 45.0, 2), (lids[1], "Адрес А, 1", 5, 45.0, 2),
            (lids[2], "Адрес Б, 2", 5, 45.9, 2), (lids[3], "Адрес В, 3", 9, 70.0, 3),
        ]:
            result = await link_listing_to_property(
                {"id": lid, "address": address, "floor": floor, "area": area, "rooms": rooms,
                 "complex_name": complex_name},
                dry_run=True, dry_run_cache=dry_run_cache, match_mode="fuzzy",
            )
            if result["method"] == "skipped":
                real_stats["skipped"] += 1
            elif result["method"] == "fuzzy":
                real_stats["fuzzy"] += 1
            elif result["method"] == "auto":
                real_stats["auto_new" if result["created"] else "auto_existing"] += 1

        assert sim["stats"]["auto_new"] == real_stats["auto_new"]
        assert sim["stats"]["auto_existing"] == real_stats["auto_existing"]
        assert sim["stats"]["fuzzy"] == real_stats["fuzzy"]
        assert sim["stats"]["skipped"] == real_stats["skipped"]
    finally:
        await _cleanup(complex_id, *lids)
