"""Pure fixtures only: complete inventory, identity vetoes, honest cohort units."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core.rental_editorial_slice import (
    AREA_EDGES, MAX_SCAN, area_profile, build_slice, condition_claim, rental_rows_sql,
)

NOW = datetime(2026,10,19,7,tzinfo=timezone.utc)


def catalog(identifier=10, **changes):
    return {"id":identifier,"name":"Алма","name_key":"алма","address":"Анет баба 1",
            "canonical_id":None,"canonical_reason":None,"parent_complex_id":None,
            "is_garbage":False,"is_street":False,"is_umbrella":False,**changes}


def evidence(**changes):
    return {"complexes":[catalog()],"aliases":[],"complex_reviews":[],"relations":[],"source_reviews":[],**changes}


def row(index=0, **changes):
    identifier = str(10000000+index)
    return {"id":identifier,"url":f"https://krisha.kz/a/show/{identifier}",
        "city":"astana","prop_type":"apartment","is_active":True,"archived_at":None,
        "rent_period":"monthly","rent_period_conflict":False,"is_duplicate":False,
        "dup_needs_review":False,"duplicate_of":None,"price":200000+index*1000,
        "area":40.,"rooms":1,"floor":5,"floors_total":10,"address":f"Анет баба {index+1}",
        "district":"Есильский р-н","complex_id":10,"bound_canonical_id":10,
        "named_canonical_id":None,"source_canonical_id":10,"complex_name":"Алма",
        "complex_url":"https://krisha.kz/complex/show/1234/","identity_raw_name_key":"алма",
        "title":"Квартира","description":"Евроремонт, полностью меблирована",
        "description_checked_at":NOW-timedelta(hours=1),"last_seen":NOW-timedelta(minutes=index),
        "rental_episode_started_at":NOW-timedelta(days=2),"rental_episode_last_seen_at":NOW-timedelta(minutes=index),
        **changes}


def build(rows, ev=None, **kwargs):
    return build_slice(rows,ev or evidence(),{},now=NOW,expected_count=len(rows),**kwargs)


def test_ready_descriptive_is_not_rank_or_physical_identity():
    result=build([row(i) for i in range(20)])
    cohort=next(c for c in result["cohorts"] if c["scope"]=="district")
    assert cohort["descriptive_ready"] is True
    assert cohort["n_unique_source_after_conservative_dedup"]==20
    assert cohort["n_physical"] is None
    assert cohort["ranking_ready"] is False
    assert cohort["condition_verified"] is False
    assert cohort["furnishing_verified"] is False
    assert result["ranking_ready"] is False
    assert result["complete_database_scan"] and result["market_coverage"]=="unknown"
    assert result["audit"]["fresh72"]==20


def test_full_scan_does_not_silently_turn_daily_or_unknown_into_monthly():
    data=build([row(0),row(1,rent_period="daily"),row(2,rent_period="unknown")])
    assert data["active_astana_apartment_rows_scanned"]==3
    assert data["active_period_counts"]=={"daily":1,"monthly":1,"unknown":1}
    assert len(data["records"])==1


@pytest.mark.parametrize("change,reason",[
    ({"last_seen":NOW-timedelta(hours=72,seconds=1)},"stale_missing_or_future_observation"),
    ({"last_seen":NOW+timedelta(seconds=1)},"stale_missing_or_future_observation"),
    ({"last_seen":None},"stale_missing_or_future_observation"),
    ({"rent_period_conflict":True},"period_conflict_or_unknown_flag"),
    ({"is_duplicate":True},"known_duplicate"),
    ({"duplicate_of":"1234"},"known_duplicate"),
    ({"dup_needs_review":True},"duplicate_review_pending"),
    ({"archived_at":NOW},"not_active_unarchived"),
    ({"price":True},"invalid_monthly_price"),
    ({"price":49999},"invalid_monthly_price"),
    ({"area":float("nan")},"invalid_area"),
    ({"area":301},"invalid_area"),
    ({"rooms":0},"invalid_rooms"),
    ({"title":"Посуточно"},"contrary_period_text"),
    ({"address":"Анет баба"},"missing_or_ambiguous_house_address"),
    ({"address":"Анет баба 1, 2"},"missing_or_ambiguous_house_address"),
    ({"address":"Нура р-н, Анет баба 1"},"district_address_conflict"),
    ({"district":"центр города"},"missing_or_unknown_district"),
    ({"url":"http://krisha.kz/a/show/10000000"},"invalid_source_identity"),
    ({"url":"https://krisha.kz/a/show/10000001"},"invalid_source_identity"),
    ({"url":"https://krisha.kz.evil/a/show/10000000"},"invalid_source_identity"),
    ({"url":"https://user@krisha.kz/a/show/10000000"},"invalid_source_identity"),
    ({"url":"https://krisha.kz:443/a/show/10000000"},"invalid_source_identity"),
    ({"url":"https://krisha.kz/a/show/10000000?q=phone"},"invalid_source_identity"),
    ({"source_canonical_id":11},"source_catalog_link_conflict_or_missing"),
    ({"identity_raw_name_key":"другой"},"stored_source_name_conflict"),
])
def test_bad_record_is_removed_before_any_median(change,reason):
    result=build([row(0,**change)])
    assert reason in result["records"][0]["exclusions"]
    assert result["n_unique_source_after_conservative_dedup"]==0
    assert result["cohorts"]==[]


@pytest.mark.parametrize("changes",[
    {"is_umbrella":True},{"is_garbage":True},{"is_street":True},
    {"canonical_reason":"junk_unmatched"},{"canonical_id":999},
])
def test_invalid_or_umbrella_catalog_is_not_downgraded_into_district(changes):
    result=build([row()],evidence(complexes=[catalog(**changes)]))
    assert result["n_unique_source_after_conservative_dedup"]==0


def test_source_address_only_is_allowed_without_guessing_a_complex():
    result=build([row(complex_id=None,bound_canonical_id=None,named_canonical_id=None,
                      source_canonical_id=None,complex_name=None,complex_url=None,identity_raw_name_key="")])
    r=result["records"][0]
    assert r["exclusions"]==[] and r["identity_status"]=="source_address_only"
    assert [c["scope"] for c in result["cohorts"]]==["district"]


def test_bad_bound_fk_is_not_erased_into_unbound_address_fallback():
    result=build([row(complex_id=999,bound_canonical_id=None,named_canonical_id=None,
                      source_canonical_id=None,complex_name=None,complex_url=None,identity_raw_name_key="")])
    assert "unresolved_catalog_identity" in result["records"][0]["exclusions"]
    assert result["n_unique_source_after_conservative_dedup"]==0


def test_null_and_nonfinite_attributes_are_audited_and_json_is_strict():
    from decimal import Decimal
    rows=[row(0,area=None,price=None,rooms=None),row(1,area=float("nan")),row(2,area=Decimal("40"))]
    result=build(rows)
    assert result["n_unique_source_after_conservative_dedup"]==0
    assert all(r["area_m2"] is None for r in result["records"])
    json.dumps(result,allow_nan=False)


def test_reviewed_alias_is_required_not_redirect_name_or_name_prefix():
    r=row(complex_name="Старая Алма",identity_raw_name_key="стараяалма")
    redirect=catalog(11,name="Старая Алма",name_key="стараяалма",canonical_id=10)
    result=build([r],evidence(complexes=[catalog(),redirect]))
    assert "stored_source_name_conflict" in result["records"][0]["exclusions"]
    result=build([r],evidence(complexes=[catalog(),redirect],aliases=[{"complex_id":10,"normalized_name":"стараяалма","status":"verified"}]))
    assert result["records"][0]["exclusions"]==[]


def test_pending_complex_review_veto_and_documented_separation():
    ev=evidence(complexes=[catalog(),catalog(11,name="Бета",name_key="бета")],
        complex_reviews=[{"complex_id_a":10,"complex_id_b":11}])
    assert "complex_duplicate_review_pending" in build([row()],ev)["records"][0]["exclusions"]
    ev["relations"]=[{"complex_id_a":10,"complex_id_b":11}]
    assert build([row()],ev)["records"][0]["exclusions"]==[]


def test_pending_source_review_through_redirect_vetoes_family():
    ev=evidence(complexes=[catalog(),catalog(12,canonical_id=10)],
        source_reviews=[{"complex_id":12,"conflict_with_complex_id":None,"kind":"conflict"}])
    assert "complex_source_review_pending" in build([row()],ev)["records"][0]["exclusions"]


def test_exact_child_address_preserves_fraction_and_rejects_colliding_child():
    parent=catalog(20,name="Зонтик",name_key="зонтик",is_umbrella=True)
    child=catalog(10,address="Анет баба 6/1",parent_complex_id=20)
    r=row(address="Анет баба 6/1",source_canonical_id=20)
    result=build([r],evidence(complexes=[parent,child]))
    assert result["records"][0]["exclusions"]==[]
    assert result["records"][0]["house_id"]==10
    assert {c["scope"] for c in result["cohorts"]}=={"district","complex","house"}
    assert build([dict(r,address="Анет баба 6")],evidence(complexes=[parent,child]))["n_unique_source_after_conservative_dedup"]==0
    collision=catalog(11,name="Алма 2",name_key="алма2",address="Анет баба 6/1",parent_complex_id=20)
    assert build([r],evidence(complexes=[parent,child,collision]))["n_unique_source_after_conservative_dedup"]==0


def test_precautionary_duplicate_suppression_is_global_and_newest_complete_row_wins():
    older=row(0,address="Анет баба 1",price=250000,last_seen=NOW-timedelta(hours=2))
    newer=row(1,address="Анет баба 1",price=200000,last_seen=NOW)
    result=build([older,newer])
    assert result["eligible_monthly_source_ids_before_precautionary_dedup"]==2
    assert result["n_unique_source_after_conservative_dedup"]==1
    assert result["dedup_is_verified_physical_identity"] is False
    assert result["records"][0]["retained_after_precautionary_dedup"] is False
    for c in result["cohorts"]:
        assert c["monthly_asking_price_kzt"]["median"]==200000
    assert build([newer,older])["cohorts"]==result["cohorts"]


def test_unknown_floor_does_not_merge_distinct_ids():
    result=build([row(0,address="Анет баба 1",floor=None),row(1,address="Анет баба 1",floor=None)])
    assert result["n_unique_source_after_conservative_dedup"]==2
    assert result["n_physical"] is None


def test_inclusive_quantiles_and_per_sqm_use_individual_ratios():
    result=build([row(i,price=p,area=a) for i,(p,a) in enumerate(((100000,35),(200000,40),(300000,42),(400000,43)))])
    c=next(c for c in result["cohorts"] if c["scope"]=="district")
    assert c["monthly_asking_price_kzt"]["p25"]==175000
    assert c["monthly_asking_price_kzt"]["median"]==250000
    assert c["monthly_asking_price_kzt"]["p75"]==325000
    assert c["monthly_asking_kzt_per_m2"]["median"]==6071.4286


def test_stale_description_does_not_refresh_condition_or_furnishing():
    result=build([row(description_checked_at=NOW-timedelta(hours=73))])
    assert result["records"][0]["condition_claim"]=="unknown"
    assert result["records"][0]["furnishing_claim"]=="unknown"
    assert result["ready_descriptive_cohorts"]==0


def test_fresh_title_claim_does_not_require_old_description():
    result=build([row(title="Евроремонт, полностью меблирована",description_checked_at=NOW-timedelta(days=10))])
    assert result["records"][0]["condition_claim"]=="euro_renovation_claim"
    assert result["records"][0]["furnishing_claim"]=="full"


@pytest.mark.parametrize("text",["без евроремонта","Евроремонта нет","Не дизайнерский ремонт","Евроремонт, требуется ремонт","Евроремонт и черновая отделка"])
def test_contradictory_condition_claim_abstains(text):
    assert condition_claim(text,None)=="unknown"


def test_unknown_claims_never_form_descriptive_ready_cohort_even_with_many_sources():
    result=build([row(i,description="Просторная квартира",title="Квартира") for i in range(20)])
    assert result["ready_descriptive_cohorts"]==0
    assert any(c["n_unique_source_after_conservative_dedup"]==20 for c in result["profile_diagnostics"])
    assert all(c["descriptive_ready"] is False for c in result["profile_diagnostics"])


def test_area_bins_partition_whole_domain_without_overlap_or_wider_than_15pct():
    for edge in AREA_EDGES:
        profile=area_profile(edge)
        assert profile is not None and profile["maximum_deviation_pct"]<=15
    assert area_profile(299.99)["id"]==area_profile(300)["id"]
    assert area_profile(34)["id"]=="a34_44"
    assert area_profile(33.99)["id"]=="a26_34"
    assert area_profile(19.99) is None and area_profile(True) is None


@pytest.mark.parametrize("expected,bound",[(None,10),(False,10),(1,10),(2,1)])
def test_incomplete_or_overflow_scan_refuses(expected,bound):
    with pytest.raises(ValueError,match="Incomplete"):
        build_slice([row(0),row(1)],evidence(),{},now=NOW,expected_count=expected,max_scan=bound)


def test_duplicate_ids_evidence_overflow_and_foreign_scope_refuse():
    with pytest.raises(ValueError,match="Repeated source ID"): build([row(),row()])
    with pytest.raises(ValueError,match="Incomplete identity"): build([row()],evidence(aliases=[{}]*100001))
    with pytest.raises(ValueError,match="outside its declared scope"): build([row(city="almaty")])


def test_private_projection_never_contains_description_contacts_or_addresses():
    r=row(description="Евроремонт полностью меблирована. +77771234567 contact@example.test <script>",property_id=42)
    result=build([r])
    text=json.dumps(result,ensure_ascii=False)
    assert "+77771234567" not in text and "@example" not in text and "<script>" not in text
    assert "Анет баба" not in text and '"property_id"' not in text
    assert result["n_physical"] is None


def test_sql_scans_all_active_periods_reuses_source_identity_and_binds_limit():
    sql=rental_rows_sql()
    assert "rental_roots AS MATERIALIZED" in sql and "rental_source_keys" in sql
    assert "r.city='astana' AND r.prop_type='apartment' AND r.is_active IS TRUE" in sql
    assert "ORDER BY r.id LIMIT $1" in sql
    assert "r.rent_period='monthly'" not in sql
    assert "UPDATE " not in sql and "INSERT " not in sql and "SELECT r.*" not in sql
