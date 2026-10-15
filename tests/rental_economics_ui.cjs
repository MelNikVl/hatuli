/* Pure JS display gates: no browser, network, timers, databases or messages. */
const assert = require('node:assert/strict');
const {ready,render} = require('../static/hatuli-rental-economics.js');
const now=Date.parse('2026-10-16T12:00:00Z');
const source={level:'район',n:10,policy_version:'3',window_days:30,source_oldest_at:'2026-10-16T08:00:00Z',source_newest_at:'2026-10-16T10:00:00Z',updated_at:'2026-10-16T11:00:00Z'};
const params={occupancy_share:.95,opex_share:.1,purchase_cost_multiplier:1.02};
const fixture=()=>({schema_version:1,city_scope:'astana',asking_prices:true,scenario:true,computed_at:'2026-10-16T11:30:00Z',est_rent:300000,rent_range:[250000,350000],net_yield_pct:7.5,gross_yield_pct:9,confidence:'medium',rent_source:{...source},params_used:{...params}});
assert.equal(ready(fixture(),now),true);
assert.equal(render(null,now),'');
for(const change of [v=>v.rent_source.n=9,v=>v.confidence='high',v=>v.rent_source.policy_version='2',v=>v.rent_source.source_oldest_at='2026-10-10T08:00:00Z',v=>v.computed_at='2026-10-17T11:30:00Z',v=>v.net_yield_pct=Infinity,v=>v.params_used.purchase_cost_multiplier=.5]){
  const v=fixture();change(v);assert.equal(render(v,now),'');
}
const low=fixture();low.confidence='low';low.rent_source.level='город';
assert.equal(ready(low,now),true);assert.match(render(low,now),/color:var\(--muted\)/);assert.match(render(low,now),/Мало данных по ЖК/);
const html=render(fixture(),now);assert.ok(html.indexOf('7.5%')<html.indexOf('9.0%'));
assert.match(html,/не подтверждённый доход/);assert.match(html,/расходы 10.0%/);
const xss=fixture();xss.rental_comps=[{id:'"><script>alert(1)</script>',price:300000},{id:'12345678',price:300000,area:'<script>',rooms:'<img onerror=1>'}];
const escaped=render(xss,now);assert.ok(!escaped.includes('<script>')&&!escaped.includes('<img '));assert.match(escaped,/&lt;script&gt;/);assert.match(escaped,/krisha.kz\/a\/show\/12345678/);
const bench=fixture();bench.benchmark={median_net:7,p90_net:9,n:10,computed_at:'2026-10-16T11:30:00Z',source_oldest_at:source.source_oldest_at,source_newest_at:source.source_newest_at,params_used:params,policy_version:'3'};
assert.match(render(bench,now),/Медиана среза/);bench.benchmark.source_oldest_at='2026-10-10T08:00:00Z';assert.ok(!render(bench,now).includes('Медиана среза'));
console.log('Rental economics display gates passed');
