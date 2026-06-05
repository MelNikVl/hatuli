"""
Location-based radius search handler.

Commands:
  /location — set a point on the map and a search radius
  /nolocation — remove the geo filter

Flow:
  1. User sends /location
  2. Bot offers: "Share GPS" button OR "Type address"
  3. Bot receives location (or geocodes the typed address)
  4. Bot shows radius buttons: 1 / 2 / 3 / 5 / 10 km
  5. Saved to DB; scheduler will filter listings by distance
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.core.geo import geocode
from bot.db import queries

logger = logging.getLogger(__name__)
router = Router()


class LocationStates(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius   = State()


# ── Keyboards ─────────────────────────────────────────────────────────────────

_KB_REQUEST_LOCATION = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📍 Поделиться геолокацией", request_location=True)],
        [KeyboardButton(text="✏️ Ввести адрес вручную")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

_KB_REMOVE = ReplyKeyboardRemove()

_KB_RADIUS = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="1 км",  callback_data="geo:radius:1"),
            InlineKeyboardButton(text="2 км",  callback_data="geo:radius:2"),
            InlineKeyboardButton(text="3 км",  callback_data="geo:radius:3"),
        ],
        [
            InlineKeyboardButton(text="5 км",  callback_data="geo:radius:5"),
            InlineKeyboardButton(text="10 км", callback_data="geo:radius:10"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="geo:radius:cancel")],
    ]
)


# ── /location ─────────────────────────────────────────────────────────────────

@router.message(Command("location"))
async def cmd_location(message: Message, state: FSMContext) -> None:
    await state.set_state(LocationStates.waiting_for_location)
    await message.answer(
        "📍 <b>Поиск по местоположению</b>\n\n"
        "Поделитесь геолокацией или введите адрес, "
        "и бот будет показывать только объявления в нужном радиусе.",
        reply_markup=_KB_REQUEST_LOCATION,
        parse_mode="HTML",
    )


@router.message(Command("nolocation"))
async def cmd_nolocation(message: Message, db_path: str) -> None:
    if not message.from_user:
        return
    await queries.save_user_location(db_path, message.from_user.id, None, None, None)
    await message.answer(
        "✅ Фильтр по местоположению отключён. Бот снова ищет объявления по всему городу.",
        reply_markup=_KB_REMOVE,
        parse_mode="HTML",
    )


# ── Receive GPS location ───────────────────────────────────────────────────────

@router.message(LocationStates.waiting_for_location, F.location)
async def handle_gps_location(message: Message, state: FSMContext) -> None:
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(lat=lat, lon=lon)
    await state.set_state(LocationStates.waiting_for_radius)
    await message.answer(
        f"✅ Координаты получены: <code>{lat:.5f}, {lon:.5f}</code>\n\n"
        "Выберите радиус поиска:",
        reply_markup=_KB_RADIUS,
        parse_mode="HTML",
    )
    # Remove the reply keyboard
    await message.answer(".", reply_markup=_KB_REMOVE)
    # Delete the "." placeholder
    try:
        from aiogram.exceptions import TelegramBadRequest
        sent = await message.answer("⬆", reply_markup=_KB_REMOVE)
        await sent.delete()
    except Exception:
        pass


# ── Receive typed address ──────────────────────────────────────────────────────

@router.message(LocationStates.waiting_for_location, F.text == "✏️ Ввести адрес вручную")
async def handle_address_prompt(message: Message) -> None:
    await message.answer(
        "Введите адрес (улица, дом).\nНапример: <code>ул. Кабанбай Батыра 17</code>",
        reply_markup=_KB_REMOVE,
        parse_mode="HTML",
    )


@router.message(LocationStates.waiting_for_location, F.text == "❌ Отмена")
async def handle_location_cancel_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=_KB_REMOVE)


@router.message(LocationStates.waiting_for_location, F.text)
async def handle_address_text(message: Message, state: FSMContext, db_path: str) -> None:
    text = (message.text or "").strip()
    if not text or text in ("✏️ Ввести адрес вручную", "❌ Отмена"):
        return

    # Determine city from user's saved prefs
    user = await queries.get_user(db_path, message.from_user.id) if message.from_user else None
    city = user.get("city") if user else None

    await message.answer("🔍 Геокодирую адрес…", reply_markup=_KB_REMOVE)

    coords = await geocode(text, city)
    if not coords:
        await message.answer(
            "❌ Не удалось определить координаты для этого адреса.\n"
            "Попробуйте написать точнее (улица + номер дома) или поделитесь GPS-локацией.",
        )
        # Re-show the keyboard
        await message.answer("Попробуйте ещё раз:", reply_markup=_KB_REQUEST_LOCATION)
        return

    lat, lon = coords
    await state.update_data(lat=lat, lon=lon)
    await state.set_state(LocationStates.waiting_for_radius)
    await message.answer(
        f"✅ Адрес найден: <code>{lat:.5f}, {lon:.5f}</code>\n\n"
        "Выберите радиус поиска:",
        reply_markup=_KB_RADIUS,
        parse_mode="HTML",
    )


# ── Receive radius ─────────────────────────────────────────────────────────────

@router.callback_query(LocationStates.waiting_for_radius, F.data.startswith("geo:radius:"))
async def handle_radius(callback: CallbackQuery, state: FSMContext, db_path: str) -> None:
    value = callback.data.split(":", 2)[2]

    if value == "cancel":
        await state.clear()
        await callback.message.edit_text("Отменено.")
        await callback.answer()
        return

    radius_km = int(value)
    data = await state.get_data()
    lat: float = data["lat"]
    lon: float = data["lon"]

    user_id = callback.from_user.id if callback.from_user else None
    if user_id:
        await queries.save_user_location(db_path, user_id, lat, lon, radius_km)

    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Фильтр по местоположению настроен!</b>\n\n"
        f"📍 Точка: <code>{lat:.5f}, {lon:.5f}</code>\n"
        f"🔵 Радиус: <b>{radius_km} км</b>\n\n"
        f"Бот будет показывать только объявления в радиусе {radius_km} км от этой точки.\n"
        f"Для отключения: /nolocation",
        parse_mode="HTML",
    )
    await callback.answer(f"Радиус {radius_km} км сохранён!")
