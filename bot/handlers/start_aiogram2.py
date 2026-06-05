"""
Onboarding flow handler - COMPATIBLE VERSION for aiogram 2.x.

Collects user preferences in a multi-step FSM conversation and stores them in SQLite.
/start and /settings both trigger the same flow.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.db import queries

logger = logging.getLogger(__name__)


# ── FSM States ────────────────────────────────────────────────────────────────

class OnboardingStates(StatesGroup):
    deal_type     = State()
    city          = State()
    district      = State()
    budget_min    = State()
    budget_max    = State()
    rooms         = State()
    area_min      = State()
    move_in       = State()
    priorities    = State()
    confirm       = State()


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Build keyboard from rows of (label, callback_data) tuples."""
    keyboard = InlineKeyboardMarkup(row_width=2)
    for row in rows:
        buttons = [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        keyboard.add(*buttons)
    return keyboard


KB_DEAL_TYPE = _kb(
    [("🏠 Аренда", "ob:deal:rent"), ("🔑 Покупка", "ob:deal:buy")],
)

KB_CITIES = _kb(
    [("Астана", "ob:city:astana"), ("Алматы", "ob:city:almaty")],
    [("Другой город (ввести текстом)", "ob:city:other")],
)

KB_BUDGET_RENT = _kb(
    [("до 150 000 ₸", "ob:bmax:150000"), ("150–250 000 ₸", "ob:bmax:250000")],
    [("250–400 000 ₸", "ob:bmax:400000"), ("400 000 ₸+", "ob:bmax:0")],
    [("Ввести вручную", "ob:bmax:custom")],
)

KB_BUDGET_BUY = _kb(
    [("до 30 млн ₸", "ob:bmax:30000000"), ("30–60 млн ₸", "ob:bmax:60000000")],
    [("60–100 млн ₸", "ob:bmax:100000000"), ("100 млн ₸+", "ob:bmax:0")],
    [("Ввести вручную", "ob:bmax:custom")],
)

KB_ROOMS = InlineKeyboardMarkup(row_width=4)
KB_ROOMS.add(
    InlineKeyboardButton(text="1", callback_data="ob:rooms:1"),
    InlineKeyboardButton(text="2", callback_data="ob:rooms:2"),
    InlineKeyboardButton(text="3", callback_data="ob:rooms:3"),
    InlineKeyboardButton(text="4+", callback_data="ob:rooms:4+"),
)
KB_ROOMS.add(InlineKeyboardButton(text="✅ Готово", callback_data="ob:rooms:done"))

KB_AREA = _kb(
    [("от 30 м²", "ob:area:30"), ("от 50 м²", "ob:area:50")],
    [("от 70 м²", "ob:area:70"), ("от 100 м²", "ob:area:100")],
    [("Ввести вручную", "ob:area:custom"), ("Не важно", "ob:area:0")],
)

KB_MOVE_IN = _kb(
    [("Как можно скорее", "ob:movein:asap")],
    [("1–3 месяца", "ob:movein:1-3months")],
    [("Гибко / не тороплюсь", "ob:movein:flexible")],
)

KB_PRIORITIES_BASE = [
    ("🚇 Рядом с метро", "metro"),
    ("🏫 Рядом со школой", "school"),
    ("🔨 Без необходимости ремонта", "no_renovation"),
    ("👤 Только от собственника", "owner"),
]


def _priorities_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    for label, key in KB_PRIORITIES_BASE:
        check = "✅ " if key in selected else ""
        keyboard.add(InlineKeyboardButton(text=f"{check}{label}", callback_data=f"ob:pri:{key}"))
    keyboard.add(InlineKeyboardButton(text="▶️ Продолжить", callback_data="ob:pri:done"))
    return keyboard


KB_CONFIRM = _kb(
    [("✅ Сохранить", "ob:confirm:yes"), ("🔄 Начать заново", "ob:confirm:restart")],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _prefs_summary(data: dict[str, Any]) -> str:
    deal = "Аренда" if data.get("deal_type") == "rent" else "Покупка"
    city = data.get("city", "не указан")
    district = data.get("district") or "любой"
    bmin = data.get("budget_min")
    bmax = data.get("budget_max")
    if bmax:
        budget = f"{bmin or 0:,}–{bmax:,} ₸".replace(",", "\u2009")
    else:
        budget = "без ограничений"
    rooms = ", ".join(data.get("rooms_list") or []) or "любое"
    area = f"от {data['area_min']:.0f} м²" if data.get("area_min") else "любая"
    movein_map = {"asap": "Как можно скорее", "1-3months": "1–3 месяца", "flexible": "Гибко"}
    movein = movein_map.get(data.get("move_in", ""), "не указано")
    pri_labels = {k: l for l, k in KB_PRIORITIES_BASE}
    priorities = ", ".join(pri_labels[p] for p in (data.get("priorities_set") or set()) if p in pri_labels) or "не выбраны"

    return (
        f"<b>Тип сделки:</b> {deal}\n"
        f"<b>Город:</b> {city}\n"
        f"<b>Район:</b> {district}\n"
        f"<b>Бюджет:</b> {budget}\n"
        f"<b>Комнат:</b> {rooms}\n"
        f"<b>Площадь:</b> {area}\n"
        f"<b>Заезд:</b> {movein}\n"
        f"<b>Приоритеты:</b> {priorities}"
    )


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start_command(message: Message, state: FSMContext, db_path: str):
    """Handle /start command"""
    user = message.from_user
    if user:
        await queries.upsert_user(db_path, user.id, user.username)

    await state.finish()
    await OnboardingStates.deal_type.set()
    await message.answer(
        "Привет! Я помогу найти подходящую квартиру. Давайте настроим фильтры.\n\n"
        "<b>Шаг 1 из 9:</b> Тип сделки",
        reply_markup=KB_DEAL_TYPE,
        parse_mode="HTML",
    )


async def settings_command(message: Message, state: FSMContext, db_path: str):
    """Handle /settings command"""
    await message.answer("Перезапускаем настройки…")
    await start_command(message, state, db_path)


async def help_command(message: Message):
    """Handle /help command"""
    await message.answer(
        "<b>Команды бота:</b>\n"
        "/start — начало работы / онбординг\n"
        "/settings — изменить фильтры поиска\n"
        "/status — ваши текущие настройки\n"
        "/help — эта справка",
        parse_mode="HTML",
    )


async def status_command(message: Message, db_path: str):
    """Handle /status command"""
    if not message.from_user:
        return
    user = await queries.get_user(db_path, message.from_user.id)
    if not user or not user.get("deal_type"):
        await message.answer("Настройки не заданы. Используйте /start для онбординга.")
        return
    prefs = {
        "deal_type": user.get("deal_type"),
        "city": user.get("city"),
        "district": user.get("district"),
        "budget_min": user.get("budget_min"),
        "budget_max": user.get("budget_max"),
        "rooms_list": user.get("rooms") or [],
        "area_min": user.get("area_min"),
        "move_in": user.get("move_in"),
        "priorities_set": set(user.get("priorities") or []),
    }
    await message.answer(
        f"<b>Ваши текущие настройки:</b>\n{_prefs_summary(prefs)}",
        parse_mode="HTML",
    )


# ── Step 1: Deal type ──────────────────────────────────────────────────────────

async def step_deal_type(callback: CallbackQuery, state: FSMContext):
    deal = callback.data.split(":", 2)[2]
    await state.update_data(deal_type=deal)
    await OnboardingStates.city.set()
    label = "Аренда" if deal == "rent" else "Покупка"
    await callback.message.edit_text(
        f"✅ Тип сделки: <b>{label}</b>\n\n<b>Шаг 2 из 9:</b> Выберите город",
        reply_markup=KB_CITIES,
        parse_mode="HTML",
    )
    await callback.answer()


# ── Step 2: City ───────────────────────────────────────────────────────────────

async def step_city_kb(callback: CallbackQuery, state: FSMContext):
    city_code = callback.data.split(":", 2)[2]
    if city_code == "other":
        await OnboardingStates.city.set()
        await callback.message.edit_text(
            "Введите название города:",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    city_label = {"astana": "Астана", "almaty": "Алматы"}.get(city_code, city_code.capitalize())
    await state.update_data(city=city_code)
    await _ask_district(callback.message, state, city_label)
    await callback.answer()


async def step_city_text(message: Message, state: FSMContext):
    city = message.text.strip() if message.text else ""
    if not city:
        await message.answer("Пожалуйста, введите название города.")
        return
    await state.update_data(city=city.lower())
    await _ask_district(message, state, city)


async def _ask_district(msg_or_callback_msg: Any, state: FSMContext, city_label: str):
    await OnboardingStates.district.set()
    district_suggestions = _kb(
        [("Центр", "ob:district:центр"), ("Не важно", "ob:district:any")],
    )
    text = (
        f"✅ Город: <b>{city_label}</b>\n\n"
        "<b>Шаг 3 из 9:</b> Укажите район (или введите текстом):"
    )
    try:
        await msg_or_callback_msg.edit_text(text, reply_markup=district_suggestions, parse_mode="HTML")
    except Exception:
        await msg_or_callback_msg.answer(text, reply_markup=district_suggestions, parse_mode="HTML")


# ── Step 3: District ──────────────────────────────────────────────────────────

async def step_district_kb(callback: CallbackQuery, state: FSMContext):
    district_code = callback.data.split(":", 2)[2]
    district = None if district_code == "any" else district_code
    await state.update_data(district=district)
    await _ask_budget(callback.message, state, district or "любой")
    await callback.answer()


async def step_district_text(message: Message, state: FSMContext):
    district = message.text.strip() if message.text else ""
    await state.update_data(district=district or None)
    await _ask_budget(message, state, district or "любой")


async def _ask_budget(msg: Any, state: FSMContext, district_label: str):
    data = await state.get_data()
    deal_type = data.get("deal_type", "rent")
    keyboard = KB_BUDGET_BUY if deal_type == "buy" else KB_BUDGET_RENT
    await OnboardingStates.budget_max.set()
    text = (
        f"✅ Район: <b>{district_label}</b>\n\n"
        "<b>Шаг 4 из 9:</b> Максимальный бюджет:"
    )
    try:
        await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=keyboard, parse_mode="HTML")


# ── Step 4: Budget max ────────────────────────────────────────────────────────

async def step_budget_max_kb(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 2)[2]
    if value == "custom":
        await OnboardingStates.budget_max.set()
        await callback.message.edit_text(
            "Введите максимальный бюджет числом (например, 350000):",
            parse_mode="HTML",
        )
        await callback.answer()
        return
    budget_max = int(value) if value != "0" else None
    await state.update_data(budget_max=budget_max, _budget_custom=False)
    await _ask_budget_min(callback.message, state, budget_max)
    await callback.answer()


async def step_budget_max_text(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(" ", "").replace("\u2009", "")
    if not text.isdigit():
        await message.answer("Введите число, например: 350000")
        return
    budget_max = int(text) or None
    await state.update_data(budget_max=budget_max)
    await _ask_budget_min(message, state, budget_max)


async def _ask_budget_min(msg: Any, state: FSMContext, budget_max: int | None):
    await OnboardingStates.budget_min.set()
    kb = _kb(
        [("Пропустить (без минимума)", "ob:bmin:skip")],
        [("Ввести вручную", "ob:bmin:custom")],
    )
    bmax_str = f"{budget_max:,}".replace(",", "\u2009") if budget_max else "без ограничений"
    text = (
        f"✅ Максимум: <b>{bmax_str} ₸</b>\n\n"
        "<b>Шаг 5 из 9:</b> Минимальный бюджет (необязательно):"
    )
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Step 5: Budget min ────────────────────────────────────────────────────────

async def step_budget_min_kb(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 2)[2]
    if value == "skip":
        await state.update_data(budget_min=None)
        await _ask_rooms(callback.message, state)
        await callback.answer()
        return
    # custom: wait for text input
    await callback.message.edit_text(
        "Введите минимальный бюджет числом (например, 100000):",
        parse_mode="HTML",
    )
    await callback.answer()


async def step_budget_min_text(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(" ", "").replace("\u2009", "")
    if not text.isdigit():
        await message.answer("Введите число, например: 100000")
        return
    await state.update_data(budget_min=int(text))
    await _ask_rooms(message, state)


async def _ask_rooms(msg: Any, state: FSMContext):
    data = await state.get_data()
    await state.update_data(rooms_list=[])
    await OnboardingStates.rooms.set()
    text = "<b>Шаг 6 из 9:</b> Выберите количество комнат (можно несколько, затем «Готово»):"
    try:
        await msg.edit_text(text, reply_markup=KB_ROOMS, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=KB_ROOMS, parse_mode="HTML")


# ── Step 6: Rooms (multi-select) ──────────────────────────────────────────────

async def step_rooms(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 2)[2]
    data = await state.get_data()
    rooms_list: list[str] = list(data.get("rooms_list") or [])

    if value == "done":
        if not rooms_list:
            await callback.answer("Выберите хотя бы один вариант", show_alert=True)
            return
        await _ask_area(callback.message, state)
        await callback.answer()
