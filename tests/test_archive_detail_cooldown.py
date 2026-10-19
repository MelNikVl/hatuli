import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.core import archive_check as archive, apartment_details as details


@pytest.fixture(autouse=True)
def isolated_detail_pause(monkeypatch, tmp_path):
    path = tmp_path / 'detail-cooldown.json'
    monkeypatch.setenv('HATULI_DETAIL_COOLDOWN_PATH', str(path))
    monkeypatch.setattr(details, '_BLOCKED_UNTIL', 0.0)
    monkeypatch.setattr(details, '_BLOCKED_STREAK', 0)
    monkeypatch.setitem(archive.REQUEST_COUNTS, 'archive_check', 0)
    return path


@pytest.mark.asyncio
async def test_existing_durable_detail_pause_prevents_archive_request(isolated_detail_pause):
    from bot.core.detail_cooldown import record_block
    record_block()
    client = AsyncMock()
    with pytest.raises(RuntimeError, match='blocked'):
        await archive._check_one(client, 'https://krisha.kz/a/show/123456789/')
    assert not client.get.called and archive.REQUEST_COUNTS['archive_check'] == 0


@pytest.mark.asyncio
async def test_unreadable_detail_pause_fails_closed_without_requests(isolated_detail_pause):
    isolated_detail_pause.write_text('not json', encoding='utf-8')
    client = AsyncMock()
    with pytest.raises(RuntimeError, match='blocked'):
        await archive._check_one(client, 'https://krisha.kz/a/show/123456789/')
    assert not client.get.called and archive.REQUEST_COUNTS['archive_check'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status,html', [(403, 'denied'), (429, 'limited'), (468, 'blocked'),
                                       (200, '<h1>Подтвердите, что вы не робот</h1>'),
                                       (200, '<html>captcha</html>')])
async def test_source_block_stops_archive_and_persists_shared_detail_pause(status, html, isolated_detail_pause):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=html))) as client:
        with pytest.raises(RuntimeError, match='blocked'):
            await archive._check_one(client, 'https://krisha.kz/a/show/123456789/')
    state = json.loads(isolated_detail_pause.read_text(encoding='utf-8'))
    assert state['streak'] >= 1 and details.detail_fetch_cooldown_remaining() > 0
    assert archive.REQUEST_COUNTS['archive_check'] == 1
    assert 'krisha' not in isolated_detail_pause.read_text(encoding='utf-8')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,html,expected', [(404, '', 'deleted'), (410, '', 'deleted'),
                                                (200, 'В архиве', 'archived'),
                                                (200, '<div class="offer__info">Объявление</div>', 'alive'),
                                                (500, 'failed', None)])
async def test_normal_archive_outcomes_and_request_budget_are_preserved(status, html, expected, isolated_detail_pause):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=html))) as client:
        assert await archive._check_one(client, 'https://krisha.kz/a/show/123456789/') == expected
    assert archive.REQUEST_COUNTS['archive_check'] == 1
    assert not isolated_detail_pause.exists()


@pytest.mark.asyncio
async def test_timeout_counts_an_actual_started_archive_attempt(isolated_detail_pause):
    client = AsyncMock()
    client.get.side_effect = httpx.ReadTimeout('timeout')
    assert await archive._check_one(client, 'https://krisha.kz/a/show/123456789/') is None
    assert archive.REQUEST_COUNTS['archive_check'] == 1
    assert not isolated_detail_pause.exists()


@pytest.mark.asyncio
async def test_cancelled_http_attempt_is_counted_and_cancellation_survives(isolated_detail_pause):
    client = AsyncMock()
    client.get.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await archive._check_one(client, 'https://krisha.kz/a/show/123456789/')
    assert archive.REQUEST_COUNTS['archive_check'] == 1


@pytest.mark.asyncio
async def test_normal_offer_captcha_widget_is_not_a_source_challenge(isolated_detail_pause):
    html = '<div class="offer__info">Квартира</div><script src="captcha.js"></script>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as client:
        assert await archive._check_one(client, 'https://krisha.kz/a/show/123456789/') == 'alive'
    assert not isolated_detail_pause.exists()
