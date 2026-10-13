// Run the deployed renderer against inert nodes; no browser or network is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; }
  set innerHTML(_) { throw new Error('Unsafe HTML sink'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this[name] = value; }
  get childNodes() { return this.children; }
}

const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/hatuli-forecast.js'), 'utf8'), {
  window, Intl, Number, Date, document: {createElement: tag => new Node(tag)},
});
const renderer = window.HatuliPriceForecast;
const flatten = root => [root, ...root.children.flatMap(flatten)];
const text = root => flatten(root).map(node => node.textContent || '').join('\n');
const forecast = {
  status: 'ready', horizon_days: 7, estimate: 31_500_000, low: 29_000_000,
  high: 33_000_000, historical_analog_count: 32, target_date: '2026-10-20',
  validation: {sample_size: 125, mape_pct: 1.3, baseline_mape_pct: 2.4, coverage_pct: 82},
};
const payload = {
  as_of: '2026-10-13T06:00:00+00:00', forecasts: [forecast],
  market_now: {status: 'ready', estimate: 30_000_000, low: 28_000_000, high: 32_000_000, analog_count: 18},
};

for (const data of [undefined, null, {}, {forecasts: []}, {market_now: payload.market_now},
  {...payload, forecasts: [{...forecast, status: 'insufficient_history'}]},
  {...payload, forecasts: [{...forecast, low: 32_000_000}]},
  {...payload, forecasts: [{...forecast, validation: {sample_size: 0, mape_pct: 1.3}}]},
  {...payload, forecasts: [{...forecast, target_date: '2026-02-30'}]}]) {
  assert.equal(renderer.render(data), null, 'No complete, validated horizon means no section');
}

const valid = renderer.render(payload);
assert.equal(valid.dataset.forecastStatus, 'ready');
assert(text(valid).includes('Цена предложения через 7 дней'));
assert(text(valid).includes('31 500 000 ₸'));
assert(text(valid).includes('20.10.2026'));
assert(text(valid).includes('125 наблюдений'));
assert.equal(valid['aria-label'], 'Оценка цены и прогноз');

const mixed = renderer.render({...payload, forecasts: [
  {status: 'insufficient_history', horizon_days: 30, reason: 'MISSING_HORIZON_REASON'}, forecast,
], unsupported_horizons: [{status: 'unavailable', horizon_days: 365, reason: 'UNSUPPORTED_REASON'}]});
assert(text(mixed).includes('Цена предложения через 7 дней'));
assert(!text(mixed).includes('30 дней'));
assert(!text(mixed).includes('365 дней'));
assert(!text(mixed).includes('MISSING_HORIZON_REASON'));
assert(!text(mixed).includes('UNSUPPORTED_REASON'));

const noMarket = renderer.render({...payload, market_now: {status: 'insufficient_analogs'}});
assert(!text(noMarket).includes('Оценка рынка сегодня'));
assert(text(noMarket).includes('Цена предложения через 7 дней'));

const target = new Node('div');
renderer.mount(target, payload);
assert.equal(target.hidden, false);
assert.equal(target.children.length, 1);
renderer.mount(target, {market_now: payload.market_now});
assert.equal(target.hidden, true);
assert.equal(target.children.length, 0, 'A failed new forecast must clear an earlier successful result');
renderer.mount(target, payload);
assert.equal(target.hidden, false);
assert.equal(target.children.length, 1);

const injection = renderer.render({...payload, limitations: ['<img src=x onerror=alert(1)>']});
assert(text(injection).includes('<img src=x onerror=alert(1)>'));
assert(!flatten(injection).some(node => ['script', 'img', 'svg'].includes(node.tag)));
const noBlankBullets = renderer.render({...payload, limitations: [null, '', '   ', 3, 'Цена объявления может отличаться от сделки.']});
assert.equal(flatten(noBlankBullets).filter(node => node.tag === 'li').length, 1);
console.log('Forecast renderer: valid, absent, mixed, stale mount and escaped text checks passed.');
