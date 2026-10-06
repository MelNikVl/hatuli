"""Thin aiogram UI. All analytics and PostgreSQL writes live in core."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from html import escape
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.core.buyer import analyze_for_buyer, extract_krisha_url, money
from bot.core.buyer_store import ensure_user, save_favorite, save_profile

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
_cache: OrderedDict = OrderedDict()
_analysis_slots = asyncio.Semaphore(4)

START = ('👋 Пришли ссылку на квартиру с Krisha.kz.\n\n'
         'Я быстро скажу:\n• что в ней хорошо;\n• что настораживает;\n'
         '• нормальная ли цена;\n• стоит ли ехать смотреть;\n'
         '• есть ли рядом варианты лучше.\n\nПока — покупка квартир в Астане.')


class Profile(StatesGroup):
    budget = State()
    rooms = State()
    area = State()
    kind = State()


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


def actions(lid: str) -> InlineKeyboardMarkup:
    return keyboard([[('🔥 Показать лучше', f'b:better:{lid}'), ('⭐ Сохранить', f'b:save:{lid}')],
                     [('⚠️ Что проверить', f'b:check:{lid}')], [('🎯 Подбирать под меня', 'p:start')]])


def render_summary(result: dict) -> str:
    score = f" · {result['score']/10:.1f}/10" if result['score'] is not None else ''
    lines = [f"<b>{escape(result['verdict']['label'])}{score}</b>"]
    for label, sign, key in [('Почему да', '+', 'positives'), ('Что смущает', '−', 'negatives')]:
        if result[key]:
            lines += ['', f'<b>{label}</b>'] + [f'{sign} {escape(t)}' for t in result[key][:3]]
    if not result['positives'] and not result['negatives']:
        lines += [escape(t) for t in result['verdict']['reasons'][:2]]
    p = result['price_summary']
    lines += ['', f"💰 <b>{money(p['asking'])}</b>"]
    if p['reliable']:
        lines += [f"Рыночный ориентир: ~{money(p['fair'])}", f"Попробовать предложить: ~{money(p['offer'])}"]
    else:
        lines.append('Мало хороших аналогов — оценка цены ненадёжна.')
    lines += ['', escape(result['urgency']['text'])]
    n = len(result['better_nearby'])
    lines += ['', f'Вариантов рядом с преимуществами: {n}.' if n else 'Убедительно лучших вариантов в проверенной выборке не нашёл.']
    if result['confidence'] == 'limited':
        lines.append('Вывод предварительный: данные неполные.')
    return '\n'.join(lines)


def render_alternative(r: dict, base: dict) -> str:
    d = r['listing']
    lines = [f"🔥 <b>Вариант с преимуществами — около {r['distance_m']} м</b>",
             f"{money(d['price'])} вместо {money(base.get('price'))}",
             f"{d['area']:g} м² вместо {base['area']:g} м²"]
    if d.get('floor') and d.get('floors_total'):
        old = f" вместо {base['floor']}/{base['floors_total']}" if base.get('floor') and base.get('floors_total') else ''
        lines.append(f"{d['floor']}/{d['floors_total']} этаж{old}")
    lines += ['', '<b>Почему лучше:</b> ' + escape('; '.join(r['advantages'][:3]))]
    if r['tradeoffs']:
        lines.append('<b>Компромисс:</b> ' + escape('; '.join(r['tradeoffs'])))
    if r.get('urgency', {}).get('level') == 'high':
        lines.append(escape(r['urgency']['text']))
    lines.extend(escape(w) for w in r['warnings'])
    return '\n'.join(lines)


def remember(uid: int, result: dict) -> None:
    key = (uid, str(result['listing']['id']))
    _cache[key] = (time.monotonic(), result)
    _cache.move_to_end(key)
    while len(_cache) > 256:
        _cache.popitem(last=False)


def recalled(uid: int, lid: str) -> dict | None:
    value = _cache.get((uid, lid))
    if value and time.monotonic() - value[0] < 600:
        return value[1]
    _cache.pop((uid, lid), None)
    return None


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(START)


@router.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer('Настройка отменена. Пришлите ссылку на квартиру.')


# Registered before FSM text handlers: a URL works even mid-onboarding.
@router.message(lambda m: bool(message_url(m)))
async def link(message: Message, state: FSMContext) -> None:
    if _analysis_slots.locked():
        await message.answer('Сейчас много запросов. Попробуйте через минуту.')
        return
    await message.answer('Смотрю цену, риски и варианты рядом…')
    try:
        async with _analysis_slots:
            await ensure_user(message.from_user.id, message.from_user.username)
            result = await asyncio.wait_for(analyze_for_buyer(message_url(message), message.from_user.id), 65)
        if not result.get('found'):
            await message.answer(result['message'])
            return
        remember(message.from_user.id, result)
        await message.answer(render_summary(result), parse_mode='HTML',
                             reply_markup=actions(str(result['listing']['id'])))
    except Exception:
        log.exception('Buyer analysis failed')
        await message.answer('Не удалось завершить анализ. Попробуйте отправить ссылку чуть позже.')


def message_url(message: Message) -> str | None:
    url = extract_krisha_url(message.text or message.caption or '')
    if url:
        return url
    for entity in (message.entities or message.caption_entities or []):
        if entity.type == 'text_link' and entity.url:
            url = extract_krisha_url(entity.url)
            if url:
                return url
    return None


@router.callback_query(F.data.startswith('b:'))
async def action(callback: CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(':')
    if len(parts) != 3 or not parts[2].isdigit():
        return
    _, command, lid = parts
    try:
        if command == 'save':
            ok = await save_favorite(callback.from_user.id, lid)
            await callback.message.answer('⭐ Сохранено в избранном Clearly.' if ok else 'Объявление не найдено.')
            return
        result = recalled(callback.from_user.id, lid)
        if not result:
            await callback.message.answer('Анализ устарел. Пришлите ссылку ещё раз — обновлю данные.')
            return
        if command == 'check':
            checks = result['risks']
            text = '⚠️ На просмотре проверьте\n\n' + '\n'.join(f'{i}. {t}' for i, t in enumerate(checks, 1)) if checks else 'Для этой квартиры пока нет конкретных пунктов проверки: данных недостаточно. Это не означает отсутствие рисков.'
            await callback.message.answer(text)
        elif command == 'better':
            if not result['better_nearby']:
                await callback.message.answer('В проверенной выборке рядом убедительно лучших вариантов не нашёл.')
            for r in result['better_nearby']:
                lid2 = str(r['listing']['id'])
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text='Открыть Krisha', url=f'https://krisha.kz/a/show/{lid2}')]])
                await callback.message.answer(render_alternative(r, result['listing']), parse_mode='HTML', reply_markup=markup)
    except Exception:
        log.exception('Buyer action failed')
        await callback.message.answer('Не удалось выполнить действие. Попробуйте позже.')


@router.callback_query(F.data == 'p:start')
async def profile_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(Profile.budget)
    await callback.message.answer('1/4. Максимальный бюджет? Можно ввести сумму в млн ₸. /cancel — отмена.',
        reply_markup=keyboard([[(f'до {n} млн', f'p:budget:{n}') for n in (25, 30)],
                               [(f'до {n} млн', f'p:budget:{n}') for n in (35, 40)]]))


async def choose_budget(message: Message, state: FSMContext, value: str) -> None:
    try:
        amount = float(value.replace(',', '.').replace(' ', ''))
        if not 1 <= amount <= 2000:
            raise ValueError
    except (ValueError, OverflowError):
        await message.answer('Введите бюджет от 1 до 2000 в миллионах ₸, например 32.5.')
        return
    await state.update_data(budget_max=round(amount * 1e6), rooms=[])
    await state.set_state(Profile.rooms)
    await message.answer('2/4. Сколько комнат? Выберите несколько, затем «Готово».', reply_markup=rooms_keyboard([]))


def rooms_keyboard(selected: list[int]) -> InlineKeyboardMarkup:
    return keyboard([[(('✓ ' if n in selected else '') + ('4+' if n == 4 else str(n)), f'p:rooms:{n}') for n in (1, 2, 3, 4)],
                     [('Готово', 'p:rooms:done')]])


@router.callback_query(Profile.budget, F.data.startswith('p:budget:'))
async def budget_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await choose_budget(callback.message, state, callback.data.rsplit(':', 1)[1])


@router.message(Profile.budget)
async def budget_text(message: Message, state: FSMContext) -> None:
    await choose_budget(message, state, message.text or '')


@router.callback_query(Profile.rooms, F.data.startswith('p:rooms:'))
async def rooms_button(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.rsplit(':', 1)[1]
    selected = (await state.get_data()).get('rooms', [])
    if value == 'done':
        if not selected:
            await callback.answer('Выберите хотя бы один вариант')
            return
        await callback.answer()
        await state.set_state(Profile.area)
        await callback.message.answer('3/4. Минимальная площадь? Можно ввести число в м².',
            reply_markup=keyboard([[('Не важно', 'p:area:0'), ('40+', 'p:area:40'), ('50+', 'p:area:50')],
                                   [('60+', 'p:area:60'), ('70+', 'p:area:70')]]))
        return
    if value not in ('1', '2', '3', '4'):
        await callback.answer()
        return
    n = int(value)
    selected = [r for r in selected if r != n] if n in selected else selected + [n]
    await state.update_data(rooms=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=rooms_keyboard(selected))


async def choose_area(message: Message, state: FSMContext, value: str) -> None:
    try:
        area = float(value.replace(',', '.'))
        if not 0 <= area <= 1000:
            raise ValueError
    except ValueError:
        await message.answer('Введите площадь от 0 до 1000 м². 0 — не важно.')
        return
    await state.update_data(area_min=area or None)
    await state.set_state(Profile.kind)
    await message.answer('4/4. Что рассматриваете?', reply_markup=keyboard([
        [('Вторичку', 'p:kind:secondary'), ('Первичку', 'p:kind:new'), ('Всё', 'p:kind:all')]]))


@router.callback_query(Profile.area, F.data.startswith('p:area:'))
async def area_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await choose_area(callback.message, state, callback.data.rsplit(':', 1)[1])


@router.message(Profile.area)
async def area_text(message: Message, state: FSMContext) -> None:
    await choose_area(message, state, message.text or '')


@router.callback_query(Profile.kind, F.data.startswith('p:kind:'))
async def kind_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    kind = callback.data.rsplit(':', 1)[1]
    if kind not in ('secondary', 'new', 'all'):
        return
    data = await state.get_data()
    data['property_type'] = None if kind == 'all' else kind
    try:
        await save_profile(callback.from_user.id, data)
    except Exception:
        log.exception('Buyer profile save failed')
        await callback.message.answer('Не удалось сохранить профиль. Нажмите выбранный вариант ещё раз.')
        return
    await state.clear()
    for key in list(_cache):
        if key[0] == callback.from_user.id:
            del _cache[key]
    await callback.message.answer('✅ Понял. Теперь буду оценивать квартиры именно под вас. Пришлите ссылку.')


@router.callback_query(F.data.startswith('p:'))
async def stale_profile(callback: CallbackQuery) -> None:
    await callback.answer('Эта кнопка устарела. Нажмите «Подбирать под меня» заново.')


@router.message()
async def help_message(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer('Выберите ответ кнопкой или /cancel для отмены. Ссылку на квартиру можно прислать в любой момент.')
    else:
        await message.answer('Пришлите ссылку https://krisha.kz/a/show/…')
