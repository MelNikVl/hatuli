"""
Отдельный Telegram-бот (SITE_BOT_TOKEN, @nik_us_bot) для входа в личный
кабинет на сайте и рассылки уведомлений по избранному — НЕ тот же бот, что
у платных алертов (service_alerts.py, BOT_TOKEN), по прямому запросу.

/start <token> — деплинк с сайта (см. bot/core/site_auth.py):
  1. Проверяем токен (login_tokens, TTL 10 мин).
  2. Проверяем подписку на канал (HATULI_CHANNEL) через get_chat_member.
  3. Апсертим users по telegram_id, помечаем токен verified/not_subscribed.

Также раз в ~1 час рассылает дайджест по избранному (изменение цены с
момента последнего уведомления) — с частотой по users.notify_frequency
('daily'|'weekly'|'off').
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("site_bot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
SITE_BOT_TOKEN = os.getenv("SITE_BOT_TOKEN", "")
HATULI_CHANNEL = os.getenv("HATULI_CHANNEL", "@hatuliapp")

dp = Dispatcher()


async def _check_subscribed(bot: Bot, telegram_id: int) -> bool:
    """True/False — реально известный статус. None — Telegram не дал
    проверить (бот не админ канала: 'member list is inaccessible' — типичная
    ошибка ИМЕННО для каналов, Bot API требует прав администратора канала,
    чтобы читать чужое членство; для обычных групп этого ограничения нет).
    Раз бот НЕ администратор @hatuliapp, эта проверка сейчас всегда падает —
    поэтому по умолчанию НЕ блокируем вход при недоступной проверке (иначе
    никто вообще не сможет войти), только логируем предупреждение."""
    try:
        member = await bot.get_chat_member(HATULI_CHANNEL, telegram_id)
        return member.status in ("member", "administrator", "creator")
    except TelegramBadRequest as e:
        if "inaccessible" in str(e).lower() or "not enough rights" in str(e).lower():
            log.warning(
                "get_chat_member недоступен для %s (%s) — бот, вероятно, НЕ "
                "администратор канала %s. Добавь бота в администраторы "
                "канала, чтобы проверка подписки реально работала. "
                "Пока считаем подписку неизвестной и НЕ блокируем вход.",
                telegram_id, e, HATULI_CHANNEL)
            return None
        log.warning("get_chat_member failed for %s: %s", telegram_id, e)
        return False


@dp.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    from bot.core.site_auth import get_token_status
    from bot.db.pg import execute

    parts = (message.text or "").split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ""
    if not token:
        await message.answer(
            "Привет! Это бот входа в личный кабинет Hatuli.\n"
            "Открой сайт и нажми «Войти через Telegram» — оттуда придёт ссылка сюда."
        )
        return

    status = await get_token_status(token)
    if not status or status.get("status") == "expired":
        await message.answer("Ссылка для входа устарела — вернись на сайт и попробуй снова.")
        return

    telegram_id = message.from_user.id
    subscribed = await _check_subscribed(bot, telegram_id)

    await execute("""
        INSERT INTO users (user_id, username, full_name, channel_subscribed, created_at, updated_at)
        VALUES ($1, $2, $3, $4, now(), now())
        ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username,
            full_name = COALESCE(users.full_name, EXCLUDED.full_name),
            channel_subscribed = EXCLUDED.channel_subscribed,
            updated_at = now()
    """, telegram_id, message.from_user.username or "", message.from_user.full_name or "", subscribed)

    if subscribed is False:
        # Точно НЕ подписан (Telegram подтвердил статус left/kicked) — блокируем.
        await execute(
            "UPDATE login_tokens SET status = 'not_subscribed', telegram_id = $2 WHERE token = $1",
            token, telegram_id)
        await message.answer(
            f"Чтобы пользоваться личным кабинетом, сначала подпишись на {HATULI_CHANNEL} — "
            "и снова нажми на ссылку входа с сайта."
        )
        return
    # subscribed is True или None (Telegram не дал проверить — бот не админ
    # канала, см. _check_subscribed) — в обоих случаях пускаем. Блокировать
    # реальных пользователей из-за нашей же незавершённой настройки бота
    # хуже, чем временно пропустить проверку.

    await execute(
        "UPDATE login_tokens SET status = 'verified', telegram_id = $2, verified_at = now() WHERE token = $1",
        token, telegram_id)
    await message.answer("Готово! Возвращайся на сайт — вход выполнен автоматически. ✅")


async def _ensure_watch_state_table() -> None:
    from bot.db.pg import execute
    # Состояние "что видел этот пользователь в последний раз" — ПЕРСОНАЛЬНОЕ
    # (не общее на объявление), т.к. частота дайджеста своя у каждого
    # (daily/weekly) — сравнивать нужно с моментом ЕГО последнего уведомления,
    # а не глобального последнего скана.
    await execute("""
        CREATE TABLE IF NOT EXISTS favorite_watch_state (
            user_id BIGINT NOT NULL,
            listing_id TEXT NOT NULL,
            last_description TEXT,
            last_is_active BOOLEAN,
            PRIMARY KEY (user_id, listing_id)
        )
    """)


async def _send_digest_cycle(bot: Bot) -> None:
    """Раз в цикл: пользователям с избранным (квартиры и/или ЖК) и
    notify_frequency != 'off', у кого подошёл срок (daily/weekly), шлём
    изменения цены/описания/архивации — по избранным квартирам напрямую и
    по любым квартирам в избранных ЖК."""
    from bot.db.pg import fetch, execute

    await _ensure_watch_state_table()
    from bot.core.site_auth import _ensure_complex_favorites_table
    await _ensure_complex_favorites_table()

    rows = await fetch("""
        SELECT user_id, notify_frequency, last_notified_at
        FROM users
        WHERE COALESCE(is_blocked, 0) = 0
          AND COALESCE(notify_frequency, 'daily') != 'off'
          AND (EXISTS (SELECT 1 FROM favorites f WHERE f.user_id = users.user_id)
               OR EXISTS (SELECT 1 FROM complex_favorites cf WHERE cf.user_id = users.user_id))
    """)
    for r in rows:
        freq = r["notify_frequency"] or "daily"
        interval = "1 day" if freq == "daily" else "7 days"
        last = r["last_notified_at"]
        if last is not None:
            due = await fetch(
                f"SELECT (now() - $1::timestamptz) > interval '{interval}' AS due", last)
            if not due[0]["due"]:
                continue

        # Наблюдаемые квартиры: избранные напрямую + все живые квартиры в
        # избранных ЖК (сравнение по нормализованному имени, как везде в проекте).
        watched = await fetch("""
            SELECT a.id AS listing_id, a.price, a.url, a.rooms, a.area,
                   a.description, a.is_active, a.complex_name,
                   ph.old_price
            FROM apartment_listings a
            LEFT JOIN LATERAL (
                SELECT old_price FROM price_history h
                WHERE h.listing_id = a.id ORDER BY changed_at DESC LIMIT 1
            ) ph ON TRUE
            WHERE a.id IN (SELECT listing_id FROM favorites WHERE user_id = $1)
               OR lower(trim(regexp_replace(a.complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) IN (
                    SELECT lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
                    FROM complex_favorites cf JOIN complexes c ON c.id = cf.complex_id
                    WHERE cf.user_id = $1
               )
        """, r["user_id"])

        if not watched:
            await execute("UPDATE users SET last_notified_at = now() WHERE user_id = $1", r["user_id"])
            continue

        state_rows = await fetch(
            "SELECT listing_id, last_description, last_is_active FROM favorite_watch_state WHERE user_id = $1",
            r["user_id"])
        state = {s["listing_id"]: s for s in state_rows}

        lines = []
        for f in watched:
            lid = f["listing_id"]
            prev = state.get(lid)
            price_lines_added = False
            if f["old_price"] and f["old_price"] != f["price"]:
                direction = "снизилась" if f["price"] < f["old_price"] else "выросла"
                lines.append(
                    f"{'▼' if direction == 'снизилась' else '▲'} {f['rooms'] or '?'}-комн, "
                    f"{f['area'] or '?'} м² — цена {direction}: "
                    f"{f['old_price']/1e6:.1f} → {f['price']/1e6:.1f} млн ₸\n{f['url']}"
                )
                price_lines_added = True
            if prev is not None:
                if prev["last_is_active"] is True and f["is_active"] is False:
                    lines.append(
                        f"🗄 {f['rooms'] or '?'}-комн, {f['area'] or '?'} м² — ушло в архив (снято с публикации)\n{f['url']}"
                    )
                elif (prev["last_description"] is not None and f["description"]
                        and prev["last_description"] != f["description"] and not price_lines_added):
                    lines.append(
                        f"📝 {f['rooms'] or '?'}-комн, {f['area'] or '?'} м² — изменилось описание\n{f['url']}"
                    )
            await execute("""
                INSERT INTO favorite_watch_state (user_id, listing_id, last_description, last_is_active)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, listing_id) DO UPDATE SET
                    last_description = EXCLUDED.last_description, last_is_active = EXCLUDED.last_is_active
            """, r["user_id"], lid, f["description"], f["is_active"])

        if lines:
            text = "Изменения по вашему избранному:\n\n" + "\n\n".join(lines)
            try:
                await bot.send_message(r["user_id"], text)
            except Exception as e:
                log.warning("digest send failed for %s: %s", r["user_id"], e)
        await execute("UPDATE users SET last_notified_at = now() WHERE user_id = $1", r["user_id"])


async def _digest_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(random.uniform(55 * 60, 65 * 60))
        try:
            await _send_digest_cycle(bot)
        except Exception as e:
            log.error("digest cycle failed: %s", e, exc_info=True)


async def main() -> None:
    if not SITE_BOT_TOKEN:
        raise SystemExit("SITE_BOT_TOKEN не задан в .env")
    from bot.db.pg import init_pool
    await init_pool(DATABASE_URL)

    bot = Bot(SITE_BOT_TOKEN)
    log.info("=== Site bot started ===")
    asyncio.create_task(_digest_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
