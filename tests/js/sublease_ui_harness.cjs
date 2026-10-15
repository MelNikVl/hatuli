'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.value = ''; this.ownText = ''; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(node => node.textContent || '').join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.ownText = ''; this.children = nodes; }
  addEventListener(kind, callback) { (this.listeners[kind] ||= []).push(callback); }
  emit(kind, event = {}) { return Promise.all((this.listeners[kind] || []).map(callback => callback(event))); }
  click() { if (this.download) downloads.push(this.download); }
}
const ids = {}, requests = [], downloads = [];
for (const id of ['sublease-form', 'sublease-status', 'sublease-calculation', 'sublease-segment', 'sublease-inventory', 'sublease-map', 'sublease-candidates', 'sublease-page', 'sublease-candidate-note', 'sublease-csv', 'sublease-next', 'sublease-previous', 'sublease-scan-button', 'sublease-segment-button']) ids[id] = new Element();
for (const id of ['sublease-calculation', 'sublease-segment', 'sublease-inventory']) ids[id].hidden = true;
for (const id of ['sublease-csv', 'sublease-next', 'sublease-previous']) ids[id].disabled = true;
const root = new Element(); root.dataset.accessLocked = 'false';
const form = ids['sublease-form']; form.reportValidity = () => true; form.elements = {};
for (const [name, value] of Object.entries({rooms: '2', furnishing: 'unknown', rent_month: '200000', nightly: '20000', occupancy: '60', avg_stay_nights: '2.5', cleaning_per_turn: '3000', consumables_per_night: '500', utilities_month: '20000', platform_fee_pct: '10', tax_pct: '3', furnish_capex: '600000', capex_months: '24', deposit_months: '1', vacancy_fraction: '10', strategy: 'daily', sort: 'breakeven'})) {
  const input = new Element('input'); input.name = name; input.value = value; form.elements[name] = input;
}
for (const name of ['occupancy', 'platform_fee_pct', 'tax_pct', 'vacancy_fraction']) form.elements[name].dataset.percent = '';
class BrowserFormData {
  constructor(source) { this.entries = Object.values(source.elements).filter(input => !input.disabled).map(input => [input.name, input.value]); }
  [Symbol.iterator]() { return this.entries[Symbol.iterator](); }
}
const context = {document: {querySelector: () => root, getElementById: id => ids[id] || null, createElement: tag => new Element(tag)},
  window: {}, FormData: BrowserFormData, AbortController, Intl, Date, URLSearchParams,
  URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}}, setTimeout: callback => callback(),
  fetch(url, options = {}) { return new Promise(resolve => { requests.push({url, options, resolve: (data, code = 200) => resolve({ok: code < 400, json: async () => data, blob: async () => ({})})}); }); }};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const index = {level: 'district', n: 10, updated_at: '2026-10-15T00:00:00Z', window_days: 30};
const economics = {breakeven_occupancy: .5, breakeven_possible: true, startup_capital: 400000, refundable_deposit: 200000, margin: 50000, revenue: 300000, costs: 250000, params_used: {occupancy: .6}, index_source: index, scenarios: []};
const item = {id: '12345678', rooms: 2, price: 200000, complex_name: 'ЖК', source_url: 'https://krisha.kz/a/show/12345678', economics, risk_flags: [], location: null};
const listingResult = {items: [item], page: 1, total: 1};

(async () => {
  // A deferred request intentionally ignores AbortController: generation
  // checks must also prevent a stale result from being rendered after edits.
  const pending = form.emit('submit', {preventDefault() {}});
  assert.equal(requests.length, 1);
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.occupancy, '0.6'); assert.equal(payload.tax_pct, '0.03');
  assert.equal(payload.platform_fee_pct, '0.1'); assert.equal(payload.vacancy_fraction, '0.1');
  form.elements.occupancy.value = '75'; await form.emit('input');
  assert.equal(requests[0].options.signal.aborted, true);
  requests[0].resolve(economics); await pending;
  assert.equal(ids['sublease-calculation'].hidden, true);
  assert.equal(ids['sublease-calculation'].children.length, 0);

  // Full furnishing omits capex; it neither writes 0 nor forgets the budget.
  for (const previous of ['600000', '']) {
    form.elements.furnish_capex.value = previous;
    form.elements.furnishing.value = 'full'; await form.elements.furnishing.emit('change'); await form.emit('change');
    assert.equal(form.elements.furnish_capex.disabled, true);
    const fullRequest = form.emit('submit', {preventDefault() {}}), request = requests.at(-1);
    assert.equal(Object.hasOwn(JSON.parse(request.options.body), 'furnish_capex'), false);
    form.elements.furnishing.value = 'unknown'; await form.elements.furnishing.emit('change'); await form.emit('change');
    assert.equal(form.elements.furnish_capex.disabled, false);
    assert.equal(form.elements.furnish_capex.value, previous);
    request.resolve(economics); await fullRequest;
    assert.equal(ids['sublease-calculation'].hidden, true);
  }
  form.elements.furnish_capex.value = '600000';

  const scan = ids['sublease-scan-button'].onclick(); requests.at(-1).resolve(listingResult); await scan;
  assert.equal(ids['sublease-inventory'].hidden, false); assert.equal(ids['sublease-csv'].disabled, false);
  const nextScan = ids['sublease-scan-button'].onclick(), stale = requests.at(-1);
  form.elements.rent_month.value = '250000'; await form.emit('change');
  assert.equal(stale.options.signal.aborted, true);
  assert.equal(ids['sublease-inventory'].hidden, true); assert.equal(ids['sublease-csv'].disabled, true);
  const beforeExport = requests.length; await ids['sublease-csv'].onclick(); assert.equal(requests.length, beforeExport);
  stale.resolve(listingResult); await nextScan;
  assert.equal(ids['sublease-inventory'].hidden, true); assert.equal(ids['sublease-candidates'].children.length, 0);

  const segment = ids['sublease-segment-button'].onclick(); requests.at(-1).resolve({indices: {monthly: null, daily: null, furnished_monthly: null}}); await segment;
  assert.equal(ids['sublease-segment'].hidden, true); assert.equal(ids['sublease-segment'].children.length, 0);

  // A broad request cannot leave the old narrow table/CSV active.
  const freshScan = ids['sublease-scan-button'].onclick(); requests.at(-1).resolve(listingResult); await freshScan;
  const broad = ids['sublease-scan-button'].onclick(); requests.at(-1).resolve({error: 'narrower_filters_required', errors: {filters: 'Уточните район'}}, 422); await broad;
  assert.equal(ids['sublease-inventory'].hidden, true); assert.equal(ids['sublease-csv'].disabled, true);
  assert.equal(ids['sublease-status'].textContent, 'Уточните район');

  // CSV in flight is invalidated by the same input-generation boundary.
  const finalScan = ids['sublease-scan-button'].onclick(); requests.at(-1).resolve(listingResult); await finalScan;
  const csv = ids['sublease-csv'].onclick(), csvRequest = requests.at(-1);
  await form.emit('input'); csvRequest.resolve({}); await csv;
  assert.equal(downloads.length, 0); assert.equal(csvRequest.options.signal.aborted, true);
  process.stdout.write('sublease UI regressions passed\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
