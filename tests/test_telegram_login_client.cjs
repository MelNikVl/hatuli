const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'hatuli-telegram-login.js'), 'utf8');
const validToken = 'confirmed_telegram-login_credential';

function client({ hash = '#token=' + validToken, response, fetchError, jsonError, pending, pendingJSON } = {}) {
  const events = [];
  const requests = [];
  const redirects = [];
  const timers = new Map();
  let nextTimer = 0;
  function waitingForAbort(signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) {reject(new Error('aborted')); return;}
      signal.addEventListener('abort', () => reject(new Error('aborted')), {once:true});
    });
  }
  const elements = {
    'telegram-login-title': { textContent: 'Входим в Hatuli' },
    'telegram-login-status': { textContent: 'Подтверждаем вход через Telegram…' },
    'telegram-login-retry': { hidden: true, href: '/cabinet' }
  };
  const location = {
    pathname: '/auth/telegram', search: '?next=https://external.example', hash,
    replace(destination) { events.push('redirect'); redirects.push(destination); }
  };
  const window = {
    location,
    history: {
      replaceState(state, unused, url) {
        events.push('clear-fragment');
        assert.equal(state, null);
        assert.equal(url, '/auth/telegram');
        location.hash = '';
        location.search = '';
      }
    }
  };
  const document = {
    getElementById(id) {
      assert.ok(Object.hasOwn(elements, id), 'Expected a known visible login element');
      return elements[id];
    }
  };
  const fetch = async (url, options) => {
    events.push('fetch');
    assert.equal(location.hash, '', 'Credential must already be removed when the request begins');
    assert.equal(location.search, '', 'Navigation arguments must not survive in page history');
    requests.push({ url, options });
    if (fetchError) throw fetchError;
    if (pending) return waitingForAbort(options.signal);
    const result = response || { ok: true, status: 200, body: { status: 'verified' } };
    return {
      ok: result.ok, status: result.status,
      async json() {
        events.push('json');
        if (jsonError) throw jsonError;
        if (pendingJSON) return waitingForAbort(options.signal);
        return result.body;
      }
    };
  };
  const done = vm.runInNewContext(source, { window, document, fetch, URLSearchParams, AbortController,
    setTimeout(callback, delay) { assert.equal(delay, 12000); const id=++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) {timers.delete(id);}
  });
  return { done, events, requests, redirects, elements, location, timers,
    triggerTimeout() {for (const callback of timers.values()) callback();} };
}

function assertVisibleRetry(state, expectedMessage) {
  assert.equal(state.elements['telegram-login-title'].textContent, 'Повтори вход');
  assert.equal(state.elements['telegram-login-retry'].hidden, false);
  assert.equal(state.elements['telegram-login-retry'].href, '/cabinet');
  assert.match(state.elements['telegram-login-status'].textContent, expectedMessage);
  assert.deepEqual(state.redirects, []);
  assert.equal(state.location.hash, '');
  assert.ok(!Object.values(state.elements).some(element => String(element.textContent).includes(validToken)),
    'The one-use credential must never be shown in status or error messages');
}

test('clears login credential before sending one same-origin POST and opens the site after success', async () => {
  const state = client();
  await state.done;
  assert.deepEqual(state.events, ['clear-fragment', 'fetch', 'json', 'redirect']);
  assert.equal(state.requests.length, 1);
  const { url, options } = state.requests[0];
  assert.equal(url, '/api/auth/complete');
  assert.ok(!url.includes(validToken));
  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  assert.equal(options.redirect, 'error');
  assert.equal(options.referrerPolicy, 'no-referrer');
  assert.equal(options.headers['Content-Type'], 'application/json');
  assert.equal(options.headers['X-Hatuli-Login'], 'telegram');
  assert.deepEqual(JSON.parse(options.body), { token: validToken });
  assert.deepEqual(state.redirects, ['/map']);
  assert.equal(state.elements['telegram-login-retry'].hidden, true);
  assert.match(state.elements['telegram-login-status'].textContent, /Вход выполнен/);
});

for (const redirect of ['https://external.example/phishing', '//external.example', '/admin-panel']) {
  test(`success always opens /map and ignores a response redirect of ${redirect}`, async () => {
    const state = client({ response: { ok: true, status: 200, body: { status: 'verified', redirect } } });
    await state.done;
    assert.deepEqual(state.redirects, ['/map']);
    assert.equal(state.location.hash, '');
  });
}

for (const hash of ['', '#', '#token=', '#other=somevalue', '#token=too-short',
                    '#token=' + 'a'.repeat(101), '#token=' + 'a'.repeat(16) + '%0A',
                    '#token=' + 'a'.repeat(16) + '%2F', '#token=' + 'a'.repeat(16) + '+']) {
  test(`missing or malformed login token does not issue a request (${JSON.stringify(hash)})`, async () => {
    const state = client({ hash });
    await state.done;
    assert.deepEqual(state.events, ['clear-fragment']);
    assert.equal(state.requests.length, 0);
    assertVisibleRetry(state, /свежую ссылку.*10 минут.*один раз/);
  });
}

for (const status of ['expired', 'consumed', 'reused', 'blocked']) {
  test(`${status} credential visibly offers a new login without redirecting or retrying the token`, async () => {
    const state = client({ response: { ok: false, status: 401, body: { status } } });
    await state.done;
    assert.equal(state.requests.length, 1);
    assertVisibleRetry(state, /уже использована, устарела или доступ закрыт/);
  });
}

for (const response of [
  { ok: false, status: 403, body: { status: 'verified' } },
  { ok: false, status: 500, body: { status: 'error' } },
  { ok: true, status: 200, body: { status: 'pending', redirect: 'https://external.example' } }
]) {
  test(`non-success status ${response.status}/${response.body.status} cannot navigate as authenticated`, async () => {
    const state = client({ response });
    await state.done;
    assert.equal(state.requests.length, 1);
    assertVisibleRetry(state, /Не удалось подтвердить вход/);
  });
}

test('network failure shows an actionable new-login link and does not repeat the request', async () => {
  const state = client({ fetchError: new Error('connection closed') });
  await state.done;
  assert.equal(state.requests.length, 1);
  assert.deepEqual(state.events, ['clear-fragment', 'fetch']);
  assertVisibleRetry(state, /Не удалось связаться.*Проверь соединение/);
});

test('invalid server JSON shows a visible retry and cannot navigate as authenticated', async () => {
  const state = client({ jsonError: new SyntaxError('invalid JSON') });
  await state.done;
  assert.equal(state.requests.length, 1);
  assertVisibleRetry(state, /Не удалось связаться.*начни вход ещё раз/);
});

for (const pendingStage of ['pending', 'pendingJSON']) {
  test(`a stalled ${pendingStage} request aborts and offers a fresh login`, async () => {
    const state = client({[pendingStage]: true});
    await new Promise(setImmediate);
    if (pendingStage === 'pendingJSON') assert.ok(state.events.includes('json'));
    assert.equal(state.timers.size, 1);
    state.triggerTimeout();
    await state.done;
    assert.equal(state.requests.length, 1);
    assert.equal(state.requests[0].options.signal.aborted, true);
    assert.equal(state.timers.size, 0);
    assertVisibleRetry(state, /Не удалось связаться.*начни вход ещё раз/);
  });
}
