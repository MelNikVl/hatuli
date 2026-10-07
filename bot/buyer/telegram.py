"""Thin aiogram UI. All analytics and PostgreSQL writes live in core."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from html import escape
import logging
import os
import secrets
import time

from aiogram import F, Router, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BufferedInputFile

from bot.core.buyer import analyze_for_buyer, extract_krisha_url, money
from bot.core.buyer_store import ensure_user, save_favorite, save_profile
from bot.core.buyer_locations import search_locations
from bot.core.geo import in_astana_bbox
from bot.core import buyer_map

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
_cache: OrderedDict = OrderedDict()
_analysis_slots = asyncio.Semaphore(4)


class MapCompletionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        state = data.get('state')
        if state:
            nonce = (await state.get_data()).get('map_nonce')
            if nonce and await buyer_map.session_completed(event.from_user.id, nonce):
                await state.clear()
                data['raw_state'] = None
                for key in list(_cache):
                    if key[0] == event.from_user.id:
                        _cache.pop(key, None)
                message = event.message if isinstance(event, CallbackQuery) else event
                await message.answer('✅ Места сохранены. Учту выбранные участки при оценке квартир.')
        return await handler(event, data)


router.message.outer_middleware(MapCompletionMiddleware())
router.callback_query.outer_middleware(MapCompletionMiddleware())


async def reset_profile_state(state):
    nonce = (await state.get_data()).get('map_nonce')
    if nonce:
        await buyer_map.cancel_session(state.key.user_id, nonce)
    await state.clear()


async def map_markup(state, profile=None):
    url = os.getenv('BUYER_MAP_URL', '').rstrip('/')
    if not url.startswith('https://'):
        return None
    data = await state.get_data()
    nonce = await buyer_map.create_session(state.key.user_id, profile, data.get('listing_anchor'))
    await state.update_data(map_nonce=nonce)
    return InlineKeyboardButton(text='🗺 Выбрать участки на карте', web_app=WebAppInfo(url=url+'?nonce='+nonce))

START = ('👋 Пришли ссылку на квартиру с Krisha.kz.\n\n'
         'Я быстро скажу:\n• что в ней хорошо;\n• что настораживает;\n'
         '• нормальная ли цена;\n• стоит ли ехать смотреть;\n'
         '• есть ли рядом варианты лучше.\n\nПока — покупка квартир в Астане.')


class Profile(StatesGroup):
    budget = State()
    rooms = State()
    area = State()
    kind = State()
    location = State()
    location_query = State()
    radius = State()


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


def actions(lid: str, has_history: bool = False) -> InlineKeyboardMarkup:
    rows = [[('🔥 Показать лучше', f'b:better:{lid}'), ('⭐ Сохранить', f'b:save:{lid}')],
            [('⚠️ Что проверить', f'b:check:{lid}')]]
    if has_history:
        rows.append([('📉 История цены', f'b:history_0:{lid}')])
    rows.append([('🎯 Подбирать под меня', 'p:start')])
    return keyboard(rows)


def exact_money(value: float) -> str:
    return f'{value:,.0f} ₸'.replace(',', ' ')


def price_event_text(event: dict) -> str:
    delta = event['new_price'] - event['old_price']
    sign = '−' if delta < 0 else '+'
    return (f"{event['at']}: {exact_money(event['old_price'])} → {exact_money(event['new_price'])} "
            f"({sign}{exact_money(abs(delta))})")


def price_history_lines(history: dict) -> list[str]:
    if history.get('available') is False:
        return ['История цены временно недоступна.']
    events = history.get('events') or []
    if not events:
        return []
    arrow = '📉' if events[-1]['new_price'] < events[-1]['old_price'] else '📈'
    lines = [arrow + ' ' + price_event_text(events[-1])]
    if len(events) > 1:
        net = history['net_change']
        signed = ('−' if net < 0 else '+') + exact_money(abs(net)) if net else 'без итогового изменения'
        lines.append(f"Всего изменений: {len(events)}; за записанную историю: {signed}.")
    return lines


def render_history(result: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    events = list(reversed(result.get('price_history', {}).get('events', [])))
    pages = max(1, (len(events) + 7) // 8)
    page = max(0, min(page, pages - 1))
    lines = [f'История цены · {page+1}/{pages}', 'Последние изменения сверху.']
    lines += [price_event_text(e) for e in events[page*8:(page+1)*8]]
    lid = str(result['listing']['id'])
    buttons = []
    if page:
        buttons.append(('← Новее', f'b:history_{page-1}:{lid}'))
    if page + 1 < pages:
        buttons.append(('Старее →', f'b:history_{page+1}:{lid}'))
    return '\n'.join(lines), keyboard([buttons] if buttons else [])


def negotiation_lines(price: dict) -> list[str]:
    asking, offer, fair = (price.get(k) for k in ('asking', 'offer', 'fair'))
    if price.get('reliable') and asking and offer and 0 < offer < asking:
        argument = ('Сошлитесь на цены похожих квартир и назовите конкретную сумму.'
                    if fair and asking > fair else
                    'Предложите эту сумму после просмотра; замеченные недостатки обсудите отдельно.')
        return [f"🤝 Торг: начните с ~{money(offer)} — на {exact_money(asking-offer)} ниже цены продавца.", argument]
    return ['🤝 Торг: после просмотра спросите «Какую скидку готовы обсудить?» '
            'Если найдёте недостатки, подкрепите предложение сметой их устранения.']


def render_summary(result: dict) -> str:
    score = f" · {result['score']/10:.1f}/10" if result['score'] is not None else ''
    lines = [f"<b>{escape(result['verdict']['label'])}{score}</b>"]
    for label, sign, key in [('Почему да', '+', 'positives'), ('Что смущает', '−', 'negatives')]:
        if result[key]:
            lines += ['', f'<b>{label}</b>'] + [f'{sign} {escape(t)}' for t in result[key][:3]]
    if not result['positives'] and not result['negatives']:
        lines += [escape(t) for t in result['verdict']['reasons'][:2]]
    lines += [''] + negotiation_lines(result['price_summary'])
    if result.get('location_unverified'):
        lines.append('Нет координат — соответствие вашей локации не проверено.')
    if result['urgency']['level'] != 'unknown':
        lines += ['', escape(result['urgency']['text'])]
    if result.get('budget_gap'):
        lines += ['', f"Чтобы уложиться в ваш бюджет, нужно обсудить снижение на {money(result['budget_gap'])}."]
    n = len(result['better_nearby'])
    lines += ['', f'Вариантов рядом с преимуществами: {n}.' if n else 'Убедительно лучших вариантов в проверенной выборке не нашёл.']
    if result['confidence'] == 'limited':
        lines.append('Вывод предварительный: данные неполные.')
    lid = str(result['listing']['id'])
    if lid.isdigit():
        lines += ['', f'<a href="https://hatuli.ai-groundtruth.com/listing/{lid}">Посмотреть подробнее</a>']
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
    from bot.core.buyer import summarize_price_history
    lines.extend(price_history_lines(summarize_price_history(d.get('price_history') or {})))
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
    await reset_profile_state(state)
    await ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(START)


@router.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext) -> None:
    await reset_profile_state(state)
    await message.answer('Настройка отменена. Пришлите ссылку на квартиру.')


@router.message(Command('map'))
async def map_command(message: Message, state: FSMContext) -> None:
    await reset_profile_state(state)
    button = await map_markup(state)
    if not button:
        await message.answer('Карта пока недоступна. Выберите место через /profile.')
        return
    await message.answer('Отметьте участки, где хотите жить, и нажмите «Сохранить места». '
                         'Можно выбрать несколько отдельных зон.',
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))


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
        await message.answer(render_summary(result), parse_mode='HTML', disable_web_page_preview=True,
                             reply_markup=actions(str(result['listing']['id']), bool(result.get('price_history', {}).get('changes'))))
        if result.get('price_history', {}).get('changes'):
            try:
                from bot.core.buyer_price_chart import render_price_chart
                chart = await asyncio.to_thread(render_price_chart, result['price_history'])
                if chart:
                    await message.answer_photo(BufferedInputFile(chart, filename='price-history.png'),
                        caption='История цены объявления · только зафиксированные изменения')
            except Exception:
                log.exception('Buyer price chart unavailable')

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
        if command.startswith('history_'):
            page = command.removeprefix('history_')
            if page.isdigit():
                text, markup = render_history(result, int(page))
                await callback.message.answer(text, reply_markup=markup)
        elif command == 'check':
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


async def begin_profile(message: Message, state: FSMContext, uid: int) -> None:
    await reset_profile_state(state)
    for (user, lid), (created, result) in reversed(_cache.items()):
        d = result['listing']
        if user == uid and time.monotonic() - created < 600 and in_astana_bbox(d.get('lat'), d.get('lon')):
            await state.update_data(listing_anchor={'label': 'Рядом с присланной квартирой', 'lat': d['lat'], 'lon': d['lon']})
            break
    await state.set_state(Profile.budget)
    await message.answer('1/5. Максимальный бюджет? Можно ввести сумму в млн ₸. /cancel — отмена.',
        reply_markup=keyboard([[(f'до {n} млн', f'p:budget:{n}') for n in (25, 30)],
                               [(f'до {n} млн', f'p:budget:{n}') for n in (35, 40)]]))


@router.callback_query(F.data == 'p:start')
async def profile_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await begin_profile(callback.message, state, callback.from_user.id)


@router.message(Command('profile'))
async def profile_command(message: Message, state: FSMContext) -> None:
    await begin_profile(message, state, message.from_user.id)


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
    await message.answer('2/5. Сколько комнат? Выберите несколько, затем «Готово».', reply_markup=rooms_keyboard([]))


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
        await callback.message.answer('3/5. Минимальная площадь? Можно ввести число в м².',
            reply_markup=keyboard([[('Не важно', 'p:area:0'), ('30+', 'p:area:30'), ('40+', 'p:area:40')],
                                   [('50+', 'p:area:50'), ('60+', 'p:area:60'), ('70+', 'p:area:70')]]))
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
    await message.answer('4/5. Что рассматриваете?', reply_markup=keyboard([
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
    await state.update_data(property_type=None if kind == 'all' else kind)
    await ask_location(callback.message, state)


async def ask_location(message: Message, state: FSMContext) -> None:
    await state.set_state(Profile.location)
    rows = [[('🏢 Название ЖК', 'p:where:complex'), ('📍 Улица / адрес', 'p:where:address')],
            [('🗺 Точка на карте', 'p:where:pin')]]
    if (await state.get_data()).get('listing_anchor'):
        rows.append([('Рядом с присланной квартирой', 'p:where:listing')])
    markup = keyboard(rows)
    button = await map_markup(state, await state.get_data())
    if button:
        markup.inline_keyboard.insert(0, [button])
    await message.answer('5/5. Где хотите жить? На карте отметьте удобные участки — можно несколько отдельных мест. '
                         'Поиск ЖК и адреса поможет найти нужную точку. Нажмите «Сохранить места» на карте. '
                         'Либо выберите точку и радиус кнопками ниже.', reply_markup=markup)



@router.callback_query(Profile.location, F.data.startswith('p:where:'))
async def location_mode(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    kind = callback.data.rsplit(':', 1)[1]
    nonce = (await state.get_data()).get('map_nonce')
    if nonce:
        await buyer_map.cancel_session(callback.from_user.id, nonce)
        await state.update_data(map_nonce=None)
    if kind == 'listing':
        point = (await state.get_data()).get('listing_anchor')
        if point:
            await preview_point(callback.message, state, point)
        return
    if kind not in ('complex', 'address', 'pin'):
        return
    await state.update_data(location_kind=kind, location_choices=[])
    await state.set_state(Profile.location_query)
    instructions = {
        'complex': 'Напишите название ЖК. Предложу варианты из справочника Clearly.',
        'address': 'Напишите улицу и номер дома или перекрёсток в Астане. '
                   'У длинной улицы важно выбрать конкретный участок. Найденную точку покажу на карте.',
        'pin': 'Нажмите скрепку → «Геопозиция» и выберите желаемое место на карте. '
               'Можно отправить любую точку, не обязательно ваше текущее местоположение.',
    }
    await callback.message.answer(instructions[kind])


async def preview_point(message: Message, state: FSMContext, point: dict) -> None:
    if not in_astana_bbox(point.get('lat'), point.get('lon')):
        await message.answer('Выберите точку в Астане.')
        return
    await state.update_data(location_lat=point['lat'], location_lon=point['lon'], location_label=point['label'])
    await state.set_state(Profile.radius)
    await message.answer_location(latitude=point['lat'], longitude=point['lon'])
    await message.answer(f"{point['label']}\nПроверьте точку на карте. Как далеко от неё рассматриваете квартиры? "
                         'Выбор радиуса подтвердит место и сохранит профиль.',
        reply_markup=keyboard([[('1 км', 'p:radius:1'), ('2 км', 'p:radius:2'), ('3 км', 'p:radius:3')],
                               [('Изменить место', 'p:relocate')]]))


@router.callback_query(Profile.radius, F.data == 'p:relocate')
async def relocate(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_location(callback.message, state)


@router.message(Profile.location, F.location)
@router.message(Profile.location_query, F.location)
async def location_pin(message: Message, state: FSMContext) -> None:
    await preview_point(message, state, {'label': 'Выбранная точка',
        'lat': message.location.latitude, 'lon': message.location.longitude})


@router.message(Profile.location_query)
async def location_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data.get('location_kind')
    if kind == 'pin':
        await message.answer('Пришлите геопозицию через скрепку → «Геопозиция» или /cancel для отмены.')
        return
    query = (message.text or '').strip()
    if not 2 <= len(query) <= 150:
        await message.answer('Введите название ЖК или адрес длиной от 2 до 150 символов.')
        return
    await state.update_data(location_choices=[])
    try:
        choices = await asyncio.wait_for(search_locations(query, kind), timeout=15)
    except Exception:
        log.exception('Buyer location search failed')
        choices = []
    if not choices:
        await message.answer('Не удалось найти точное место. Уточните название/адрес или пришлите точку на карте через скрепку → «Геопозиция».')
        return
    nonce = secrets.token_hex(3)
    await state.update_data(location_choices=choices, location_nonce=nonce)
    await message.answer('Выберите найденное место. Если ни одно не подходит — напишите другой запрос или пришлите точку.',
        reply_markup=keyboard([[(c['label'][:80], f'p:loc:{nonce}:{i}')] for i, c in enumerate(choices)]))


@router.callback_query(Profile.location_query, F.data.startswith('p:loc:'))
async def location_choice(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parts = callback.data.split(':')
    choices = data.get('location_choices') or []
    if len(parts) != 4 or parts[2] != data.get('location_nonce') or not parts[3].isdigit() or int(parts[3]) >= len(choices):
        await callback.answer('Результаты устарели. Повторите поиск.')
        return
    await callback.answer()
    await preview_point(callback.message, state, choices[int(parts[3])])


@router.callback_query(Profile.radius, F.data.startswith('p:radius:'))
async def radius_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    value = callback.data.rsplit(':', 1)[1]
    if value not in ('1', '2', '3'):
        return
    data = await state.get_data()
    data['radius_km'] = int(value)
    try:
        await save_profile(callback.from_user.id, data)
    except Exception:
        log.exception('Buyer profile save failed')
        await callback.message.answer('Не удалось сохранить профиль. Нажмите выбранный радиус ещё раз.')
        return
    await state.clear()
    for key in list(_cache):
        if key[0] == callback.from_user.id:
            del _cache[key]
    await callback.message.answer(f"✅ Понял. Теперь буду оценивать квартиры именно под вас.\n"
        f"Локация: {data.get('location_label', 'выбранная точка')}, радиус {value} км.\n"
        'Пришлите ссылку. Изменить параметры — /profile.')


@router.callback_query(F.data.startswith('p:'))
async def stale_profile(callback: CallbackQuery) -> None:
    await callback.answer('Эта кнопка устарела. Нажмите «Подбирать под меня» заново.')


@router.message()
async def help_message(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer('Выберите ответ кнопкой или /cancel для отмены. Ссылку на квартиру можно прислать в любой момент.')
    else:
        await message.answer('Пришлите ссылку https://krisha.kz/a/show/…')
