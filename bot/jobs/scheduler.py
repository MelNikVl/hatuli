"""
Background jobs for the modernised bot.

Scheduler functions use aiogram Bot directly (no PTB Application).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time as _time_module
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

ALERTS_ENABLED = False  # Set True to enable Telegram alerts


if TYPE_CHECKING:
    from aiogram import Bot
    from bot.config import Config
    from bot.db.compat import BotDB, UserSettings

logger = logging.getLogger(__name__)

# ── Rate limiter for krisha.kz requests ───────────────────────────────────────

_krisha_rate_lock = asyncio.Lock()
_krisha_last_ts: float = 0.0
_KRISHA_MIN_GAP = 61.0  # seconds between requests to krisha.kz


async def _rate_limited_parse(config: "Config", **kwargs: Any):
    """Call parse_krisha with a rate limit of at most 1 request per _KRISHA_MIN_GAP seconds."""
    global _krisha_last_ts
    from bot.core.parser import parse_krisha

    async with _krisha_rate_lock:
        elapsed = _time_module.monotonic() - _krisha_last_ts
        if elapsed < _KRISHA_MIN_GAP:
            await asyncio.sleep(_KRISHA_MIN_GAP - elapsed)
        result = await parse_krisha(config, **kwargs)
        _krisha_last_ts = _time_module.monotonic()
    return result


# ── Adaptive frequency tracking ───────────────────────────────────────────────

_empty_cycles: dict[tuple, int] = {}  # group_key -> consecutive empty cycles

ASTANA_TZ = timezone(timedelta(hours=5))

# ─────────────────────────────── helpers ──────────────────────────────────────

def _matches_city(listing: Any, city: str) -> bool:
    _ALIASES: dict[str, set[str]] = {
        "astana": {"астана", "astana", "нур-султан", "nur-sultan"},
        "almaty": {"алматы", "almaty"},
    }
    city = city.lower().strip()
    aliases = _ALIASES.get(city)
    if not aliases:
        return True
    haystack = f"{listing.address} {listing.title}".lower()
    opposite = set().union(*[vals for k, vals in _ALIASES.items() if k != city])
    if any(a in haystack for a in aliases):
        return True
    if any(a in haystack for a in opposite):
        return False
    return True


def _fits_user_filters(user: "UserSettings", listing: Any) -> bool:
    if user.price_min is not None and listing.price < user.price_min:
        return False
    if user.price_max is not None and user.price_max > 0 and listing.price > user.price_max:
        return False
    if user.city and not _matches_city(listing, user.city):
        return False
    return True


# ──────────────────────────── notification senders ────────────────────────────

async def _send_new_listing(bot: "Bot", user_id: int, listing: Any, deal_type: str) -> None:
    """Send a listing card using the aiogram bot."""
    from bot.core.cards import send_listing_card

    listing_dict = listing.to_dict()
    listing_dict["deal_type"] = deal_type
    try:
        await send_listing_card(bot, user_id, listing_dict)
    except Exception:
        logger.exception("_send_new_listing failed user=%s listing=%s", user_id, listing.id)


async def _send_subscription_expired(bot: "Bot", user_id: int) -> None:
    try:
        if ALERTS_ENABLED: await bot.send_message(
            chat_id=user_id,
            text="⛔️ Ваша подписка истекла. Напишите администратору для продления.",
        )
    except Exception:
        logger.exception("Failed to send expiry notice to user=%s", user_id)


async def _send_daily_report(bot: "Bot", user_id: int, rows: list[tuple]) -> None:
    if not rows:
        try:
            if ALERTS_ENABLED: await bot.send_message(
                chat_id=user_id,
                text="Сегодня новых объектов по вашим фильтрам не найдено",
            )
        except Exception:
            logger.exception("Failed to send empty daily report to user=%s", user_id)
        return

    lines = ["📊 <b>Ежедневная сводка</b>", "<code>Адрес | Цена | Метраж | Цена/м² | Ссылка</code>"]
    for address, price, area, url in rows[:30]:
        price_m2 = "-"
        if area and area > 0:
            price_m2 = f"{int(price / area):,}".replace(",", "\u2009")
        area_text = f"{area:.1f}" if area else "-"
        price_fmt = f"{price:,}".replace(",", "\u2009")
        lines.append(f"• {address or '-'} | {price_fmt} | {area_text} | {price_m2} | <a href='{url}'>link</a>")

    try:
        if ALERTS_ENABLED: await bot.send_message(
            chat_id=user_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to send daily report to user=%s", user_id)


# ─────────────────────────── scheduled tasks ──────────────────────────────────

async def _get_listing_coords(listing: Any, db_path: str) -> tuple[float, float] | None:
    """
    Return (lat, lon) for a listing.
    Tries the DB cache first; geocodes via Nominatim on cache miss and stores result.
    """
    from bot.core.geo import geocode
    from bot.db.queries import get_listing_coords, save_listing_coords

    # Check cache
    cached = await get_listing_coords(db_path, listing.id)
    if cached:
        return cached

    # Geocode — use district or full address
    address = listing.district or listing.address
    if not address:
        return None

    coords = await geocode(address)
    if coords:
        await save_listing_coords(db_path, listing.id, coords[0], coords[1])
    return coords


async def check_new_listings(bot: "Bot", db: "BotDB", config: "Config") -> None:
    """Fetch new listings for all active users and send notifications."""
    from bot.core.geo import within_radius
    from bot.db.queries import get_users_with_location
    from bot.db import queries as q

    try:
        active_users = await db.get_active_users()
        grouped: dict[tuple[str, str, int], list] = defaultdict(list)
        for user in active_users:
            if user.price_max is None or not user.city or not user.deal_type:
                continue
            grouped[(user.city, user.deal_type, user.price_max)].append(user)

        # Load geo-filter users (from the new aiogram schema)
        geo_users = await get_users_with_location(config.db_path)
        geo_by_id: dict[int, dict] = {u["user_id"]: u for u in geo_users}

        for (city, deal_type, price_max), users in grouped.items():
            group_key = (city, deal_type, price_max)
            price_min_vals = [u.price_min for u in users if u.price_min is not None]
            area_min_vals = [u.area_min for u in users if u.area_min is not None]
            area_max_vals = [u.area_max for u in users if u.area_max is not None]

            req_price_min = min(price_min_vals) if price_min_vals else None
            req_area_min = min(area_min_vals) if area_min_vals else None
            req_area_max = max(area_max_vals) if area_max_vals else None

            from dataclasses import replace
            local_cfg = replace(config, city=city, deal_type=deal_type, max_price=price_max)

            listings = await _rate_limited_parse(
                local_cfg,
                deal_type=deal_type,
                price_min=req_price_min,
                price_max=price_max,
                area_min=req_area_min,
                area_max=req_area_max,
                db=db,
            )
            await db.log_event("parser", f"city={city} deal_type={deal_type} listings={len(listings)}")

            new_notifications_count = 0

            for listing in listings:
                await db.save_listing(listing, city=city, deal_type=deal_type)

                for user in users:
                    # Check if user's notifications are paused
                    user_data = await q.get_user(config.db_path, user.user_id)
                    if user_data and user_data.get("is_paused"):
                        continue

                    if not _fits_user_filters(user, listing):
                        continue
                    if await db.is_user_notified_about_listing(user.user_id, listing.id):
                        continue

                    # Geo filter: if user has a radius set, check distance
                    geo = geo_by_id.get(user.user_id)
                    if geo and geo.get("location_lat") is not None:
                        coords = await _get_listing_coords(listing, config.db_path)
                        if coords:
                            if not within_radius(
                                geo["location_lat"], geo["location_lon"],
                                float(geo["radius_km"]),
                                coords[0], coords[1],
                            ):
                                continue
                        # If geocoding failed, let the listing through (don't block it)

                    await _send_new_listing(bot, user.user_id, listing, deal_type=user.deal_type or "rent")
                    await db.mark_user_listing_notified(user.user_id, listing.id)
                    new_notifications_count += 1

            # Adaptive frequency: track empty cycles per group
            if new_notifications_count == 0:
                _empty_cycles[group_key] = _empty_cycles.get(group_key, 0) + 1
            else:
                _empty_cycles[group_key] = 0

    except Exception as exc:
        logger.exception("check_new_listings failed")
        await db.log_event("error", f"parser error: {exc}")


async def check_expired_subscriptions(bot: "Bot", db: "BotDB") -> None:
    """Notify and block users whose subscriptions have expired."""
    try:
        users = await db.get_expired_users()
        for user in users:
            await _send_subscription_expired(bot, user.user_id)
            await db.set_user_blocked(user.user_id, True)
            await db.log_event("subscription", f"expired user={user.user_id}")
    except Exception:
        logger.exception("check_expired_subscriptions failed")


async def send_daily_reports(bot: "Bot", db: "BotDB") -> None:
    """Send daily listing summaries to users whose report hour has arrived."""
    try:
        users = await db.get_active_users()
        now_astana = datetime.now(ASTANA_TZ)
        today_key = now_astana.date().isoformat()

        for user in users:
            if user.daily_report_hour is None or user.daily_report_hour != now_astana.hour:
                continue
            if await db.has_daily_report_event(user.user_id, today_key):
                continue

            day_start = datetime(now_astana.year, now_astana.month, now_astana.day, tzinfo=ASTANA_TZ)
            day_end = day_start + timedelta(days=1)
            rows = await db.get_user_daily_listings(
                user.user_id,
                day_start.astimezone(timezone.utc),
                day_end.astimezone(timezone.utc),
            )
            await _send_daily_report(bot, user.user_id, rows)
            await db.log_event("daily_report", f"user:{user.user_id}|date:{today_key}|rows:{len(rows)}")
    except Exception:
        logger.exception("send_daily_reports failed")


async def check_price_changes(bot: "Bot", db_path: str) -> None:
    """
    Check all followed listings for price drops (>=5%) and notify users.
    Runs every 30 minutes via APScheduler.
    """
    from bot.db.queries import get_followed_listings_all, update_follow_price

    try:
        followed = await get_followed_listings_all(db_path)
    except Exception:
        logger.exception("check_price_changes: failed to fetch followed listings")
        return

    for follow in followed:
        url = follow.get("url")
        if not url:
            continue

        try:
            import httpx
            from bs4 import BeautifulSoup

            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            price_tag = (
                soup.select_one("div.offer__price")
                or soup.select_one(".price")
                or soup.select_one("[data-name='price']")
                or soup.select_one(".a-price")
            )
            if not price_tag:
                continue

            import re
            raw_price = re.sub(r"[^\d]", "", price_tag.get_text())
            if not raw_price:
                continue

            new_price = int(raw_price)
            old_price = follow.get("price_last_seen")
            follow_id = follow.get("id")
            user_id = follow.get("user_id")
            title = follow.get("title") or "Объявление"

            # Notify if price dropped by 5%+
            if old_price and old_price > 0 and new_price < old_price * 0.95:
                old_fmt = f"{old_price:,}".replace(",", "\u2009")
                new_fmt = f"{new_price:,}".replace(",", "\u2009")
                try:
                    if ALERTS_ENABLED: await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"📉 <b>Цена снизилась!</b>\n\n"
                            f"{title}\n\n"
                            f"Новая цена: <b>{new_fmt} ₸</b>\n"
                            f"Было: {old_fmt} ₸\n\n"
                            f"<a href='{url}'>Посмотреть объявление</a>"
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    logger.info(
                        "Price drop notification: user=%s follow=%s old=%s new=%s",
                        user_id, follow_id, old_price, new_price,
                    )
                except Exception:
                    logger.exception(
                        "check_price_changes: failed to send notification user=%s", user_id
                    )

            # Always update price_last_seen
            if follow_id is not None:
                await update_follow_price(db_path, follow_id, new_price)

        except Exception:
            logger.exception("check_price_changes: error processing follow=%s url=%s", follow.get("id"), url)

        # Rate limit: 1 request per 10 seconds
        await asyncio.sleep(10)


async def parser_loop(bot: "Bot", db: "BotDB", config: "Config") -> None:
    """Run check_new_listings in an infinite loop with adaptive random delay."""
    import bot.state as _state

    while True:
        # Check admin-controlled pause flag
        if not _state.parser_enabled:
            await asyncio.sleep(15)  # poll every 15 s while paused
            continue

        max_empty = max(_empty_cycles.values(), default=0)
        if max_empty >= 3:
            delay = random.randint(600, 1200)  # 10–20 min adaptive slow-down
            logger.info("Adaptive: slowing to %ds (all groups quiet)", delay)
        else:
            # Use admin-configured interval (defaults: 60–300 s)
            delay = random.randint(_state.parse_interval_min, _state.parse_interval_max)

        await asyncio.sleep(delay)
        await check_new_listings(bot, db, config)


# ── Investment property scanner ───────────────────────────────────────────────

_notified_investment_ids: set[str] = set()  # in-memory dedup между циклами


def _build_investment_alert(listing: dict, score_data: dict = None) -> str:
    prop_icons = {"storage": "📦", "garage": "🚗", "commercial": "🏪"}
    prop_names = {"storage": "КЛАДОВКА", "garage": "ГАРАЖ", "parking": "ПАРКИНГ", "commercial": "КОММЕРЦИЯ"}

    icon = prop_icons.get(listing.get("prop_type", ""), "🏠")
    ptype = prop_names.get(listing.get("prop_type", ""), "ОБЪЕКТ")
    price = listing.get("price", 0)
    price_fmt = f"{price:,}".replace(",", "\u2009")
    area = listing.get("area")
    area_str = f"{area:.0f} м²" if area else "площадь не указана"
    est_rent = listing.get("est_rent", 0)
    rent_fmt = f"{est_rent:,}".replace(",", "\u2009")
    y = listing.get("yield_pct", 0)
    payback = listing.get("payback_years", "?")
    address = listing.get("district") or listing.get("address") or "адрес не указан"
    url = listing.get("url", "")

    if score_data:
        total = score_data.get("total_score", 0)
        bd = score_data.get("breakdown", {})
        reasons = score_data.get("reasons", [])
        reasons_str = "\n".join(f"  \u2022 {r}" for r in reasons if r)
        score_bar = "\U0001f7e2" if total >= 80 else "\U0001f7e1" if total >= 65 else "\U0001f534"
        return (
            f"{score_bar} <b>SCORE: {total}/100</b>\n"
            f"{icon} <b>{ptype}</b> | {address} | {area_str}\n"
            f"\U0001f4b0 Цена: <b>{price_fmt} \u20b8</b>\n"
            f"\U0001f4c8 Аренда ~{rent_fmt} \u20b8/мес\n"
            f"\u26a1\ufe0f Yield: <b>{y}%</b> | Окупаемость: {payback} лет\n"
            f"\U0001f4ca Y:{bd.get('yield',0)} L:{bd.get('location',0)} SD:{bd.get('supply_demand',0)} LQ:{bd.get('liquidity',0)} Q:{bd.get('quality',0)}\n"
            f"{reasons_str}\n"
            f"\U0001f517 <a href=\'{url}\'>Открыть на Krisha</a>"
        )

    return (
        f"{icon} <b>{ptype}</b> | {address} | {area_str}\n"
        f"\U0001f4b0 Цена: <b>{price_fmt} \u20b8</b>\n"
        f"\U0001f4c8 Аренда ~{rent_fmt} \u20b8/мес\n"
        f"\u26a1\ufe0f Yield: <b>{y}%</b> | Окупаемость: {payback} лет\n"
        f"\U0001f517 <a href=\'{url}\'>Открыть на Krisha</a>"
    )


async def check_investment_objects(bot: "Bot", db: "BotDB", config: "Config") -> None:
    """Parse, fetch descriptions, score, save to DB, alert top."""
    from bot.core.parser import parse_investment, fetch_listing_description
    from bot.core.investment_score import compute_score, MIN_ALERT_SCORE, MAX_ALERTS_PER_CYCLE
    from bot.db.investment_queries import upsert_investment_listing, mark_notified, get_top_unnotified
    from bot.db.models import init_investment_table
    from collections import Counter

    try:
        await init_investment_table(config.db_path)
        results = await parse_investment(config, limit=40, max_price=10_000_000, db=db)

        complex_counter: Counter = Counter()
        for r in results:
            cname = _extract_complex_name(r.get("address", ""))
            complex_counter[cname] += 1

        # Pre-score without description to decide which ones to deep-fetch
        candidates = []
        for listing in results:
            cname = _extract_complex_name(listing.get("address", ""))
            same_count = complex_counter.get(cname, 1)
            pre_score = compute_score(listing, same_complex_count=same_count)
            candidates.append((listing, same_count, pre_score))

        # Fetch descriptions for listings with pre-score >= 50
        import aiosqlite
        new_count = 0
        updated_count = 0

        for listing, same_count, pre_score in candidates:
            # Check if we already have description cached in DB
            async with aiosqlite.connect(config.db_path) as adb:
                row = await (await adb.execute(
                    "SELECT description FROM investment_listings WHERE id=?",
                    (listing["id"],)
                )).fetchone()

            cached_desc = (row[0] if row and row[0] else "") if row else ""

            if not cached_desc and pre_score["total_score"] >= 50:
                url = listing.get("url", "")
                if url:
                    logger.info("Fetching description for %s (pre-score=%d)", listing["id"], pre_score["total_score"])
                    details = await fetch_listing_description(url)
                    desc = details.get("description", "") + " " + details.get("details", "")
                    listing["description"] = desc.strip()
                else:
                    listing["description"] = ""
            else:
                listing["description"] = cached_desc

            # Re-score with description
            score_data = compute_score(listing, same_complex_count=same_count)
            listing["score_total"] = score_data["total_score"]

            status = await upsert_investment_listing(config.db_path, listing, score_data)
            if status == "new":
                new_count += 1
            elif status == "updated":
                updated_count += 1

            # Save description to DB
            if listing.get("description"):
                async with aiosqlite.connect(config.db_path) as adb:
                    await adb.execute(
                        "UPDATE investment_listings SET description=? WHERE id=?",
                        (listing["description"], listing["id"])
                    )
                    await adb.commit()

        await db.log_event("investment",
            f"parsed={len(results)} new={new_count} updated={updated_count}")

        # Alert top unnotified — PostgreSQL
        from bot.db.pg import fetch as pg_fetch, execute as pg_execute
        top = await pg_fetch(
            """SELECT id, score_total, rooms, district, area, price,
                      est_rent, yield_pct, payback_years, url,
                      floor, floors_total, complex_name, bargain_rec, bargain_discount_pct
               FROM apartment_listings
               WHERE notified=FALSE AND score_total>=70
               ORDER BY score_total DESC LIMIT 3"""
        )
        for row in top:
            floor_info = f" | {row['floor']}/{row['floors_total']} эт" if row["floor"] and row["floors_total"] else ""
            bargain_info = f"\n🤝 Торг: ~{row['bargain_discount_pct']}%" if row["bargain_discount_pct"] else ""
            text = (
                f"🏠 <b>КВАРТИРА SCORE: {row['score_total']}/100</b>\n"
                f"{row.get('rooms','?')}-комн | {row.get('district','')} | {row.get('area','')} м²{floor_info}\n"
                f"🏢 {row.get('complex_name','') or ''}\n"
                f"💰 Цена: <b>{row['price']:,} ₸</b>\n"
                f"📈 Аренда: {row.get('est_rent',0):,} ₸/мес\n"
                f"⚡ Yield: <b>{row.get('yield_pct',0)}%</b> | Окупаемость: {row.get('payback_years','?')} лет"
                f"{bargain_info}\n"
                f"🔗 <a href='{row['url']}'>Открыть на Krisha</a>"
            )
            try:
                if ALERTS_ENABLED: await bot.send_message(chat_id=config.admin_telegram_id, text=text,
                                       parse_mode="HTML", disable_web_page_preview=True)
                await pg_execute("UPDATE apartment_listings SET notified=TRUE WHERE id=$1", row["id"])
                await asyncio.sleep(1)
            except Exception:
                logger.exception("apt alert failed id=%s", row["id"])

    except Exception:
        logger.exception("check_apartments failed")


async def apartment_loop(bot, db, config):
    """Scan apartments every 10-20 min."""
    await asyncio.sleep(60)  # first scan after 1 min
    await check_apartments(bot, db, config)
    while True:
        delay = random.randint(600, 1200)
        logger.info("apartment_loop: next scan in %d sec", delay)
        await asyncio.sleep(delay)
        await check_apartments(bot, db, config)


async def investment_loop(bot, db, config):
    """Investment objects scanner (parking, storage)."""
    await asyncio.sleep(30)
    while True:
        try:
            await check_investment_objects(bot, db, config)
        except Exception as e:
            logger.error("investment_loop error: %s", e, exc_info=True)
        delay = random.randint(300, 900)
        logger.info("investment_loop: next scan in %d sec", delay)
        await asyncio.sleep(delay)
