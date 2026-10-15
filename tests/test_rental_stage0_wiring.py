"""Period-safe collector wiring and explicit yield assumptions; no live data."""
import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot.core.rental_yield import NetYieldParams, monthly_yield_scenario


@pytest.mark.parametrize('changes', [dict(occupancy_share=float('nan')),dict(opex_share=float('inf')),
    dict(occupancy_share=True),dict(opex_share=-.01),dict(purchase_cost_multiplier=.99)])
def test_invalid_assumptions_cannot_produce_net_yield(changes):
    with pytest.raises(ValueError):
        NetYieldParams(**changes)


def test_explicit_default_scenario_preserves_formula_and_can_be_changed():
    result=monthly_yield_scenario(40_000_000,280_000,NetYieldParams())
    assert result['gross_yield_pct']==8.4 and result['net_yield_pct']==7.0
    assert result['net_annual']==2_856_000
    assert result['scenario'] and result['asking_prices']
    changed=NetYieldParams.from_settings(lambda key, default:{'NET_YIELD_VACANCY':'.8','NET_YIELD_OPEX_SHARE':'.2','NET_YIELD_PURCHASE_COST':'1.1'}[key])
    assert monthly_yield_scenario(40_000_000,280_000,changed)['net_yield_pct']==4.6


@pytest.mark.parametrize('price,rent', [(0,280000),(40000000,0),(True,280000),(40000000,float('nan'))])
def test_unknown_values_do_not_become_zero_yield(price,rent):
    assert monthly_yield_scenario(price,rent,NetYieldParams()) is None


def test_main_bot_never_schedules_a_second_rental_loop():
    source=Path('bot/main.py').read_text(encoding='utf-8')
    main=next(node for node in ast.parse(source).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='main')
    assert not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='_rental_loop' for node in ast.walk(main))
    assert not Path('bot/core/rental_index.py').exists()


@pytest.mark.asyncio
async def test_daily_and_monthly_cursors_do_not_share_completion_or_timing(monkeypatch):
    import service_rental as service
    from bot.core.rental_service_metrics import RentalCyclePlan
    monkeypatch.setattr(service,'record_rental_cycle',AsyncMock())
    operations=service.RentalOperations(AsyncMock(return_value={'page':1,'saved':2,'completed':True}),
        *[AsyncMock() for _ in range(8)])
    state=service.RentalServiceState()
    plan=RentalCyclePlan(backfill_batch=0,archive_batch=0,db_every=30)
    monthly='/arenda/kvartiry/astana/'
    daily='/arenda/kvartiry-posutochno/astana/'
    await service.run_one_cycle(None,monthly,'apartment',state,operations,plan,type_count=4)
    await service.run_one_cycle(None,daily,'apartment',state,operations,plan,type_count=4)
    assert state.completed_types=={'apartment','apartment_daily'}
    assert set(state.last_success)=={'apartment','apartment_daily'}
    operations.rebuild_index.assert_not_awaited()
    result=await service.run_one_cycle(None,monthly,'apartment',state,operations,plan,type_count=4)
    assert result['rent_period']=='monthly' and result['scope']=='apartment'
    assert 'apartment' in state.page_intervals and 'apartment_daily' not in state.page_intervals
    assert any(call.args[0]=='RENTAL_CRAWL_V1_APARTMENT_PAGE_INTERVAL_SEC' for call in operations.set_setting.await_args_list)


def test_stage_zero_schema_is_additive_and_does_not_invent_historical_periods():
    first=Path('migrations/109_rental_period_complex_hex.sql').read_text(encoding='utf-8')
    second=Path('migrations/110_rental_index_v2.sql').read_text(encoding='utf-8')
    assert "DEFAULT 'unknown'" in first and 'rent_period_conflict' in first
    assert 'UNIQUE NULLS NOT DISTINCT' in second and 'source_oldest_at' in second
    assert 'UPDATE rental_listings' not in first and 'DELETE FROM rental_listings' not in first
