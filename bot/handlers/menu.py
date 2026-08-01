"""
Reply keyboard menu handler.

Provides a persistent bottom menu for the bot with quick-access buttons.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from bot.db import queries

logger = logging.getLogger(__name__)
router = Router()

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏠 Главная"), KeyboardButton(text="📋 Мои фильтры")],
        [KeyboardButton(text="⏹ Пауза уведомлений"), KeyboardButton(text="▶️ Возобновить уведомления")],
        [KeyboardButton(text="🔄 Настроить заново"), KeyboardButton(text="🗺 Последние на карте")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# ── 🏠 Главная ────────────────────────────────────────────────────────────────

@router.message(F.text == "🏠 Главная")
async def menu_home(message: Message, db_path: str) -> None:
    if not message.from_user:
        return
    user = await queries.get_user(db_path, message.from_user.id)
    if not user or not user.get("deal_type"):
        await message.answer(
            "Добро пожаловать! Настройки не заданы. Используйте /start для настройки.",
            reply_markup=MAIN_MENU,
        )
        return

    deal = "Аренда" if user.get("deal_type") == "rent" else "Покупка"
    city = user.get("city") or "не указан"
    bmax = user.get("budget_max")
    budget = f"до {bmax:,} ₸".replace(",", "\u2009") if bmax else "без ограничений"
    paused = bool(user.get("is_paused"))
    status = "⏹ Уведомления на паузе" if paused else "▶️ Уведомления активны"

    await message.answer(
        f"🏠 <b>Главная</b>\n\n"
        f"<b>Тип:</b> {deal}\n"
        f"<b>Город:</b> {city}\n"
        f"<b>Бюджет:</b> {budget}\n"
        f"<b>Статус:</b> {status}\n\n"
        f"Используйте меню ниже для управления ботом.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


# ── 📋 Мои фильтры ────────────────────────────────────────────────────────────

@router.message(F.text == "📋 Мои фильтры")
async def menu_my_filters(message: Message, db_path: str) -> None:
    from bot.handlers.start import _show_card
    await _show_card(message, db_path)


# ── ⏹ Пауза уведомлений ──────────────────────────────────────────────────────

@router.message(F.text == "⏹ Пауза уведомлений")
async def menu_pause(message: Message, db_path: str) -> None:
    if not message.from_user:
        return
    await queries.set_user_paused(db_path, message.from_user.id, paused=True)
    await message.answer(
        "⏹ <b>Уведомления приостановлены.</b>\n\n"
        "Бот не будет присылать новые объявления. "
        "Нажмите «▶️ Возобновить уведомления», чтобы снова получать их.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


# ── ▶️ Возобновить уведомления ────────────────────────────────────────────────

@router.message(F.text == "▶️ Возобновить уведомления")
async def menu_resume(message: Message, db_path: str) -> None:
    if not message.from_user:
        return
    await queries.set_user_paused(db_path, message.from_user.id, paused=False)
    await message.answer(
        "▶️ <b>Уведомления возобновлены!</b>\n\n"
        "Бот снова будет присылать подходящие объявления.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


# ── 🔄 Настроить заново — с подтверждением ───────────────────────────────────

@router.message(F.text == "🔄 Настроить заново")
async def menu_restart(message: Message, db_path: str) -> None:
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, сбросить", callback_data="reset:confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="reset:cancel"),
            ]
        ]
    )
    await message.answer(
        "⚠️ <b>Вы уверены?</b>\n\n"
        "Это удалит все ваши фильтры и историю просмотров. "
        "После сброса вы заново пройдёте настройку.",
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )


@router.callback_query(F.data == "reset:confirm")
async def cb_reset_confirm(callback: CallbackQuery, state: FSMContext, db_path: str) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return

    user_id = callback.from_user.id

    # Delete history and reset all filter fields
    await queries.reset_user_data(db_path, user_id)

    await callback.message.edit_text(
        "✅ <b>Данные сброшены.</b> Запускаем настройку заново…",
        parse_mode="HTML",
    )
    await callback.answer()

    # Restart onboarding
    from bot.handlers.start import _start_onboarding
    await state.clear()
    await _start_onboarding(callback.message, state, db_path)


@router.callback_query(F.data == "reset:cancel")
async def cb_reset_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text("❌ Сброс отменён.")
    await callback.answer()


# ── 🗺 Последние на карте ─────────────────────────────────────────────────────

@router.message(F.text == "🗺 Последние на карте")
async def menu_last_on_map(message: Message, db_path: str) -> None:
    if not message.from_user:
        return

    user = await queries.get_user(db_path, message.from_user.id)
    if not user or not user.get("deal_type"):
        await message.answer(
            "Настройте фильтры поиска командой /start.",
            reply_markup=MAIN_MENU,
        )
        return

    listings = await queries.get_recent_listings_for_user(
        db_path,
        city=user.get("city"),
        deal_type=user.get("deal_type"),
        budget_max=user.get("budget_max"),
        n=5,
    )

    if not listings:
        await message.answer(
            "Объявлений по вашим фильтрам пока не найдено. Бот пришлёт их, как только они появятся.",
            reply_markup=MAIN_MENU,
        )
        return

    city = user.get("city") or ""
    city_name_map = {"astana": "Астана", "almaty": "Алматы"}
    city_display = city_name_map.get(city.lower(), city.capitalize())

    # Build one inline button per listing  (url → krisha.kz)
    buttons: list[list[InlineKeyboardButton]] = []
    for item in listings:
        price = item.get("price") or 0
        price_str = f"{price:,}".replace(",", "\u2009")
        address = item.get("address") or "адрес не указан"
        url = item.get("url") or ""

        label = f"📍 {address} — {price_str} ₸"
        if len(label) > 64:
            label = label[:61] + "…"

        if url:
            buttons.append([InlineKeyboardButton(text=label, url=url)])

    # Add city-level Yandex Maps button at the bottom
    if city_display:
        yandex_url = f"https://yandex.kz/maps/?text={quote(city_display + ' квартиры')}"
        buttons.append([InlineKeyboardButton(text=f"🗺 {city_display} на Яндекс.Картах", url=yandex_url)])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await message.answer(
        f"🗺 <b>Последние объявления по вашим фильтрам:</b>\n"
        f"<i>Нажмите на кнопку, чтобы открыть объявление на Krisha.kz</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )
