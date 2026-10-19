const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../bot/templates/dashboard.html'), 'utf8');
const start = template.indexOf('async function loadAllRentalPoints(');
const end = template.indexOf('// Один AbortController', start);
assert(start > 0 && end > start);
const production = template.slice(start, end);
// Compile all real inline script bodies too: the changed applyFilters closure
// must remain syntactically valid in the whole page, not just in this helper.
for (const script of template.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if (/\bsrc\s*=/.test(script[1])) continue;
  const source = script[2].replace(/\{\{[\s\S]*?\}\}/g, 'null').replace(/\{%[\s\S]*?%\}/g, '');
  new vm.Script(source);
}

function loader(fetch) {
  const context = vm.createContext({URLSearchParams, AbortController, Set, Number, Error, Promise,
                                   cachedFetchJson: fetch});
  vm.runInContext(production, context);
  return context.loadAllRentalPoints;
}

(async () => {
  let calls = [];
  const load = loader(async url => {
    const params = new URL(url, 'https://test').searchParams;
    const offset = Number(params.get('offset'));
    const take = Number(params.get('limit'));
    calls.push(offset);
    assert.equal(take, 300);
    assert.equal(params.get('price_max'), '300000');
    assert.equal(params.has('min_lat'), false); // city scope is explicit
    const rows = Math.min(take, 1001 - offset);
    return {points: offset === 900 ? [{id:'101000', lat:51.1, lon:71.4}] : [],
      batch_rows:rows, total_matching:1001, no_geo:offset === 900 ? 100 : rows,
      has_more:offset + rows < 1001, next_offset:offset + rows};
  });
  const progress = [];
  const data = await load({price_max:'300000', min_lat:'51'}, {onBatch:(_, state) => progress.push(state.raw_loaded)});
  assert.deepEqual(calls, [0, 300, 600, 900]);
  assert.equal(data.raw_loaded, 1001);
  assert.equal(data.no_geo, 1000);
  assert.equal(data.points.length, 1);
  assert.equal(data.points[0].id, '101000');
  assert.deepEqual(progress, [300, 600, 900, 1001]);

  let duplicateVisits = 0;
  const dedup = loader(async () => ++duplicateVisits === 1
    ? {points:[{id:'1'}], batch_rows:300, no_geo:299, total_matching:301, has_more:true, next_offset:300}
    : {points:[{id:'1'}], batch_rows:1, no_geo:0, total_matching:301, has_more:false});
  const deduplicated = await dedup();
  assert.equal(deduplicated.points.length, 1);
  assert.equal(deduplicated.raw_loaded, 301);

  let resolve, generation = 1, staleVisits = 0, staleRenders = 0;
  const stale = loader(() => { staleVisits++; return new Promise(done => {resolve = done;}); });
  const pending = stale({}, {isCurrent:() => generation === 1, onBatch:() => staleRenders++});
  generation = 2;
  resolve({points:[{id:'1'}], batch_rows:300, total_matching:1001, no_geo:0, has_more:true, next_offset:300});
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.equal(staleVisits, 1);
  assert.equal(staleRenders, 0);

  const controller = new AbortController();
  controller.abort();
  const aborted = loader(() => assert.fail('Aborted load sent a request'));
  await assert.rejects(aborted({}, {signal:controller.signal}), error => error.name === 'AbortError');

  let repeatedRequests = 0;
  const stuck = loader(async () => {repeatedRequests++; return {points:[], batch_rows:300,
    no_geo:300, total_matching:1001, has_more:true, next_offset:0};});
  await assert.rejects(stuck(), /did not advance/);
  assert.equal(repeatedRequests, 1);
  const denied = loader(async () => ({error:'Forbidden'}));
  await assert.rejects(denied(), /Invalid rental page/);

  assert(template.includes('signal:rentalSignal, isCurrent:rentalCurrent'));
  assert.equal((template.match(/loadAllRentalPoints\(\)/g) || []).length, 2);
  assert(!template.includes("cachedFetchJson('/admin/api/map-points?type=rental')"));
  const pinStart = template.indexOf('function buildPricePinHtml(');
  const cardStart = template.indexOf('function buildRentalCardHtml(');
  const cardEnd = template.indexOf('// Подсветка выбранного пина', cardStart);
  const render = vm.createContext({Number, Math, Set,
    SVOY_DOM_DEV_IDS:new Set(), fmtPriceShort:value => value ? String(value) : '—',
    ptPhotos:{}, carIdx:{}, proxyImg:value => value, escapeHtml:value => String(value).replace(/[&<>"']/g,
      char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))});
  vm.runInContext(template.slice(pinStart, cardStart), render);
  vm.runInContext(template.slice(cardStart, cardEnd), render);
  for (const price of [null, 0, undefined, NaN]) {
    const point = {id:'1234567', price, prev_price:250000, rooms:0, photos:[]};
    const card = render.buildRentalCardHtml(point);
    const pin = render.buildPricePinHtml(point, '#0077FF', false, '', true);
    assert(card.includes('Цена не указана') && pin.includes('Цена не указана'));
    assert(card.includes('Студия') && pin.includes('Студия'));
    assert(!card.includes('Цена снизилась') && !pin.includes('price-trend-arrow'));
    assert(!card.includes('0 тыс ₸/мес') && !pin.includes('0K'));
    assert(card.includes('openRentalDetailModal') && !card.includes('openDetailModal'));
    assert(card.includes('Кто сдаёт — не указано') && !card.includes('toggleFavorite'));
  }
  const priced = render.buildRentalCardHtml({id:'1234567', price:250000, rooms:2, photos:[]});
  assert(priced.includes('250 тыс ₸/мес'));
  const salePin = render.buildPricePinHtml({id:'7654321', price:30000000, rooms:2}, '#fff', false, '');
  assert(salePin.includes('30000000') && !salePin.includes('Цена не указана'));
  const rentalDetailStart = template.indexOf('function renderRentalDetailHtml(');
  const rentalDetailEnd = template.indexOf('function openRentalDetailModal(', rentalDetailStart);
  vm.runInContext(template.slice(rentalDetailStart, rentalDetailEnd), render);
  const detailHtml = render.renderRentalDetailHtml({id:'1234567', title:'<script>bad</script>', price:null,
    rooms:0, photos:[], address:'<img src=x onerror=bad>', status_label:'Снято с публикации',
    monthly_category:true, prop_type:'apartment', rent_period:'monthly', source_url:'javascript:bad', history_available:true,
    price_history:[{old_price:300000,new_price:250000,changed_at:'2026-10-13T10:00:00Z'}]});
  assert(detailHtml.includes('Цена не указана') && detailHtml.includes('Студия'));
  assert(detailHtml.includes('&lt;script&gt;') && !detailHtml.includes('<script>bad'));
  assert(detailHtml.includes('https://krisha.kz/a/show/1234567') && !detailHtml.includes('javascript:bad'));
  assert(detailHtml.includes('История заявленной цены') && detailHtml.includes('не подтверждены'));
  assert(!detailHtml.includes('доходность') && !detailHtml.includes('прогноз продажи'));
  for (const [rent_period, title, suffix] of [['daily','Посуточная аренда','/сутки'],['monthly','Помесячная аренда','/мес'],['unknown','единица ставки не определена','']]) {
    const html = render.renderRentalDetailHtml({id:'1234567',prop_type:'apartment',rent_period,monthly_category:true,price:20000});
    assert(html.includes(title));
    if (suffix) assert(html.includes(suffix));
    if (rent_period !== 'monthly') assert(!html.includes('/мес'));
  }
  const parkingHtml = render.renderRentalDetailHtml({id:'1234567',prop_type:'parking',rent_period:'monthly',monthly_category:true,price:20000});
  assert(parkingHtml.includes('единица ставки не определена') && !parkingHtml.includes('/мес'));
  process.stdout.write('Rental pagination, price and period detail production renderer passed\n');
})().catch(error => {console.error(error); process.exitCode = 1;});
