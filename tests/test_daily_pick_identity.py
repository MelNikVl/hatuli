"""Offline identity and owner evidence; no PostgreSQL, HTTP or migrations."""
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.core.daily_pick_identity import annotate_daily_pick_identity_rows, normalize_daily_pick_identity, public_daily_pick_identity, validate_public_daily_identity
from scripts.publish_daily_picks import CANDIDATES_SQL, IDENTITY_READS, owner_evidence_rows


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Daily identity tests must never connect to a real database")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


def identity_row(**changes):
    row = {
        "id": "1016000000", "complex_id": 99, "complex_resolution": "url",
        "resolved_house_id": None, "house_attribution": None,
        "canonical_complex_name": "Old alias", "complex_year_built": 2000,
        "identity_checked": True, "identity_complex_id": 10, "identity_complex_name": "Канонический ЖК",
        "identity_complex_valid": True, "identity_complex_is_umbrella": False,
        "identity_complex_parent_id": None, "identity_complex_year_built": 2022,
        "identity_house_id": None, "identity_house_name": None, "identity_house_valid": False,
        "identity_house_is_umbrella": None, "identity_house_parent_id": None, "identity_house_year_built": None,
        "identity_complex_review_pending": False, "identity_property_match_pending": False,
        "identity_name_ambiguous": False, "identity_source_name_conflict": False, "address": "Анет баба, 4", "property_id": 123,
        "identity_status": "provisional", "private_note": "INTERNAL-ONLY",
    }
    return {**row, **changes}


def test_valid_final_canonical_changes_id_label_and_year_without_mutating_input():
    row = identity_row()
    before = copy.deepcopy(row)
    result = normalize_daily_pick_identity(row)
    assert result["complex_id"] == 10
    assert result["canonical_complex_name"] == "Канонический ЖК"
    assert result["complex_year_built"] == 2022
    assert result["identity_scope"] == "complex"
    assert row == before
    assert result["property_id"] == 123  # A provisional bootstrap is not proof of a duplicate.


@pytest.mark.parametrize("change", [
    {"identity_checked": None}, {"identity_complex_valid": False},
    {"identity_complex_id": None}, {"identity_complex_id": True},
    {"identity_complex_review_pending": True}, {"identity_property_match_pending": True},
    {"identity_name_ambiguous": True}, {"identity_name_ambiguous": None},
    {"complex_resolution": "unbound"}, {"complex_resolution": "geo"},
    {"identity_complex_is_umbrella": 1}, {"identity_complex_name": ""},
])
def test_unverified_conflicted_or_geographic_membership_is_excluded(change):
    assert normalize_daily_pick_identity(identity_row(**change)) is None


def test_verified_umbrella_child_uses_child_year_and_never_parent_characteristics():
    row = identity_row(identity_complex_is_umbrella=True, identity_complex_year_built=2005,
                       resolved_house_id=199, house_attribution="address", identity_house_id=20,
                       identity_house_name="ЖК · корпус 2", identity_house_valid=True,
                       identity_house_is_umbrella=False, identity_house_parent_id=10,
                       identity_house_year_built=2024)
    result = normalize_daily_pick_identity(row)
    assert result["complex_id"] == result["resolved_house_id"] == 20
    assert result["canonical_complex_name"] == "ЖК · корпус 2"
    assert result["complex_year_built"] == 2024
    assert result["identity_scope"] == "house"
    public = public_daily_pick_identity(result)
    assert public["canonical_id"] == public["house_id"] == 20
    assert public["umbrella_id"] == 10 and public["umbrella_name"] == "Канонический ЖК"
    assert public["house_label"] == "ЖК · корпус 2"
    assert public["binding_method"] == "address"


@pytest.mark.parametrize("method", [None, "geo", "address_geo"])
def test_umbrella_without_exact_house_evidence_is_never_a_best_pick(method):
    row = identity_row(identity_complex_is_umbrella=True, resolved_house_id=20,
                       house_attribution=method, identity_house_id=20, identity_house_name="Корпус 2",
                       identity_house_valid=True, identity_house_is_umbrella=False, identity_house_parent_id=10)
    assert normalize_daily_pick_identity(row) is None


def test_child_of_another_umbrella_or_conflicting_explicit_house_is_excluded():
    row = identity_row(resolved_house_id=20, house_attribution="token", identity_house_id=20,
                       identity_house_name="Другой корпус", identity_house_valid=True,
                       identity_house_is_umbrella=False, identity_house_parent_id=55)
    assert normalize_daily_pick_identity(row) is None
    assert normalize_daily_pick_identity({**row, "identity_complex_is_umbrella": True}) is None


@pytest.mark.parametrize("address", ["Анет баба, 4", "ул. Анет баба 4/2", "Анет баба, 4, кв. 12"])
def test_ordinary_home_without_complex_keeps_only_source_address_attribution(address):
    row = identity_row(complex_id=None, complex_resolution=None, address=address)
    result = normalize_daily_pick_identity(row)
    assert result["complex_id"] is None
    assert result["canonical_complex_name"] == ""
    assert result["identity_scope"] == "source_address"
    assert "не подтверждена" in result["identity_note"]


@pytest.mark.parametrize("address", [None, "Анет баба", "Анет баба, 4, 6", "Астана, 4", "уч. 6",
                                     "Анет баба, 4 телефон 87010000000", "Анет баба, 87010000000"])
def test_missing_or_ambiguous_source_house_does_not_invent_exact_address(address):
    assert normalize_daily_pick_identity(identity_row(complex_id=None, complex_resolution=None, address=address)) is None


def test_identity_reads_are_bounded_separate_scans_not_correlated_per_listing():
    query = CANDIDATES_SQL.lower()
    assert "identity_walk" not in query and "exists(" not in query and "property_match_candidates" not in query
    assert len(IDENTITY_READS) == 5
    for name, read, maximum in IDENTITY_READS:
        assert "limit $1" in read.lower() and 0 < maximum <= 500_000
        assert not any(word in read.lower().split() for word in ("update", "insert", "delete", "alter"))


def catalog_row(identifier, **changes):
    return {"id": identifier, "name": f"ЖК {identifier}", "name_key": f"жк{identifier}",
            "canonical_id": None, "canonical_reason": None, "is_garbage": False, "is_street": False,
            "is_umbrella": False, "parent_complex_id": None, "year_built": 2020, **changes}


def annotate(rows, complexes, **extra):
    # These synthetic observations explicitly carry the catalog key unless
    # a test supplies a distinct raw source key.
    lookup = {item['id']: item for item in complexes}
    rows = [{**row, 'identity_raw_name_key': row.get('identity_raw_name_key',
             lookup.get(1, {}).get('name_key', ''))} for row in rows]
    return annotate_daily_pick_identity_rows(rows, complexes=complexes, aliases=extra.get("aliases", []),
        complex_reviews=extra.get("complex_reviews", []), relations=extra.get("relations", []),
        property_reviews=extra.get("property_reviews", []))


def test_batched_catalog_follows_complete_redirects_and_fails_closed_on_cycles_missing_and_bad_roots():
    catalog = [catalog_row(1), catalog_row(2, canonical_id=3), catalog_row(3, canonical_id=1),
               catalog_row(4, canonical_id=5), catalog_row(5, canonical_id=4), catalog_row(6, canonical_id=999),
               catalog_row(7, is_garbage=True), catalog_row(8, is_street=True), catalog_row(9, canonical_reason="junk_unmatched")]
    rows = [{"id": str(index), "complex_id": index, "complex_resolution": "url"} for index in (2, 4, 6, 7, 8, 9)]
    result = annotate(rows, catalog)
    assert result[0]["identity_complex_id"] == 1 and result[0]["identity_complex_valid"] is True
    assert normalize_daily_pick_identity(result[0])["complex_id"] == 1
    assert all(not row["identity_complex_valid"] and normalize_daily_pick_identity(row) is None for row in result[1:])


def test_children_imply_umbrella_even_without_the_legacy_flag_and_labels_remain_separate():
    catalog = [catalog_row(1), catalog_row(2, parent_complex_id=1)]
    row = {"id": "A", "complex_id": 1, "complex_resolution": "url", "resolved_house_id": 2, "house_attribution": "address", "identity_raw_name_key": "жк1"}
    result = annotate([row], catalog)[0]
    assert result["identity_complex_is_umbrella"] is True
    assert result["identity_house_is_umbrella"] is False
    public = normalize_daily_pick_identity(result)["complex_identity"]
    assert public["house_label"] == "ЖК 2" and public["umbrella_name"] == "ЖК 1"


def test_catalog_names_and_verified_aliases_detect_ambiguity_by_final_root_not_spelling_count():
    rows = [{"id": "A", "complex_id": 1, "complex_resolution": "name", "identity_raw_name_key": "same"}]
    catalog = [catalog_row(1, name_key="same"), catalog_row(2, canonical_id=1, name_key="same")]
    assert annotate(rows, catalog)[0]["identity_name_ambiguous"] is False
    assert annotate(rows, catalog + [catalog_row(3, name_key="same")])[0]["identity_name_ambiguous"] is True
    aliases = [{"complex_id": 3, "normalized_name": "same"}]
    assert annotate(rows, catalog + [catalog_row(3)], aliases=aliases)[0]["identity_name_ambiguous"] is True


def test_pending_complex_pairs_cover_aliases_and_reviewed_separation_prevents_false_blocking():
    catalog = [catalog_row(1), catalog_row(2), catalog_row(3, canonical_id=1)]
    rows = [{"id": "A", "complex_id": 3, "complex_resolution": "url"}]
    pending = [{"complex_id_a": 1, "complex_id_b": 2}]
    assert annotate(rows, catalog, complex_reviews=pending)[0]["identity_complex_review_pending"] is True
    assert annotate(rows, catalog, complex_reviews=pending, relations=pending)[0]["identity_complex_review_pending"] is False


def test_pending_property_evidence_covers_source_and_candidate_properties_including_relisted_members():
    rows = [{"id": "source-relist", "property_id": 10}, {"id": "candidate-relist", "property_id": 20},
            {"id": "other", "property_id": 30}, {"id": "source-old", "property_id": None}]
    pending = [{"listing_id": "source-old", "candidate_property_id": 20, "source_property_id": 10}]
    result = annotate(rows, [], property_reviews=pending)
    assert [row["identity_property_match_pending"] for row in result] == [True, True, False, True]


@pytest.mark.asyncio
async def test_separate_evidence_scans_share_one_read_only_transaction_and_keep_bounded_parameters(monkeypatch):
    from scripts import publish_daily_picks as publisher
    options_seen = []
    @asynccontextmanager
    async def transaction(**options):
        options_seen.append(options)
        yield
    source = {"id": "A", "complex_id": 1, "complex_resolution": "url"}
    connection = SimpleNamespace(transaction=transaction, close=AsyncMock(),
        fetch=AsyncMock(side_effect=[[source], [catalog_row(1)], [], [], [], []]))
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr("asyncpg.connect", connect)
    now = datetime.now(timezone.utc)
    result = await publisher.read_candidates("postgresql://unused:unused@127.0.0.1:1/identity_test", now)
    assert options_seen == [{"isolation": "repeatable_read", "readonly": True}]
    assert connect.await_args.kwargs["server_settings"]["default_transaction_read_only"] == "on"
    assert connect.await_args.kwargs["command_timeout"] == 60
    assert connection.fetch.await_args_list[0].args == (CANDIDATES_SQL, now)
    assert [call.args for call in connection.fetch.await_args_list[1:]] == [(query, maximum + 1) for _, query, maximum in IDENTITY_READS]
    assert result[0]["identity_complex_valid"] is True
    assert "identity_checked" not in source
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_incomplete_bounded_scan_fails_closed_and_closes_reader(monkeypatch):
    from scripts import publish_daily_picks as publisher
    @asynccontextmanager
    async def transaction(**options):
        yield
    connection = SimpleNamespace(transaction=transaction, close=AsyncMock(),
        fetch=AsyncMock(side_effect=[[], [catalog_row(1), catalog_row(2)]]))
    monkeypatch.setattr("asyncpg.connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(publisher, "IDENTITY_READS", (("complexes", "SELECT bounded LIMIT $1", 1),))
    with pytest.raises(ValueError, match="bounded complexes scan"):
        await publisher.read_candidates("postgresql://unused:unused@127.0.0.1:1/identity_test", datetime.now(timezone.utc))
    connection.close.assert_awaited_once()


def owner_row(**changes):
    return {"id": "1016000000", "url": "https://krisha.kz/a/show/1016000000", "is_owner": True,
            "price": 30_000_000, "rooms": 2, "area": 60, "floor": 5, "floors_total": 12, "source_complex_name": None, **changes}


def owner_check(**changes):
    return {"id": "1016000000", "source_url": "https://krisha.kz/a/show/1016000000", "is_owner": True,
            "status": "active", "sale_astana": True, "price": 30_000_000, "rooms": 2, "area": 60,
            "floor": 5, "floors_total": 12, "source_complex_name": None, **changes}


def test_owner_benefit_requires_matching_browser_evidence_and_does_not_trust_an_incoming_marker():
    row = owner_row(owner_source_verified=True)
    assert owner_evidence_rows([row])[0]["owner_source_verified"] is False
    assert owner_evidence_rows([row], {row["id"]: owner_check()})[0]["owner_source_verified"] is True
    assert owner_evidence_rows([owner_row(is_owner=False)], {row["id"]: owner_check()})[0]["owner_source_verified"] is False
    assert row["owner_source_verified"] is True


@pytest.mark.parametrize("change", [
    {"id": "1016000001"}, {"source_url": "https://krisha.kz/a/show/1016000001"}, {"status": "archived"},
    {"sale_astana": False}, {"is_owner": False}, {"is_owner": None}, {"price": 31_000_000},
    {"rooms": 3}, {"area": 61}, {"area": float("nan")}, {"floor": 4}, {"floors_total": None},
])
def test_owner_confirmation_on_a_different_or_incomplete_offer_does_not_qualify(change):
    row = owner_row()
    assert owner_evidence_rows([row], {row["id"]: owner_check(**change)})[0]["owner_source_verified"] is False


def test_unverified_private_preview_can_show_only_a_potential_owner_benefit():
    rows = owner_evidence_rows([owner_row(), owner_row(is_owner=False)], review_preview=True)
    assert [row["owner_source_verified"] for row in rows] == [True, False]
    # Actual publishing rebuilds this marker from browser records every time.
    assert not any(row["owner_source_verified"] for row in owner_evidence_rows(rows))


@pytest.mark.asyncio
async def test_real_generation_loads_owner_checks_before_the_initial_ranking(monkeypatch):
    import scripts.publish_daily_picks as publisher
    order = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_test_identity")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    def load_checks(path, now):
        order.append("checks")
        return {"1016000000": owner_check()}
    async def read(*args):
        order.append("read")
        return [owner_row()]
    def rank(rows, now):
        order.append("rank")
        assert rows[0]["owner_source_verified"] is True
        return {"items": []}
    monkeypatch.setattr(publisher, "load_source_checks", load_checks)
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "build_daily_picks", rank)
    await publisher.generate(SimpleNamespace(source_checks="browser-evidence.json", skip_photo_check=True),
                             datetime.now(timezone.utc))
    assert order == ["checks", "read", "rank"]


def test_public_identity_projection_strips_internal_evidence_and_requires_legacy_republication():
    normalized = normalize_daily_pick_identity(identity_row())
    normalized["complex_identity"].update(property_id=123, private_note="PRIVATE", address="PRIVATE")
    public = public_daily_pick_identity(normalized)
    assert "PRIVATE" not in str(public) and "property_id" not in public and "address" not in public
    assert public["status"] == "catalog_bound" and public["canonical_name"] == "Канонический ЖК"
    with pytest.raises(ValueError):
        public_daily_pick_identity({"complex_name": "An old, unsupported label"})


@pytest.mark.parametrize("change", [{"checked": False}, {"canonical_id": True}, {"binding_method": "geo"},
                                      {"binding_method": []}, {"house_id": 10, "house_label": "Канонический ЖК"},
                                      {"status": "source_address_only"}, {"status": "house_from_source"}])
def test_public_identity_never_upgrades_unverified_or_project_metadata_to_exact_building(change):
    public = public_daily_pick_identity(normalize_daily_pick_identity(identity_row()))
    with pytest.raises(ValueError):
        validate_public_daily_identity({**public, **change})
