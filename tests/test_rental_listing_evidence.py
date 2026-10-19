"""Individual listing rent evidence must not inherit legacy investment claims."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot.core import comparison_rental_estimate as rents
from bot.core import listing_detail, insights, complex_metrics

NOW = datetime.now(timezone.utc)


def sale(**changes):
    return {"id": "1234567", "is_active": True, "complex_id": 10,
            "complex_resolution": "url", "rooms": 2, "area": 60,
            "price": 30_000_000, "market_type": "secondary", "complex_name": "",
            "est_rent": 900_000, "yield_pct": 36, "net_yield_pct": 30,
            "payback_years": 2.8, "rent_source": "город (все комн.), n=400",
            **changes}


def rental_rows():
    return [{"id": str(100000 + i), "complex_id": 10, "rooms": 2, "area": 59 + i*.2,
             "price": price, "floor": i + 1, "floors_total": 12, "is_active": True,
             "is_duplicate": False, "dup_needs_review": False, "prop_type": "apartment",
             "rent_period": "monthly", "complex_binding_verified": True,
             "city": "astana", "last_seen": NOW - timedelta(hours=i + 1)}
            for i, price in enumerate((200_000, 220_000, 240_000, 260_000, 280_000))]


@pytest.mark.asyncio
async def test_single_offer_query_is_narrowed_before_sort_and_returns_only_asking_range(monkeypatch):
    from bot.db import pg
    fetch = AsyncMock(return_value=rental_rows())
    monkeypatch.setattr(pg, "fetch", fetch)
    result = await rents.rental_estimate_for_listing(sale(), now=NOW)
    sql, observed, cid, rooms, area = fetch.await_args.args
    assert (observed, cid, rooms, area) == (NOW, 10, 2, 60)
    assert "bound.id=$2" in sql and "r.rooms=$3" in sql
    assert sql.index("r.rooms=$3") < sql.index("ORDER BY r.last_seen DESC")
    assert "r.area BETWEEN $4::double precision*0.85" in sql and "LIMIT 200" in sql
    assert "TRUE AS complex_binding_verified" in sql
    assert result["monthly_median"] == 240_000 and result["asking_prices"] is True
    assert "yield_pct" not in result and "ids" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"complex_id": None, "complex_name": "same name"}, {"complex_resolution": "geo"},
    {"complex_resolution": "unbound"}, {"complex_id": None, "resolved_house_id": 10, "house_attribution": "geo"},
    {"complex_resolution": "house", "house_attribution": "geo"},
    {"is_active": False}, {"archived_at": NOW}, {"is_duplicate": True}, {"dup_needs_review": True},
    {"rooms": None}, {"rooms": True}, {"area": float("nan")}, {"city": "almaty"},
    {"market_type": "primary", "build_status": "under_construction"},
])
async def test_unknown_identity_or_trait_does_not_guess_from_city_name_or_legacy_values(monkeypatch, changes):
    from bot.db import pg
    fetch = AsyncMock(return_value=rental_rows())
    monkeypatch.setattr(pg, "fetch", fetch)
    assert await rents.rental_estimate_for_listing(sale(**changes), now=NOW) is None
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_verified_house_id_and_canonical_alias_mapping(monkeypatch):
    from bot.db import pg
    rows = rental_rows()
    for row in rows:
        row["complex_id"] = 20
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(pg, "fetch", fetch)
    result = await rents.rental_estimate_for_listing(sale(complex_id=None, resolved_house_id=10,
        complex_resolution=None, house_attribution="address"), now=NOW)
    assert result["monthly_median"] == 240_000
    assert fetch.await_args.args[2] == 10  # SQL maps the explicit alias FK to canonical 20.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private-db-error"), TimeoutError("timed out")])
async def test_source_query_failure_suppresses_rent_only(monkeypatch, failure):
    from bot.db import pg
    monkeypatch.setattr(pg, "fetch", AsyncMock(side_effect=failure))
    assert await rents.rental_estimate_for_listing(sale(), now=NOW) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("row_change", [{"complex_binding_verified": False}, {"last_seen": NOW-timedelta(hours=73)},
                                       {"is_active": False}, {"title": "Сдаётся посуточно"}])
async def test_actual_flow_revalidates_qualified_rows_and_minimum_count(monkeypatch, row_change):
    from bot.db import pg
    rows = rental_rows()
    rows[0].update(row_change)
    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=rows))
    assert await rents.rental_estimate_for_listing(sale(), now=NOW) is None


def patch_listing_flow(monkeypatch, rows):
    from bot.db import pg
    from bot.core import bargain, listing_intel, listing_risks, price_forecast_runtime
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value=sale()))
    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr(bargain, "get_comparables", AsyncMock(return_value=([], {})))
    monkeypatch.setattr(bargain, "analyze_bargain", lambda *args: {})
    monkeypatch.setattr(listing_intel, "build_negotiation_points", lambda *args: [])
    monkeypatch.setattr(listing_intel, "build_seller_questions", lambda *args: [])
    monkeypatch.setattr(listing_risks, "compute_listing_risks_safe", AsyncMock(return_value={}))
    monkeypatch.setattr(listing_detail, "fetch_liquidity_forecast", AsyncMock(return_value=None))
    monkeypatch.setattr(price_forecast_runtime, "forecast_for_offer", lambda *args, **kwargs: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_evidence", [False, True])
async def test_full_listing_modal_never_exports_legacy_city_rent_yield_or_high_yield_badge(monkeypatch, with_evidence):
    patch_listing_flow(monkeypatch, rental_rows() if with_evidence else [])
    result = await listing_detail.build_listing_detail("1234567", "admin", similar_limit=0)
    for key in ("est_rent", "yield_pct", "net_yield_pct", "payback_years"):
        assert result[key] is None
    assert result["high_yield"] is False
    assert bool(result["rental_estimate"]) is with_evidence
    assert "город" not in result["rent_source"]


def test_insights_ignore_legacy_manual_growth_and_bank_verdict(monkeypatch):
    monkeypatch.setattr(insights, "total_return_pct", lambda *args: pytest.fail("Legacy financial formula called"))
    result = insights.build_insights_block(sale())
    for forbidden in ("выгоднее депозита", "депозит доходнее", "рост ~", "%/год", "аренда покрывает", "900"):
        assert forbidden not in result


def test_insights_use_only_fresh_validated_asking_range_without_achieved_income():
    estimate = rents.estimate_rental_offer(sale(), rental_rows(), NOW)
    result = insights.build_insights_block(sale(rental_estimate=estimate))
    assert "Похожие квартиры в этом ЖК" in result
    assert "заявленные ставки, а не подтверждённый доход" in result
    assert "депозит" not in result and "аренда покрывает" not in result
    estimate["oldest_observed_at"] = (NOW-timedelta(hours=73)).isoformat()
    assert "Похожие квартиры" not in insights.build_insights_block(sale(rental_estimate=estimate))


def test_complex_rental_counts_filter_all_memberships_without_changing_sale_statistics():
    sql = complex_metrics.CURRENT_METRICS_SQL
    rental_part = sql.split("eligible_rentals AS", 1)[1].split("UPDATE complexes", 1)[0]
    for clause in ("r.city = 'astana'", "r.prop_type = 'apartment'", "r.is_active IS TRUE",
                   "r.archived_at IS NULL", "r.dup_needs_review", "r.duplicate_of", "interval '72 hours'",
                   "r.complex_url", "canonical.krisha_url", "sl.source = 'krisha'", "sl.confidence >= 0.85"):
        assert clause in rental_part
    assert "FROM eligible_rentals r JOIN complexes child" in rental_part
    sale_part = sql.split("eligible_rentals AS", 1)[0]
    for clause in ("count(*) FILTER (WHERE l.is_active IS NOT FALSE) AS active_count",
                   "count(*) FILTER (WHERE l.is_active IS FALSE) AS sold_count",
                   "avg(l.price / NULLIF(l.area, 0)) FILTER (WHERE l.is_active IS NOT FALSE) AS price_m2",
                   "avg(l.yield_pct) FILTER (WHERE l.is_active IS NOT FALSE AND l.yield_pct > 0) AS yield_pct"):
        assert clause in sale_part
