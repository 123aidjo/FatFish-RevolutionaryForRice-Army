"use strict";

const memoryKinds = {person:"人物", event:"经历", fact:"事实", preference:"偏好", promise:"约定",
  autobiography:"自传", reflection:"反思"};
const memoryStates = {active:"有效", inactive:"已失效", superseded:"已修订", conflict:"待确认冲突"};
const memoryOrigins = {message:"获准消息", system:"系统记录", inference:"推测性总结"};
const memoryRelations = {participated_in:"参与", related_to:"关联", supports:"支持", supersedes:"修订",
  contradicts:"冲突", belongs_to:"属于", mentioned:"提到", knows:"认识"};
const memoryActions = {created:"形成记忆", conflict:"记录冲突", superseded:"被新版本取代", reactivated:"重新启用",
  admin_edit:"管理员修订", active:"启用", inactive:"失效", goal_proposed:"目标待确认", goal_active:"目标进行中",
  goal_completed:"完成目标", goal_cancelled:"取消目标", goal_expired:"目标逾期"};
const goalStates = {proposed:"待确认", active:"进行中", completed:"已完成", cancelled:"已取消", expired:"已过期"};
const memoryTabs = {overview:"记忆总览", graph:"记忆图谱", people:"人物档案", timeline:"经历时间线",
  search:"记忆搜索", manage:"管理与自我模型", engine:"语义检索与成长", short:"短期会话"};

function memoryScope(parts, dataset) {
  const [platform,kind,channel,mode] = typeof parts === "string" ? JSON.parse(parts) : parts;
  return {platform,kind,channel,mode,dataset};
}

function memoryScopeLabel(parts) {
  const [platform,kind,channel] = typeof parts === "string" ? JSON.parse(parts) : parts;
  if (platform === "system") return "角色自传 · 系统";
  if (channel === "demo-room") return "QQ · 演示群";
  return (names[platform] || platform) + (kind === "dm" ? " · 私聊 " : " · 会话 ") + channel;
}

function memoryPick(options, chosen) {
  const control = node("select");
  for (const [key,label] of Object.entries(options)) {
    const option = node("option",label);
    option.value = key;
    control.append(option);
  }
  if (chosen !== undefined) control.value = chosen;
  return control;
}

function memoryQuery(path, params) {
  const query = new URLSearchParams();
  for (const [key,value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined) query.set(key,String(value));
  }
  return request("/cognition" + path + (query.size ? "?" + query : ""));
}

function memoryDate(value, fallback = "暂无数据") {
  return value === null || value === undefined ? fallback : timeLabel(value);
}

function memoryPager(host, offset, count, more, step, move) {
  const bar = node("div","","memory-pager");
  const previous = action("上一页"), next = action("下一页");
  previous.disabled = !offset;
  next.disabled = !more;
  previous.onclick = () => move(Math.max(0,offset-step));
  next.onclick = () => move(offset+step);
  bar.append(previous,node("span",count ? "第 " + (offset + 1) + "–" + (offset + count) + " 条" : "暂无条目"),next);
  host.append(bar);
}

function memoryList(host, items, open) {
  if (!items.length) { host.append(empty("这个范围暂无符合条件的记忆。")); return; }
  const list = node("ul","","memory-items");
  for (const item of items) {
    const li = node("li");
    const button = action("", "memory-item");
    button.append(tag(memoryKinds[item.kind] || item.kind,"memory-kind " + item.kind),
      node("strong",item.value), node("small",
        (memoryStates[item.state] || item.state) + " · 置信度 " + Math.round(item.confidence*100) + "%"));
    button.onclick = () => open(item.id,item.scope);
    li.append(button);
    list.append(li);
  }
  host.append(list);
}

async function cognitionPage(root) {
  const mast = node("header","","memory-mast");
  const intro = node("div");
  const avatar = node("img");
  avatar.src = "/assets/whale-girl.png";
  avatar.alt = "";
  const mastBadges = node("div","","memory-badges");
  intro.append(mastBadges,node("h2","鲸小鱼的记忆树"),
    node("p","查看她记得的人、发生过的事，以及每条记忆从哪里来。"));
  mast.append(intro,avatar);
  const controls = node("div","","memory-controls");
  const datasets = node("div","","memory-segments");
  datasets.setAttribute("aria-label","数据来源");
  const real = action("正式数据"), demo = action("演示数据");
  datasets.append(real,demo);
  const scope = node("select");
  const menu = node("div","","memory-tabs");
  menu.setAttribute("aria-label","记忆工作台页面");
  const body = node("div","","memory-body");
  const detail = node("aside","","memory-detail card");
  detail.hidden = true;
  detail.setAttribute("aria-label","记忆节点详情");
  detail.setAttribute("tabindex","-1");
  root.append(mast,controls,menu,body,detail);
  controls.append(datasets,labeled("记忆范围",scope));
  let dataset = "real", tab = "overview", snapshot, version = 0, detailVersion = 0, snapshotVersion = 0;
  let subject = "", anchor = null, graphKind = "", timelineView = "all";
  const activeScope = () => memoryScope(snapshot.scopes[Number(scope.value)].scope,dataset);
  const selected = () => snapshot.scopes[Number(scope.value)].scope;
  const closeDetail = () => { detailVersion+=1; detail.hidden=true; detail.replaceChildren(); };
  let originControl = null;

  const open = async (id, declared, focus = true) => {
    const ticket = ++detailVersion, kindOfData = dataset;
    const params = declared ? memoryScope(declared,kindOfData) : activeScope();
    if (focus && document.activeElement !== detail && !detail.contains(document.activeElement)) originControl = document.activeElement;
    detail.hidden = false;
    detail.replaceChildren(empty("正在读取节点…"));
    if (focus) detail.focus({preventScroll:true});
    try {
      const data = await memoryQuery("/nodes/" + id,params);
      if (ticket !== detailVersion || dataset !== kindOfData) return;
      renderMemoryDetail(detail,data,params,open,() => {
        closeDetail();
        if (originControl?.isConnected) originControl.focus();
      }, async idAfter => {
        await loadSnapshot(false);
        if (idAfter) await open(idAfter,declared,false);
      });
      if (focus && tab !== "graph") detail.scrollIntoView({block:"nearest"});
    } catch (error) {
      if (ticket === detailVersion) detail.replaceChildren(empty(error.message));
    }
  };

  async function draw() {
    const serial = ++version;
    const params = activeScope();
    body.replaceChildren(empty("正在加载…"));
    if (tab === "graph" && detail.parentElement !== body) detail.remove();
    else if (tab !== "graph" && detail.parentElement !== root) root.append(detail);
    for (const [key,button] of Object.entries(buttons)) button.setAttribute("aria-pressed",String(tab===key));
    const content = node("div","","stack");
    try {
      if (tab === "overview") await renderMemoryOverview(content,snapshot,params,open);
      if (tab === "graph") await renderMemoryGraph(content,params,{
        role:snapshot.role,kind:graphKind,anchor,open,
        setKind(value){ graphKind=value; anchor=null; draw(); },
        expand(value){ anchor=value; draw(); },
        showRole(){ closeDetail(); tab="manage"; draw(); }});
      if (tab === "people") await renderMemoryPeople(content,params,subject,open,selectedSubject => {
        subject = selectedSubject;
        draw();
      }, person => {
        subject=person; timelineView="scoped"; tab="timeline"; draw();
      });
      if (tab === "timeline") await renderMemoryTimeline(content,params,timelineView,subject,open,value => {
        timelineView=value; if (value==="all") subject=""; draw();
      });
      if (tab === "search") await renderMemorySearch(content,params,open);
      if (tab === "manage") await renderMemoryManage(content,snapshot,params,open);
      if (tab === "engine") await renderMemoryEngine(content,params,open);
      if (tab === "short") {
        if(dataset==="demo") content.append(empty("演示工作台使用独立的结构化记忆库。"));
        else await conversationMemory(content);
      }
      if (serial === version) {
        if (tab==="graph") content.querySelector(".memory-graph-layout").append(detail);
        body.replaceChildren(content);
      }
    } catch(error) {
      if (serial === version) body.replaceChildren(empty(error.message));
    }
  }

  async function loadSnapshot(reset) {
    const serial = ++snapshotVersion;
    version += 1;
    const prior = !reset && snapshot ? selected() : null;
    const data = await memoryQuery("",{dataset});
    if (serial !== snapshotVersion) return;
    snapshot = data;
    mastBadges.replaceChildren(tag(dataset === "demo" ? "演示数据 · 独立临时库" : "正式数据",
      dataset === "demo" ? "sim" : "good"),tag("M2 已安装"));
    real.setAttribute("aria-pressed",String(dataset==="real"));
    demo.setAttribute("aria-pressed",String(dataset==="demo"));
    scope.replaceChildren(...data.scopes.map((row,index) => {
      const option = node("option",memoryScopeLabel(row.scope) + " · " + row.nodes + " 条");
      option.value=String(index); return option;
    }));
    const existing = prior ? data.scopes.findIndex(row => JSON.stringify(row.scope)===JSON.stringify(prior)) : -1;
    if (existing >= 0) scope.value=String(existing);
    else if (dataset==="demo") scope.value=String(Math.max(0,data.scopes.findIndex(row=>row.scope[2]==="demo-room")));
    await draw();
  }

  const buttons = {};
  for (const [key,label] of Object.entries(memoryTabs)) {
    const button = action(label);
    buttons[key] = button;
    button.onclick = () => {
      if (tab !== key) closeDetail();
      if (key==="timeline" && tab!==key) subject="";
      tab=key; draw();
    };
    menu.append(button);
  }
  for (const [name,button] of [["real",real],["demo",demo]]) {
    button.onclick = () => {
      if(dataset===name) return;
      dataset=name; subject=""; anchor=null; graphKind=""; timelineView="all";
      closeDetail();
      body.replaceChildren(empty("正在切换数据…"));
      scope.disabled=true;
      real.disabled=demo.disabled=true;
      Object.values(buttons).forEach(item=>{item.disabled=true;});
      loadSnapshot(true).catch(error => toast(error.message,true)).finally(()=>{
        scope.disabled=false;
        real.disabled=demo.disabled=false;
        Object.values(buttons).forEach(item=>{item.disabled=false;});
      });
    };
  }
  scope.onchange = () => { subject=""; anchor=null; closeDetail(); draw(); };
  await loadSnapshot(true);
}

async function renderMemoryOverview(host,snapshot,params,open) {
  const s=snapshot.stats;
  const heading=card("记忆概况");
  const grid=node("div","","memory-metrics");
  for(const [label,value] of [["可访问记忆",s.total],...Object.keys(memoryKinds).map(k=>[memoryKinds[k],s.kinds[k]])]){
    const stat=node("div","","memory-stat");
    stat.append(node("span",label),node("strong",String(value)));
    grid.append(stat);
  }
  heading.append(grid);
  const operational=card("处理与注入状态");
  operational.append(memoryFlags(snapshot.settings),
    row("正在等待的任务",String(s.jobs.pending+s.jobs.running)),
    row("已处理任务",String(s.jobs.done)),row("失败任务",tag(String(s.jobs.failed),s.jobs.failed?"warn":"")),
    row("最近记忆更新",memoryDate(s.last_updated)),row("最近反思",memoryDate(s.last_reflection)));
  const recent=card("当前范围最近更新");
  const {items}=await memoryQuery("/nodes",{...params,limit:5});
  memoryList(recent,items,open);
  const pair=node("div","","memory-columns");
  pair.append(operational,recent);
  host.append(heading,pair);
}

function memoryFlags(config) {
  const box=node("div","","memory-flags");
  for(const [key,label] of [["extraction","实时长期提取"],["reflection","自动反思"],["recall","回复上下文注入"]]){
    box.append(row(label,tag(config[key]?"已启用":"未启用",config[key]?"good":"")));
  }
  return box;
}

function memorySVG(name, attrs={}) {
  const el=document.createElementNS("http://www.w3.org/2000/svg",name);
  for(const [key,value] of Object.entries(attrs)) el.setAttribute(key,String(value));
  return el;
}

function memoryEdgePath(a,b,avoidCenter) {
  const start="M "+a[0]+" "+a[1], end=b[0]+" "+b[1];
  if(!avoidCenter)return start+" L "+end;
  const dx=b[0]-a[0],dy=b[1]-a[1],squared=dx*dx+dy*dy;
  const fraction=((450-a[0])*dx+(300-a[1])*dy)/squared;
  if(fraction<=0 || fraction>=1)return start+" L "+end;
  const distance=Math.hypot(a[0]+dx*fraction-450,a[1]+dy*fraction-300);
  if(distance>=78)return start+" L "+end;
  const length=Math.sqrt(squared),nx=-dy/length,ny=dx/length;
  const side=((a[0]-450)*nx+(a[1]-300)*ny)>=0?1:-1;
  const bend=Math.min(220,(92-distance)/(2*fraction*(1-fraction)))*side;
  return start+" Q "+((a[0]+b[0])/2+nx*bend)+" "+((a[1]+b[1])/2+ny*bend)+" "+end;
}

async function renderMemoryGraph(host,params,options) {
  const pane=node("div","","memory-graph-layout");
  const stage=card("会话关系图");
  const toolbar=node("div","","memory-actions");
  const filter=memoryPick({"":"全部类型",...memoryKinds},options.kind);
  filter.onchange=()=>options.setKind(filter.value);
  const zoomIn=action("放大"), zoomOut=action("缩小"), reset=action("回到中心");
  toolbar.append(labeled("节点类型",filter),zoomIn,zoomOut,reset);
  const paged=node("div"), legend=node("div","","memory-legend");
  for(const [key,title] of Object.entries(memoryKinds)) legend.append(tag(title,"memory-kind "+key));
  legend.append(node("span","实线：实际关联"),node("span","虚线：修订"),node("span","点线：冲突"),
    node("span","角色中心仅用于导航"));
  const graphSpace=node("div","","memory-graph-space");
  stage.append(toolbar,graphSpace,legend,paged);
  pane.append(stage);
  host.append(pane);
  let scale=1,x=0,y=0, transform=()=>{}, fetchID=0;
  const home=()=>{scale=1;x=0;y=0;transform();};
  const zoom=factor=>{scale=Math.max(.65,Math.min(2.6,scale*factor));transform();};
  zoomIn.onclick=()=>zoom(1.22);
  zoomOut.onclick=()=>zoom(1/1.22);
  reset.onclick=()=>{home(); if(options.anchor) options.expand(null);};
  const load=async offset=>{
    const serial=++fetchID;
    const payload=await memoryQuery("/graph",{...params,node_kind:options.kind,limit:24,offset,anchor:options.anchor});
    if(serial!==fetchID) return;
    graphSpace.replaceChildren();
    stage.querySelector(".memory-accessible-nodes")?.remove();
    paged.replaceChildren();
    if(!payload.nodes.length){
      graphSpace.append(empty("此范围尚无结构化记忆。可以切换到演示数据浏览图谱。"));
      return;
    }
    paged.append(node("p",options.anchor?"显示选中节点和它的直接关系":"仅展示当前页节点及它们之间已有的关系。"));
    memoryPager(paged,offset,payload.nodes.length-(options.anchor?1:0),
      offset+payload.limit<payload.total,payload.limit,pos=>load(pos).catch(e=>toast(e.message,true)));
    const svg=memorySVG("svg",{viewBox:"0 0 900 600",role:"group","aria-label":"可缩放的认知记忆关系图"});
    const canvas=memorySVG("g");
    svg.append(canvas);
    graphSpace.append(svg);
    transform=()=>canvas.setAttribute("transform","translate("+x+" "+y+") translate(450 300) scale("+scale+") translate(-450 -300)");
    home();
    const coords=new Map();
    const centerX=450,centerY=300;
    const around=payload.nodes.filter(item=>item.id!==options.anchor);
    const rings=around.length>12?[around.slice(0,10),around.slice(10)]:[around];
    for(const [j,ring] of rings.entries()){
      for(const [i,item] of ring.entries()){
        const angle=-Math.PI/2+2*Math.PI*(i+.22*j)/ring.length;
        coords.set(item.id,[centerX+Math.cos(angle)*(j===0?225:375),centerY+Math.sin(angle)*(j===0?145:250)]);
      }
    }
    if(options.anchor) coords.set(options.anchor,[centerX,centerY]);
    const lines=[];
    for(const edge of payload.edges){
      const a=coords.get(edge.origin_id),b=coords.get(edge.target_id);
      if(!a||!b) continue;
      const line=memorySVG("path",{d:memoryEdgePath(a,b,!options.anchor),class:"memory-edge "+edge.relation});
      const label=memorySVG("title");
      label.textContent=memoryRelations[edge.relation]||edge.relation;
      line.append(label);canvas.append(line);lines.push([line,edge]);
    }
    if(!options.anchor){
      const core=memorySVG("g",{class:"memory-core",role:"button",tabindex:0,
        "aria-label":"打开鲸小鱼的角色自我模型"});
      const halo=memorySVG("circle",{cx:centerX,cy:centerY,r:66});
      const figure=memorySVG("image",{href:"/assets/whale-girl.png",x:411,y:246,width:78,height:78});
      const text=memorySVG("text",{x:centerX,y:348});
      text.textContent=options.role;
      core.append(halo,figure,text);canvas.append(core);
      core.addEventListener("click",options.showRole);
      core.addEventListener("keydown",event=>{
        if(event.key==="Enter"||event.key===" "){event.preventDefault();options.showRole();}
      });
    }
    const dots=new Map();
    const accessible=node("details","","memory-accessible-nodes");
    accessible.append(node("summary","用列表打开当前页节点"));
    for(const item of payload.nodes){
      const [cx,cy]=coords.get(item.id);
      const group=memorySVG("g",{tabindex:0,role:"button",class:"memory-dot "+item.kind,
        "aria-label":(memoryKinds[item.kind]||item.kind)+" "+item.topic+" "+item.value});
      const rect=memorySVG("rect",{x:cx-52,y:cy-22,width:104,height:44,rx:12});
      const type=memorySVG("text",{x:cx,y:cy-4,class:"memory-dot-type"});
      type.textContent=memoryKinds[item.kind]||item.kind;
      const label=memorySVG("text",{x:cx,y:cy+13});
      label.textContent=(item.kind==="person"?(item.value.match(/「(.+?)」/)?.[1]||item.topic):item.topic||item.value).slice(0,9);
      const title=memorySVG("title"); title.textContent=item.value;
      group.append(rect,type,label,title);canvas.append(group);dots.set(item.id,group);
      const select=()=>{
        const linked=new Set([item.id]);
        for(const [line,edge] of lines){
          const related=edge.origin_id===item.id||edge.target_id===item.id;
          line.classList.toggle("memory-edge-highlight",related);
          line.classList.toggle("memory-faded",!related);
          if(related){linked.add(edge.origin_id);linked.add(edge.target_id);}
        }
        for(const [id,dot] of dots){
          dot.classList.toggle("memory-node-selected",id===item.id);
          dot.classList.toggle("memory-faded",!linked.has(id));
        }
        options.open(item.id,null,false);
        const expand=action("展开此节点关系");
        expand.onclick=()=>options.expand(item.id);
        graphSpace.querySelector(".memory-expand")?.remove();
        const wrap=node("div","","memory-expand");
        wrap.append(expand); graphSpace.append(wrap);
      };
      group.addEventListener("click",select);
      const listButton=action(memoryKinds[item.kind]+" · "+item.topic);
      listButton.onclick=select;
      accessible.append(listButton);
      group.addEventListener("keydown",event=>{
        if(event.key==="Enter"||event.key===" "){event.preventDefault();select();}
      });
    }
    stage.append(accessible);
    let pointer=null;
    svg.addEventListener("pointerdown",event=>{
      if(event.target.closest("g[role=button]")) return;
      pointer={id:event.pointerId,x:event.clientX,y:event.clientY};
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove",event=>{
      if(pointer?.id!==event.pointerId) return;
      const ratio=900/svg.getBoundingClientRect().width;
      x+=(event.clientX-pointer.x)*ratio;y+=(event.clientY-pointer.y)*ratio;
      Object.assign(pointer,{x:event.clientX,y:event.clientY});transform();
    });
    for(const type of ["pointerup","pointercancel","lostpointercapture"]) svg.addEventListener(type,()=>{pointer=null;});
    svg.addEventListener("wheel",event=>{if(event.ctrlKey){event.preventDefault();zoom(event.deltaY<0?1.12:1/1.12);}},{passive:false});
  };
  await load(0);
}

async function renderMemoryPeople(host,params,subject,open,choose,timeline) {
  const column=node("div","","memory-columns");
  const listing=card("此会话中的人物");
  listing.append(node("p","同名档案按已核验的平台 ID 分开。"));
  const rows=node("div");
  listing.append(rows);
  const profile=card("人物档案");
  column.append(listing,profile);host.append(column);
  const load=async offset=>{
    const found=await memoryQuery("/people",{...params,limit:15,offset});
    rows.replaceChildren();
    if(!found.items.length) rows.append(empty("此范围暂无可访问的人物记忆。"));
    for(const person of found.items){
      const item=action("","memory-item");
      item.setAttribute("aria-pressed",String(subject===person.subject));
      item.append(node("strong",person.alias?.match(/「(.+?)」/)?.[1]||person.alias||"未知称呼"),
        node("small","平台 ID "+person.subject+" · "+person.memories+" 条记忆"),
        node("small","最近经历 "+memoryDate(person.latest_event)));
      item.onclick=()=>choose(person.subject);rows.append(item);
    }
    memoryPager(rows,offset,found.items.length,found.more,15,next=>load(next).catch(e=>toast(e.message,true)));
  };
  const details=async offset=>{
    const found=await memoryQuery("/nodes",{...params,subject,limit:15,offset});
    profile.replaceChildren(node("h2","人物档案"),row("会话",memoryScopeLabel([params.platform,params.kind,params.channel])),
      row("平台 ID",subject));
    const path=action("查看此人时间线");
    path.onclick=()=>timeline(subject);
    profile.append(path);
    memoryList(profile,found.items,open);
    memoryPager(profile,offset,found.items.length,found.more,15,next=>details(next).catch(e=>toast(e.message,true)));
  };
  await Promise.all([load(0),subject?details(0):Promise.resolve(profile.append(empty("选择一个人物，查看档案和相关记忆。")))]);
}

async function renderMemoryTimeline(host,params,view,subject,open,choose) {
  const panel=card("经历与版本");
  host.append(panel);
  const filter=memoryPick({all:"全部获准范围",scoped:"当前范围",autobiography:"系统自传",
    episodes:"情景记忆",goals:"约定及状态变化"},view);
  filter.onchange=()=>choose(filter.value);
  panel.append(labeled("时间线类型",filter));
  if(subject) panel.append(tag("人物 ID "+subject,"sim"));
  const list=node("div","","memory-timeline");
  panel.append(list);
  const page=async offset=>{
    const query=view==="autobiography"?memoryScope(["system","role","fishbot-v2","live"],params.dataset):params;
    const found=await memoryQuery("/timeline",{...query,view,subject:view==="autobiography"?"":subject,limit:20,offset});
    list.replaceChildren();
    if(!found.items.length) list.append(empty("暂无符合条件的时间线记录。"));
    for(const entry of found.items){
      const section=node("article","","memory-time-item");
      const title=action(entry.topic,"memory-link");
      title.onclick=()=>open(entry.id,entry.scope);
      section.append(node("small",memoryDate(entry.at,"日期未知")+" · "+memoryScopeLabel(entry.scope)),
        title,node("p",entry.value),
        tag(entry.entry==="revision"?memoryActions[entry.action]||entry.action:memoryKinds[entry.kind],
          "memory-kind "+entry.kind),
        node("small",(memoryOrigins[entry.origin]||entry.origin)+" · "+memoryStates[entry.state]+" · 置信度 "+
          Math.round(entry.confidence*100)+"%"+(entry.subject?" · "+entry.subject:"")));
      if(entry.entry==="node" && entry.origin==="message") section.append(node("small","来源消息时间 · 事件实际日期未独立确认"));
      if(entry.entry==="node" && entry.topic==="M2 启动") section.append(node("small","系统首次观测 · 历史改名日期未知"));
      list.append(section);
    }
    memoryPager(list,offset,found.items.length,found.more,20,next=>page(next).catch(e=>toast(e.message,true)));
  };
  await page(0);
}

async function renderMemorySearch(host,params,open) {
  const search=card("筛选记忆");
  const form=node("form","","memory-filter");
  const query=input("q","",{maxLength:100}), kind=memoryPick({"":"全部类型",...memoryKinds});
  const state=memoryPick({"":"全部状态",...memoryStates}), sort=memoryPick({recent:"最近更新",importance:"重要度",oldest:"最早形成"});
  const since=input("since","",{type:"date"}), until=input("until","",{type:"date"});
  const confidence=input("confidence","0",{type:"number",min:0,max:1,step:.05});
  const run=action("搜索","primary");run.type="submit";
  form.append(labeled("中文关键词、姓名或别名",query),labeled("类型",kind),labeled("状态",state),
    labeled("最低置信度（0–1）",confidence),labeled("起始日期",since),labeled("截止日期",until),
    labeled("排序",sort),run);
  const results=node("div");
  search.append(form,results);host.append(search);
  let count=0;
  const load=async offset=>{
    const serial=++count;
    const found=await memoryQuery("/nodes",{...params,q:query.value,node_kind:kind.value,state:state.value,sort:sort.value,
      confidence:confidence.value||0,since:since.value?new Date(since.value+"T00:00:00").getTime()/1000:0,
      until:until.value?new Date(until.value+"T23:59:59").getTime()/1000:null,limit:15,offset});
    if(serial!==count) return;
    results.replaceChildren();memoryList(results,found.items,open);
    memoryPager(results,offset,found.items.length,found.more,15,next=>load(next).catch(e=>toast(e.message,true)));
  };
  form.onsubmit=event=>{event.preventDefault();load(0).catch(e=>toast(e.message,true));};

  const preview=card("召回预览");
  preview.append(node("p","使用当前范围内有来源的人物与 M2 检索规则进行只读预览。"));
  host.append(preview);
  const data=await memoryQuery("/people",{...params,limit:50});
  const people=memoryPick(Object.fromEntries(data.items.map(p=>[p.subject,
    (p.alias?.match(/「(.+?)」/)?.[1]||"未知称呼")+" · "+p.subject])));
  const morePeople=action("加载更多人物");
  morePeople.hidden=!data.more;
  let peopleOffset=data.items.length;
  morePeople.onclick=()=>perform(morePeople,async()=>{
    const found=await memoryQuery("/people",{...params,limit:50,offset:peopleOffset});
    for(const p of found.items){
      const choice=node("option",(p.alias?.match(/「(.+?)」/)?.[1]||"未知称呼")+" · "+p.subject);
      choice.value=p.subject;people.append(choice);
    }
    peopleOffset+=found.items.length;
    morePeople.hidden=!found.more;
  },"已加载人物",false);
  const question=input("preview","你还记得我喜欢什么吗？",{maxLength:300,required:true});
  const reveal=action("预览召回","primary");
  reveal.disabled=!data.items.length;
  const outcome=node("div");
  preview.append(labeled("会话中已核验的人物",people),morePeople,labeled("检索文本",question),reveal,outcome);
  if(!data.items.length) outcome.append(empty("此范围尚无可预览的人物。"));
  reveal.onclick=()=>perform(reveal,async()=>{
    if(!question.reportValidity()) throw new Error("请输入检索文本。");
    const result=await request("/cognition/preview",{method:"POST",data:{scope:params,subject:people.value,text:question.value}});
    const excluded=result.excluded;
    outcome.replaceChildren(
      row("可能注入",result.count+" 条，估算上界 "+result.estimated_tokens_upper+" / "+result.token_budget+" Token"),
      row("本次排除","策略 "+excluded.policy+" · 预算 "+excluded.budget+" · 条数 "+excluded.limit),
      node("p","其他会话与人物从检索入口隔离。预览使用当前设置，无模型调用。"));
    if(result.input_rejected) outcome.append(empty("这段文本未通过记忆检索规则。"));
    else if(!result.items.length) outcome.append(empty("暂无命中。"));
    for(const hit of result.items){
      const item=action("","memory-item");
      item.append(node("strong",hit.memory.记忆),node("small",
        (hit.score===null?"关系拓展":"关联分 "+hit.score)+" · 置信度 "+Math.round(hit.confidence*100)+"% · "+hit.sources.length+" 条可追溯来源"));
      item.onclick=()=>open(hit.id,hit.scope);outcome.append(item);
    }
  },"召回预览已完成",false);
  await load(0);
}

async function renderMemoryManage(host,snapshot,params,open){
  const configured=card("记忆引擎与权限");
  configured.append(memoryFlags(snapshot.settings),
    row("会话访问",memoryScopeLabel([params.platform,params.kind,params.channel])+" · 已获准浏览"),
    row("处理任务","完成 "+snapshot.stats.jobs.done+" · 等待 "+snapshot.stats.jobs.pending+" · 运行 "+
      snapshot.stats.jobs.running+" · 失败 "+snapshot.stats.jobs.failed),
    row("最近反思",memoryDate(snapshot.stats.last_reflection)),
    row("检索调用次数",snapshot.stats.recall_calls===null?"暂无数据":String(snapshot.stats.recall_calls)),
    row("Token 使用统计",snapshot.stats.token_usage===null?"暂无数据":String(snapshot.stats.token_usage)));
  const role=card("角色自我模型");
  const info=await memoryQuery("/self",params);
  role.append(row("角色名称",snapshot.role),row("角色配置版本",snapshot.role_revision),
    row("角色昵称",snapshot.role_aliases.join("、")||"暂无"),
    row("系统首次或最近观测",memoryDate(info.observed_persona?.observed_at)),
    row("当前范围最近话题",info.recent_topics.join("、")||"暂无数据"),node("h3","尚未完成的目标"));
  if(!info.open_goals.length)role.append(empty("这个范围暂无未完成目标。"));
  for(const goal of info.open_goals){
    const button=action(goal.condition,"memory-item");button.onclick=()=>open(goal.node_id);
    role.append(button,row(goalStates[goal.state],memoryDate(goal.due_at,"时间未指定")));
  }
  role.append(node('h3','基于经历形成的认识'),node('p','这些是有证据的推测性认识，不会自动改写静态人设。'));
  for(const view of info.grounded_views || []) {
    const button=action(view.value,'memory-item');button.onclick=()=>open(view.id);
    role.append(button,row('置信度',String(view.confidence)));
  }
  if(!info.grounded_views?.length)role.append(empty('尚未形成有独立证据支持的长期认识。'));
  const system=card("系统里程碑");
  const facts=await memoryQuery("/nodes",{...memoryScope(["system","role","fishbot-v2","live"],params.dataset),limit:6});
  memoryList(system,facts.items,open);
  const reflections=card("最近反思");
  const thoughts=await memoryQuery("/nodes",{...params,node_kind:"reflection",limit:6});
  memoryList(reflections,thoughts.items,open);
  const pair=node("div","","memory-columns");pair.append(configured,role);
  const related=node("div","","memory-columns");related.append(system,reflections);
  host.append(pair,related);
}

function renderMemoryDetail(host,data,params,open,close,changed) {
  const head=node("div","","memory-detail-head");
  const exit=action("关闭");
  exit.onclick=close;
  head.append(node("h2","记忆详情 · #"+data.id),exit);
  host.replaceChildren(head,tag(memoryKinds[data.kind],"memory-kind "+data.kind),
    node("p",data.value,"memory-detail-copy prewrap"),
    row("状态",memoryStates[data.state]),row("范围",memoryScopeLabel(data.scope)),
    row("人物",data.subject||"角色系统"),row("来源性质",memoryOrigins[data.origin]),
    row("置信度与重要度",Math.round(data.confidence*100)+"% · "+data.importance+"/5"),
    row(data.origin==="message"?"来源时点（节点形成时间未独立记录）":"节点记录时间",memoryDate(data.created_at)),
    row("最近修订",memoryDate(data.updated_at)),
    row("实际发生时间",data.origin==="system"&&data.topic!=="M2 启动"?memoryDate(data.created_at):"未独立确认"));
  if(data.revisions.some(r=>r.action==="admin_edit"))host.append(tag("管理员修订 · 原消息仅供参考","warn"));
  const sources=node("details","","memory-block");
  sources.append(node("summary","来源证据"));
  if(!data.sources.length)sources.append(empty("未记录可直接追溯的来源。"));
  for(const source of data.sources){
    if(!source.job_id){
      sources.append(row(memoryOrigins[source.origin]||source.origin,
        source.system_ref?"记录于 "+memoryDate(source.at)+" · "+source.system_ref:"查看图谱中的支持证据"));
      continue;
    }
    const look=action("查看消息 "+source.message_id);
    const message=node("div","","memory-source");
    look.onclick=()=>perform(look,async()=>{
      const record=await memoryQuery("/nodes/"+data.id+"/sources/"+source.job_id,params);
      message.replaceChildren(node("small",record.name+" · "+memoryDate(record.at)),
        node("pre",record.text,"prewrap"),...(record.truncated?[node("small","仅显示前 1200 字符")]:[]));
    },"来源已展开",false);
    sources.append(look,message);
  }
  const edges=node("details","","memory-block");
  edges.append(node("summary","关联节点（"+data.edges.length+"）"));
  if(!data.edges.length)edges.append(empty("此节点没有关系。"));
  for(const edge of data.edges){
    const target=edge.origin_id===data.id?edge.target_id:edge.origin_id;
    const link=action((memoryRelations[edge.relation]||edge.relation)+" · #"+target);
    link.onclick=()=>open(target,data.scope,false);edges.append(link);
  }
  const history=node("details","","memory-block");
  history.append(node("summary","修订历史"));
  for(const version of data.revisions){
    const label=memoryActions[version.action]||version.action;
    if(version.evidence_node){
      const link=action(label+" · #"+version.evidence_node);
      link.onclick=()=>open(version.evidence_node,data.scope,false);
      history.append(row(memoryDate(version.at),link));
    }else history.append(row(memoryDate(version.at),label));
    if (version.action.startsWith('growth_') && version.previous_state) history.append(node('p',version.previous_state));
  }
  host.append(sources,edges,history);
  if (data.lifecycle) {
    const life=card('记忆成长');
    life.append(row('档位',data.lifecycle.tier||'待分档'),row('生命周期',data.lifecycle.status),
      row('到期时间',memoryDate(data.lifecycle.expires,'永久 / 尚未分配')),
      row('有效召回次数',String(data.lifecycle.recalls)),row('最近召回',memoryDate(data.lifecycle.last_recall)));
    host.append(life);
  }
  if(data.goal){
    const form=node("form","","memory-goal");
    const state=memoryPick(goalStates,data.goal.state);
    const due=input("due","",{type:"datetime-local"});
    if(data.goal.due_at){
      const local=new Date(data.goal.due_at*1000);
      due.value=new Date(local.getTime()-local.getTimezoneOffset()*60000).toISOString().slice(0,16);
    }
    const save=action("保存目标状态","primary");save.type="submit";
    form.append(labeled("目标状态",state),labeled("目标时间",due),save);
    form.onsubmit=e=>{
      e.preventDefault();
      perform(save,async()=>{
        await request("/cognition/nodes/"+data.id+"/goal",{method:"PUT",data:{scope:params,
          updated_at:data.goal.updated_at,state:state.value,due_at:due.value?new Date(due.value).getTime()/1000:null}});
        await changed(data.id);
      },"目标已更新",false);
    };
    host.append(form);
  }

  if(data.state==="active"&&data.origin==="message"){
    const editor=node("details","","memory-block");
    editor.append(node("summary","修订记忆内容"));
    const value=area("value",data.value,300,true);
    const save=action("保存修订","primary");
    save.onclick=()=>perform(save,async()=>{
      if(!value.reportValidity()) throw new Error("请输入记忆内容。");
      const result=await request("/cognition/nodes/"+data.id+"/content",{method:"PUT",
        data:{scope:params,updated_at:data.updated_at,value:value.value}});
      await changed(result.id);
    },"修订已保存，旧版本保留在历史中",false);
    editor.append(labeled("仅修改记忆内容",value),save);host.append(editor);
  }

  const actions=node("div","","memory-actions");
  if(["active","inactive"].includes(data.state)){
    const toggle=action(data.state==="active"?"标为失效":"恢复有效");
    toggle.onclick=()=>perform(toggle,async()=>{
      await request("/cognition/nodes/"+data.id+"/state",{method:"PUT",data:{scope:params,
        updated_at:data.updated_at,state:data.state==="active"?"inactive":"active"}});
      await changed(data.id);
    },"状态已更新",false);
    actions.append(toggle);
  }
  const forget=action("遗忘此记忆","danger");
  forget.onclick=async()=>{
    try{
      const impact=await memoryQuery("/nodes/"+data.id+"/forget-impact",params);
      const confirmation=window.prompt("将删除 "+impact.versions+" 个版本、"+impact.edges+" 条关系和 "+
        impact.reflections+" 个派生反思。涉及 "+impact.sources+" 条来源的处理任务及对应短期记忆。原始聊天审计与历史备份保留。请输入节点编号 "+
        data.id+" 确认。");
      if(confirmation===null)return;
      if(confirmation.trim()!==String(data.id)) {toast("节点编号不匹配，操作已取消。",true);return;}
      await perform(forget,async()=>{
        await request("/cognition/nodes/"+data.id+"/forget",{method:"POST",
          data:{scope:params,updated_at:data.updated_at,confirmation:Number(confirmation)}});
        close();await changed(null);
      },"此结构化记忆及派生内容已清理",false);
    }catch(error){toast(error.message,true);}
  };
  actions.append(forget);
  host.append(actions);
}




async function renderMemoryEngine(host, params, open) {
  if (params.dataset === "demo") { host.append(empty("请切换到真实数据后配置语义检索与记忆成长，演示数据不调用模型。")); return; }
  const data = await request('/cognition/engine');
  const cfg = data.config;
  const status = card('索引与后台任务');
  status.append(row('当前可用向量数',String(data.index.current_ready)),row('实际向量维度',String(data.index.detected_dimension || '尚未检测')),
    row('等待新一代索引',String(data.index.needs_rebuild || 0)),
    node('pre',JSON.stringify({index:data.index.states,errors:data.index.errors,tasks:data.tasks},null,2)));
  for (const [label,path] of [['测试 Embedding 连接','test'],['重建全部索引','rebuild'],['重试失败索引','retry']]) {
    const button=action(label);button.onclick=()=>perform(button,()=>request('/cognition/engine/'+path,{method:'POST'}),
      r=>r.dimension?'连接成功，实际维度 '+r.dimension:'已排队；启用语义检索后由后台分批处理');status.append(button);
  }
  host.append(status);
  const form=node('form','','card');form.append(node('h2','配置 Embedding 与记忆成长'),node('p','先保存参数再测试连接。后台模型任务默认关闭。Key 留空保留原值；提取、评价与反思使用当前角色聊天模型。Token 预算采用保守上界估算。'));
  form.append(row('Embedding 密钥',data.key_configured?'已配置，保存时留空可保留':'未配置，本机 Ollama 通常无需密钥'));
  const labels={embedding_enabled:'启用语义检索与后台索引',base_url:'Embedding API 地址（包含 /v1 路径）',model:'Embedding 模型',dimensions:'向量维度（留空自动检测）',api_key:'Embedding API Key',
    timeout:'索引请求超时（秒）',query_timeout:'聊天检索等待上限（秒）',interval:'后台检查间隔（秒）',batch_size:'每批数量',
    keyword_weight:'关键词权重',semantic_weight:'语义权重',graph_weight:'图谱权重',min_similarity:'最低余弦相似度',min_score:'最低相关性分数',candidates:'候选数量',final_count:'最终召回数量',token_budget:'召回上下文 Token 上限',
    llm_extraction:'启用 LLM 批量记忆提取',growth_enabled:'启用自动分档与到期评价',reflection_enabled:'启用有证据的后台反思',include_reflections:'允许将反思用于聊天',include_autobiography:'允许召回角色自传',
    task_interval:'评价与反思间隔（秒）',max_output_tokens:'单次后台模型输出上限',daily_requests:'每日后台 LLM 请求上限',daily_tokens:'每日后台 LLM Token 预算',archive_days:'归档复评天数（留空永久保留）',allow_soft_delete:'允许评价后软删除',initial_tier:'无匹配档位时使用的初始档位 ID'};
  const fields={};
  for(const [key,label] of Object.entries(labels)) {
    const value=cfg[key]; const field=node('input');
    field.type=key==='api_key'?'password':typeof value==='boolean'?'checkbox':typeof value==='number'||['dimensions','archive_days'].includes(key)?'number':'text';
    if(field.type==='checkbox')field.checked=value;else field.value=value ?? '';
    if(field.type==='number')field.step='any';
    field.autocomplete='off';fields[key]=field;form.append(labeled(label,field));
  }
  const clear=node('input');clear.type='checkbox';form.append(labeled('清除已保存的 Embedding Key',clear));
  const tiers=card('自定义记忆档位');const rows=[];
  const columns={id:'ID',name:'名称',order:'顺序',retention_days:'保留天数（空值=永久）',min_importance:'最低重要度',min_confidence:'最低置信度',min_confirmations:'最低独立证据数',min_recalls:'最低召回次数'};
  function addTier(tier){
    const box=node('details');box.open=true;box.append(node('summary',tier.name||'新档位'));const inputs={};
    for(const [key,label] of Object.entries(columns)) { const field=node('input');field.type=['id','name'].includes(key)?'text':'number';field.step='any';field.value=tier[key]??'';inputs[key]=field;box.append(labeled(label,field)); }
    const remove=action('删除此档位');remove.onclick=()=>{box.remove();const i=rows.findIndex(r=>r.inputs===inputs);rows.splice(i,1);};box.append(remove);tiers.append(box);rows.push({inputs});
  }
  cfg.tiers.forEach(addTier);
  const add=action('添加档位');add.onclick=()=>addTier({id:'tier_'+Date.now(),name:'新档位',order:Math.max(...rows.map(r=>Number(r.inputs.order.value)),0)+10,min_importance:3,min_confidence:.7,min_confirmations:2,min_recalls:0});
  tiers.append(add);form.append(tiers);
  const save=node('button','保存记忆设置','primary');save.type='submit';form.append(save);
  form.onsubmit=e=>{e.preventDefault();const updated={...cfg};
    for(const [key,field] of Object.entries(fields))updated[key]=field.type==='checkbox'?field.checked:field.type==='number'?(field.value===''?null:Number(field.value)):field.value;
    updated.tiers=rows.map(({inputs})=>Object.fromEntries(Object.entries(inputs).map(([key,field])=>[key,field.type==='number'?(field.value===''?null:Number(field.value)):field.value])));
    perform(save,()=>request('/cognition/engine',{method:'PUT',data:{revision:data.revision,config:updated,clear_api_key:clear.checked}}),'设置已保存；已删除档位的记忆迁入初始档位并保留记录');
  };host.append(form);
  const searchBox=card('混合检索测试 · 当前会话');
  const subject=node('input');subject.placeholder='主体 ID，可从记忆详情的主体字段复制';
  const query=node('input');query.placeholder='例如：我晚上应该避免喝什么？';query.maxLength=300;
  const historical=node('input');historical.type='checkbox';
  const result=node('div');const test=action('测试检索');
  test.onclick=()=>perform(test,async()=>{const answer=await request('/cognition/engine/search',{method:'POST',data:{scope:params,subject:subject.value,text:query.value,history:historical.checked}});
    result.replaceChildren(node('p',answer.degraded?'Embedding 不可用，已降级为关键词检索':'检索完成'));
    for(const item of answer.items){const button=action(item.node.value);button.onclick=()=>open(item.node.id);result.append(button,node('p',`分数 ${item.score.toFixed(3)} · ${item.channels.join(' + ')} · 置信度 ${item.node.confidence}`));}
    if(!answer.items.length)result.append(empty('没有达到阈值的相关记忆'));
  },'检索完成',false);
  searchBox.append(labeled('记忆主体',subject),query,labeled('包含历史归档',historical),test,result);host.append(searchBox);
  const history=card('档位与生命周期 · 当前会话');
  for(const [label,kind] of [['评价本会话记忆','evaluate'],['反思本会话经历','reflection']]) {const button=action(label);button.onclick=()=>perform(button,()=>request('/cognition/engine/tasks',{method:'POST',data:{scope:params,kind}}),r=>'已排队 '+r.queued+' 个后台任务');history.append(button);}
  const state=memoryPick({active:'有效',archived:'归档',soft_deleted:'软删除',inactive:'失效'},'active');const list=node('div');history.append(state,list);
  async function load(){const result=await memoryQuery('/engine/nodes',{...params,status:state.value});list.replaceChildren();
    for(const item of result.items){const box=card('#'+item.id);const view=action(item.value);view.onclick=()=>open(item.id);box.append(view,row('档位',item.tier||'待分档'),row('到期时间',memoryDate(item.expires,'永久 / 尚未分配')),row('重要度 / 置信度',item.importance+' / '+item.confidence),row('召回次数',String(item.recalls)));
      const tier=memoryPick(Object.fromEntries(cfg.tiers.map(t=>[t.id,t.name])),item.tier||cfg.initial_tier);
      const operation=memoryPick({retain:'保留/调整档位',promote:'晋升',demote:'降级',archive:'归档',restore:'恢复',soft_delete:'软删除'},item.status==='archived'?'restore':'retain');
      const apply=action('应用');apply.onclick=()=>perform(apply,async()=>{await request('/cognition/engine/nodes/'+item.id,{method:'PUT',data:{scope:params,updated_at:item.updated_at,action:operation.value,tier:tier.value}});await load();},'记忆已更新，详情中可查看修订记录',false);
      box.append(tier,operation,apply);list.append(box);
    }
    if(!result.items.length)list.append(empty());
  }
  state.onchange=()=>load().catch(e=>toast(e.message,true));await load();host.append(history);
}
