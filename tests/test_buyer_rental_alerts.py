"""Typed buyer alerts use mocked storage and bots only; no delivery or DB."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from unittest.mock import AsyncMock
import asyncio
import json

import pytest

from bot.core import buyer_alerts as alerts
from bot.core.rental_economics import build_rental_economics
from bot.core.rental_yield import NetYieldParams


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/rental_stage1_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    denied = AsyncMock(side_effect=AssertionError("No real database or Telegram"))
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    monkeypatch.setattr("bot.db.pg.init_pool", denied)
    monkeypatch.setattr("bot.db.settings.get_int", lambda key, default: default)


def candidate(**values):
    now = datetime.now(timezone.utc)
    row = {"id": "10000001", "price": 40000000, "area": 60.0, "rooms": 2,
           "complex_id": 10, "complex_name": "<ЖК>", "complex_resolution": "url", "resolved_house_id": None,
           "house_attribution": None, "district": "Есиль", "is_active": True, "is_duplicate": False,
           "dup_needs_review": False, "archived_at": None, "duplicate_of": None,
           "first_seen": now-timedelta(hours=3), "last_seen": now-timedelta(minutes=5),
           "property_id": 77, "property_identity_status": "confirmed", "effective_score": 80, **values}
    ix = {"level": "район", "median_price": 280000, "p25_price": 250000, "p75_price": 310000,
          "price_per_sqm": 5000, "sample_count": 10, "rent_period": "monthly", "policy_version": "3", "window_days": 30,
          "source_oldest_at": now-timedelta(hours=3), "source_newest_at": now-timedelta(hours=1), "updated_at": now-timedelta(minutes=1)}
    row["rental_economics"] = build_rental_economics(row, ix, NetYieldParams(), now=now)
    return row


@pytest.mark.parametrize("params", [{}, {"net_yield_min": True}, {"net_yield_min": float("nan")},
    {"net_yield_min": 0}, {"net_yield_min": 101}, {"net_yield_min": 7,"rooms": [True]},
    {"net_yield_min": 7,"budget_max": "40000000"}, {"net_yield_min": 7,"districts": [""]}])
def test_reject_invalid_subscription(params):
    with pytest.raises(ValueError):
        alerts.validate_params("yield", params)


def test_command_uses_existing_profile_and_explicit_filters():
    assert alerts.parse_yield_command('/yield 7.5 районы="Есиль,Алматы" жк=10,10', {"budget_max": 40000000,"rooms": [2]}) == {
        "net_yield_min": 7.5,"budget_max": 40000000,"rooms": [2],"districts": ["Алматы","Есиль"],"complex_ids": [10]}


def test_yield_alert_crossing_and_medium_required():
    row = candidate()
    assert alerts.evaluate_yield_alert(row, {"net_yield_min": 7})["net_yield_pct"] == 7.5
    assert alerts.evaluate_yield_alert(row, {"net_yield_min": 8}) is None
    assert alerts.evaluate_yield_alert(row, {"net_yield_min": 7,"rooms": [1]}) is None
    assert alerts.evaluate_yield_alert(row, {"net_yield_min": 7}, event="price_drop", previous_economics={"confidence":"medium","net_yield_pct":6})
    for previous in (None, {"confidence":"low","net_yield_pct":6}, {"confidence":"high","net_yield_pct":7}):
        assert alerts.evaluate_yield_alert(row, {"net_yield_min":7}, event="price_drop", previous_economics=previous) is None
    row["rental_economics"]["confidence"] = "low"
    row["rental_economics"]["rent_source"]["level"] = "город"
    assert alerts.evaluate_yield_alert(row, {"net_yield_min":7}) is None


@pytest.mark.parametrize("encoded",[False,True])
def test_raw_sale_row_uses_existing_buyer_deal_readmodel_without_map_bonuses(encoded):
    row=candidate(score_total=73,hex_details={"deal":73,"confidence":60})
    row["effective_score"]=98  # Generated map ranking includes extra bonuses.
    if encoded: row["hex_details"]=json.dumps(row["hex_details"])
    payload=alerts.evaluate_yield_alert(row,{"net_yield_min":7})
    assert payload["deal_score"]==73
    assert "Deal Score: 73/100" in alerts.render_alert("yield",payload)
    del row["effective_score"]  # No synthetic alias is needed in raw fixtures.
    assert alerts.evaluate_yield_alert(row,{"net_yield_min":7})["deal_score"]==73


@pytest.mark.parametrize("details",[{"deal":73,"confidence":49},None,"not-json"])
def test_unready_buyer_deal_score_hidden_without_legacy_fallback(details):
    payload=alerts.evaluate_yield_alert(candidate(score_total=73,hex_details=details),{"net_yield_min":7})
    assert payload["deal_score"] is None and "Deal Score:" not in alerts.render_alert("yield",payload)


class Ledger:
    def __init__(self, cap=5):
        self.cap, self.notices, self.enabled = cap, {}, True
        self.merges, self.lock = {}, asyncio.Lock()
        self.attempts=0
        self.params = {"net_yield_min": 7}
        self.subscription = {"id":1,"user_id":123,"params":self.params}
        self.current = candidate()
    @asynccontextmanager
    async def acquire(self):
        yield self
    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield self
    async def execute(self, sql, *args):
        if "delivery_status='" in sql:
            status = sql.split("delivery_status='")[1].split("'")[0]
            if status=="cancelled" and self.notices[args[1]]["status"]=="sending": self.attempts-=1
            self.notices[args[1]]["status"] = status
            if status=="sending": self.attempts+=1
        return "UPDATE 1"
    async def fetch(self, sql, *args):
        return [self.subscription] if self.enabled else []
    async def fetchval(self, sql, *args):
        if "s.params=$3::jsonb" in sql:
            return self.enabled and json.loads(args[2])==self.params
        if "SELECT count(*)<$2" in sql:
            return self.attempts<args[1]
        if "AND\n                    (SELECT count" in sql:
            return self.enabled and len(self.notices) < args[2]
        if "SELECT EXISTS(SELECT 1 FROM user_listing_notifications n" in sql:
            return any(item["property_id"] == args[2] or self.merges.get(item["payload"]["listing_id"]) == args[2] for item in self.notices.values())
        if "INSERT INTO user_listing_notifications" in sql:
            if args[1] in self.notices:
                return None
            self.notices[args[1]] = {"property_id":args[4],"payload":json.loads(args[5]),"status":"pending"}
            return args[0]
        return self.enabled
    async def fetchrow(self, sql, *args):
        if "FROM apartment_listings" in sql:
            return self.current
        for identifier, item in self.notices.items():
            if item["status"] == "pending" and self.enabled and self.attempts<self.cap:
                return {"user_id":123,"listing_id":identifier,"kind":"yield","payload":item["payload"],"params":self.params}
        return None
    def install(self, monkeypatch):
        monkeypatch.setattr("bot.db.pg.get_pool", lambda:self)
        for name in ("fetch","fetchval","fetchrow","execute"):
            monkeypatch.setattr("bot.db.pg."+name,getattr(self,name))


@pytest.mark.asyncio
async def test_concurrent_enqueue_one_property_relisting_and_daily_cap(monkeypatch):
    ledger = Ledger()
    ledger.install(monkeypatch)
    row = candidate()
    results = await asyncio.gather(*(alerts.enqueue_typed_candidate("yield",row) for _ in range(10)))
    assert sum(item["queued"] for item in results) == 1
    assert (await alerts.enqueue_typed_candidate("yield",candidate(id="10000002")))["queued"] == 0
    for identifier in range(78,85):
        await alerts.enqueue_typed_candidate("yield",candidate(id=str(10000000+identifier), property_id=identifier))
    assert len(ledger.notices) == 5


@pytest.mark.asyncio
async def test_provisional_duplicate_stale_and_merged_identity_are_blocked(monkeypatch):
    ledger=Ledger(); ledger.install(monkeypatch)
    for fields in ({"property_id":None},{"property_id":True},{"property_identity_status":"provisional"},
                   {"is_duplicate":True},{"last_seen":datetime.now(timezone.utc)-timedelta(days=4)}):
        assert (await alerts.enqueue_typed_candidate("yield",candidate(**fields)))["queued"] == 0
    assert (await alerts.enqueue_typed_candidate("yield",candidate()))["queued"] == 1
    ledger.merges["10000001"]=90
    assert (await alerts.enqueue_typed_candidate("yield",candidate(id="10000002",property_id=90)))["queued"] == 0


@pytest.mark.asyncio
async def test_dispatch_is_at_most_once_and_uses_fresh_candidate(monkeypatch):
    ledger=Ledger(); ledger.install(monkeypatch)
    await alerts.enqueue_typed_candidate("yield",candidate())
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",AsyncMock(return_value=ledger.current["rental_economics"]))
    bot=type("MockBot",(),{"send_message":AsyncMock()})()
    results=await asyncio.gather(alerts.dispatch_pending(bot),alerts.dispatch_pending(bot))
    assert sum(item["sent"] for item in results)==1
    bot.send_message.assert_awaited_once()
    text=bot.send_message.await_args.args[1]
    assert "Сценарная чистая" in text and "https://hatuli.ai-groundtruth.com/listing/10000001" in text
    assert (await alerts.dispatch_pending(bot))["sent"]==0


@pytest.mark.asyncio
async def test_dispatch_cancels_lost_consent_and_changed_source(monkeypatch):
    ledger=Ledger(); ledger.install(monkeypatch)
    await alerts.enqueue_typed_candidate("yield",candidate())
    bot=type("MockBot",(),{"send_message":AsyncMock()})()
    ledger.enabled=False
    assert (await alerts.dispatch_pending(bot))["sent"]==0
    ledger.enabled=True
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",AsyncMock(return_value=None))
    assert (await alerts.dispatch_pending(bot))["sent"]==0
    assert ledger.notices["yield:77"]["status"]=="cancelled"
    assert ledger.attempts==0
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_send_failure_is_never_automatically_retried(monkeypatch):
    ledger=Ledger(); ledger.install(monkeypatch)
    await alerts.enqueue_typed_candidate("yield",candidate())
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",AsyncMock(return_value=ledger.current["rental_economics"]))
    bot=type("MockBot",(),{"send_message":AsyncMock(side_effect=TimeoutError)})()
    await alerts.dispatch_pending(bot); await alerts.dispatch_pending(bot)
    assert ledger.notices["yield:77"]["status"]=="failed"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_sublease_hook_explicitly_blocked_without_property_identity():
    assert await alerts.enqueue_sublease_candidates(["10000001"]) == {"scanned":1,"queued":0,"reason":"blocked_on_verified_rental_property_identity"}


@pytest.mark.asyncio
async def test_new_listing_with_early_cut_keeps_new_trigger(monkeypatch):
    row=candidate(alert_old_price=41000000)
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",AsyncMock(return_value=row["rental_economics"]))
    enqueue=AsyncMock(return_value={"queued":1}); monkeypatch.setattr(alerts,"enqueue_typed_candidate",enqueue)
    assert await alerts._enqueue_yield_rows([row])==1
    assert enqueue.await_args.kwargs["event"]=="new"
    row["first_seen"]=datetime.now(timezone.utc)-timedelta(days=3)
    await alerts._enqueue_yield_rows([row])
    assert enqueue.await_args.kwargs["event"]=="price_drop"


@pytest.mark.asyncio
async def test_delivery_day_cap_defers_prior_day_pending_instead_of_dropping(monkeypatch):
    ledger=Ledger(cap=1);ledger.install(monkeypatch)
    monkeypatch.setattr("bot.db.settings.get_int",lambda key,default:1 if key=="BUYER_ALERT_DAILY_CAP" else default)
    await alerts.enqueue_typed_candidate("yield",candidate())
    ledger.notices["yield:78"]={"property_id":78,"payload":{**ledger.notices["yield:77"]["payload"],"listing_id":"10000002"},"status":"pending"}
    async def current(sql,*args):
        if "FROM apartment_listings" in sql:
            return candidate(id=args[0],property_id=77 if args[0]=="10000001" else 78)
        return await Ledger.fetchrow(ledger,sql,*args)
    monkeypatch.setattr("bot.db.pg.fetchrow",current)
    async def economics(row): return row["rental_economics"]
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",economics)
    bot=type("MockBot",(),{"send_message":AsyncMock()})()
    assert (await alerts.dispatch_pending(bot))["sent"]==1
    assert ledger.notices["yield:78"]["status"]=="pending"
    ledger.attempts=0  # Next database calendar day opens one new reservation.
    assert (await alerts.dispatch_pending(bot))["sent"]==1
    assert bot.send_message.await_count==2


def test_durable_cursor_rejects_future_malformed_and_retains_complete_key():
    now=datetime.now(timezone.utc)
    raw=json.dumps({"until":now.isoformat(),"after_seen":(now-timedelta(minutes=1)).isoformat(),"after_id":"12345678"})
    until,after,identifier=alerts.yield_scan_cursor(raw,now)
    assert until==now and after<now and identifier=="12345678"
    for value in (None,"{}","not-json",json.dumps({"until":(now+timedelta(hours=1)).isoformat()})):
        assert alerts.yield_scan_cursor(value,now)==(now,None,"")


def test_render_sublease_uses_stage3_structured_payload():
    text=alerts.render_alert("sublease",{"listing_id":"10000001","rent_month":250000,"breakeven_occupancy":.4,"occupancy_assumed":.6,"margin":120000})
    assert "40.0%" in text and "60.0%" in text and "письменное согласие" in text
