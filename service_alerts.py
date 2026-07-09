#!/usr/bin/env python3
"""
Сервис ПЛАТНЫХ АЛЕРТОВ (Telegram Stars).

Делает две вещи параллельно:
  1. Бот (polling): /start, /subscribe — оплата подписки в Telegram Stars,
     /status — сколько осталось, /score <url> — быстрый скор по ссылке из БД.
  2. Фоновый цикл (каждые ALERT_INTERVAL_MIN минут):
     - новые объекты со score_total >= ALERT_MIN_SCORE и notified=FALSE
       -> рассылка всем активным подписчикам, notified=TRUE
     - снижения цены >= ALERT_PRICE_DROP_PCT из price_history (alerted=FALSE)
       -> рассылка, alerted=TRUE

Оплата: Telegram Stars (currency XTR) — не требует платёжного провайдера,
эквайринга и юрлица. Идеально для физлица. Вывод звёзд — через Fragment.

Запуск:  python service_alerts.py
Логи:    alerts.log

.env (добавить):
  ALERT_MIN_SCORE=70          # порог скора для алерта
  ALERT_PRICE_DROP_PCT=3      # минимальное снижение цены для алерта, %
  ALERT_INTERVAL_MIN=10       # период проверки, минут
  SUB_PRICE_STARS=500         # цена подписки в звёздах (~$10)
  SUB_DAYS=30                 # длительность подписки, дней
  FREE_TRIAL_DAYS=3           # триал новым пользователям (0 = выключен)
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from bot.db.pg import init_pool, execute as pg_exec, fetch as pg_fetch, fetchrow as pg_get
from bot.core.insights import build_insights_block

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("alerts.log", encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("alerts_service")

# ── Конфиг ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

ALERT_MIN_SCORE = int(os.getenv("ALERT_MIN_SCORE", "70"))
ALERT_PRICE_DROP_PCT = float(os.getenv("ALERT_PRICE_DROP_PCT", "3"))
ALERT_INTERVAL_MIN = int(os.getenv("ALERT_INTERVAL_MIN", "10"))
SUB_PRICE_STARS = int(os.getenv("SUB_PRICE_STARS", "500"))
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))
FREE_TRIAL_DAYS = int(os.getenv("FREE_TRIAL_DAYS", "3"))

router = Router()


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _fmt_price(p) -> str:
    return f"{int(p):,} ₸".replace(",", "\u2009") if p else "—"


def _listing_card(r: dict) -> str:
    """HTML-карточка объекта для алерта."""
    title = html.escape(r.get("title") or "Квартира")
    district = html.escape(r.get("district") or "")
    complex_name = html.escape(r.get("complex_name") or "")
    score = r.get("score_total") or 0
    yield_pct = r.get("yield_pct") or 0
    bargain_rec = html.escape(r.get("bargain_rec") or "")
    bargain_target = r.get("bargain_target")
    url = r.get("url") or ""

    lines = [
        f"🎯 <b>Скор {score}/100</b> — {title}",
        f"💰 <b>{_fmt_price(r.get('price'))}</b>"
        + (f" · доходность {yield_pct:.1f}%" if yield_pct else ""),
    ]
    loc = " · ".join(x for x in (district, complex_name) if x)
    if loc:
        lines.append(f"📍 {loc}")
    if bargain_target:
        lines.append(f"🤝 Торг: цель {_fmt_price(bargain_target)}" + (f" — {bargain_rec}" if bargain_rec else ""))
    lines.append(f'🔗 <a href="{url}">Открыть на Krisha</a>')
    return "\n".join(lines)


def _drop_card(d: dict) -> str:
    old, new = d["old_price"], d["new_price"]
    drop_pct = (old - new) / old * 100 if old else 0
    title = html.escape(d.get("title") or "Квартира")
    url = d.get("url") or ""
    return (
        f"📉 <b>Цена снижена на {drop_pct:.1f}%</b> — {title}\n"
        f"Было {_fmt_price(old)} → стало <b>{_fmt_price(new)}</b>\n"
        f"Продавец готов торговаться — момент для звонка.\n"
        f'🔗 <a href="{url}">Открыть на Krisha</a>'
    )


async def _active_subscribers() -> list[int]:
    rows = await pg_fetch(
        "SELECT user_id FROM users "
        "WHERE subscription_end > NOW() AND COALESCE(is_blocked,0) = 0"
    )
    return [r["user_id"] for r in rows]


async def _broadcast(bot: Bot, user_ids: list[int], text: str) -> int:
    """Шлём с паузами, чтобы не упереться в лимиты Telegram (30 msg/sec)."""
    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(
                uid, text, parse_mode="HTML", disable_web_page_preview=True
            )
            sent += 1
        except Exception as e:
            # 403 = юзер заблокировал бота — помечаем
            if "blocked" in str(e).lower() or "403" in str(e):
                await pg_exec("UPDATE users SET is_blocked=1 WHERE user_id=$1", uid)
            log.warning("send to %s failed: %s", uid, e)
        await asyncio.sleep(0.05)
    return sent


# ── Фоновый цикл алертов ──────────────────────────────────────────────────────

async def alerts_cycle(bot: Bot) -> None:
    subs = await _active_subscribers()
    if not subs:
        log.info("cycle: нет активных подписчиков, пропуск")
        # всё равно помечаем notified, чтобы при первом подписчике не хлынул архив
    # 1) Новые топ-объекты
    top = await pg_fetch(
        """
        SELECT id, url, title, price, area, district, complex_name,
               score_total, yield_pct, est_rent, bargain_target, bargain_rec,
               is_owner, seller_type, renovation, description
        FROM apartment_listings
        WHERE notified = FALSE
          AND score_total >= $1
          AND first_seen > NOW() - INTERVAL '24 hours'
        ORDER BY score_total DESC
        LIMIT 10
        """,
        ALERT_MIN_SCORE,
    )
    for r in top:
        r = dict(r)
        changes = [dict(c) for c in await pg_fetch(
            "SELECT old_price, new_price, changed_at FROM price_history "
            "WHERE listing_id=$1 ORDER BY changed_at", r["id"],
        )]
        card = _listing_card(r) + "\n" + build_insights_block(r, changes)
        if subs:
            n = await _broadcast(bot, subs, card)
            log.info("alert %s (score %s) -> %d подписчикам", r["id"], r["score_total"], n)
        await pg_exec("UPDATE apartment_listings SET notified=TRUE WHERE id=$1", r["id"])

    # Гигиена: старые ненотифицированные ниже порога тоже закрываем,
    # чтобы индекс не разрастался
    await pg_exec(
        "UPDATE apartment_listings SET notified=TRUE "
        "WHERE notified=FALSE AND first_seen < NOW() - INTERVAL '48 hours'"
    )

    # 2) Снижения цены
    drops = await pg_fetch(
        """
        SELECT ph.id AS ph_id, ph.old_price, ph.new_price,
               al.title, al.url
        FROM price_history ph
        JOIN apartment_listings al ON al.id = ph.listing_id
        WHERE ph.alerted = FALSE
          AND ph.old_price > ph.new_price
          AND (ph.old_price - ph.new_price)::float / ph.old_price * 100 >= $1
        ORDER BY ph.changed_at
        LIMIT 10
        """,
        ALERT_PRICE_DROP_PCT,
    )
    for d in drops:
        d = dict(d)
        if subs:
            n = await _broadcast(bot, subs, _drop_card(d))
            log.info("price-drop alert ph_id=%s -> %d подписчикам", d["ph_id"], n)
        await pg_exec("UPDATE price_history SET alerted=TRUE WHERE id=$1", d["ph_id"])

    # Мелкие изменения цены просто закрываем без рассылки
    await pg_exec(
        "UPDATE price_history SET alerted=TRUE WHERE alerted=FALSE "
        "AND (old_price <= new_price OR "
        "(old_price - new_price)::float / NULLIF(old_price,0) * 100 < $1)",
        ALERT_PRICE_DROP_PCT,
    )


async def alerts_loop(bot: Bot) -> None:
    while True:
        try:
            await alerts_cycle(bot)
        except Exception as e:
            log.error("alerts cycle error: %s", e, exc_info=True)
        await asyncio.sleep(ALERT_INTERVAL_MIN * 60)


# ── Хендлеры бота ─────────────────────────────────────────────────────────────

WELCOME = (
    "👋 <b>Hatuli — алерты по недооценённым квартирам Астаны</b>\n\n"
    "Парсер сканирует Krisha.kz каждые 10–30 минут и оценивает каждую "
    "квартиру по 7 критериям (доходность, цена против рынка, локация, ЖК…).\n\n"
    "Подписчики получают мгновенно:\n"
    f"🎯 объекты со скором {ALERT_MIN_SCORE}+ из 100 — раньше, чем их разберут\n"
    "📉 сигналы о снижении цены — момент для торга\n"
    "🤝 рекомендованную цену торга по реальным аналогам из базы\n\n"
    f"Подписка: <b>{SUB_PRICE_STARS} ⭐ / {SUB_DAYS} дней</b> — /subscribe\n"
    "Разбор любой квартиры по ссылке — /score\n"
    "Статус подписки — /status"
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = message.from_user
    if not u:
        return
    # upsert юзера (+ триал, если включён и юзер новый)
    existing = await pg_get("SELECT user_id, subscription_end FROM users WHERE user_id=$1", u.id)
    if not existing:
        if FREE_TRIAL_DAYS > 0:
            await pg_exec(
                "INSERT INTO users (user_id, username, subscription_end) "
                "VALUES ($1, $2, NOW() + make_interval(days => $3)) "
                "ON CONFLICT (user_id) DO NOTHING",
                u.id, u.username or "", FREE_TRIAL_DAYS,
            )
            await message.answer(
                WELCOME + f"\n\n🎁 Вам активирован пробный период на {FREE_TRIAL_DAYS} дн.",
                parse_mode="HTML",
            )
            return
        await pg_exec(
            "INSERT INTO users (user_id, username) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO NOTHING",
            u.id, u.username or "",
        )
    await message.answer(WELCOME, parse_mode="HTML")


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    await message.answer_invoice(
        title=f"Подписка Hatuli — {SUB_DAYS} дней",
        description=(
            f"Алерты по квартирам Астаны со скором {ALERT_MIN_SCORE}+ "
            "и сигналы о снижении цен."
        ),
        payload=f"sub_{SUB_DAYS}d",
        currency="XTR",  # Telegram Stars — провайдер не нужен
        prices=[LabeledPrice(label=f"{SUB_DAYS} дней", amount=SUB_PRICE_STARS)],
    )


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, bot: Bot) -> None:
    u = message.from_user
    if not u:
        return
    # Продлеваем от max(сейчас, текущий конец подписки)
    await pg_exec(
        """
        INSERT INTO users (user_id, username, subscription_end)
        VALUES ($1, $2, NOW() + make_interval(days => $3))
        ON CONFLICT (user_id) DO UPDATE SET
            subscription_end = GREATEST(COALESCE(users.subscription_end, NOW()), NOW())
                               + make_interval(days => $3),
            username = EXCLUDED.username
        """,
        u.id, u.username or "", SUB_DAYS,
    )
    row = await pg_get("SELECT subscription_end FROM users WHERE user_id=$1", u.id)
    end = row["subscription_end"].strftime("%d.%m.%Y") if row and row["subscription_end"] else "?"
    await message.answer(
        f"✅ Оплата получена! Подписка активна до <b>{end}</b>.\n"
        "Алерты начнут приходить автоматически.",
        parse_mode="HTML",
    )
    log.info("PAYMENT: user=%s stars=%s until=%s", u.id, SUB_PRICE_STARS, end)
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 Новая оплата: @{u.username or u.id}, {SUB_PRICE_STARS} ⭐, до {end}",
            )
        except Exception:
            pass


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    u = message.from_user
    if not u:
        return
    row = await pg_get("SELECT subscription_end FROM users WHERE user_id=$1", u.id)
    end = row["subscription_end"] if row else None
    if end is None:
        await message.answer("Подписка не активна. Оформить — /subscribe")
        return
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    if end > now:
        days = (end - now).days
        await message.answer(
            f"✅ Подписка активна до <b>{end.strftime('%d.%m.%Y')}</b> "
            f"(осталось {days} дн.). Продлить — /subscribe",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"⏳ Подписка истекла {end.strftime('%d.%m.%Y')}. Продлить — /subscribe"
        )


@router.message(Command("score"))
async def cmd_score(message: Message) -> None:
    """Разбор объявления по ссылке: /score https://krisha.kz/a/show/123456"""
    u = message.from_user
    if not u:
        return
    # подписка обязательна (кроме админа)
    if u.id != ADMIN_ID:
        row = await pg_get("SELECT subscription_end FROM users WHERE user_id=$1", u.id)
        import datetime as dt
        if not row or not row["subscription_end"] or row["subscription_end"] < dt.datetime.now(dt.timezone.utc):
            await message.answer("Команда доступна по подписке — /subscribe")
            return

    text = message.text or ""
    m_id = re.search(r"show/(\d+)", text) or re.search(r"\b(\d{7,})\b", text)
    if not m_id:
        await message.answer(
            "Пришли ссылку на объявление:\n<code>/score https://krisha.kz/a/show/123456789</code>",
            parse_mode="HTML",
        )
        return
    listing_id = m_id.group(1)

    r = await pg_get(
        """
        SELECT id, url, title, price, area, rooms, district, complex_name,
               score_total, score_yield, score_price_market, score_location,
               score_apt_type, score_floor, score_complex, score_supply,
               yield_pct, est_rent, bargain_target, bargain_discount_pct, bargain_rec,
               is_owner, seller_type, renovation, description, floor, floors_total,
               first_seen, last_seen
        FROM apartment_listings WHERE id=$1
        """,
        listing_id,
    )
    if not r:
        await message.answer(
            "Этого объявления пока нет в базе — парсер охватывает Астану "
            "и мог ещё не дойти до него. Попробуй позже."
        )
        return
    r = dict(r)

    changes = [dict(c) for c in await pg_fetch(
        "SELECT old_price, new_price, changed_at FROM price_history "
        "WHERE listing_id=$1 ORDER BY changed_at", listing_id,
    )]

    # Полная карточка с breakdown
    def _s(v):
        return v if v is not None else "?"
    bd_lines = [
        f"🎯 <b>Скор {r['score_total']}/100</b>",
        f"  доходность {_s(r['score_yield'])}/20 · цена vs рынок {_s(r['score_price_market'])}/15",
        f"  локация {_s(r['score_location'])}/20 · тип {_s(r['score_apt_type'])}/15",
        f"  этаж {_s(r['score_floor'])}/8 · ЖК {_s(r['score_complex'])}/15 · supply {_s(r['score_supply'])}/7",
    ]
    if r.get("score_floor") is None or r.get("score_complex") is None:
        bd_lines.append("  ⚠️ часть скора не пересчитана (устаревшая запись) — данные ниже могут быть неполными")
    days_in_db = ""
    if r.get("first_seen"):
        import datetime as dt
        days = (dt.datetime.now(dt.timezone.utc) - r["first_seen"]).days
        days_in_db = f"\n⏱ В базе {days} дн."

    header = _listing_card(r)
    insights = build_insights_block(r, changes)
    await message.answer(
        header + "\n" + "\n".join(bd_lines) + "\n" + insights + days_in_db,
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Админ-статистика: подписчики, доход."""
    u = message.from_user
    if not u or u.id != ADMIN_ID:
        return
    active = await pg_get("SELECT COUNT(*) AS c FROM users WHERE subscription_end > NOW()")
    total = await pg_get("SELECT COUNT(*) AS c FROM users")
    pending = await pg_get(
        "SELECT COUNT(*) AS c FROM apartment_listings WHERE notified=FALSE AND score_total >= $1",
        ALERT_MIN_SCORE,
    )
    await message.answer(
        f"👥 Всего юзеров: {total['c']}\n"
        f"✅ Активных подписок: {active['c']}\n"
        f"📬 Алертов в очереди: {pending['c']}"
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в .env")
    await init_pool(DATABASE_URL)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    log.info(
        "=== Alerts service started (score>=%d, drop>=%.1f%%, every %d min, %d⭐/%dd) ===",
        ALERT_MIN_SCORE, ALERT_PRICE_DROP_PCT, ALERT_INTERVAL_MIN, SUB_PRICE_STARS, SUB_DAYS,
    )

    loop_task = asyncio.create_task(alerts_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        loop_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())