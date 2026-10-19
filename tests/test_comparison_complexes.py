"""Offline catalog behavior; connection denied except explicit mocked reader."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.core.comparison_complexes import (
    PUBLIC_NAME_POLICY, build_comparison_complexes, complex_catalog_path, load_comparison_complexes,
    publish_comparison_complexes, validate_comparison_complexes,
)
from scripts.publish_comparison_complexes import READS, read_catalog

NOW = datetime(2026, 10, 19, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def deny_connections(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/placeholder")
    def denied(*args, **kwargs):
        raise AssertionError("Live network/database forbidden")
    monkeypatch.setattr(socket, "create_connection", denied)
    # Windows Proactor uses a loopback socketpair to create its event loop.
    # Block the actual DB entry points, while each reader test mocks asyncpg.
    import asyncpg
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=denied))
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(side_effect=denied))


def project(identifier=1, **extra):
    return {"id": identifier, "name": f"Проект {identifier}", "lat": 51.12, "lon": 71.42, **extra}


def build(rows=None, **kwargs):
    return build_comparison_complexes(rows if rows is not None else [project()], now=NOW, **kwargs)


def test_includes_catalog_without_any_current_listing_or_newbuild_unit():
    data = build([project(1), project(2, is_newbuild=False, listings_count=0), project(3, rental_listings_count=0)])
    assert [x["id"] for x in data["items"]] == [1, 2, 3]
    assert data["coverage"]["all_catalog_rows_read"] is True
    assert data["coverage"]["city_registry_verified"] is False
    assert data["coverage"]["city_evidence_counts"] == {"catalog_coordinate_area": 3}


@pytest.mark.parametrize("name", [
    "Aisar 3 От Bi Group Сервер", "Akjol От Хозяев", "Apple City - Заезжай И Живи!",
    "Aiqyn 🏡", "Aiqyn:", "Amanat ✨", "Amanat!", "Akbulak Premium Город Астана",
    "13 магистраль", "Sandi Qala 2 Продается 3-Комнат", "Имеется Тёплый Паркинг",
    "Sunrise +7 (700) 123-45-67", "Sunrise whatsapp", "Sunrise https://example.com",
])
def test_public_name_quality_excludes_fragments_even_when_stored_as_trusted(name):
    data = build([project(1), project(2, name=name, is_newbuild=True, housing_class="бизнес",
                                      krisha_url="https://krisha.kz/complex/show/astana/p/")])
    assert [row["id"] for row in data["items"]] == [1]
    assert data["coverage"]["name_junk_roots"] == 1
    assert data["coverage"]["public_name_policy"] == PUBLIC_NAME_POLICY


def test_real_project_names_numbers_phases_and_fractional_houses_are_preserved():
    names = ["Aisar 3", "Aisar", "GreenLine. Garden", "GreenLine. Headliner", "Нурсая 2",
             "Нурсая", "O'NER", "Дом 17/2А", "Garden", "River Park"]
    data = build([project(i, name=name) for i, name in enumerate(names, 1)])
    assert {row["name"] for row in data["items"]} == set(names)
    assert data["coverage"]["name_junk_roots"] == 0
    assert data["coverage"]["ambiguous_name_roots"] == 0


def test_exact_normalized_nonhouse_name_ambiguity_is_held_without_merging_ids():
    rows = [project(1), project(2, name='ЖК «Aiqyn»'), project(3, name="AIQYN"),
            project(4, name="Aiqyn 2"), project(5, parent_complex_id=2)]
    before = deepcopy(rows)
    data = build(rows)
    assert rows == before
    assert {row["id"] for row in data["items"]} == {1, 4}
    assert data["coverage"]["ambiguous_name_roots"] == 2
    assert data["coverage"]["ambiguous_name_groups"] == 1


def test_exact_known_brand_variant_is_withheld_but_prefix_neighbours_are_not():
    data = build([project(1), project(2, name="Garden"), project(3, name="Бигвилль Garden"),
                  project(4, name="Garden Towers"), project(5, name="Garden 2")])
    assert {row["id"] for row in data["items"]} == {1, 4, 5}
    assert data["coverage"]["ambiguous_name_roots"] == 2


def test_reviewed_separate_equal_names_and_explicit_houses_remain_distinct():
    data = build([project(1, name="Garden"), project(2, name="Garden"),
                  project(3, name="Дом 17/2А", parent_complex_id=1),
                  project(4, name="Дом 17/2А", parent_complex_id=2)],
                 relations=[{"complex_id_a": 1, "complex_id_b": 2,
                             "relation_type": "separate_neighbor_complex"}])
    assert data["count"] == 4
    assert data["coverage"]["ambiguous_name_roots"] == 0
    assert {row["parent_id"] for row in data["items"] if row["id"] in {3, 4}} == {1, 2}


def test_reviewed_separation_on_redirected_id_resolves_root_pair():
    data = build([project(1, name="Garden"), project(2, name="Garden"),
                  project(3, canonical_id=1)],
                 reviews=[{"complex_id_a": 1, "complex_id_b": 2}],
                 relations=[{"complex_id_a": 3, "complex_id_b": 2, "relation_type": "sibling_phase"}])
    assert {row["id"] for row in data["items"]} == {1, 2}


def test_name_junk_parent_does_not_silently_detach_its_house():
    data = build([project(1), project(2, name="Amanat!"),
                  project(3, name="Дом 17/2А", parent_complex_id=2)])
    assert [row["id"] for row in data["items"]] == [1]


def test_all_name_junk_fails_closed_instead_of_falling_back_to_raw_inventory():
    with pytest.raises(ValueError, match="Empty or incomplete"):
        build([project(1, name="Amanat!"), project(2, name="Aiqyn 🏡")])


def test_verified_alias_status_does_not_expose_contact_or_marketing_fragment():
    data = build(aliases=[{"complex_id": 1, "name": "Amanat!", "status": "verified"},
                          {"complex_id": 1, "name": "Старое имя", "status": "verified"}])
    assert data["items"][0]["aliases"] == ["Старое имя"]


def test_new_policy_uses_a_new_default_file_and_respects_explicit_environment(monkeypatch):
    monkeypatch.delenv("HATULI_COMPARISON_COMPLEXES_PATH", raising=False)
    assert complex_catalog_path().name == "comparison-complexes-v2.json"
    monkeypatch.setenv("HATULI_COMPARISON_COMPLEXES_PATH", "private/explicit.json")
    assert complex_catalog_path() == Path("private/explicit.json")


def test_v1_receipt_and_name_junk_cannot_be_loaded_as_new_policy():
    data = build()
    old = deepcopy(data)
    old["schema_version"] = 1
    with pytest.raises(ValueError):
        validate_comparison_complexes(old, now=NOW)
    missing_policy = deepcopy(data)
    missing_policy["coverage"].pop("public_name_policy")
    with pytest.raises(ValueError, match="coverage receipt"):
        validate_comparison_complexes(missing_policy, now=NOW)
    for field, value in (("name", "Amanat!"), ("aliases", ["Aiqyn 🏡"])):
        changed = deepcopy(data)
        changed["items"][0][field] = value
        with pytest.raises(ValueError):
            validate_comparison_complexes(changed, now=NOW)


def test_existing_redirect_chain_is_not_extra_choice_or_unreviewed_alias():
    data = build([project(1), project(2, canonical_id=1, name="Старое имя"), project(3, canonical_id=2)])
    assert data["count"] == 1
    assert data["items"][0]["aliases"] == []
    assert data["coverage"]["redirect_rows"] == 2


@pytest.mark.parametrize("city", ["konaev", "new-city", "алатау", "semey"])
def test_explicit_nonlocal_source_city_cannot_be_restored_by_astana_coordinates(city):
    data = build([project(1, krisha_url=f"https://krisha.kz/complex/show/{city}/project/"), project(99)])
    assert [row["id"] for row in data["items"]] == [99]
    assert data["coverage"]["city_conflict_roots"] == 1


def test_foreign_legacy_link_or_geo_quarantine_on_redirect_holds_canonical_family():
    for change in ({"krisha_url": "https://krisha.kz/complex/show/almaty/project/"},
                   {"geo_quarantined_at": NOW}):
        data = build([project(1), project(2, canonical_id=1, **change), project(99)])
        assert [row["id"] for row in data["items"]] == [99]


@pytest.mark.parametrize("field,value", [("name", None), ("name", ""), ("label", None),
                                         ("aliases", [None]), ("aliases", [""]), ("aliases", [["bad"]])])
def test_invalid_catalog_strings_fail_closed(field, value):
    data = build()
    data["items"][0][field] = value
    with pytest.raises(ValueError):
        validate_comparison_complexes(data, now=NOW)


def test_verified_aliases_follow_redirects_only_when_unambiguous():
    data = build([project(1, name="Новый"), project(2, canonical_id=1), project(3, name="Сосед")], aliases=[
        {"complex_id": 2, "name": "Прежний", "status": "verified"},
        {"complex_id": 1, "name": "Непроверенный", "status": "observed"},
        {"complex_id": 1, "name": "Сосед", "status": "verified"},
    ])
    assert next(x for x in data["items"] if x["id"] == 1)["aliases"] == ["Прежний"]
    assert "Сосед" not in data["items"][0]["aliases"]


def test_umbrella_child_house_numbers_and_reciprocal_ids_survive():
    data = build([project(1, name="Зонтик", is_umbrella=True),
                  project(2, name="Дом 17/2А", parent_complex_id=1, address="ул. Мира, 17/2А"),
                  project(3, name="Дом 17/2Б", parent_complex_id=1, address="ул. Мира, 17/2Б")])
    by_id = {x["id"]: x for x in data["items"]}
    assert by_id[1]["child_ids"] == [2, 3]
    assert by_id[2]["parent_id"] == 1 and by_id[2]["parent_name"] == "Зонтик"
    assert by_id[2]["address"] == "ул. Мира, 17/2А"
    assert "17/2А" in by_id[2]["label"]
    assert data["coverage"]["house_rows"] == 2


@pytest.mark.parametrize("url", [
    "https://krisha.kz/complex/show/almaty/same-project/",
    "https://krisha.kz/complex/show/shymkent/same-project/",
    "https://korter.kz/same-project-алматы",
])
def test_foreign_source_city_overrules_plausible_astana_coordinates_and_parent(url):
    data = build([project(1), project(2, krisha_url=url), project(3, parent_complex_id=2)])
    assert [x["id"] for x in data["items"]] == [1]
    assert data["coverage"]["city_conflict_roots"] == 1


def test_verified_foreign_city_is_excluded_and_no_unknown_city_promoted():
    data = build([project(1), project(2), project(3, lat=None, lon=None)],
                 addresses=[{"complex_id": 2, "city": "Алматы", "status": "verified"}])
    assert [x["id"] for x in data["items"]] == [1]
    assert data["coverage"]["unknown_city_roots"] == 1


@pytest.mark.parametrize("extra", [
    {"krisha_url": "https://krisha.kz/complex/show/nur-sultan/project/"},
    {"korter_url": "https://korter.kz/project-астана"},
    {"address": "г. Астана, ул. Мира 17/2"},
])
def test_explicit_city_can_include_coordinate_free_project(extra):
    data = build([project(1, lat=None, lon=None, **extra)])
    assert data["count"] == 1
    assert data["items"][0]["city_evidence"] != "catalog_coordinate_area"


def test_slug_containing_foreign_city_string_is_not_a_foreign_city():
    assert build([project(1, krisha_url="https://krisha.kz/complex/show/astana/almatau-astana/")])["count"] == 1


@pytest.mark.parametrize("coordinates", [{"lat": 43.2, "lon": 76.9}, {"lat": float("nan")}, {"lon": None}, {"lat": True}])
def test_invalid_or_outside_coordinates_fail_closed_even_with_astana_url(coordinates):
    data = build([project(1), project(2, **coordinates, krisha_url="https://krisha.kz/complex/show/astana/p/")])
    assert [x["id"] for x in data["items"]] == [1]


def test_children_can_inherit_eligible_parent_city_without_guessing_physical_house():
    data = build([project(1), project(2, lat=None, lon=None, parent_complex_id=1)])
    assert next(x for x in data["items"] if x["id"] == 2)["city_evidence"] == "parent_catalog_city"


def test_quarantine_pending_identity_and_bad_redirects_are_excluded():
    data = build([project(1), project(2, is_garbage=True), project(3, is_street=True),
                  project(4, canonical_reason="junk_unmatched"), project(5, geo_quarantined_at=NOW),
                  project(6), project(7), project(8, canonical_id=9), project(9, canonical_id=8),
                  project(10, canonical_id=999), project(11), project(12, parent_complex_id=5)],
                 reviews=[{"complex_id_a": 6, "complex_id_b": 7}],
                 conflicts=[{"complex_id": 11, "conflict_with_complex_id": None}])
    assert [x["id"] for x in data["items"]] == [1]
    assert data["coverage"]["invalid_redirect_rows"] == 3
    assert data["coverage"]["pending_duplicate_roots"] == 2


def test_review_pair_already_collapsed_or_reviewed_separate_does_not_hide_valid_group():
    data = build([project(1), project(2, canonical_id=1), project(3)],
                 reviews=[{"complex_id_a": 1, "complex_id_b": 2}, {"complex_id_a": 1, "complex_id_b": 3}],
                 relations=[{"complex_id_a": 1, "complex_id_b": 3, "relation_type": "sibling_phase"}])
    assert [x["id"] for x in data["items"]] == [1, 3]


def test_no_private_fields_and_no_apartment_suffix_in_public_address():
    data = build([project(1, phone="+77000000000", notes="secret", description="private",
                          address="ул. Мира, 17/2А, кв. 77 — private tail")])
    assert data["items"][0]["address"] == "ул. Мира, 17/2А"
    assert all(x not in json.dumps(data) for x in ("phone", "notes", "description", "private", "secret"))


def test_file_load_and_atomic_failure_preserve_previous_bytes_and_mtime(tmp_path):
    path = tmp_path / "catalog.json"
    publish_comparison_complexes(build([project(i) for i in range(1, 21)]), path, now=NOW)
    previous, mtime = path.read_bytes(), path.stat().st_mtime_ns
    cases = [build(), {**build(), "items": [], "count": 0},
             {**build([project(i) for i in range(1, 21)]), "generated_at": (NOW-timedelta(hours=2)).isoformat()}]
    for data in cases:
        with pytest.raises(ValueError):
            publish_comparison_complexes(data, path, now=NOW)
        assert path.read_bytes() == previous and path.stat().st_mtime_ns == mtime
    assert load_comparison_complexes(path, now=NOW)["count"] == 20
    assert load_comparison_complexes(path, now=NOW+timedelta(days=8)) is None
    assert not list(tmp_path.glob("*.tmp"))


def test_validator_rejects_broken_parent_graph_and_private_extra_fields():
    data = build([project(1), project(2, parent_complex_id=1)])
    for mutation in (lambda x: x["items"][0].update(phone="private"),
                     lambda x: x["items"][0].update(child_ids=[]),
                     lambda x: x["items"][1].update(parent_name="Wrong")):
        changed = deepcopy(data)
        mutation(changed)
        with pytest.raises(ValueError):
            validate_comparison_complexes(changed, now=NOW)


def test_read_only_bounded_export_closes_connection_without_app_pool(monkeypatch):
    transaction = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    class Transaction:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *args):
            return None
    requested = []
    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            return Transaction()
        async def execute(self, query):
            assert query == "SET LOCAL statement_timeout = '20s'"
        async def fetch(self, query, bound):
            requested.append((query, bound))
            assert query.lstrip().upper().startswith("SELECT ")
            return [project()] if "FROM complexes ORDER" in query else []
        close = AsyncMock()
    c = Connection()
    connect = AsyncMock(return_value=c)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    data = asyncio.run(read_catalog("postgresql://placeholder@127.0.0.1:1/placeholder"))
    assert data["complexes"][0]["id"] == 1
    assert len(requested) == len(READS)
    assert connect.call_args.kwargs["server_settings"]["default_transaction_read_only"] == "on"
    c.close.assert_awaited_once()


def test_reader_rejects_truncation_instead_of_publishing_partial(monkeypatch):
    class Transaction:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *args):
            return None
    c = SimpleNamespace(transaction=lambda **kwargs: Transaction(), execute=AsyncMock(),
                        fetch=AsyncMock(return_value=[{}] * (READS[0][2]+1)), close=AsyncMock())
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=AsyncMock(return_value=c)))
    with pytest.raises(ValueError, match="Incomplete bounded"):
        asyncio.run(read_catalog("postgresql://placeholder@127.0.0.1:1/placeholder"))
    c.close.assert_awaited_once()


def test_imports_do_not_load_database_pool():
    # Imports used by the selector may not transitively initialize bot.db.pg.
    import subprocess
    env = dict(__import__("os").environ, DATABASE_URL="postgresql://placeholder@127.0.0.1:1/placeholder")
    result = subprocess.run([sys.executable, "-c", "import sys; import bot.core.comparison_complexes; import scripts.publish_comparison_complexes; assert 'bot.db.pg' not in sys.modules"],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
