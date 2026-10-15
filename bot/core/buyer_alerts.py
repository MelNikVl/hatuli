"""Typed searches and one buyer-bot outbox using the existing search/notice tables.

Only explicit opt-in searches, medium/high qualified scenarios and confirmed
physical identity enter the queue. Ambiguous send failures are not retried
automatically: a duplicate financial alert is worse than a missed pilot alert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import re
import logging

KINDS = {"yield", "sublease"}
DEFAULT_DAILY_CAP = 5
log = logging.getLogger(__name__)


def rental_summary_lines(economics):
    from bot.core.rental_economics import utc, number
    if not isinstance(economics, dict) or economics.get("asking_prices") is not True or economics.get("scenario") is not True:
        return []
    now = datetime.now(timezone.utc)
    source = economics.get("rent_source") or {}
    oldest, newest, updated = (utc(source.get(key)) for key in ("source_oldest_at", "source_newest_at", "updated_at"))
    rent_range = economics.get("rent_range")
    if (None in (oldest, newest, updated) or not now-timedelta(hours=72) <= oldest <= newest <= updated <= now
            or source.get("policy_version") != "3" or number(economics.get("net_yield_pct"), -100, 100) is None
            or not isinstance(rent_range, list) or len(rent_range) != 2 or any(number(value, 1, 5000000) is None for value in rent_range)):
        return []
    def money(value):
        return f"{value:,.0f} ₸".replace(",", " ")
    lines = [f"<b>Аренда · сценарная чистая доходность {economics['net_yield_pct']:.1f}% в год</b>",
             f"Заявленные ставки: {money(rent_range[0])}–{money(rent_range[1])}/мес.",
             f"Основа: {escape(str(source.get('level') or ''))}, {int(source.get('n') or 0)} предложений."]
    if economics.get("benchmark") and number(economics["benchmark"].get("median_net"), -100, 100) is not None:
        lines.append(f"Медиана сценарной доходности среза: {economics['benchmark']['median_net']:.1f}%.")
    lines.append("Расходы и время аренды — допущения; это не подтверждённый доход.")
    return lines


def parse_yield_command(text, profile=None):
    import shlex
    tokens = shlex.split(text or "")
    if tokens and tokens[0].startswith("/"):
        tokens.pop(0)
    if not tokens:
        raise ValueError("Пример: /yield 7.5 бюджет=40000000 комнаты=1,2 районы=Есиль жк=123")
    profile = profile or {}
    params = {"net_yield_min": tokens.pop(0)}
    for field in ("budget_max", "rooms"):
        if profile.get(field):
            params[field] = profile[field]
    aliases = {"бюджет": "budget_max", "budget": "budget_max", "комнаты": "rooms", "rooms": "rooms",
               "районы": "districts", "districts": "districts", "жк": "complex_ids", "complexes": "complex_ids"}
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator or key.lower() not in aliases:
            raise ValueError("Параметры: бюджет=40000000 комнаты=1,2 районы=Есиль жк=123")
        key = aliases[key.lower()]
        if key == "budget_max":
            params[key] = int(value)
        elif key == "districts":
            params[key] = value.split(",")
        else:
            params[key] = [int(item) for item in value.split(",")]
    return validate_params("yield", params)


def validate_params(kind, value):
    if kind not in KINDS or not isinstance(value, dict):
        raise ValueError("Неизвестный тип подписки")
    key, maximum = ("net_yield_min", 100) if kind == "yield" else ("breakeven_occupancy_max", 1)
    raw = value.get(key)
    if isinstance(raw, bool):
        raise ValueError("Укажите порог")
    try:
        threshold = float(raw)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Укажите числовой порог") from None
    if not math.isfinite(threshold) or not 0 < threshold <= maximum:
        raise ValueError("Порог вне допустимого диапазона")
    result = {key: threshold}
    for name, upper in (("budget_max", 2_000_000_000), ("max_rent", 10_000_000)):
        if value.get(name) is not None:
            if type(value[name]) is not int or not 1 <= value[name] <= upper:
                raise ValueError("Проверьте бюджет")
            result[name] = value[name]
    for name, maximum_length in (("rooms", 6), ("complex_ids", 50), ("districts", 20)):
        raw = value.get(name)
        if raw is None:
            continue
        if not isinstance(raw, list) or not 1 <= len(raw) <= maximum_length:
            raise ValueError("Проверьте фильтры подписки")
        if name == "districts":
            if any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 80 for item in raw):
                raise ValueError("Проверьте районы")
            result[name] = sorted(set(item.strip() for item in raw))
        else:
            if any(type(item) is not int or item < 1 or name == "rooms" and item > 6 for item in raw):
                raise ValueError("Проверьте комнатность или ЖК")
            result[name] = sorted(set(raw))
    return result


def matches_filters(candidate, params):
    return not (params.get("budget_max") and candidate.get("price", math.inf) > params["budget_max"]
                or params.get("max_rent") and candidate.get("price", math.inf) > params["max_rent"]
                or params.get("rooms") and candidate.get("rooms") not in params["rooms"]
                or params.get("districts") and candidate.get("district") not in params["districts"]
                or params.get("complex_ids") and candidate.get("complex_id") not in params["complex_ids"])


def evaluate_yield_alert(candidate, params, *, event="new", previous_economics=None):
    from bot.core.rental_economics import cached_rental_economics
    try:
        params = validate_params("yield", params)
    except ValueError:
        return None
    economics = cached_rental_economics(candidate)
    if (not economics or economics["confidence"] not in ("medium", "high") or not matches_filters(candidate, params)
            or economics["net_yield_pct"] < params["net_yield_min"] or event not in ("new", "price_drop")):
        return None
    if event == "price_drop":
        if (not isinstance(previous_economics, dict) or previous_economics.get("confidence") not in ("medium", "high")
                or not isinstance(previous_economics.get("net_yield_pct"), (int, float))
                or previous_economics["net_yield_pct"] >= params["net_yield_min"]):
            return None
    return {"listing_id": str(candidate["id"]), "price": candidate["price"], "rooms": candidate.get("rooms"),
            "complex_name": candidate.get("complex_name"), "est_rent": economics["est_rent"], "rent_range": economics["rent_range"],
            "net_yield_pct": economics["net_yield_pct"], "confidence": economics["confidence"],
            "benchmark": economics.get("benchmark"), "deal_score": candidate.get("effective_score"),
            "asking_prices": True, "scenario": True}


async def save_subscription(user_id, kind, params):
    from bot.db import pg
    from bot.core.buyer_store import ensure_user
    params = validate_params(kind, params)
    await ensure_user(user_id)
    async with pg.get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"buyer_alert:{user_id}")
            await connection.execute("""INSERT INTO saved_searches(user_id,listing_id,kind,params,enabled)
                VALUES($1,NULL,$2,$3::jsonb,TRUE) ON CONFLICT(user_id,kind) WHERE kind IN ('yield','sublease')
                DO UPDATE SET params=EXCLUDED.params,enabled=TRUE""", user_id, kind, json.dumps(params))
            await connection.execute("UPDATE users SET notify_frequency='daily' WHERE user_id=$1 AND notify_frequency='off'", user_id)


async def disable_subscription(user_id, kind=None):
    from bot.db import pg
    if kind is not None and kind not in KINDS:
        raise ValueError("Unknown search kind")
    await pg.execute("UPDATE saved_searches SET enabled=FALSE WHERE user_id=$1 AND kind IN ('yield','sublease') AND ($2::text IS NULL OR kind=$2)", user_id, kind)
    await pg.execute("UPDATE user_listing_notifications SET delivery_status='cancelled',attempted_at=NULL WHERE user_id=$1 AND kind IN ('yield','sublease') AND delivery_status='pending' AND ($2::text IS NULL OR kind=$2)", user_id, kind)


async def enqueue_typed_candidate(kind, candidate, *, event="new", previous_economics=None):
    from bot.db import pg, settings
    from bot.core.rental_economics import clean_sale
    now = datetime.now(timezone.utc)
    if kind not in KINDS or type(candidate.get("property_id")) is not int or candidate["property_id"] <= 0:
        return {"queued": 0, "reason": "verified_property_identity_required"}
    if candidate.get("property_identity_status") != "confirmed":
        return {"queued": 0, "reason": "verified_property_identity_required"}
    if kind == "yield" and not clean_sale(candidate, now):
        return {"queued": 0, "reason": "ineligible_sale"}
    cap = max(1, min(20, settings.get_int("BUYER_ALERT_DAILY_CAP", DEFAULT_DAILY_CAP)))
    subscriptions = await pg.fetch("""SELECT s.id,s.user_id,s.params FROM saved_searches s JOIN users u ON u.user_id=s.user_id
        WHERE s.kind=$1 AND s.enabled IS TRUE AND coalesce(u.is_blocked,0)=0
          AND coalesce(u.is_paused,0)=0 AND u.notify_frequency IS DISTINCT FROM 'off'""", kind)
    queued = 0
    for subscription in subscriptions:
        params = subscription["params"]
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                continue
        if kind == "yield":
            payload = evaluate_yield_alert(candidate, params, event=event, previous_economics=previous_economics)
        else:
            from bot.core.sublease import evaluate_sublease_alert
            payload = evaluate_sublease_alert(candidate, params)
        if payload is None:
            continue
        user_id = subscription["user_id"]
        async with pg.get_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"buyer_alert:{user_id}")
                allowed = await connection.fetchval("""SELECT EXISTS(SELECT 1 FROM saved_searches s JOIN users u ON u.user_id=s.user_id
                    WHERE s.id=$1 AND s.user_id=$2 AND s.enabled AND coalesce(u.is_blocked,0)=0 AND coalesce(u.is_paused,0)=0
                      AND u.notify_frequency IS DISTINCT FROM 'off') AND
                    (SELECT count(*) FROM user_listing_notifications WHERE user_id=$2 AND kind IN ('yield','sublease')
                      AND notified_at >= date_trunc('day',now() AT TIME ZONE 'Asia/Almaty') AT TIME ZONE 'Asia/Almaty') < $3""",
                    subscription["id"], user_id, cap)
                if not allowed:
                    continue
                duplicate = await connection.fetchval("""SELECT EXISTS(SELECT 1 FROM user_listing_notifications n
                    WHERE n.user_id=$1 AND n.kind=$2 AND (n.property_id=$3 OR EXISTS (
                        SELECT 1 FROM property_listings previous WHERE previous.listing_id=n.payload->>'listing_id'
                          AND previous.property_id=$3)))""", user_id, kind, candidate["property_id"])
                if duplicate:
                    continue
                # Prefixing the legacy listing key keeps typed notifications
                # independent of the old per-listing follow notification key.
                result = await connection.fetchval("""INSERT INTO user_listing_notifications
                    (user_id,listing_id,subscription_id,kind,property_id,payload,delivery_status)
                    VALUES($1,$2,$3,$4,$5,$6::jsonb,'pending') ON CONFLICT DO NOTHING RETURNING user_id""",
                    user_id, f"{kind}:{candidate['property_id']}", subscription["id"], kind, candidate["property_id"], json.dumps(payload, ensure_ascii=False, allow_nan=False))
                queued += result is not None
    return {"queued": queued}


def render_alert(kind, payload):
    def money(value):
        return f"{value:,.0f} ₸".replace(",", " ")
    identifier = str(payload.get("listing_id") or "")
    if not re.fullmatch(r"\d{5,20}", identifier):
        raise ValueError("Invalid alert listing")
    if kind == "yield":
        lower, upper = payload["rent_range"]
        lines = ["🏠 <b>Квартира по вашей доходности</b>", money(payload["price"]),
                 f"Сценарная чистая доходность: <b>{payload['net_yield_pct']:.1f}% в год</b>",
                 f"Заявленные ставки похожих квартир: {money(lower)}–{money(upper)}/мес."]
        if payload.get("benchmark"):
            lines.append(f"Медиана среза: {payload['benchmark']['median_net']:.1f}%.")
        if payload.get("deal_score") is not None:
            lines.append(f"Deal Score: {int(payload['deal_score'])}/100.")
        lines.append("Доход рассчитан по ставкам объявлений и допущениям, а не подтверждённым договорам.")
    else:
        lines = ["🏠 <b>Вариант по сценарию субаренды</b>",
                 f"Аренда: {money(payload['rent_month'])}/мес. Точка безубыточности: {float(payload['breakeven_occupancy'])*100:.1f}% загрузки.",
                 f"При сценарной загрузке {float(payload['occupancy_assumed'])*100:.1f}% маржа: {money(payload['margin'])}/мес.",
                 "Нужны письменное согласие собственника и проверка правил дома."]
    lines.append(f'<a href="https://hatuli.ai-groundtruth.com/listing/{identifier}">Карточка Hatuli</a>')
    return "\n".join(lines)


async def dispatch_pending(bot, *, limit=10):
    """The sole typed-alert sender runs inside the existing buyer-bot process."""
    from bot.db import pg, settings
    limit = max(1, min(20, int(limit)))
    cap=max(1,min(20,settings.get_int("BUYER_ALERT_DAILY_CAP",DEFAULT_DAILY_CAP)))
    sent = 0
    for _ in range(limit):
        async with pg.get_pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow("""SELECT n.user_id,n.listing_id,n.kind,n.payload,s.params FROM user_listing_notifications n
                    JOIN saved_searches s ON s.id=n.subscription_id JOIN users u ON u.user_id=n.user_id
                    WHERE n.delivery_status='pending' AND s.enabled AND coalesce(u.is_blocked,0)=0
                      AND coalesce(u.is_paused,0)=0 AND u.notify_frequency IS DISTINCT FROM 'off'
                      AND (SELECT count(*) FROM user_listing_notifications attempts
                        WHERE attempts.user_id=n.user_id AND attempts.kind IN ('yield','sublease')
                          AND attempts.attempted_at >= date_trunc('day',now() AT TIME ZONE 'Asia/Almaty') AT TIME ZONE 'Asia/Almaty') < $1
                    ORDER BY n.notified_at FOR UPDATE OF n SKIP LOCKED LIMIT 1""",cap)
                if not row:
                    break
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f"buyer_alert:{row['user_id']}")
                allowed=await connection.fetchval("""SELECT count(*)<$2 FROM user_listing_notifications
                    WHERE user_id=$1 AND kind IN ('yield','sublease')
                      AND attempted_at >= date_trunc('day',now() AT TIME ZONE 'Asia/Almaty') AT TIME ZONE 'Asia/Almaty'""",row["user_id"],cap)
                if not allowed:
                    continue
                await connection.execute("UPDATE user_listing_notifications SET delivery_status='sending',attempted_at=now() WHERE user_id=$1 AND listing_id=$2", row["user_id"], row["listing_id"])
        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            current_params = json.loads(row["params"]) if isinstance(row["params"], str) else row["params"]
            if not isinstance(payload, dict) or row["kind"] != "yield":
                # Rental property identity is not implemented yet. Never send a
                # stale sublease payload without a fresh verified candidate.
                raise ValueError("Unverifiable queued candidate")
        except (ValueError, TypeError):
            await pg.execute("UPDATE user_listing_notifications SET delivery_status='cancelled',attempted_at=NULL WHERE user_id=$1 AND listing_id=$2", row["user_id"], row["listing_id"])
            continue
        # Recheck the current price/identity/source and current search just
        # before sending; a queued number is not a fresh observation forever.
        if row["kind"] == "yield":
            current = await pg.fetchrow("""SELECT a.*,pl.property_id,p.identity_status AS property_identity_status
                FROM apartment_listings a JOIN property_listings pl ON pl.listing_id=a.id
                JOIN properties p ON p.property_id=pl.property_id WHERE a.id=$1""", payload["listing_id"])
            if current:
                from bot.core.rental_economics import rental_economics_for_listing
                current = dict(current)
                current["rental_economics"] = await rental_economics_for_listing(current)
            fresh = evaluate_yield_alert(dict(current), current_params) if current and current["property_identity_status"] == "confirmed" else None
            if fresh is None or current["property_id"] != int(row["listing_id"].split(":", 1)[1]):
                await pg.execute("UPDATE user_listing_notifications SET delivery_status='cancelled',attempted_at=NULL WHERE user_id=$1 AND listing_id=$2", row["user_id"], row["listing_id"])
                continue
            payload = fresh
        if not await pg.fetchval("""SELECT EXISTS(SELECT 1 FROM user_listing_notifications n
            JOIN saved_searches s ON s.id=n.subscription_id JOIN users u ON u.user_id=n.user_id
            WHERE n.user_id=$1 AND n.listing_id=$2 AND n.delivery_status='sending'
              AND s.enabled AND s.params=$3::jsonb AND coalesce(u.is_blocked,0)=0
              AND coalesce(u.is_paused,0)=0 AND u.notify_frequency IS DISTINCT FROM 'off')""",
            row["user_id"],row["listing_id"],json.dumps(current_params,allow_nan=False)):
            await pg.execute("UPDATE user_listing_notifications SET delivery_status='cancelled',attempted_at=NULL WHERE user_id=$1 AND listing_id=$2",row["user_id"],row["listing_id"])
            continue
        try:
            await bot.send_message(row["user_id"], render_alert(row["kind"], payload), parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await pg.execute("UPDATE user_listing_notifications SET delivery_status='failed' WHERE user_id=$1 AND listing_id=$2", row["user_id"], row["listing_id"])
        else:
            await pg.execute("UPDATE user_listing_notifications SET delivery_status='sent',delivered_at=now() WHERE user_id=$1 AND listing_id=$2", row["user_id"], row["listing_id"])
            sent += 1
    return {"sent": sent}


async def dispatcher_loop(bot):
    while True:
        try:
            from bot.db import settings
            await settings.load()
            await enqueue_recent_yield_candidates()
            await dispatch_pending(bot)
        except Exception as exc:
            # Delivery status is durable; never blindly replay an uncertain send.
            log.warning("Buyer alert cycle unavailable (%s)", type(exc).__name__)
        await asyncio.sleep(20)


YIELD_SCAN_CURSOR_KEY = "BUYER_YIELD_SCAN_CURSOR_V1"


def yield_scan_cursor(raw, now):
    from bot.core.rental_economics import utc
    try:
        value=json.loads(raw) if isinstance(raw,str) else raw
        until,after=utc(value.get("until")),utc(value.get("after_seen"))
        identifier=value.get("after_id")
        if (until and after and now-timedelta(hours=24)<=until<=now and after<=until
                and isinstance(identifier,str) and re.fullmatch(r"\d{5,20}",identifier)):
            return until,after,identifier
    except (ValueError,TypeError,AttributeError):
        pass
    return now,None,""


async def enqueue_recent_yield_candidates(*, limit=200):
    from bot.db import pg
    if not await pg.fetchval("SELECT EXISTS(SELECT 1 FROM saved_searches WHERE kind='yield' AND enabled IS TRUE)"):
        return {"scanned":0,"queued":0,"has_more":False}
    limit=max(1,min(500,int(limit)))
    now=datetime.now(timezone.utc)
    async with pg.get_pool().acquire() as connection:
        async with connection.transaction():
            if not await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))","buyer_yield_scan:v1"):
                return {"scanned":0,"queued":0,"reason":"scan_busy"}
            raw=await connection.fetchval("SELECT value FROM app_settings WHERE key=$1",YIELD_SCAN_CURSOR_KEY)
            until,after,identifier=yield_scan_cursor(raw,now)
            rows=await connection.fetch("""SELECT a.*,pl.property_id,p.identity_status AS property_identity_status,
                    drop.old_price AS alert_old_price
                FROM apartment_listings a JOIN property_listings pl ON pl.listing_id=a.id
                JOIN properties p ON p.property_id=pl.property_id AND p.identity_status='confirmed'
                LEFT JOIN LATERAL (SELECT ph.old_price FROM price_history ph WHERE ph.listing_id=a.id
                  AND ph.new_price=a.price AND ph.old_price>ph.new_price AND ph.changed_at BETWEEN $1::timestamptz-interval '24 hours' AND $4
                  ORDER BY ph.changed_at DESC LIMIT 1) drop ON TRUE
                WHERE a.is_active IS TRUE AND a.archived_at IS NULL AND NOT coalesce(a.is_duplicate,false)
                  AND NOT coalesce(a.dup_needs_review,false) AND nullif(btrim(a.duplicate_of),'') IS NULL
                  AND a.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $4
                  AND (a.first_seen BETWEEN $1::timestamptz-interval '24 hours' AND $4 OR drop.old_price IS NOT NULL)
                  AND ($2::timestamptz IS NULL OR (a.last_seen,a.id)>($2::timestamptz,$3::text))
                ORDER BY a.last_seen,a.id LIMIT $5""",now,after,identifier,until,limit+1)
            page=rows[:limit]
            queued=await _enqueue_yield_rows(page)
            has_more=len(rows)>limit
            cursor={"until":until.isoformat(),"after_seen":page[-1]["last_seen"].isoformat(),"after_id":page[-1]["id"]} if has_more else {}
            await connection.execute("INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",YIELD_SCAN_CURSOR_KEY,json.dumps(cursor))
    return {"scanned":len(page),"queued":queued,"has_more":has_more}


async def _enqueue_yield_rows(rows):
    from bot.core.rental_economics import rental_economics_for_listing,utc
    from bot.core.rental_yield import NetYieldParams,monthly_yield_scenario
    from bot.db import settings
    queued=0
    for raw in rows:
        candidate=dict(raw)
        economics=await rental_economics_for_listing(candidate)
        if not economics:
            continue
        candidate["rental_economics"]=economics
        old_price=candidate.get("alert_old_price")
        previous=None
        if old_price:
            scenario=monthly_yield_scenario(float(old_price),economics["est_rent"],NetYieldParams.from_settings(settings.get))
            previous={"confidence":economics["confidence"],"net_yield_pct":scenario["net_yield_pct"]} if scenario else None
        first=utc(candidate.get("first_seen"))
        new=first is not None and datetime.now(timezone.utc)-timedelta(hours=24)<=first<=datetime.now(timezone.utc)
        result=await enqueue_typed_candidate("yield",candidate,event="new" if new else "price_drop",previous_economics=previous)
        queued+=result["queued"]
    return queued


async def enqueue_sublease_candidates(ids):
    """Fast parser handoff; current rentals have no verified property identity.

    Keep the shared hook in place without fabricating identity from source ID
    or URL. A future explicit rental identity binding can supply qualified
    candidate economics to enqueue_typed_candidate('sublease', candidate).
    """
    return {"scanned": len(ids), "queued": 0, "reason": "blocked_on_verified_rental_property_identity"}
