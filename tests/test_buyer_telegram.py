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


def message(text='https://krisha.kz/a/show/123456'):
    return SimpleNamespace(text=text, caption=None, entities=None, caption_entities=None,
        from_user=SimpleNamespace(id=42, username='test'), answer=AsyncMock(), edit_reply_markup=AsyncMock())


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
    assert buttons == ['🔥 Показать лучше', '⭐ Сохранить', '⚠️ Что проверить', '🎯 Подбирать под меня']


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
async def test_four_step_profile_multiple_rooms_and_manual_inputs():
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
        assert await ctx.get_state() is None
    save.assert_awaited_once_with(42, dict(budget_max=32_500_000, rooms=[2, 4], area_min=55, property_type='new'))
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
