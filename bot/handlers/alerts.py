"""
Callback handlers for listing card action buttons.

Handles:
  ❤️  fav:<listing_id>     — save to favorites
  👎  skip:<listing_id>    — mark as "not for me" (blocked)
  🔔  follow:<listing_id>  — subscribe to price/status updates
  📞  contact:<listing_id> — show contact info
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.db import queries

logger = logging.getLogger(__name__)
router = Router()

_FAV_PAGE_SIZE = 5


def _build_fav_keyboard(page: int, total: int) -> InlineKeyboardMarkup | None:
    """Build pagination keyboard for favorites."""
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="← Назад", callback_data=f"fav_page:{page - 1}"))
    if (page + 1) * _FAV_PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="Далее →", callback_data=f"fav_page:{page + 1}"))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def _send_favorites_page(
    message: Message, db_path: str, user_id: int, page: int
) -> None:
    total = await queries.count_favorites(db_path, user_id)
    if total == 0:
        await message.answer("У вас нет избранных объявлений.")
        return

    offset = page * _FAV_PAGE_SIZE
    listings = await queries.get_favorites_paginated(
        db_path, user_id, offset=offset, limit=_FAV_PAGE_SIZE
    )

    lines = [f"<b>❤️ Избранное</b> (стр. {page + 1}, всего {total}):\n"]
    for lst in listings:
        price = lst.get("price") or 0
        price_str = f"{price:,} ₸".replace(",", "\u2009")
        address = lst.get("address") or lst.get("title") or "—"
        url = lst.get("url") or ""
        lines.append(f"• <b>{price_str}</b> — {address}\n  <a href='{url}'>Открыть</a>")

    kb = _build_fav_keyboard(page, total)
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, db_path: str) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
    await _send_favorites_page(message, db_path, user_id, page=0)


@router.callback_query(F.data.startswith("fav_page:"))
async def cb_fav_page(callback: CallbackQuery, db_path: str) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id:
        await callback.answer()
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    await _send_favorites_page(callback.message, db_path, user_id, page=page)
    await callback.answer()


# ── Favorite ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fav:"))
async def cb_favorite(callback: CallbackQuery, db_path: str) -> None:
    listing_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id:
        await callback.answer("Ошибка пользователя", show_alert=True)
        return

    try:
        already = await queries.is_favorite(db_path, user_id, listing_id)
        if already:
            await queries.remove_favorite(db_path, user_id, listing_id)
            await callback.answer("Удалено из избранного")
            return

        await queries.add_favorite(db_path, user_id, listing_id)
        await queries.log_view(db_path, user_id, listing_id, "favorite")
        count = await queries.count_favorites(db_path, user_id)

        msg = "❤️ Добавлено в избранное!"
        if count >= 3:
            msg += "\n\nУ вас уже 3+ избранных — можете сравнить: /compare"

        await callback.answer(msg, show_alert=count >= 3)
    except Exception:
        logger.exception("cb_favorite failed user=%s listing=%s", user_id, listing_id)
        await callback.answer("Произошла ошибка", show_alert=True)


# ── Skip / Block ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery, db_path: str) -> None:
    listing_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id:
        await callback.answer("Ошибка пользователя", show_alert=True)
        return

    try:
        await queries.block_listing(db_path, user_id, listing_id)
        await queries.log_view(db_path, user_id, listing_id, "skip")
        await callback.answer("👎 Объявление скрыто")

        # Try to delete the card message so it doesn't clutter the chat
        try:
            await callback.message.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("cb_skip failed user=%s listing=%s", user_id, listing_id)
        await callback.answer("Произошла ошибка", show_alert=True)


# ── Follow / Saved search ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("follow:"))
async def cb_follow(callback: CallbackQuery, db_path: str) -> None:
    listing_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id:
        await callback.answer("Ошибка пользователя", show_alert=True)
        return

    try:
        already = await queries.is_following(db_path, user_id, listing_id)
        if already:
            await callback.answer("🔔 Вы уже следите за этим объявлением")
            return

        await queries.add_saved_search(db_path, user_id, listing_id)
        await queries.log_view(db_path, user_id, listing_id, "follow")
        await callback.answer(
            "🔔 Отслеживается! Уведомим об изменении цены или статуса.",
            show_alert=True,
        )
    except Exception:
        logger.exception("cb_follow failed user=%s listing=%s", user_id, listing_id)
        await callback.answer("Произошла ошибка", show_alert=True)


# ── Contact ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("contact:"))
async def cb_contact(callback: CallbackQuery, db_path: str) -> None:
    listing_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id:
        await callback.answer("Ошибка пользователя", show_alert=True)
        return

    try:
        await queries.log_view(db_path, user_id, listing_id, "contact")
        listing = await queries.get_listing(db_path, listing_id)

        if not listing:
            await callback.answer("Объявление не найдено", show_alert=True)
            return

        phone = listing.get("phone")
        url = listing.get("url", "")

        if phone:
            text = f"📞 Телефон: <code>{phone}</code>\n🔗 <a href='{url}'>Объявление</a>"
        else:
            text = f"📞 Контакт не указан в объявлении.\n🔗 <a href='{url}'>Открыть объявление</a>"

        await callback.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
        await callback.answer()
    except Exception:
        logger.exception("cb_contact failed user=%s listing=%s", user_id, listing_id)
        await callback.answer("Произошла ошибка", show_alert=True)
