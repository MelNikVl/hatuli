const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'hatuli-cabinet-login.js'), 'utf8');
const credential = 'placeholder_login_credential';
const deepLink = 'https://t.me/placeholder_bot?start=' + credential;
const start = {token: credential, deep_link: deepLink};
const flush = () => new Promise(resolve => setImmediate(resolve));

function element() {
  return {
    disabled: false, attributes: {}, listeners: {}, children: [], value: '',
    set textContent(value) {this.value = value; this.children = [];},
    get textContent() {return this.value + this.children.map(child => child.textContent).join('');},
    replaceChildren(...children) {this.value = ''; this.children = children;},
    addEventListener(event, handler) {this.listeners[event] = handler;},
    setAttribute(key, value) {this.attributes[key] = value;},
    removeAttribute(key) {delete this.attributes[key];}
  };
}
function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}
function client(responses, {popupError = false} = {}) {
  let now = 0, timerId = 0, reloads = 0;
  const timers = new Map(), requests = [], opened = [], windowEvents = {};
  const button = element(), status = element();
  const setTimeout = (handler, delay) => {
    const id = ++timerId;
    timers.set(id, {at: now + delay, handler});
    return id;
  };
  const fetch = async (url, options) => {
    requests.push({url, options});
    const supplied = responses.shift();
    if (supplied === undefined) throw new Error('Unexpected mocked request');
    const value = await Promise.race([
      Promise.resolve(supplied),
      new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('Aborted')), {once: true}))
    ]);
    if (value instanceof Error) throw value;
    return {ok: !value.httpError, async json() {
      if (value.jsonError) throw new SyntaxError('Invalid JSON');
      return value.body === undefined ? value : value.body;
    }};
  };
  const window = {
    open(url, target, features) {
      if (popupError) throw new Error('Popup blocked');
      opened.push({url, target, features});
    },
    location: {reload() {reloads++;}},
    addEventListener(name, handler) {windowEvents[name] = handler;}
  };
  vm.runInNewContext(source, {
    document: {getElementById: id => id === 'login-btn' ? button : status,
      createElement: () => element(), createTextNode: text => ({textContent: text})},
    window, fetch, URL, AbortController, Date: {now: () => now}, setTimeout,
    clearTimeout: id => timers.delete(id)
  });
  return {
    button, status, timers, requests, opened, windowEvents,
    reloads: () => reloads,
    click: () => button.listeners.click(),
    async tick(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of [...timers].sort((a, b) => a[1].at-b[1].at)) {
        if (timer.at <= now && timers.has(id)) {
          timers.delete(id);
          timer.handler();
          await flush();
        }
      }
      await flush();
    }
  };
}
function retryVisible(state) {
  assert.equal(state.button.disabled, false);
  assert.equal(state.button.attributes['aria-busy'], undefined);
  assert.match(state.status.textContent, /нажми|Нажми/);
  assert.equal(state.timers.size, 0);
  assert(!state.status.textContent.includes(credential));
}

test('same-origin start, safe fallback, verified poll and reload', async () => {
  const state = client([start, {status: 'verified'}]);
  await state.click();
  assert.equal(state.button.disabled, true);
  assert.equal(state.requests.length, 1);
  const request = state.requests[0];
  assert.equal(request.url, '/api/auth/start');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.cache, 'no-store');
  assert.equal(request.options.redirect, 'error');
  const anchor = state.status.children[1];
  assert.equal(anchor.href, deepLink);
  assert.equal(anchor.rel, 'noopener noreferrer');
  assert(!state.status.textContent.includes(credential));
  await state.tick(2000);
  assert.equal(state.reloads(), 1);
  assert.equal(state.requests.length, 2);
  assert.equal(state.timers.size, 0);
});

test('start network failure releases the button and a new click works', async () => {
  const state = client([new Error('Network'), start, {status: 'verified'}]);
  await state.click();
  retryVisible(state);
  await state.click();
  await state.tick(2000);
  assert.equal(state.reloads(), 1);
});

for (const response of [
  {httpError: true}, {jsonError: true}, {}, {token: 'short', deep_link: deepLink},
  {token: credential, deep_link: 'https://invalid.example/'},
  {token: credential, deep_link: 'https://t.me.example.org/placeholder_bot'},
  {token: credential, deep_link: deepLink + '&other=unexpected'}
]) {
  test('invalid start data cannot open a link or leave a disabled button', async () => {
    const state = client([response]);
    await state.click();
    retryVisible(state);
    assert.equal(state.opened.length, 0);
    assert.equal(state.requests.length, 1);
  });
}

test('blocked popup leaves a usable link and continues polling', async () => {
  const state = client([start, {status: 'verified'}], {popupError: true});
  await state.click();
  assert.equal(state.status.children[1].href, deepLink);
  await state.tick(2000);
  assert.equal(state.reloads(), 1);
});

for (const response of [new Error('Network'), {httpError: true}, {jsonError: true}, {status: 'unexpected'},
                        {status: 'expired'}, {status: 'not_found'}, {status: 'consumed'}]) {
  test('poll error ends polling and offers a fresh login', async () => {
    const state = client([start, response]);
    await state.click();
    await state.tick(2000);
    retryVisible(state);
    await state.tick(60000);
    assert.equal(state.requests.length, 2);
    assert.equal(state.reloads(), 0);
  });
}

test('pending subscription keeps the safe link and can finish later', async () => {
  const state = client([start, {status: 'pending'}, {status: 'not_subscribed'}, {status: 'verified'}]);
  await state.click();
  await state.tick(2000);
  await state.tick(2000);
  assert.equal(state.status.children[1].href, deepLink);
  assert.equal(state.button.disabled, true);
  await state.tick(2000);
  assert.equal(state.reloads(), 1);
});

test('a slow poll never creates overlapping requests', async () => {
  const pending = deferred();
  const state = client([start, pending.promise, {status: 'verified'}]);
  await state.click();
  await state.tick(2000);
  await state.tick(5000);
  assert.equal(state.requests.length, 2);
  pending.resolve({status: 'pending'});
  await flush();
  await state.tick(2000);
  assert.equal(state.requests.length, 3);
  assert.equal(state.reloads(), 1);
});

test('the finite deadline ends a background wait without another request', async () => {
  const state = client([start]);
  await state.click();
  await state.tick(600000);
  retryVisible(state);
  assert.equal(state.requests.length, 1);
});

test('a hung start is aborted and releases the button', async () => {
  const never = deferred();
  const state = client([never.promise]);
  const click = state.click();
  await state.tick(12000);
  await click;
  retryVisible(state);
  assert.equal(state.requests[0].options.signal.aborted, true);
});

test('page hide cancels an active poll and ignores its stale success', async () => {
  const pending = deferred();
  const state = client([start, pending.promise]);
  await state.click();
  await state.tick(2000);
  state.windowEvents.pagehide();
  pending.resolve({status: 'verified'});
  await flush();
  assert.equal(state.requests[1].options.signal.aborted, true);
  assert.equal(state.reloads(), 0);
  assert.equal(state.timers.size, 0);
});

test('back-forward cache restoration does not leave the old login disabled', async () => {
  const state = client([start]);
  await state.click();
  state.windowEvents.pagehide();
  state.windowEvents.pageshow({persisted: true});
  retryVisible(state);
  assert.equal(state.requests.length, 1);
});
