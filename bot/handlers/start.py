"""
Onboarding flow handler.

Collects user preferences in a multi-step FSM conversation and stores them in SQLite.
/start and /settings both trigger the same flow.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.db import queries

logger = logging.getLogger(__name__)

router = Router()


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
    owner_only    = State()
    property_type = State()
    confirm       = State()


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Build keyboard from rows of (label, callback_data) tuples."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


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

KB_ROOMS = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data="ob:rooms:1"),
            InlineKeyboardButton(text="2", callback_data="ob:rooms:2"),
            InlineKeyboardButton(text="3", callback_data="ob:rooms:3"),
            InlineKeyboardButton(text="4+", callback_data="ob:rooms:4+"),
        ],
        [InlineKeyboardButton(text="✅ Готово", callback_data="ob:rooms:done")],
    ]
)

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

# Astana districts for quick selection
_ASTANA_DISTRICTS = [
    ("Есиль (левый берег)", "ob:district:есиль"),
    ("Алматы (правый берег)", "ob:district:алматы р-н"),
    ("Сарыарка", "ob:district:сарыарка"),
    ("Байконур", "ob:district:байконур"),
]
_ALMATY_DISTRICTS = [
    ("Алмалинский", "ob:district:алмалинский"),
    ("Бостандыкский", "ob:district:бостандыкский"),
    ("Медеуский", "ob:district:медеуский"),
    ("Ауэзовский", "ob:district:ауэзовский"),
]


def _district_keyboard(city: str) -> InlineKeyboardMarkup:
    """Build a district keyboard appropriate for the given city."""
    if city in ("astana", "астана"):
        districts = _ASTANA_DISTRICTS
    elif city in ("almaty", "алматы"):
        districts = _ALMATY_DISTRICTS
    else:
        districts = []

    rows = []
    # Two districts per row
    for i in range(0, len(districts), 2):
        pair = districts[i : i + 2]
        rows.append([InlineKeyboardButton(text=label, callback_data=data) for label, data in pair])

    rows.append([InlineKeyboardButton(text="🗺 Любой район", callback_data="ob:district:any")])
    rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="ob:district:manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


KB_PRIORITIES_BASE = [
    ("🏫 Рядом со школой", "school"),
    ("🔨 Без необходимости ремонта", "no_renovation"),
    ("👤 Только от собственника", "owner"),
]


def _priorities_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for label, key in KB_PRIORITIES_BASE:
        check = "✅ " if key in selected else ""
        rows.append(
            [InlineKeyboardButton(text=f"{check}{label}", callback_data=f"ob:pri:{key}")]
        )
    rows.append([InlineKeyboardButton(text="▶️ Продолжить", callback_data="ob:pri:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


KB_OWNER_ONLY = _kb(
    [("🏠 Только от хозяев", "ob:owner:1")],
    [("🏢 Агентства тоже", "ob:owner:0")],
    [("Не важно", "ob:owner:skip")],
)

KB_PROPERTY_TYPE = _kb(
    [("🏗 Новостройка", "ob:proptype:new")],
    [("🏛 Вторичка", "ob:proptype:secondary")],
    [("Не важно", "ob:proptype:skip")],
)

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

    owner_val = data.get("owner_only")
    if owner_val == 1:
        owner_str = "только от хозяев"
    elif owner_val == 0:
        owner_str = "любой"
    else:
        owner_str = "не важно"

    proptype_map = {"new": "Новостройка", "secondary": "Вторичка"}
    proptype_str = proptype_map.get(data.get("property_type") or "", "не важно")

    return (
        f"<b>Тип сделки:</b> {deal}\n"
        f"<b>Город:</b> {city}\n"
        f"<b>Район:</b> {district}\n"
        f"<b>Бюджет:</b> {budget}\n"
        f"<b>Комнат:</b> {rooms}\n"
        f"<b>Площадь:</b> {area}\n"
        f"<b>Заезд:</b> {movein}\n"
        f"<b>Приоритеты:</b> {priorities}\n"
        f"<b>Продавец:</b> {owner_str}\n"
        f"<b>Тип жилья:</b> {proptype_str}"
    )


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _start_onboarding(message: Message, state: FSMContext, db_path: str) -> None:
    user = message.from_user
    if user:
        await queries.upsert_user(db_path, user.id, user.username)

    await state.clear()
    await state.set_state(OnboardingStates.deal_type)
    await message.answer(
        "Привет! Я помогу найти подходящую квартиру. Давайте настроим фильтры.\n\n"
        "<b>Шаг 1 из 11:</b> Тип сделки",
        reply_markup=KB_DEAL_TYPE,
        parse_mode="HTML",
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db_path: str) -> None:
    await _start_onboarding(message, state, db_path)


_KB_SETTINGS_GUARD = _kb(
    [("⚙️ Изменить настройки", "ob:settings:change")],
    [("✅ Оставить как есть", "ob:settings:keep")],
)


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext, db_path: str) -> None:
    if not message.from_user:
        return
    user = await queries.get_user(db_path, message.from_user.id)
    if user and user.get("deal_type"):
        # Show guard: don't wipe settings accidentally
        await message.answer(
            "У вас уже есть сохранённые настройки. Что хотите сделать?",
            reply_markup=_KB_SETTINGS_GUARD,
            parse_mode="HTML",
        )
    else:
        await _start_onboarding(message, state, db_path)


@router.callback_query(F.data == "ob:settings:keep")
async def cb_settings_keep(callback: CallbackQuery) -> None:
    await callback.message.edit_text("✅ Настройки не изменены.")
    await callback.answer()


@router.callback_query(F.data == "ob:settings:change")
async def cb_settings_change(callback: CallbackQuery, state: FSMContext, db_path: str) -> None:
    await callback.message.edit_text("Начинаем изменение настроек…")
    if callback.from_user:
        await queries.upsert_user(db_path, callback.from_user.id, callback.from_user.username)
    await state.clear()
    await state.set_state(OnboardingStates.deal_type)
    await callback.message.answer(
        "<b>Шаг 1 из 11:</b> Тип сделки",
        reply_markup=KB_DEAL_TYPE,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды бота:</b>\n"
        "/start — начало работы / онбординг\n"
        "/settings — изменить фильтры поиска\n"
        "/status — ваши текущие настройки\n"
        "/card — посмотреть заполненность профиля\n"
        "/location — поиск по радиусу от точки на карте\n"
        "/nolocation — отключить фильтр по радиусу\n"
        "/help — эта справка",
        parse_mode="HTML",
    )


async def _show_card(message: Message, db_path: str) -> None:
    """Show user's filter card with filled/missing field indicators."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if not message.from_user:
        return
    user = await queries.get_user(db_path, message.from_user.id)

    def _flag(val) -> str:
        return "✅" if val else "❌"

    if not user:
        await message.answer(
            "Профиль не найден. Используйте /start для регистрации.",
            parse_mode="HTML",
        )
        return

    deal_map = {"rent": "Аренда", "buy": "Покупка"}
    deal = deal_map.get(user.get("deal_type", ""), user.get("deal_type") or "")
    city = user.get("city") or ""
    district = user.get("district") or ""
    budget_min = user.get("budget_min")
    budget_max = user.get("budget_max")
    rooms = user.get("rooms") or []
    area_min = user.get("area_min")
    move_in = user.get("move_in") or ""
    priorities = user.get("priorities") or []
    owner_only = user.get("owner_only")
    property_type = user.get("property_type") or ""

    movein_map = {"asap": "Как можно скорее", "1-3months": "1–3 месяца", "flexible": "Гибко"}
    movein_label = movein_map.get(move_in, move_in) if move_in else ""

    pri_labels = {k: lbl for lbl, k in KB_PRIORITIES_BASE}
    priorities_str = ", ".join(pri_labels[p] for p in priorities if p in pri_labels) or ""

    budget_str = ""
    if budget_max:
        bmin_s = f"{budget_min:,}".replace(",", "\u2009") if budget_min else "0"
        bmax_s = f"{budget_max:,}".replace(",", "\u2009")
        budget_str = f"{bmin_s}–{bmax_s} ₸"

    if owner_only == 1:
        owner_str = "только от хозяев"
    elif owner_only == 0:
        owner_str = "агентства тоже"
    else:
        owner_str = "не важно"

    proptype_map = {"new": "Новостройка", "secondary": "Вторичка"}
    proptype_str = proptype_map.get(property_type, "не важно")

    lines = [
        "<b>📋 Ваш профиль поиска:</b>\n",
        f"{_flag(deal)} <b>Тип сделки:</b> {deal or '—'}",
        f"{_flag(city)} <b>Город:</b> {city or '—'}",
        f"{_flag(district)} <b>Район:</b> {district or 'любой'}",
        f"{_flag(budget_max)} <b>Бюджет:</b> {budget_str or '—'}",
        f"{_flag(rooms)} <b>Комнат:</b> {', '.join(str(r) for r in rooms) if rooms else '—'}",
        f"{_flag(area_min)} <b>Площадь:</b> {f'от {area_min:.0f} м²' if area_min else '—'}",
        f"{_flag(move_in)} <b>Заезд:</b> {movein_label or '—'}",
        f"{_flag(priorities)} <b>Приоритеты:</b> {priorities_str or '—'}",
        f"ℹ️ <b>Продавец:</b> {owner_str}",
        f"ℹ️ <b>Тип жилья:</b> {proptype_str}",
    ]

    # Check if critical fields are missing — offer to fill
    missing_critical = not deal or not city or not budget_max
    kb = None
    if missing_critical:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Заполнить", callback_data="ob:settings:change")]
            ]
        )
        lines.append("\n<i>Заполните обязательные поля для получения уведомлений.</i>")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.message(Command("card"))
async def cmd_card(message: Message, db_path: str) -> None:
    await _show_card(message, db_path)


@router.message(Command("status"))
async def cmd_status(message: Message, db_path: str) -> None:
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

@router.callback_query(OnboardingStates.deal_type, F.data.startswith("ob:deal:"))
async def step_deal_type(callback: CallbackQuery, state: FSMContext) -> None:
    deal = callback.data.split(":", 2)[2]
    await state.update_data(deal_type=deal)
    await state.set_state(OnboardingStates.city)
    label = "Аренда" if deal == "rent" else "Покупка"
    await callback.message.edit_text(
        f"✅ Тип сделки: <b>{label}</b>\n\n<b>Шаг 2 из 11:</b> Выберите город",
        reply_markup=KB_CITIES,
        parse_mode="HTML",
    )
    await callback.answer()


# ── Step 2: City ───────────────────────────────────────────────────────────────

@router.callback_query(OnboardingStates.city, F.data.startswith("ob:city:"))
async def step_city_kb(callback: CallbackQuery, state: FSMContext) -> None:
    city_code = callback.data.split(":", 2)[2]
    if city_code == "other":
        await state.set_state(OnboardingStates.city)
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


@router.message(OnboardingStates.city)
async def step_city_text(message: Message, state: FSMContext) -> None:
    city = message.text.strip() if message.text else ""
    if not city:
        await message.answer("Пожалуйста, введите название города.")
        return
    await state.update_data(city=city.lower())
    await _ask_district(message, state, city)


async def _ask_district(msg_or_callback_msg: Any, state: FSMContext, city_label: str) -> None:
    await state.set_state(OnboardingStates.district)
    data = await state.get_data()
    city = data.get("city", "")
    keyboard = _district_keyboard(city)
    text = (
        f"✅ Город: <b>{city_label}</b>\n\n"
        "<b>Шаг 3 из 11:</b> Выберите район или введите вручную:"
    )
    try:
        await msg_or_callback_msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await msg_or_callback_msg.answer(text, reply_markup=keyboard, parse_mode="HTML")


# ── Step 3: District ──────────────────────────────────────────────────────────

@router.callback_query(OnboardingStates.district, F.data.startswith("ob:district:"))
async def step_district_kb(callback: CallbackQuery, state: FSMContext) -> None:
    district_code = callback.data.split(":", 2)[2]

    if district_code == "manual":
        await callback.message.edit_text(
            "Введите название района (например: Есиль, Медеуский, Левый берег):",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    district = None if district_code == "any" else district_code
    await state.update_data(district=district)
    await _ask_budget(callback.message, state, district or "любой")
    await callback.answer()


@router.message(OnboardingStates.district)
async def step_district_text(message: Message, state: FSMContext) -> None:
    district = message.text.strip() if message.text else ""
    await state.update_data(district=district or None)
    await _ask_budget(message, state, district or "любой")


async def _ask_budget(msg: Any, state: FSMContext, district_label: str) -> None:
    data = await state.get_data()
    deal_type = data.get("deal_type", "rent")
    keyboard = KB_BUDGET_BUY if deal_type == "buy" else KB_BUDGET_RENT
    await state.set_state(OnboardingStates.budget_max)
    text = (
        f"✅ Район: <b>{district_label}</b>\n\n"
        "<b>Шаг 4 из 11:</b> Максимальный бюджет:"
    )
    try:
        await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=keyboard, parse_mode="HTML")


# ── Step 4: Budget max ────────────────────────────────────────────────────────

@router.callback_query(OnboardingStates.budget_max, F.data.startswith("ob:bmax:"))
async def step_budget_max_kb(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    if value == "custom":
        await state.set_state(OnboardingStates.budget_max)
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


@router.message(OnboardingStates.budget_max)
async def step_budget_max_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(" ", "").replace("\u2009", "")
    if not text.isdigit():
        await message.answer("Введите число, например: 350000")
        return
    budget_max = int(text) or None
    await state.update_data(budget_max=budget_max)
    await _ask_budget_min(message, state, budget_max)


async def _ask_budget_min(msg: Any, state: FSMContext, budget_max: int | None) -> None:
    await state.set_state(OnboardingStates.budget_min)
    kb = _kb(
        [("Пропустить (без минимума)", "ob:bmin:skip")],
        [("Ввести вручную", "ob:bmin:custom")],
    )
    bmax_str = f"{budget_max:,}".replace(",", "\u2009") if budget_max else "без ограничений"
    text = (
        f"✅ Максимум: <b>{bmax_str} ₸</b>\n\n"
        "<b>Шаг 5 из 11:</b> Минимальный бюджет (необязательно):"
    )
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Step 5: Budget min ────────────────────────────────────────────────────────

@router.callback_query(OnboardingStates.budget_min, F.data.startswith("ob:bmin:"))
async def step_budget_min_kb(callback: CallbackQuery, state: FSMContext) -> None:
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


@router.message(OnboardingStates.budget_min)
async def step_budget_min_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(" ", "").replace("\u2009", "")
    if not text.isdigit():
        await message.answer("Введите число, например: 100000")
        return
    await state.update_data(budget_min=int(text))
    await _ask_rooms(message, state)


async def _ask_rooms(msg: Any, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(rooms_list=[])
    await state.set_state(OnboardingStates.rooms)
    text = "<b>Шаг 6 из 11:</b> Выберите количество комнат (можно несколько, затем «Готово»):"
    try:
        await msg.edit_text(text, reply_markup=KB_ROOMS, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=KB_ROOMS, parse_mode="HTML")


# ── Step 6: Rooms (multi-select) ──────────────────────────────────────────────

@router.callback_query(OnboardingStates.rooms, F.data.startswith("ob:rooms:"))
async def step_rooms(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    data = await state.get_data()
    rooms_list: list[str] = list(data.get("rooms_list") or [])

    if value == "done":
        if not rooms_list:
            await callback.answer("Выберите хотя бы один вариант", show_alert=True)
            return
        await _ask_area(callback.message, state)
        await callback.answer()
        return

    # Toggle selection
    if value in rooms_list:
        rooms_list.remove(value)
    else:
        rooms_list.append(value)

    await state.update_data(rooms_list=rooms_list)

    # Rebuild keyboard with updated selection indicators
    selected_labels = {r: f"✅ {r}" for r in rooms_list}
    rows = []
    for val in ("1", "2", "3", "4+"):
        label = selected_labels.get(val, val)
        rows.append(InlineKeyboardButton(text=label, callback_data=f"ob:rooms:{val}"))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[rows, [InlineKeyboardButton(text="✅ Готово", callback_data="ob:rooms:done")]]
    )
    selected_str = ", ".join(rooms_list) if rooms_list else "не выбрано"
    await callback.message.edit_text(
        f"<b>Шаг 6 из 11:</b> Комнаты (выбрано: {selected_str}):",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


# ── Step 7: Area ───────────────────────────────────────────────────────────────

async def _ask_area(msg: Any, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.area_min)
    text = "<b>Шаг 7 из 11:</b> Минимальная площадь:"
    try:
        await msg.edit_text(text, reply_markup=KB_AREA, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=KB_AREA, parse_mode="HTML")


@router.callback_query(OnboardingStates.area_min, F.data.startswith("ob:area:"))
async def step_area_kb(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    if value == "custom":
        await callback.message.edit_text(
            "Введите минимальную площадь в м² (число):",
            parse_mode="HTML",
        )
        await callback.answer()
        return
    area = float(value) if value != "0" else None
    await state.update_data(area_min=area)
    await _ask_move_in(callback.message, state, area)
    await callback.answer()


@router.message(OnboardingStates.area_min)
async def step_area_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(",", ".")
    try:
        area = float(text)
    except ValueError:
        await message.answer("Введите число, например: 55")
        return
    await state.update_data(area_min=area)
    await _ask_move_in(message, state, area)


async def _ask_move_in(msg: Any, state: FSMContext, area: float | None) -> None:
    await state.set_state(OnboardingStates.move_in)
    area_str = f"{area:.0f} м²" if area else "любая"
    text = f"✅ Площадь: <b>от {area_str}</b>\n\n<b>Шаг 8 из 11:</b> Когда планируете заезд?"
    try:
        await msg.edit_text(text, reply_markup=KB_MOVE_IN, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=KB_MOVE_IN, parse_mode="HTML")


# ── Step 8: Move-in ───────────────────────────────────────────────────────────

@router.callback_query(OnboardingStates.move_in, F.data.startswith("ob:movein:"))
async def step_move_in(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    await state.update_data(move_in=value)
    await state.set_state(OnboardingStates.priorities)
    data = await state.get_data()
    priorities_set: set[str] = set(data.get("priorities_set") or [])
    await callback.message.edit_text(
        "<b>Шаг 9 из 11:</b> Выберите приоритеты (необязательно):",
        reply_markup=_priorities_keyboard(priorities_set),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Step 9: Priorities (multi-select) ─────────────────────────────────────────

@router.callback_query(OnboardingStates.priorities, F.data.startswith("ob:pri:"))
async def step_priorities(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    data = await state.get_data()
    priorities_set: set[str] = set(data.get("priorities_set") or [])

    if value == "done":
        await state.update_data(priorities_set=list(priorities_set))
        await _ask_owner_only(callback.message, state)
        await callback.answer()
        return

    # Toggle
    if value in priorities_set:
        priorities_set.remove(value)
    else:
        priorities_set.add(value)

    await state.update_data(priorities_set=list(priorities_set))
    await callback.message.edit_text(
        "<b>Шаг 9 из 11:</b> Приоритеты (нажмите «Продолжить» когда готово):",
        reply_markup=_priorities_keyboard(priorities_set),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Step 10: Owner only ────────────────────────────────────────────────────────

async def _ask_owner_only(msg: Any, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.owner_only)
    text = "<b>Шаг 10 из 11:</b> От кого искать объявления?"
    try:
        await msg.edit_text(text, reply_markup=KB_OWNER_ONLY, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=KB_OWNER_ONLY, parse_mode="HTML")


@router.callback_query(OnboardingStates.owner_only, F.data.startswith("ob:owner:"))
async def step_owner_only(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    if value == "1":
        await state.update_data(owner_only=1)
    elif value == "0":
        await state.update_data(owner_only=0)
    else:  # skip
        await state.update_data(owner_only=None)
    await _ask_property_type(callback.message, state)
    await callback.answer()


# ── Step 11: Property type ─────────────────────────────────────────────────────

async def _ask_property_type(msg: Any, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.property_type)
    text = "<b>Шаг 11 из 11:</b> Тип жилья?"
    try:
        await msg.edit_text(text, reply_markup=KB_PROPERTY_TYPE, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=KB_PROPERTY_TYPE, parse_mode="HTML")


@router.callback_query(OnboardingStates.property_type, F.data.startswith("ob:proptype:"))
async def step_property_type(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 2)[2]
    if value == "new":
        await state.update_data(property_type="new")
    elif value == "secondary":
        await state.update_data(property_type="secondary")
    else:  # skip
        await state.update_data(property_type=None)
    await _show_confirm(callback.message, state)
    await callback.answer()


# ── Confirm ───────────────────────────────────────────────────────────────────

async def _show_confirm(msg: Any, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.confirm)
    data = await state.get_data()
    summary = _prefs_summary(data)
    try:
        await msg.edit_text(
            f"<b>Проверьте настройки:</b>\n\n{summary}\n\nВсё верно?",
            reply_markup=KB_CONFIRM,
            parse_mode="HTML",
        )
    except Exception:
        await msg.answer(
            f"<b>Проверьте настройки:</b>\n\n{summary}\n\nВсё верно?",
            reply_markup=KB_CONFIRM,
            parse_mode="HTML",
        )


@router.callback_query(OnboardingStates.confirm, F.data.startswith("ob:confirm:"))
async def step_confirm(callback: CallbackQuery, state: FSMContext, db_path: str) -> None:
    action = callback.data.split(":", 2)[2]

    if action == "restart":
        await state.clear()
        await callback.message.edit_text("Начинаем заново…", parse_mode="HTML")
        user = callback.from_user
        if user:
            await queries.upsert_user(db_path, user.id, user.username)
        await state.set_state(OnboardingStates.deal_type)
        await callback.message.answer(
            "<b>Шаг 1 из 11:</b> Тип сделки",
            reply_markup=KB_DEAL_TYPE,
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Save to DB
    data = await state.get_data()
    user_id = callback.from_user.id if callback.from_user else None
    if user_id:
        prefs = {
            "deal_type": data.get("deal_type"),
            "city": data.get("city"),
            "district": data.get("district"),
            "budget_min": data.get("budget_min"),
            "budget_max": data.get("budget_max"),
            "rooms": data.get("rooms_list") or [],
            "area_min": data.get("area_min"),
            "move_in": data.get("move_in"),
            "priorities": list(data.get("priorities_set") or []),
            "owner_only": data.get("owner_only"),
            "property_type": data.get("property_type"),
        }
        await queries.save_user_prefs(db_path, user_id, prefs)

    await state.clear()
    await callback.message.edit_text(
        "✅ <b>Настройки сохранены!</b>\n\n"
        "Бот будет присылать подходящие объявления по вашим критериям.\n"
        "Для изменения настроек используйте /settings.",
        parse_mode="HTML",
    )
    from bot.handlers.menu import MAIN_MENU
    await callback.message.answer("Меню:", reply_markup=MAIN_MENU)
    await callback.answer("Настройки сохранены!")
