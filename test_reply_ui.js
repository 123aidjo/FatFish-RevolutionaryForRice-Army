const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const cfg=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag,text=''){this.tag=tag;this.text=text;this.children=[];this.checked=false;this.value='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
}
const node=(tag,text)=>new Element(tag,text),requests=[],pending=[];
const env={node,URLSearchParams,action:text=>node('button',text),card:text=>node('section',text),
  input:(name,value,opts)=>Object.assign(node('input'),{name,value,...opts}),
  row:(label,value)=>node('p',label+': '+value),timeLabel:String,
  labeled:(label,field)=>{const el=node('label',label);el.append(field);return el;},
  perform:(button,task)=>{const promise=task();pending.push(promise);return promise;},
  request:async(path,options)=>{requests.push({path,options});
    if(options)return {saved:true};
    if(path==='/reply-decision')return {config:cfg,revision:'fixture',base_interval:4,name:'测试角色',aliases:['小鱼'],scopes:[['qq','group','fixture','live']],states:[]};
    return {totals:{eco:{tokens:0,calls:0,sent:0,planner_average_tokens:null,silent_average_tokens:null,tokens_per_sent:null,estimated_requests:0,unknown_requests:0}},groups:[],hourly:[]};
  }};
vm.createContext(env);
vm.runInContext(fs.readFileSync('memory-workspace.js','utf8'),env);
const source=fs.readFileSync('app.js','utf8');
const section=source.slice(source.indexOf('async function replyDecisionControls('),source.indexOf('async function douyinAccount('));
assert(section && !section.includes('???'));
vm.runInContext(section,env);
const all=el=>[el,...el.children.flatMap(all)];
(async()=>{
  const host=node('main');await env.replyDecisionControls(host);
  const forms=all(host).filter(x=>x.tag==='form');assert.equal(forms.length,2);
  const labelled=text=>all(host).find(x=>x.tag==='label'&&x.text===text).children[0];
  labelled('回复模式').value='balanced';
  labelled('启用动态退避').checked=false;
  forms[0].onsubmit({preventDefault(){}});await Promise.all(pending);
  const saved=requests.find(x=>x.options?.method==='PUT').options.data;
  assert.equal(saved.config.mode,'balanced');assert.equal(saved.config.backoff,false);
  assert.equal(saved.config.planner_input_tokens,800);assert.equal(saved.config.planner_key,'');
  labelled('会话').value=JSON.stringify(['qq','group','fixture','live']);
  forms[1].onsubmit({preventDefault(){}});await Promise.all(pending);
  assert(requests.at(-1).path.includes('fixture'));
  console.log('Reply decision UI interactions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
