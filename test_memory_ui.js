// Dependency-free interaction smoke test; not a substitute for browser layout inspection.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('memory-workspace.js', 'utf8');
assert(!source.includes('???'), 'UI text must not contain encoding replacement runs');
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(tag, text = '') { this.tag = tag; this.text = text; this.children = []; }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  remove() { this.parent.children = this.parent.children.filter(x => x !== this); }
}
const node = (tag, text) => new Element(tag, text);
const requests = [], pending = [], opened = [];
const scope = {platform:'qq',kind:'group',channel:'fixture-room',mode:'live',dataset:'real'};
const context = {
  node, URLSearchParams,
  action: text => node('button', text), card: text => node('section', text), empty: text => node('p', text),
  row: (label, value) => node('p', label + ': ' + value),
  labeled: (label, control) => { const item = node('label', label); item.append(control); return item; },
  timeLabel: String, toast: () => {},
  request: async (path, options) => {
    requests.push({path, options});
    if (options?.method === 'PUT') return {saved:true};
    if (path === '/cognition/engine') return {config,revision:'fixture-revision',key_configured:false,index:{states:{},errors:[],current_ready:0},tasks:[]};
    if (path === '/cognition/engine/search') return {degraded:false,items:[{node:{id:42,value:'测试记忆',confidence:.8},score:.9,channels:['semantic']}]};
    if (path === '/cognition/engine/test') return {dimension:3};
    return {items:[]};
  },
  perform: (button, task) => { const promise = task(); pending.push(promise); return promise; },
};
vm.createContext(context);
vm.runInContext(source, context);
const all = element => [element, ...element.children.flatMap(all)];
(async () => {
  const host = node('main');
  await context.renderMemoryEngine(host, scope, id => opened.push(id));
  const button = text => all(host).find(x => x.tag === 'button' && x.text === text);
  const field = text => all(host).find(x => x.tag === 'label' && x.text === text).children[0];
  await button('测试 Embedding 连接').onclick();
  button('添加档位').onclick();
  const form = all(host).find(x => x.tag === 'form');
  form.onsubmit({preventDefault(){}});
  await Promise.all(pending);
  const saved = requests.find(x => x.options?.method === 'PUT').options.data;
  assert.equal(saved.revision, 'fixture-revision');
  assert.equal(saved.config.tiers.length, config.tiers.length + 1);
  assert.equal(saved.config.dimensions, null);
  assert.equal(saved.config.api_key, '');
  assert.equal(saved.config.embedding_enabled, false);
  field('记忆主体').value = 'fixture-subject';
  all(host).find(x => x.placeholder?.startsWith('例如')).value = '如何理解同义表达';
  await button('测试检索').onclick();
  button('测试记忆').onclick();
  assert.deepEqual(opened, [42]);
  const query = requests.find(x => x.path === '/cognition/engine/search').options.data;
  assert.equal(query.scope, scope);
  assert.equal(query.subject, 'fixture-subject');
  console.log('Memory UI interaction smoke test passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
