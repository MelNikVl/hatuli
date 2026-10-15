"""Private manual request queue, CSRF, bounded inputs and atomic duplicate/rate guards."""
import asyncio
import os
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from bot.core import analysis_requests as core
from bot.web import analysis_requests as web


@pytest.mark.parametrize('url,contact', [
    ('https://krisha.kz/a/show/12345678', '@Buyer_Name'),
    ('http://www.krisha.kz/a/show/12345678/?utm_source=test#top', 'buyer@example.kz'),
])
def test_normalizes_only_allowed_listing_links_and_contacts(url, contact):
    value = core.normalize_submission({'listing_url': url, 'contact': contact})
    assert value['listing_url'] == 'https://krisha.kz/a/show/12345678'
    assert value['contact_normalized'] == contact.lower()


@pytest.mark.parametrize('url,contact', [
    ('https://krisha.kz.evil.test/a/show/12345678', '@Buyer_Name'),
    ('https://krisha.kz@evil.test/a/show/12345678', '@Buyer_Name'),
    ('https://evil.test@krisha.kz/a/show/12345678', '@Buyer_Name'),
    ('https://krisha.kz:443/a/show/12345678', '@Buyer_Name'),
    ('https://127.0.0.1/a/show/12345678', '@Buyer_Name'),
    ('https://krisha.kz/a/show/1234', '@Buyer_Name'),
    ('https://krisha.kz/a/show/12345678', 'buyer@-example.kz'),
    ('https://krisha.kz/a/show/12345678', 'buyer@example..kz'),
    ('https://krisha.kz/a/show/12345678', '@abc'),
    ('https://krisha.kz/a/show/12345678', 'buyer@example.kz\n@other'),
])
def test_rejects_url_spoofing_or_invalid_contact(url, contact):
    with pytest.raises(core.RequestFieldsError):
        core.normalize_submission({'listing_url': url, 'contact': contact})


@pytest.mark.parametrize('scheme,host,header,value,allowed', [
    ('http', '192.168.0.15:8082', 'origin', 'http://192.168.0.15:8082', True),
    ('http', '192.168.0.15:8082', 'origin', 'http://192.168.0.15', False),
    ('https', 'hatuli.example', 'origin', 'https://hatuli.example:443', True),
    ('https', 'hatuli.example', 'referer', 'https://hatuli.example/request-analysis', True),
    ('https', 'hatuli.example', 'origin', 'http://hatuli.example', False),
    ('http', 'origin.internal:8082', 'origin', 'https://forwarded.example', False),
    ('https', 'hatuli.example', 'origin', 'https://user@hatuli.example', False),
    ('https', 'hatuli.example', 'origin', 'null', False),
])
def test_origin_uses_effective_scheme_and_port_without_trusting_forwarded_host(scheme, host, header, value, allowed):
    from fastapi import Request

    request = Request({'type': 'http', 'method': 'POST', 'path': '/request-analysis', 'root_path': '',
        'scheme': scheme, 'server': ('origin.internal', 8082),
        'headers': [(b'host', host.encode()), (header.encode(), value.encode()),
                    (b'x-forwarded-host', b'forwarded.example'), (b'x-forwarded-proto', b'https')]})
    assert web._same_origin(request) is allowed


@pytest_asyncio.fixture
async def queue_client(tmp_path):
    import httpx
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from jinja2 import ChoiceLoader, DictLoader
    from bot.db.pg import init_pool, close_pool, execute, fetchval
    from bot.core.admin_sessions import AdminSessionMiddleware
    from bot.core.auth_users import hash_password

    dsn = os.getenv('DATABASE_URL')
    if not dsn:
        pytest.skip('Set DATABASE_URL to an isolated test database')
    await init_pool(dsn)
    marker = uuid4().hex
    secret = 'analysis-fixture-' + marker
    client_host = 'analysis-client-' + marker
    client_key = core.fingerprint(secret, 'analysis-client', client_host)
    username = '__test_analysis_' + marker
    password_hash = hash_password('test-request-password')
    admin_id = await fetchval('INSERT INTO admin_users(username,password_hash) VALUES($1,$2) RETURNING id',
                              username, password_hash)
    app = FastAPI()
    app.state.analysis_form_secret = secret
    app.add_middleware(AdminSessionMiddleware)
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'bot' / 'templates'))
    # Exercise backend independently of the concurrently authored public page;
    # the private queue uses its real template and verified auth middleware.
    templates.env.loader = ChoiceLoader([DictLoader({'request_analysis.html': '''
        <input name="form_token" value="{{ form_token }}">
        <div id="accepted">{{ accepted }}</div><div id="reference">{{ reference or '' }}</div>
        {% for field,error in errors.items() %}<p>{{ field }}: {{ error }}</p>{% endfor %}
        <input name="contact" value="{{ values.contact }}">
    '''}), templates.env.loader])
    app.include_router(web.make_analysis_requests_router(templates))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(client_host, 1234)),
                                base_url='http://test', headers={'Origin': 'http://test'}) as client:
        try:
            yield {'client': client, 'secret': secret, 'client_key': client_key,
                   'admin_id': admin_id, 'username': username, 'password_hash': password_hash}
        finally:
            await execute('DELETE FROM analysis_requests WHERE client_key=$1 OR contact LIKE $2',
                          client_key, '%' + marker + '%')
            await execute('DELETE FROM admin_sessions WHERE user_id=$1', admin_id)
            await execute('DELETE FROM admin_users WHERE id=$1', admin_id)
    await close_pool()


async def _fields(ctx, listing_id='12345678', contact='@Buyer_Name', comment=''):
    response = await ctx['client'].get('/request-analysis')
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=lax' in response.headers['set-cookie']
    token = re.search(r'name="form_token" value="([^"]+)"', response.text)[1]
    return {'listing_url': 'https://krisha.kz/a/show/' + listing_id,
            'contact': contact, 'comment': comment, 'website': '', 'form_token': token}


async def _admin(ctx):
    from bot.core.admin_sessions import COOKIE_NAME, create_admin_session
    from bot.db.pg import fetchval
    token = await create_admin_session(ctx['admin_id'], password_hash=ctx['password_hash'])
    ctx['client'].cookies.set(COOKIE_NAME, token)
    return await fetchval('SELECT csrf_token FROM admin_sessions WHERE user_id=$1', ctx['admin_id'])


@pytest.mark.asyncio
async def test_html_form_persists_private_request_and_canonical_link(queue_client):
    from bot.db.pg import fetchrow
    fields = await _fields(queue_client, comment='<script>unsafe()</script>')
    fields['listing_url'] += '/?utm_source=test'
    response = await queue_client['client'].post('/request-analysis', data=fields)
    assert response.status_code == 202 and '<div id="accepted">True</div>' in response.text
    reference = re.search(r'<div id="reference">([^<]+)</div>', response.text)[1]
    row = await fetchrow('SELECT * FROM analysis_requests WHERE request_id=$1', UUID(reference))
    assert row['listing_url'] == 'https://krisha.kz/a/show/12345678'
    assert row['contact'] == '@Buyer_Name' and row['comment'] == '<script>unsafe()</script>'
    assert row['status'] == 'new' and row['private_note'] == '' and row['site_user_id'] is None
    assert row['client_key'] == queue_client['client_key']
    assert '@Buyer_Name' not in response.text  # acknowledgement clears submitted contact
    assert (await queue_client['client'].get('/api/analysis-requests')).status_code == 405


@pytest.mark.asyncio
async def test_json_concurrent_duplicates_return_one_reference(queue_client):
    from bot.db.pg import fetchval
    fields = await _fields(queue_client)
    responses = await asyncio.gather(*(queue_client['client'].post('/api/analysis-requests', json=fields) for _ in range(6)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()['reference'] for response in responses}) == 1
    assert await fetchval('SELECT count(*) FROM analysis_requests WHERE client_key=$1', queue_client['client_key']) == 1
    fields['contact'] = '@buyer_name'
    repeat = await queue_client['client'].post('/api/analysis-requests', json=fields)
    assert repeat.json()['reference'] == responses[0].json()['reference']


@pytest.mark.asyncio
async def test_admin_reopen_and_resubmit_share_contact_lock(queue_client):
    from bot.db.pg import get_pool, fetchval

    values = {'listing_url': 'https://krisha.kz/a/show/12345678', 'contact': '@Buyer_Name'}
    first = await core.save_request(values, client_key=queue_client['client_key'], secret=queue_client['secret'])
    original_id = UUID(first['reference'])
    assert await core.update_request(original_id, 'done', '', queue_client['username'], 1)
    contact_key = core.fingerprint(queue_client['secret'], 'analysis-contact', 'telegram:@buyer_name')
    lock_name = 'analysis-request:contact:' + contact_key
    waiting = 0
    tasks = []
    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                lock_id = await conn.fetchval('SELECT hashtextextended($1, 0)', lock_name)
                await conn.fetchval('SELECT pg_advisory_xact_lock($1)', lock_id)
                tasks = [
                    asyncio.create_task(core.update_request(original_id, 'new', '', queue_client['username'], 2)),
                    asyncio.create_task(core.save_request(values, client_key=queue_client['client_key'], secret=queue_client['secret'])),
                ]
                # Wait for both operations to reach the actual PostgreSQL lock,
                # rather than relying on a timing-dependent scheduling sleep.
                for _ in range(200):
                    waiting = await conn.fetchval('''SELECT count(*) FROM pg_locks
                        WHERE locktype='advisory' AND NOT granted
                          AND classid=(($1::bigint >> 32)&4294967295)::oid
                          AND objid=($1::bigint&4294967295)::oid''', lock_id)
                    if waiting == 2:
                        break
                    await asyncio.sleep(.01)
                assert await conn.fetchval('SELECT status FROM analysis_requests WHERE request_id=$1', original_id) == 'done'
    finally:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert waiting == 2
    reopened, saved = results
    assert reopened is True or isinstance(reopened, core.RequestConflict)
    assert isinstance(saved, dict)
    assert await fetchval('''SELECT count(*) FROM analysis_requests
        WHERE listing_id='12345678' AND contact_key=$1 AND status IN ('new','reviewing')''', contact_key) == 1
    assert await fetchval("SELECT status FROM analysis_requests WHERE request_id=$1", UUID(saved['reference'])) == 'new'


@pytest.mark.asyncio
async def test_client_rate_limit_applies_atomically_to_new_requests(queue_client):
    from bot.db.pg import fetchval
    fields = await _fields(queue_client)
    responses = await asyncio.gather(*(queue_client['client'].post('/api/analysis-requests',
        json={**fields, 'listing_url': 'https://krisha.kz/a/show/' + str(12345670 + i), 'contact': '@buyer_' + str(i)})
        for i in range(6)))
    assert sorted(response.status_code for response in responses) == [202, 202, 202, 429, 429, 429]
    assert await fetchval('SELECT count(*) FROM analysis_requests WHERE client_key=$1', queue_client['client_key']) == 3


@pytest.mark.asyncio
async def test_contact_limit_cannot_be_bypassed_with_different_clients(queue_client):
    marker = queue_client['username'].replace('__test_analysis_', '')
    values = {'contact': marker + '@example.kz'}
    for i in range(3):
        await core.save_request({**values, 'listing_url': 'https://krisha.kz/a/show/' + str(12345670 + i)},
            client_key=core.fingerprint(queue_client['secret'], 'other-client', str(i)), secret=queue_client['secret'])
    with pytest.raises(core.RequestRateLimit):
        await core.save_request({**values, 'listing_url': 'https://krisha.kz/a/show/12345679'},
            client_key=core.fingerprint(queue_client['secret'], 'other-client', 'new'), secret=queue_client['secret'])


@pytest.mark.asyncio
async def test_form_token_is_browser_bound_expiring_and_origin_checked(queue_client):
    from bot.db.pg import fetchval
    client = queue_client['client']
    fields = await _fields(queue_client)
    assert (await client.post('/api/analysis-requests', json=fields, headers={'Origin': 'https://other.test'})).status_code == 403
    assert (await client.post('/api/analysis-requests', json={**fields, 'form_token': ''})).status_code == 403
    nonce = client.cookies.get(web.COOKIE_NAME)
    expired = web._token(queue_client['secret'], nonce, int(time.time()) - web.TOKEN_TTL - 2)
    assert (await client.post('/api/analysis-requests', json={**fields, 'form_token': expired})).status_code == 403
    client.cookies.clear()
    client.cookies.set(web.COOKIE_NAME, '0' * 48)
    assert (await client.post('/api/analysis-requests', json=fields)).status_code == 403
    assert await fetchval('SELECT count(*) FROM analysis_requests WHERE client_key=$1', queue_client['client_key']) == 0


@pytest.mark.asyncio
async def test_honeypot_oversize_and_malformed_fields_do_not_enter_queue(queue_client):
    from bot.db.pg import fetchval
    client = queue_client['client']
    fields = await _fields(queue_client)
    response = await client.post('/api/analysis-requests', json={**fields, 'website': 'spam.test'})
    assert response.status_code == 202 and response.json()['accepted']
    assert (await client.post('/api/analysis-requests', json={**fields, 'comment': 'x' * 9000})).status_code == 422
    assert (await client.post('/api/analysis-requests', json={**fields, 'contact': ['invalid']})).status_code == 422
    assert (await client.post('/api/analysis-requests', json={**fields, 'contact': 'invalid'})).status_code == 422
    assert await fetchval('SELECT count(*) FROM analysis_requests WHERE client_key=$1', queue_client['client_key']) == 0


@pytest.mark.asyncio
async def test_queue_requires_verified_session_and_protects_private_status_updates(queue_client):
    from bot.db.pg import fetchrow
    client = queue_client['client']
    fields = await _fields(queue_client, comment='<script>unsafe()</script>')
    accepted = await client.post('/api/analysis-requests', json=fields)
    reference = accepted.json()['reference']
    client.cookies.set('admin_auth', '1')
    assert (await client.get('/admin/analysis-requests')).status_code == 302
    assert (await client.post('/admin/analysis-requests/' + reference + '/status', data={})).status_code == 401
    csrf = await _admin(queue_client)
    queue = await client.get('/admin/analysis-requests')
    assert queue.status_code == 200 and '@Buyer_Name' in queue.text
    assert '&lt;script&gt;unsafe()&lt;/script&gt;' in queue.text
    assert '<script>unsafe()</script>' not in queue.text and 'mc.yandex' not in queue.text
    path = '/admin/analysis-requests/' + reference + '/status'
    update = {'status': 'reviewing', 'private_note': 'Private note', 'version': '1', 'csrf_token': csrf}
    assert (await client.post(path, data={**update, 'csrf_token': 'invalid'})).status_code == 403
    assert (await client.post(path, data=update, headers={'Origin': 'https://other.test'})).status_code == 403
    assert (await client.post(path, data=update)).status_code == 303
    assert (await client.post(path, data={**update, 'private_note': 'Stale overwrite'})).status_code == 409
    row = await fetchrow('SELECT status,private_note,reviewed_by,version FROM analysis_requests WHERE request_id=$1', UUID(reference))
    assert dict(row) == {'status': 'reviewing', 'private_note': 'Private note', 'reviewed_by': queue_client['username'], 'version': 2}
    client.cookies.clear()
    denied = await client.get('/admin/analysis-requests')
    assert denied.status_code == 302 and 'Private note' not in denied.text
