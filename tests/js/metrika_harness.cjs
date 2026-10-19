// Inert unit harness. No browser, HTTP, collector, cookies, or external library.
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
const loaded = [];
let ready;
let maskedBeforeLoad = false;
function field() {
  const classes = new Set();
  return {value: 'PRIVATE-USER-INPUT', classList: {add(...names) { names.forEach(name => classes.add(name)); }}, classes};
}
const fields = [field(), field(), field()];
const forms = [field(), field()];
const document = {
  referrer: scenario.referrer || '',
  readyState: scenario.loading ? 'loading' : 'complete',
  scripts: [],
  querySelectorAll(selector) { return selector === 'form' ? forms : fields; },
  createElement() { return {}; },
  getElementsByTagName() {
    return [{parentNode: {insertBefore(script) {
      maskedBeforeLoad = fields.every(node => node.classes.has('ym-disable-keys')) &&
        forms.every(node => node.classes.has('ym-hide-content') && node.classes.has('ym-disable-submit'));
      loaded.push(script.src);
    }}}];
  },
  addEventListener(name, callback) { if (name === 'DOMContentLoaded') ready = callback; }
};
const window = {location: {origin: 'https://hatuli.ai-groundtruth.com', hostname: scenario.hostname || 'hatuli.ai-groundtruth.com', hash: scenario.hash || ''}};
const sandbox = {window, document, URL, Date};
vm.createContext(sandbox);
vm.runInContext(scenario.script, sandbox);
const beforeDomReady = loaded.length;
if (ready) ready();
if (scenario.repeat) vm.runInContext(scenario.script, sandbox);
const calls = window.ym && window.ym.a ? window.ym.a.map(call => Array.from(call)) : [];
process.stdout.write(JSON.stringify({loaded, calls, masked_before_load: maskedBeforeLoad, before_dom_ready: beforeDomReady}));
