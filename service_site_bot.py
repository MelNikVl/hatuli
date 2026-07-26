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
    try:
        member = await bot.get_chat_member(HATULI_CHANNEL, telegram_id)
        return member.status in ("member", "administrator", "creator")
    except TelegramBadRequest as e:
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

    if not subscribed:
        await execute(
            "UPDATE login_tokens SET status = 'not_subscribed', telegram_id = $2 WHERE token = $1",
            token, telegram_id)
        await message.answer(
            f"Чтобы пользоваться личным кабинетом, сначала подпишись на {HATULI_CHANNEL} — "
            "и снова нажми на ссылку входа с сайта."
        )
        return

    await execute(
        "UPDATE login_tokens SET status = 'verified', telegram_id = $2, verified_at = now() WHERE token = $1",
        token, telegram_id)
    await message.answer("Готово! Возвращайся на сайт — вход выполнен автоматически. ✅")


async def _send_digest_cycle(bot: Bot) -> None:
    """Раз в цикл: пользователям с избранным и notify_frequency != 'off',
    у кого подошёл срок (daily/weekly), шлём изменения цены по избранному
    с момента последнего уведомления."""
    from bot.db.pg import fetch, execute

    rows = await fetch("""
        SELECT user_id, notify_frequency, last_notified_at
        FROM users
        WHERE COALESCE(is_blocked, 0) = 0
          AND COALESCE(notify_frequency, 'daily') != 'off'
          AND EXISTS (SELECT 1 FROM favorites f WHERE f.user_id = users.user_id)
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
        lines = []
        favs = await fetch("""
            SELECT f.listing_id, a.price, a.url, a.rooms, a.area,
                   ph.old_price, ph.changed_at
            FROM favorites f
            JOIN apartment_listings a ON a.id = f.listing_id
            LEFT JOIN LATERAL (
                SELECT old_price, changed_at FROM price_history h
                WHERE h.listing_id = a.id ORDER BY changed_at DESC LIMIT 1
            ) ph ON TRUE
            WHERE f.user_id = $1
        """, r["user_id"])
        for f in favs:
            if f["old_price"] and f["old_price"] != f["price"]:
                direction = "снизилась" if f["price"] < f["old_price"] else "выросла"
                lines.append(
                    f"{'▼' if direction == 'снизилась' else '▲'} {f['rooms'] or '?'}-комн, "
                    f"{f['area'] or '?'} м² — цена {direction}: "
                    f"{f['old_price']/1e6:.1f} → {f['price']/1e6:.1f} млн ₸\n{f['url']}"
                )
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
