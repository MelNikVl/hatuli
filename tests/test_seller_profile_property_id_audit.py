"""Регрессия для задачи 2026-08-16 ("read-only аудит Seller Profile на
базе property_id") — scripts/audit_seller_profile_property_id.py.

Большинство тестов — ЧИСТЫЕ (синтетические списки dict, без БД):
_group_by_seller/_audit_seller/_select_sample/_summarize — тот же приём,
что tests/test_seller_profile_snapshot.py (_aggregate тестируется без
БД). Один DB-тест (test_run_audit_end_to_end_against_real_schema)
проверяет, что весь конвейер (SQL в _load_rows/_load_seller_profiles_
ambiguity + агрегация) реально собирается вместе на настоящей схеме."""
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


def _row(listing_id, seller_name, property_id=None, is_active=True,
         complex_id=None, floor=None, area_sqm=None, rooms=None,
         property_first_seen_at=None, property_last_seen_at=None):
    return {
        "listing_id": listing_id, "seller_name": seller_name, "is_active": is_active,
        "property_id": property_id,
        "complex_id": complex_id, "floor": floor, "area_sqm": area_sqm, "rooms": rooms,
        "property_first_seen_at": property_first_seen_at,
        "property_last_seen_at": property_last_seen_at,
    }


# ── 1. 10 listings одного property_id ─────────────────────────────────

def test_ten_listings_same_property_gives_one_unique_and_nine_relist():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [_row(f"L{i}", "Продавец А", property_id=1) for i in range(10)]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец а", by_seller["продавец а"], {})

    assert audit["listing_count"] == 10
    assert audit["unique_property_count"] == 1
    assert audit["property_relist_count"] == 9
    assert audit["repeated_property_count"] == 1
    assert audit["repeated_property_ratio"] == pytest.approx(1.0)


# ── 2. 10 listings разных property_id ─────────────────────────────────

def test_ten_listings_different_properties_gives_ten_unique_zero_relist():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [_row(f"L{i}", "Продавец Б", property_id=i) for i in range(10)]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец б", by_seller["продавец б"], {})

    assert audit["listing_count"] == 10
    assert audit["unique_property_count"] == 10
    assert audit["property_relist_count"] == 0
    assert audit["repeated_property_count"] == 0
    assert audit["repeated_property_ratio"] == pytest.approx(0.0)


# ── 3. Один property_id у ДВУХ seller identity — релисты не смешиваются ─

def test_shared_property_across_two_sellers_does_not_mix_relists():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [
        _row("L1", "Продавец В", property_id=99),
        _row("L2", "Продавец В", property_id=99),
        _row("L3", "Продавец В", property_id=99),
        _row("L4", "Продавец Г", property_id=99),  # ДРУГАЯ identity, тот же property
    ]
    by_seller = _group_by_seller(rows)
    audit_v = _audit_seller("продавец в", by_seller["продавец в"], {})
    audit_g = _audit_seller("продавец г", by_seller["продавец г"], {})

    # Продавец В: 3 листинга своих -> 1 unique property, 2 "лишних"
    assert audit_v["listing_count"] == 3
    assert audit_v["unique_property_count"] == 1
    assert audit_v["property_relist_count"] == 2

    # Продавец Г: СВОЙ ОДИН листинг на тот же property_id — НЕ 4, НЕ 3
    # "лишних" листинга Продавца В ему не приписаны.
    assert audit_g["listing_count"] == 1
    assert audit_g["unique_property_count"] == 1
    assert audit_g["property_relist_count"] == 0


# ── 4. Listings без property_id отражаются в coverage ─────────────────

def test_listings_without_property_id_reflected_in_coverage_not_dropped():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [
        _row("L1", "Продавец Д", property_id=1),
        _row("L2", "Продавец Д", property_id=None),
        _row("L3", "Продавец Д", property_id=None),
    ]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец д", by_seller["продавец д"], {})

    # НЕ молча отброшены — listing_count всё ещё 3, coverage явный.
    assert audit["listing_count"] == 3
    assert audit["listings_with_property_id"] == 1
    assert audit["listings_without_property_id"] == 2
    assert audit["coverage_ratio"] == pytest.approx(1 / 3, rel=1e-3)
    assert audit["coverage_denominator"] == 3


# ── 5. active/censored маркируется отдельно ────────────────────────────

def test_active_censored_and_concluded_spans_kept_separate():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    concluded_first = NOW - timedelta(days=40)
    concluded_last = NOW - timedelta(days=10)   # span 30, is_active=False
    censored_first = NOW - timedelta(days=20)
    censored_last = NOW - timedelta(days=1)     # span 19, is_active=True (ещё видим)

    rows = [
        _row("L1", "Продавец Е", property_id=1, is_active=False,
             property_first_seen_at=concluded_first, property_last_seen_at=concluded_last),
        _row("L2", "Продавец Е", property_id=2, is_active=True,
             property_first_seen_at=censored_first, property_last_seen_at=censored_last),
    ]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец е", by_seller["продавец е"], {})

    assert audit["observed_span_concluded_count"] == 1
    assert audit["observed_span_concluded_mean_days"] == pytest.approx(30.0)
    assert audit["observed_span_active_censored_count"] == 1
    assert audit["observed_span_active_censored_mean_days"] == pytest.approx(19.0)
    # НЕ смешаны в один общий "средний срок" — иначе это была бы ровно
    # та ошибка, которую задача просила избежать (NOW() в обычном
    # среднем без маркировки).


def test_property_with_any_active_listing_counts_as_censored_not_concluded():
    """Property с ДВУМЯ listing: один архивный, один ещё активный —
    censored (is_active НЕ False хотя бы у одного listing этой property),
    не concluded — квартира формально всё ещё "на рынке"."""
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    first_at = NOW - timedelta(days=50)
    last_at = NOW - timedelta(days=2)
    rows = [
        _row("L1", "Продавец Ж", property_id=1, is_active=False,
             property_first_seen_at=first_at, property_last_seen_at=last_at),
        _row("L2", "Продавец Ж", property_id=1, is_active=True,
             property_first_seen_at=first_at, property_last_seen_at=last_at),
    ]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец ж", by_seller["продавец ж"], {})

    assert audit["observed_span_active_censored_count"] == 1
    assert audit["observed_span_concluded_count"] == 0


# ── 6. observed span НЕ называется true DOM ────────────────────────────

def test_observed_span_field_names_do_not_claim_true_dom():
    """Прямая проверка именования (задача: "явно назвать observed_span,
    а не true DOM") — ни одно поле результата _audit_seller не содержит
    "dom"/"true_dom", а докстринг модуля явно оговаривает разницу."""
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller
    import audit_seller_profile_property_id as audit_module

    rows = [_row("L1", "Продавец З", property_id=1,
                  property_first_seen_at=NOW - timedelta(days=5), property_last_seen_at=NOW)]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец з", by_seller["продавец з"], {})

    assert not any("dom" in key.lower() for key in audit.keys())
    assert any(key.startswith("observed_span_") for key in audit.keys())
    assert "true_dom" in audit_module.__doc__.lower()


# ── 7. ambiguity считается из фактического источника ───────────────────

def test_ambiguity_recomputed_matches_stored_when_present():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller, _AMBIGUOUS_NAME_MIN_LISTINGS

    rows = [_row(f"L{i}", "Частое Имя", property_id=i) for i in range(_AMBIGUOUS_NAME_MIN_LISTINGS + 1)]
    by_seller = _group_by_seller(rows)
    stored = {"частое имя": {"is_ambiguous": True, "total_listings_count": _AMBIGUOUS_NAME_MIN_LISTINGS + 1}}
    audit = _audit_seller("частое имя", by_seller["частое имя"], stored)

    assert audit["is_ambiguous_recomputed"] is True
    assert audit["is_ambiguous_stored"] is True
    assert audit["ambiguity_source_matches"] is True


def test_ambiguity_source_matches_none_when_seller_profiles_has_no_row():
    """"связь корректна" ТОЛЬКО когда реально нашли строку seller_profiles
    по этому seller_name — иначе ambiguity_source_matches=None (не
    False!), задача п.8: "ambiguity_ratio считать только если связь
    корректна" — None здесь ИМЕННО отсутствие связи, не "не совпало"."""
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [_row("L1", "Новый Продавец", property_id=1)]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("новый продавец", by_seller["новый продавец"], {})  # пустой seller_profiles

    assert audit["is_ambiguous_stored"] is None
    assert audit["ambiguity_source_matches"] is None


def test_ambiguity_mismatch_detected_when_stored_is_stale():
    """seller_profiles.is_ambiguous посчитан РАНЬШЕ (другое количество
    листингов на тот момент) — аудит честно показывает несовпадение, не
    скрывает его."""
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [_row(f"L{i}", "Устаревший Продавец", property_id=i) for i in range(3)]
    by_seller = _group_by_seller(rows)
    stored = {"устаревший продавец": {"is_ambiguous": True, "total_listings_count": 20}}  # stale
    audit = _audit_seller("устаревший продавец", by_seller["устаревший продавец"], stored)

    assert audit["is_ambiguous_recomputed"] is False  # 3 листинга сейчас
    assert audit["is_ambiguous_stored"] is True        # но stored говорит True
    assert audit["ambiguity_source_matches"] is False


# ── complex_diversity / completeness (доп. содержательные тесты) ───────

def test_complex_diversity_counts_distinct_non_null_complex_ids():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller

    rows = [
        _row("L1", "Продавец И", property_id=1, complex_id=10),
        _row("L2", "Продавец И", property_id=2, complex_id=10),  # тот же ЖК
        _row("L3", "Продавец И", property_id=3, complex_id=20),
        _row("L4", "Продавец И", property_id=4, complex_id=None),  # первичка/нет ЖК
    ]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец и", by_seller["продавец и"], {})

    assert audit["complex_diversity_count"] == 2
    assert audit["complex_diversity_denominator"] == 4


def test_completeness_ratio_uses_explicit_fields_and_denominator():
    """COMPLETENESS_FIELDS = (complex_id, floor, area_sqm, rooms) — 4
    поля на property; знаменатель = unique_property_count * 4, выведен
    явно (задача: "вывести знаменатель каждой метрики")."""
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller, COMPLETENESS_FIELDS

    assert COMPLETENESS_FIELDS == ("complex_id", "floor", "area_sqm", "rooms")

    rows = [
        # property 1: все 4 поля заполнены
        _row("L1", "Продавец К", property_id=1, complex_id=10, floor=5, area_sqm=45.0, rooms=2),
        # property 2: только complex_id и floor (2 из 4)
        _row("L2", "Продавец К", property_id=2, complex_id=10, floor=3, area_sqm=None, rooms=None),
    ]
    by_seller = _group_by_seller(rows)
    audit = _audit_seller("продавец к", by_seller["продавец к"], {})

    assert audit["completeness_denominator"] == 2 * len(COMPLETENESS_FIELDS)  # 8
    assert audit["completeness_filled"] == 6  # 4 + 2
    assert audit["completeness_ratio"] == pytest.approx(6 / 8)


# ── generic-стоп-лист / нормализация — та же группировка, что снапшот ──

def test_generic_stoplist_names_excluded_same_as_snapshot():
    from audit_seller_profile_property_id import _group_by_seller

    rows = [_row(f"L{i}", "Хозяин", property_id=i) for i in range(5)] + [_row("L9", "Реальное Имя")]
    by_seller = _group_by_seller(rows)
    assert "хозяин" not in by_seller
    assert "реальное имя" in by_seller


# ── _select_sample — детерминированная выборка ──────────────────────────

def test_select_sample_is_deterministic_and_prioritizes_largest():
    from audit_seller_profile_property_id import _select_sample

    by_seller = {
        "мало": [_row("L1", "Мало")],
        "средне": [_row(f"L{i}", "Средне") for i in range(5)],
        "много": [_row(f"L{i}", "Много") for i in range(20)],
    }
    sampled = _select_sample(by_seller, sample=2)
    assert set(sampled.keys()) == {"много", "средне"}

    # Повторный вызов — тот же результат (детерминированность).
    sampled_again = _select_sample(by_seller, sample=2)
    assert list(sampled.keys()) == list(sampled_again.keys())


def test_select_sample_none_returns_everything():
    from audit_seller_profile_property_id import _select_sample

    by_seller = {"а": [_row("L1", "А")], "б": [_row("L2", "Б")]}
    assert _select_sample(by_seller, None) == by_seller


# ── _summarize — old vs new, distribution, top-20 ───────────────────────

def test_summarize_diff_abs_and_pct_old_vs_new():
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller, _summarize

    rows = [_row(f"L{i}", "Продавец Л", property_id=1) for i in range(4)]  # 4 listings, 1 property
    by_seller = _group_by_seller(rows)
    audits = [_audit_seller("продавец л", by_seller["продавец л"], {})]
    summary = _summarize(audits)

    assert summary["old_total_listing_count"] == 4
    assert summary["new_total_unique_property_count"] == 1
    assert summary["diff_abs"] == 1 - 4  # -3
    assert summary["diff_pct"] == pytest.approx(-75.0)


def test_summarize_distribution_buckets_not_labeled_owner_or_realtor():
    """Задача: "НЕ называть эти группы owner/realtor без дополнительного
    подтверждения" — структурная проверка ключей бакетов."""
    from audit_seller_profile_property_id import _group_by_seller, _audit_seller, _summarize

    rows = (
        [_row("L1", "Один", property_id=1)]
        + [_row(f"L{i}", "Три", property_id=i) for i in range(3)]
        + [_row(f"L{i}", "Десять", property_id=i) for i in range(10)]
        + [_row(f"L{i}", "Тридцать", property_id=i) for i in range(30)]
    )
    by_seller = _group_by_seller(rows)
    audits = [_audit_seller(name, group, {}) for name, group in by_seller.items()]
    summary = _summarize(audits)

    buckets = summary["unique_property_count_distribution"]
    assert set(buckets.keys()) == {"1", "2-5", "6-20", ">20"}
    assert not any(k.lower() in ("owner", "realtor", "agency", "риелтор", "агентство") for k in buckets)
    assert buckets["1"] == 1   # "Один" -> 1 unique property
    assert buckets["2-5"] == 1  # "Три" -> 3 РАЗНЫХ property_id -> 3 unique properties
    assert buckets["6-20"] == 1  # "Десять" -> 10 unique properties
    assert buckets[">20"] == 1  # "Тридцать" -> 30 unique properties


def test_summarize_top20_sorted_by_absolute_discrepancy_desc():
    from audit_seller_profile_property_id import _summarize

    audits = [
        {"seller_name": "а", "listing_count": 5, "unique_property_count": 5,
         "listings_with_property_id": 5, "listings_without_property_id": 0,
         "ambiguity_source_matches": None},
        {"seller_name": "б", "listing_count": 50, "unique_property_count": 3,
         "listings_with_property_id": 50, "listings_without_property_id": 0,
         "ambiguity_source_matches": None},
        {"seller_name": "в", "listing_count": 10, "unique_property_count": 9,
         "listings_with_property_id": 10, "listings_without_property_id": 0,
         "ambiguity_source_matches": None},
    ]
    summary = _summarize(audits)
    names_ranked = [row["seller_name"] for row in summary["top20_largest_discrepancies"]]
    assert names_ranked[0] == "б"  # |50-3|=47, наибольшее расхождение
    assert names_ranked[-1] == "а"  # |5-5|=0, наименьшее расхождение (в = |10-9|=1, между ними)


def test_summarize_ambiguity_cross_check_denominator_excludes_unmatched():
    from audit_seller_profile_property_id import _summarize

    audits = [
        {"seller_name": "а", "listing_count": 1, "unique_property_count": 1,
         "listings_with_property_id": 1, "listings_without_property_id": 0,
         "ambiguity_source_matches": True},
        {"seller_name": "б", "listing_count": 1, "unique_property_count": 1,
         "listings_with_property_id": 1, "listings_without_property_id": 0,
         "ambiguity_source_matches": False},
        {"seller_name": "в", "listing_count": 1, "unique_property_count": 1,
         "listings_with_property_id": 1, "listings_without_property_id": 0,
         "ambiguity_source_matches": None},  # не участвует — нет seller_profiles строки
    ]
    summary = _summarize(audits)
    assert summary["ambiguity_cross_check"]["denominator"] == 2  # НЕ 3
    assert summary["ambiguity_cross_check"]["checked"] == 2
    assert summary["ambiguity_cross_check"]["mismatches"] == 1


# ── read-only гарантия ──────────────────────────────────────────────────

def test_module_has_no_write_sql():
    """Структурная гарантия "полностью read-only" (задача, гейты) — ни
    вызова execute()/import execute из bot.db.pg (единственный write-
    примитив — fetch/fetchrow/fetchval read-only, см. её докстринг), ни
    write-SQL строкового литерала в исходнике. Разбор через ast, не
    голый substring по всему файлу — иначе слово "execute(" в ЭТОМ ЖЕ
    докстринге (документирующем гарантию) ложно проваливал бы
    собственную проверку."""
    import ast
    import audit_seller_profile_property_id as audit_module

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

    # Write-SQL строковые литералы (INSERT/UPDATE/DELETE/TRUNCATE) — по
    # STRING-константам AST, не по всему файлу (докстринги/комментарии
    # свободно упоминают эти слова как прозу, не как реальный SQL).
    write_verbs = ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            assert not any(v in upper for v in write_verbs), node.value


# ── DB-интеграционный тест (единственный, реальная схема) ───────────────

@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_listing(lid, seller_name):
    from bot.db.pg import execute
    await execute("""
        INSERT INTO apartment_listings (id, seller_name, is_active, price, area, first_seen, last_seen)
        VALUES ($1, $2, TRUE, 10000000, 40, now(), now())
        ON CONFLICT (id) DO UPDATE SET seller_name = EXCLUDED.seller_name
    """, lid, seller_name)


async def _cleanup(*listing_ids):
    from bot.db.pg import execute
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))


@pytest.mark.asyncio
async def test_run_audit_end_to_end_against_real_schema(db):
    """Проверяет, что _load_rows/_load_seller_profiles_ambiguity SQL
    реально валиден против настоящей схемы (properties/property_listings/
    seller_profiles) и весь конвейер run_audit() собирается вместе —
    НЕ проверяет глубокую логику (это уже покрыто чистыми тестами выше),
    только "ничего не падает, результат содержит ожидаемого продавца"."""
    from audit_seller_profile_property_id import run_audit

    lid_a, lid_b = "__test_audit_pid_a__", "__test_audit_pid_b__"
    await _insert_listing(lid_a, "__test_audit_seller__")
    await _insert_listing(lid_b, "__test_audit_seller__")
    try:
        result = await run_audit(sample=None)
        names = {a["seller_name"] for a in result["per_seller"]}
        assert "__test_audit_seller__" in names
        target = next(a for a in result["per_seller"] if a["seller_name"] == "__test_audit_seller__")
        assert target["listing_count"] == 2
        # property_listings пуста для этих синтетических listing'ов -> 0
        # (backfill не запускался на них) — coverage честно отражает это.
        assert target["unique_property_count"] == 0
        assert target["listings_without_property_id"] == 2
        assert "summary" in result
        assert result["summary"]["sellers_audited"] >= 1
    finally:
        await _cleanup(lid_a, lid_b)


@pytest.mark.asyncio
async def test_run_audit_does_not_write_to_seller_profiles(db):
    """Прямая проверка гейта "не изменяет seller_profiles" — снимок
    count(*) до/после run_audit() должен совпасть."""
    from bot.db.pg import fetchval
    from audit_seller_profile_property_id import run_audit

    before = await fetchval("SELECT count(*) FROM seller_profiles")
    await run_audit(sample=5)
    after = await fetchval("SELECT count(*) FROM seller_profiles")
    assert before == after
