from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update, User, Chat

from bot.buyer import telegram as ui
from bot.core.buyer import summarize
from tests.test_buyer import listing


@pytest.fixture(autouse=True)
def map_disabled_unless_requested(monkeypatch):
    monkeypatch.delenv('BUYER_MAP_URL', raising=False)


def message(text='https://krisha.kz/a/show/123456'):
    return SimpleNamespace(text=text, caption=None, entities=None, caption_entities=None,
        from_user=SimpleNamespace(id=42, username='test'), answer=AsyncMock(), answer_photo=AsyncMock(), edit_reply_markup=AsyncMock(), answer_location=AsyncMock(), location=None)


def callback(data, msg):
    return SimpleNamespace(data=data, message=msg, from_user=msg.from_user, answer=AsyncMock())


def state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=42, user_id=42))


@pytest.mark.asyncio
async def test_start_has_no_onboarding():
    msg, ctx = message('/start'), state()
    with patch.object(ui, 'ensure_user', AsyncMock()) as ensure:
        await ui.start(msg, ctx)
    assert await ctx.get_state() is None
    assert 'Пришли ссылку' in msg.answer.await_args.args[0]
    ensure.assert_awaited_once_with(42, 'test')


@pytest.mark.asyncio
async def test_link_handler_short_answer_and_buttons():
    msg = message()
    result = dict(summarize(listing()), found=True)
    with patch.object(ui, 'ensure_user', AsyncMock()), patch.object(ui, 'analyze_for_buyer', AsyncMock(return_value=result)) as analyze:
        await ui.link(msg, state())
    analyze.assert_awaited_once_with(msg.text, 42)
    answer = msg.answer.await_args
    assert len(answer.args[0]) < 1200
    buttons = [b.text for row in answer.kwargs['reply_markup'].inline_keyboard for b in row]
    assert buttons == ['📋 Сравнить списком', '⭐ Сохранить', '⚠️ Что проверить', '🎯 Подбирать под меня']


@pytest.mark.asyncio
async def test_unknown_link_and_service_failure():
    msg = message()
    with patch.object(ui, 'ensure_user', AsyncMock()), patch.object(ui, 'analyze_for_buyer', AsyncMock(return_value={'found': False, 'message': 'Не удалось загрузить'})):
        await ui.link(msg, state())
    assert msg.answer.await_args.args[0] == 'Не удалось загрузить'
    with patch.object(ui, 'ensure_user', AsyncMock()), patch.object(ui, 'analyze_for_buyer', AsyncMock(side_effect=RuntimeError)):
        await ui.link(msg, state())
    assert 'Не удалось завершить' in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_five_step_profile_multiple_rooms_and_manual_inputs():
    msg, ctx = message(), state()
    with patch.object(ui, 'save_profile', AsyncMock()) as save:
        await ui.profile_start(callback('p:start', msg), ctx)
        assert await ctx.get_state() == ui.Profile.budget.state
        msg.text = '32,5'
        await ui.budget_text(msg, ctx)
        assert await ctx.get_state() == ui.Profile.rooms.state
        await ui.rooms_button(callback('p:rooms:2', msg), ctx)
        await ui.rooms_button(callback('p:rooms:4', msg), ctx)
        await ui.rooms_button(callback('p:rooms:done', msg), ctx)
        assert await ctx.get_state() == ui.Profile.area.state
        msg.text = '55'
        await ui.area_text(msg, ctx)
        assert await ctx.get_state() == ui.Profile.kind.state
        await ui.kind_button(callback('p:kind:new', msg), ctx)
        assert await ctx.get_state() == ui.Profile.location.state
        save.assert_not_called()
        await ui.location_mode(callback('p:where:pin', msg), ctx)
        msg.location = SimpleNamespace(latitude=51.13, longitude=71.43)
        await ui.location_pin(msg, ctx)
        assert await ctx.get_state() == ui.Profile.radius.state
        msg.answer_location.assert_awaited_once_with(latitude=51.13, longitude=71.43)
        save.assert_not_called()
        await ui.radius_button(callback('p:radius:2', msg), ctx)
        assert await ctx.get_state() is None
    save.assert_awaited_once()
    saved = save.await_args.args[1]
    assert {k: saved[k] for k in ('budget_max', 'rooms', 'area_min', 'property_type', 'location_lat', 'location_lon', 'radius_km')} == dict(
        budget_max=32_500_000, rooms=[2, 4], area_min=55, property_type='new', location_lat=51.13, location_lon=71.43, radius_km=2)
    assert 'именно под вас' in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_invalid_inputs_and_cancel_preserve_profile():
    msg, ctx = message(), state()
    with patch.object(ui, 'save_profile', AsyncMock()) as save:
        await ui.profile_start(callback('p:start', msg), ctx)
        for text in ('abc', '-10', 'nan', 'inf', '2001'):
            await ui.choose_budget(msg, ctx, text)
            assert await ctx.get_state() == ui.Profile.budget.state
        await ui.choose_budget(msg, ctx, '30')
        cb = callback('p:rooms:done', msg)
        await ui.rooms_button(cb, ctx)
        assert await ctx.get_state() == ui.Profile.rooms.state
        await ui.cancel(msg, ctx)
    save.assert_not_called()
    assert await ctx.get_state() is None


@pytest.mark.asyncio
async def test_save_callback_uses_core_and_acknowledges():
    msg = message()
    cb = callback('b:save:123456', msg)
    with patch.object(ui, 'save_favorite', AsyncMock(return_value=True)) as save:
        await ui.action(cb)
    cb.answer.assert_awaited_once()
    save.assert_awaited_once_with(42, '123456')
    assert 'Сохранено' in msg.answer.await_args.args[0]


def test_cache_is_user_scoped_and_expires():
    ui._cache.clear()
    r = dict(summarize(listing()), found=True)
    ui.remember(42, r)
    assert ui.recalled(43, '123456') is None
    assert ui.recalled(42, '123456') is r
    with patch.object(ui.time, 'monotonic', return_value=10**12):
        assert ui.recalled(42, '123456') is None


def test_caption_and_hidden_link():
    msg = message(None)
    msg.caption = 'Смотри https://krisha.kz/a/show/123456'
    assert ui.message_url(msg) == 'https://krisha.kz/a/show/123456'
    msg.caption = None
    msg.entities = [SimpleNamespace(type='text_link', url='https://krisha.kz/a/show/123456')]
    assert ui.message_url(msg) == 'https://krisha.kz/a/show/123456'


def test_render_escapes_listing_text_and_tradeoffs():
    r = summarize(listing())
    r['positives'] = ['<script>']
    assert '&lt;script&gt;' in ui.render_summary(r)
    assert '<script>' not in ui.render_summary(r)


@pytest.mark.asyncio
async def test_dispatcher_routes_url_during_profile_and_ignores_group():
    # Feed actual aiogram Updates to verify router ordering and private scope.
    dp = Dispatcher()
    dp.include_router(ui.router)
    bot = Bot('123456:ABC_test')
    response = Message(message_id=2, date=datetime.now(timezone.utc), chat=Chat(id=42, type='private'))
    bot.session = AsyncMock(return_value=response)
    ctx = dp.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    await ctx.set_state(ui.Profile.budget)
    with patch.object(ui, 'ensure_user', AsyncMock()), patch.object(ui, 'analyze_for_buyer', AsyncMock(return_value=dict(summarize(listing()), found=True))) as analyze:
        for i, kind in enumerate(('private', 'group')):
            msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=42, type=kind),
                from_user=User(id=42, is_bot=False, first_name='Test'), text='https://krisha.kz/a/show/123456')
            await dp.feed_update(bot, Update(update_id=i, message=msg))
        analyze.assert_awaited_once()
        assert await ctx.get_state() == ui.Profile.budget.state
    # Router singleton must not retain a parent after this test.
    dp.sub_routers.remove(ui.router)
    ui.router._parent_router = None
    await dp.storage.close()


@pytest.mark.asyncio
async def test_one_room_area_has_thirty_metres():
    msg, ctx = message(), state()
    await ctx.update_data(rooms=[1])
    await ui.rooms_button(callback('p:rooms:done', msg), ctx)
    buttons = [b for row in msg.answer.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
    assert any(b.text == '30+' and b.callback_data == 'p:area:30' for b in buttons)
    await ui.area_button(callback('p:area:30', msg), ctx)
    assert (await ctx.get_data())['area_min'] == 30


@pytest.mark.asyncio
async def test_location_search_requires_choice_and_confirmation():
    msg, ctx = message('Highvill'), state()
    await ctx.set_state(ui.Profile.location)
    await ui.location_mode(callback('p:where:complex', msg), ctx)
    choices = [{'label': 'ЖК Highvill', 'lat': 51.13, 'lon': 71.43}]
    with patch.object(ui, 'search_locations', AsyncMock(return_value=choices)) as search, \
         patch.object(ui, 'save_profile', AsyncMock()) as save:
        await ui.location_text(msg, ctx)
        search.assert_awaited_once_with('Highvill', 'complex')
        assert await ctx.get_state() == ui.Profile.location_query.state
        data = await ctx.get_data()
        stale = callback('p:loc:stale:0', msg)
        await ui.location_choice(stale, ctx)
        msg.answer_location.assert_not_called()
        await ui.location_choice(callback(f"p:loc:{data['location_nonce']}:0", msg), ctx)
        assert await ctx.get_state() == ui.Profile.radius.state
        msg.answer_location.assert_awaited_once()
        save.assert_not_called()


@pytest.mark.asyncio
async def test_geo_outside_astana_and_empty_search_not_saved():
    msg, ctx = message('неизвестный адрес'), state()
    await ctx.update_data(location_kind='address')
    await ctx.set_state(ui.Profile.location_query)
    with patch.object(ui, 'search_locations', AsyncMock(return_value=[])), patch.object(ui, 'save_profile', AsyncMock()) as save:
        await ui.location_text(msg, ctx)
        assert 'Не удалось найти' in msg.answer.await_args.args[0]
        msg.location = SimpleNamespace(latitude=43.2, longitude=76.9)
        await ui.location_pin(msg, ctx)
        assert 'Астане' in msg.answer.await_args.args[0]
        assert await ctx.get_state() == ui.Profile.location_query.state
        save.assert_not_called()


def test_summary_replaces_price_history_with_negotiation():
    result = summarize(listing(price_history={'events': [
        {'at': '06.10.2026', 'old_price': 32_000_000, 'new_price': 31_990_000}]}))
    result['negatives'] = ['Один', 'Два', 'Три']
    text = ui.render_summary(result)
    assert '06.10.2026' not in text and '→' not in text and 'Всего изменений' not in text
    assert '💰' not in text and '🤝 Торг:' in text
    assert 'на 1 000 000 ₸ ниже' in text
    buttons = [b.text for row in ui.actions('123456', True).inline_keyboard for b in row]
    assert '📉 История цены' in buttons


def test_price_history_pagination_keeps_every_event():
    result = summarize(listing(price_history={'events': [
        {'at': f'{i+1:02}.10.2026', 'old_price': 32_000_000 - i*100_000, 'new_price': 31_900_000 - i*100_000}
        for i in range(19)]}))
    pages = [ui.render_history(result, page)[0] for page in range(3)]
    assert all(p.count('→') <= 8 for p in pages)
    assert sum(p.count('→') for p in pages) == 19
    assert '19.10.2026' in pages[0] and '01.10.2026' in pages[-1]
    assert '1/3' in pages[0] and '3/3' in pages[-1]


def test_history_failure_is_not_reported_as_no_changes():
    result = summarize(listing(price_history={'available': False}))
    assert result['price_history']['available'] is False
    assert 'История цены' not in ui.render_summary(result)


@pytest.mark.asyncio
async def test_map_button_carries_user_draft_and_completion_clears_cache(monkeypatch):
    monkeypatch.setenv('BUYER_MAP_URL', 'https://example.test/buyer/map')
    msg, ctx = message(), state()
    await ctx.update_data(budget_max=25000000, rooms=[1], area_min=30, property_type=None)
    with patch.object(ui.buyer_map, 'create_session', AsyncMock(return_value='safe_nonce')) as create:
        await ui.ask_location(msg, ctx)
    assert create.await_args.args[0] == 42
    button=msg.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.web_app.url=='https://example.test/buyer/map?nonce=safe_nonce'
    assert create.await_args.args[1]['area_min']==30
    ui.remember(42,dict(summarize(listing()),found=True))
    handler=AsyncMock()
    data={'state':ctx,'raw_state':ui.Profile.location.state}
    with patch.object(ui.buyer_map,'session_completed',AsyncMock(return_value=True)):
        await ui.MapCompletionMiddleware()(handler,msg,data)
    assert await ctx.get_state() is None and data['raw_state'] is None
    assert not any(key[0]==42 for key in ui._cache)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_price_sends_chart_and_quiet_summary():
    msg=message()
    result=dict(summarize(listing(bargain={}, price_history={'events':[
        {'at':'07.10.2026','old_price':35000000,'new_price':33000000}]}),
        {'budget_max':32000000}),found=True)
    text=ui.render_summary(result)
    assert 'бюджет' not in text and 'ненадёжна' not in text and 'Срочность неизвестна' not in text
    assert 'Посмотреть подробнее</a>' in text
    with patch.object(ui,'ensure_user',AsyncMock()), patch.object(ui,'analyze_for_buyer',AsyncMock(return_value=result)):
        await ui.link(msg,state())
    msg.answer_photo.assert_awaited_once()
    assert msg.answer_photo.await_args.args[0].data.startswith(b'\x89PNG')


def test_large_budget_gap_is_separate_from_negatives():
    result=summarize(listing(price=40000000),{'budget_max':30000000})
    assert not any('бюджет' in n for n in result['negatives'])
    assert 'обсудить снижение на 10.0 млн' in ui.render_summary(result)


def test_negotiation_does_not_invent_discount_without_target():
    text=' '.join(ui.negotiation_lines({'reliable':False,'asking':26300000}))
    assert 'Какую скидку' in text and '₸' not in text
    assert 'Сошлитесь' in ' '.join(ui.negotiation_lines({'reliable':True,'asking':35000000,'offer':32000000,'fair':33000000}))


@pytest.mark.asyncio
async def test_two_similar_cards_sent_automatically():
    from bot.core.buyer import recommend_nearby
    base=listing()
    result=dict(summarize(base),found=True)
    result['better_nearby']=recommend_nearby(base,[listing(id='123457'),listing(id='123458',lat=51.14)],{})
    msg=message()
    with patch.object(ui,'ensure_user',AsyncMock()),patch.object(ui,'analyze_for_buyer',AsyncMock(return_value=result)):
        await ui.link(msg,state())
    cards=[call.args[0] for call in msg.answer.await_args_list if 'Похожий вариант' in call.args[0]]
    assert len(cards)==2 and all('Почему лучше' not in text for text in cards)


@pytest.mark.asyncio
async def test_nearby_map_replaces_automatic_cards_when_available(monkeypatch):
    from bot.core.buyer import recommend_nearby
    monkeypatch.setenv('BUYER_MAP_URL','https://example.test/buyer/map')
    base=listing();result=dict(summarize(base),found=True)
    result['better_nearby']=recommend_nearby(base,[listing(id='123457'),listing(id='123458',lat=51.14)],{})
    msg=message()
    with patch.object(ui,'ensure_user',AsyncMock()),patch.object(ui,'analyze_for_buyer',AsyncMock(return_value=result)):
        await ui.link(msg,state())
    assert msg.answer.await_count==2
    markup=msg.answer.await_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].web_app.url=='https://example.test/buyer/map/nearby?ids=123456,123457,123458'
    assert msg.answer.await_args_list[0].kwargs['reply_markup'].is_persistent


@pytest.mark.asyncio
async def test_menu_explains_next_step_and_works_during_onboarding():
    msg,ctx=message('🔎 Проверить квартиру'),state()
    await ctx.set_state(ui.Profile.budget)
    await ui.menu_choice(msg,ctx)
    assert await ctx.get_state() is None
    assert 'Пришлите ссылку' in msg.answer.await_args.args[0]
    assert len(msg.answer.await_args.kwargs['reply_markup'].keyboard)==3
    ui._cache.clear()
    await ui.nearby_command(msg,ctx)
    assert 'Сначала пришлите ссылку' in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_favorites_uses_only_current_user_and_escapes_labels():
    msg=message()
    with patch.object(ui,'list_favorites',AsyncMock(return_value=[{
        'listing_id':'123456','price':25000000,'rooms':1,'area':'<bad>','is_active':False}])) as saved:
        await ui.favorites_command(msg,state())
    saved.assert_awaited_once_with(42)
    assert '&lt;bad&gt;' in msg.answer.await_args.args[0]
    assert 'снято с публикации' in msg.answer.await_args.args[0]
