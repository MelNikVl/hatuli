const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/hatuli-rent.js'), 'utf8');

function harness() {
  const node = () => ({hidden:true, textContent:'', value:'', checked:false, style:{}, handlers:{},
    addEventListener(event, handler) { this.handlers[event] = handler; },
    reportValidity() { return true; }, focus() {}, append() {}, remove() {}, click() {}});
  const nodes = new Map();
  const get = selector => { if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector); };
  const form = get('#rent-scenario-form');
  form.action = 'https://hatuli.test/api/rent/scenario';
  form.values = {monthly_rent_a:'250000',monthly_rent_b:'280000',horizon_months:'12',waiting_days:'30',monthly_owner_expenses:'20000',oneoff_expenses:'100000',refundable_deposit:'250000'};
  form.querySelector = get;
  const inquiry = get('#rent-request-form');
  inquiry.action = 'https://hatuli.test/rent/request';
  inquiry.values = {kind:'owner',contact:'@Example_User',brief:'Тестовый запрос без внешней отправки',consent:'yes',form_token:'test',website:''};
  inquiry.querySelector = get;
  const document = {querySelector(selector) { return ['#rent-examples','#rent-example'].includes(selector) ? null : get(selector); },
    querySelectorAll() { return []; },body:node(),createElement:node};
  let blobs = [], calls = [];
  const context = vm.createContext({document,Intl,Date,Number,Math,Error,JSON,Object,String,AbortController,Blob,
    URL:{createObjectURL(blob) { blobs.push(blob); return 'blob:test'; },revokeObjectURL() {}},
    FormData:class {constructor(element) {this.values={...element.values};} [Symbol.iterator]() {return Object.entries(this.values)[Symbol.iterator]();}},
    fetch(url, options) { return new Promise(resolve => calls.push({url,options,resolve})); }});
  vm.runInContext(source,context);
  return {nodes,get,form,inquiry,calls,blobs};
}
const response = {scenario_a:{net_horizon:2660000},scenario_b:{net_horizon:2740000},difference:80000,forgone_rent:250000,max_whole_waiting_days:38,refundable_deposit:250000};
const reply = (call, data=response, ok=true) => call.resolve({ok,status:ok?200:422,json:async()=>data});
const event = {preventDefault(){}};

(async () => {
  // Editing while a response is in flight invalidates even a fetch that ignores abort.
  const stale = harness();
  const pending = stale.form.handlers.submit(event);
  stale.form.values.monthly_rent_a='999000';
  stale.form.handlers.input();
  assert.equal(stale.calls[0].options.signal.aborted,true);
  reply(stale.calls[0]);
  await pending;
  assert.equal(stale.get('#rent-scenario-result').hidden,true);
  stale.get('#rent-download').handlers.click();
  assert.equal(stale.blobs.length,0);
  assert.equal(stale.get('[type=submit]').disabled,false);

  // Submitted parameters, rather than later FormData, are written to the report.
  const valid = harness();
  const validPending=valid.form.handlers.submit(event);
  assert.equal(JSON.parse(valid.calls[0].options.body).monthly_rent_a,'250000');
  valid.form.values.monthly_rent_a='999000';
  reply(valid.calls[0]);
  await validPending;
  assert.equal(valid.get('#rent-scenario-result').hidden,false);
  assert.match(valid.get('#rent-result-title').textContent,/80/);
  assert.match(valid.get('[data-break-even]').textContent,/38 полных/);
  valid.get('#rent-download').handlers.click();
  const report=await valid.blobs[0].text();
  assert.match(report,/Ставка А: 250/);
  assert.doesNotMatch(report,/999/);
  assert.match(report,/Депозит|депозит/);

  const invalid=harness();
  const invalidPending=invalid.form.handlers.submit(event);
  reply(invalid.calls[0],{errors:{waiting_days:'Ожидание превышает горизонт'}},false);
  await invalidPending;
  assert.equal(invalid.get('#rent-scenario-result').hidden,true);
  assert.equal(invalid.get('#rent-scenario-error').hidden,false);
  assert.match(invalid.get('#rent-scenario-error').textContent,/превышает/);

  const inquiry=harness();
  const inquiryPending=inquiry.inquiry.handlers.submit(event);
  reply(inquiry.calls[0],{accepted:true,reference:'test-reference'});
  await inquiryPending;
  assert.match(inquiry.get('#rent-request-status').textContent,/test-reference/);
  assert.equal(inquiry.get('[name=consent]').checked,false);
  assert.equal(inquiry.calls.length,1);

  console.log('4 rental UI checks passed: stale response, immutable report, input errors, request acknowledgement');
})().catch(error=>{console.error(error);process.exitCode=1;});
