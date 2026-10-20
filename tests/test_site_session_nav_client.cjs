const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/hatuli-session-nav.js'), 'utf8');

for (const status of [true, false, 'true', null]) {
  test(`only a verified boolean updates navigation: ${status}`, async () => {
    const link = {textContent: 'Войти', className: 'pub-login-btn'};
    await vm.runInNewContext(code, {document: {querySelector: () => link},
      fetch: async (url, options) => {
        assert.equal(url, '/api/auth/status');
        assert.equal(options.credentials, 'same-origin');
        assert.equal(options.cache, 'no-store');
        assert.equal(options.redirect, 'error');
        return {ok: true, json: async () => ({authenticated: status})};
      }});
    assert.equal(link.textContent, status === true ? 'Кабинет' : 'Войти');
    assert.equal(link.className, status === true ? 'pub-user-chip' : 'pub-login-btn');
  });
}
test('network failure keeps the cabinet link usable', async () => {
  const link = {textContent: 'Войти'};
  await vm.runInNewContext(code, {document: {querySelector: () => link},
    fetch: async () => {throw new Error('offline');}});
  assert.equal(link.textContent, 'Войти');
});
test('server-rendered authorized header needs no status request', async () => {
  await vm.runInNewContext(code, {document: {querySelector: () => null},
    fetch: () => {throw new Error('unnecessary request');}});
});
