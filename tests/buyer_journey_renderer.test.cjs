// Unit-test the real renderer against inert nodes; this is not browser automation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.isConnected = true; }
  set innerHTML(_) { throw new Error('Unsafe HTML sink'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
const window = {};
const context = {window, URL, Intl, Number, String, document: {createElement: tag => new Node(tag)}};
const source = fs.readFileSync(path.join(__dirname, '../static/hatuli-journey.js'), 'utf8');
new vm.Script(source); // A syntax error in the deployed script must fail the test.
vm.runInNewContext(source, context);
const flatten = root => [root, ...root.children.flatMap(flatten)];
const unsafe = ['javascript:alert(1)', 'http://krisha.kz/a/show/123456',
  'https://krisha.kz.evil.test/a/show/123456', 'https://evil.test/a/show/123456',
  'https://krisha.kz@evil.test/a/show/123456', 'https://evil@krisha.kz/a/show/123456',
  'https://krisha.kz:999/a/show/123456', 'https://krisha.kz/a/show/123456/extra'];
const payload = {found:true, publications:{count:2, message:'<img src=x onerror=alert(1)>',
  items:[{id:'123456',rooms:2,area:50,price:100}], discrepancies:[{field:'area',
    label:'<script>alert(1)</script>',observations:unsafe.map(url => ({url, value:'<img onerror=alert(1)>', observed_at:'2026-10-13'}))}]},
  alternatives_message:'<svg onload=alert(1)>', alternatives:[
    {id:'123457',url:'https://krisha.kz/a/show/123457',rooms:2,area:50,price:100,advantages:['<script>boom</script>']},
    {id:'"><img src=x onerror=alert(1)>',url:'javascript:alert(1)',rooms:2,area:50,price:100},
    {id:'123459',url:'https://evil.test/a/show/123459',rooms:2,area:50,price:100},
    {id:'123460',url:'https://krisha.kz/a/show/123460',rooms:2,area:50,price:100}]};
const container = new Node('section');
window.HatuliBuyerJourney.render(container, payload);
const nodes = flatten(container);
const hrefs = nodes.filter(n => n.href).map(n => n.href);
assert.deepEqual(hrefs.filter(h => h.startsWith('https:')), ['https://krisha.kz/a/show/123457']);
assert(!hrefs.some(h => unsafe.includes(h) || h.includes('<')));
assert.equal(nodes.filter(n => n.className === 'hj-alternative').length, 3);
assert(nodes.some(n => n.textContent === '<img src=x onerror=alert(1)>'));
assert(nodes.some(n => n.textContent === '<script>boom</script>'));
assert(!nodes.some(n => ['script', 'img', 'svg'].includes(n.tag)));
assert(nodes.some(n => String(n.textContent).includes('Сначала нужно подтвердить')));
const detached = new Node('section'); detached.isConnected = false;
window.HatuliBuyerJourney.render(detached, payload);
assert.equal(detached.children.length, 0);
