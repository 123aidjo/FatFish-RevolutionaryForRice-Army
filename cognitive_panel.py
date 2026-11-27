"""记忆管理 API，沿用本机鉴权并在查询前限定记忆作用域。"""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from cognition import Cognition, accepted_text
from cognitive_store import MemoryChangeError, SYSTEM_SCOPE, digest
from cognitive_workspace import DEMO_SCOPE, DemoMemory, MemoryWorkspace
from live_qq import CHANNEL, owner_channel
from policy import allowed
from settings import ConfigModel, Platform
from store import Store
from memory_engine import EngineConfig, EngineError, Embeddings, VectorIndex, read_config, save_config, search
from memory_growth import Growth
from contextlib import closing

Kind = Literal["person", "event", "fact", "preference", "promise", "autobiography", "reflection"]
NodeState = Literal["active", "inactive", "superseded", "conflict"]
Dataset = Literal["real", "demo"]


class Scope(ConfigModel):
    platform: Platform | Literal["system"]
    kind: Literal["group", "dm", "live", "role"]
    channel: str = Field(min_length=1, max_length=100)
    mode: Literal["live","simulation"] = "live"
    dataset: Dataset = "real"

    def key(self) -> str:
        key = json.dumps([self.platform,self.kind,self.channel,self.mode],ensure_ascii=False,separators=(",",":"))
        if (self.platform == "system" or self.kind == "role") and key != SYSTEM_SCOPE:
            raise HTTPException(422, "系统记忆作用域无效")
        return key


class Change(ConfigModel):
    scope: Scope
    updated_at: float = Field(ge=0, allow_inf_nan=False)


class NodeChange(Change):
    state: Literal["active","inactive"]


class ContentChange(Change):
    value: str = Field(min_length=1,max_length=300)


class GoalChange(Change):
    state: Literal["proposed","active","completed","cancelled","expired"]
    due_at: float | None = Field(default=None, ge=0, le=253402214400)


class Forget(Change):
    confirmation: int = Field(gt=0)


class ReflectionRequest(ConfigModel):
    scope: Scope
    subject: str = Field(min_length=1,max_length=100)
    topic: str = Field(min_length=1,max_length=100)


class Preview(ConfigModel):
    scope: Scope
    subject: str = Field(min_length=1,max_length=100)
    text: str = Field(min_length=1,max_length=300)


class EngineEdit(ConfigModel):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    config: EngineConfig
    clear_api_key: bool = False


class HybridQuery(Preview):
    history: bool = False


class GrowthChange(Change):
    action: Literal['promote','retain','demote','archive','restore','soft_delete']
    tier: str | None = None


class TaskRequest(ConfigModel):
    scope: Scope
    kind: Literal['evaluate','reflection'] = 'evaluate'


def page(limit: int, offset: int, maximum=100):
    if not (0 <= offset <= 1000000 and 1 <= limit <= maximum):
        raise HTTPException(422, "分页参数超出范围")


def scope_permitted(key, settings, live_qq=None):
    if key == SYSTEM_SCOPE: return True
    platform,kind,channel,mode=json.loads(key)
    if platform not in settings.platforms or kind=='role': return False
    if mode=='live' and platform=='qq' and live_qq is not None:
        if not live_qq.enabled(): return False
        actual=(platform,kind,channel)
        return ((actual==CHANNEL and settings.platforms['qq'].record_memory)
                or (actual==owner_channel(settings) and settings.qq_owner_record_memory))
    cfg=settings.platforms[platform]
    return cfg.enabled and cfg.receive and cfg.record_memory and allowed(cfg.allowlist,kind,channel)


def routes(store: Store, current, demo: DemoMemory, *, live_qq=None, redactor=lambda: str) -> APIRouter:
    api = APIRouter(prefix="/cognition")
    Scoped = Annotated[Scope, Depends(Scope)]

    def permits(key: str, dataset: Dataset, settings) -> bool:
        if dataset == "demo":
            return key in {SYSTEM_SCOPE,DEMO_SCOPE}
        if key == SYSTEM_SCOPE:
            return True
        platform,kind,channel,mode = json.loads(key)
        if platform not in settings.platforms or kind=="role":
            return False
        if mode=="live" and platform=="qq" and live_qq is not None:
            if not live_qq.enabled():
                return False
            actual = (platform,kind,channel)
            return ((actual==CHANNEL and settings.platforms["qq"].record_memory)
                    or (actual==owner_channel(settings) and settings.qq_owner_record_memory))
        cfg = settings.platforms[platform]
        return cfg.enabled and cfg.receive and cfg.record_memory and allowed(cfg.allowlist,kind,channel)

    def workspace(scope: Scope, snapshot=None):
        snapshot = snapshot or current()
        key = scope.key()
        if not permits(key,scope.dataset,snapshot.settings):
            raise HTTPException(403, "该会话未获准访问记忆")
        return MemoryWorkspace(demo.get() if scope.dataset=="demo" else store), key

    def visible_scopes(data: MemoryWorkspace, dataset: Dataset, settings):
        counts = {json.dumps(x["scope"],ensure_ascii=False,separators=(",",":")):x["nodes"]
                  for x in data.memory.scopes()}
        keys = {SYSTEM_SCOPE, *counts}
        if dataset=="demo":
            keys.add(DEMO_SCOPE)
        elif live_qq is not None:
            keys.add(json.dumps([*CHANNEL,"live"],separators=(",",":")))
            dm = owner_channel(settings)
            if dm:
                keys.add(json.dumps([*dm,"live"],separators=(",",":")))
        if dataset=="real":
            for platform,item in settings.platforms.items():
                for entry in item.allowlist:
                    kind,sep,channel = entry.partition(":")
                    if not sep:
                        kind,channel = "group",kind
                    if channel and kind in {"group","dm","live"}:
                        keys.add(json.dumps([platform,kind,channel,"live"],ensure_ascii=False,separators=(",",":")))
        return [{"scope":json.loads(key),"nodes":counts.get(key,0)}
                for key in sorted(keys,key=lambda s:(s!=SYSTEM_SCOPE,s)) if permits(key,dataset,settings)]

    def clean(data):
        redact = redactor()
        def visit(value):
            if isinstance(value,str):
                return redact(value)
            if isinstance(value,dict):
                return {key:visit(item) for key,item in value.items()}
            if isinstance(value,list):
                return [visit(item) for item in value]
            return value
        return visit(data)

    @api.get("")
    def status(dataset: Dataset="real"):
        snapshot = current()
        data = MemoryWorkspace(demo.get() if dataset=="demo" else store)
        scopes = visible_scopes(data,dataset,snapshot.settings)
        keys = [json.dumps(row["scope"],ensure_ascii=False,separators=(",",":")) for row in scopes]
        return clean({"settings": snapshot.settings.cognition.model_dump(), "scopes": scopes,
                      "system_scope": json.loads(SYSTEM_SCOPE), "role": snapshot.settings.persona.name,
                      "role_aliases": snapshot.settings.persona.aliases, "role_core": "/api/persona",
                      "revision": snapshot.revision[:12],
                      "role_revision":digest(snapshot.settings.persona.model_dump_json())[:12],
                      "dataset":dataset, "stats":data.stats(keys)})

    @api.get("/self")
    def self_state(scope: Scoped):
        data,key = workspace(scope)
        return clean(data.memory.self_state(key))

    @api.get("/nodes")
    def nodes(scope: Scoped, q: str = "", node_kind: Kind | None = None, state: NodeState | None = None,
              subject: str = "", since: float = 0, until: float | None = None, confidence: float = 0,
              sort: Literal["recent","importance","oldest"]="recent", limit: int = 50, offset: int = 0):
        page(limit,offset)
        if not (len(q)<=100 and len(subject)<=100 and 0<=confidence<=1 and 0<=since and (until is None or since<=until)):
            raise HTTPException(422, "检索参数超出范围")
        data,key = workspace(scope)
        found = data.memory.list(key,q=q.strip(),kind=node_kind or "",state=state or "",subject=subject,
                                 since=since,until=until,confidence=confidence,sort=sort,limit=limit+1,offset=offset)
        return clean({"items":found[:limit],"more":len(found)>limit})

    @api.get("/nodes/{node_id}")
    def detail(node_id: int, scope: Scoped):
        data,key = workspace(scope)
        record = data.memory.get(key,node_id)
        if record is None:
            raise HTTPException(404, "记忆不存在")
        return clean(record)

    @api.get("/graph")
    def graph(scope: Scoped, node_kind: Kind | None = None, limit: int=28, offset: int=0, anchor: int|None=None):
        page(limit,offset,40)
        data,key = workspace(scope)
        result = data.graph(key,node_kind or "",limit,offset,anchor)
        if result is None:
            raise HTTPException(404, "本会话中不存在该节点")
        return clean(result)

    @api.get("/people")
    def people(scope: Scoped, limit: int=20, offset: int=0):
        page(limit,offset)
        data,key = workspace(scope)
        found = data.people(key,limit+1,offset)
        return clean({"items":found[:limit],"more":len(found)>limit})

    @api.get("/timeline")
    def timeline(scope: Scoped, view: Literal["all","scoped","autobiography","episodes","goals"]="scoped",
                 subject: str="", limit: int=20, offset: int=0):
        page(limit,offset)
        if len(subject)>100:
            raise HTTPException(422,"身份参数太长")
        snapshot=current()
        data,key=workspace(scope,snapshot)
        scopes = [key]
        if view=="all" and not subject:
            scopes = [json.dumps(row["scope"],ensure_ascii=False,separators=(",",":"))
                      for row in visible_scopes(data,scope.dataset,snapshot.settings)]
        kinds = {"autobiography":("autobiography",),"episodes":("event",),"goals":("promise",)}.get(view,())
        found=data.timeline(scopes,kinds,subject,limit+1,offset)
        return clean({"items":found[:limit],"more":len(found)>limit})

    @api.get("/nodes/{node_id}/sources/{job_id}")
    def source(node_id: int,job_id: int,scope: Scoped):
        data,key=workspace(scope)
        result=data.source(key,node_id,job_id)
        if result is None:
            raise HTTPException(404,"该节点没有这条已授权来源")
        return clean(result)

    @api.get("/nodes/{node_id}/forget-impact")
    def impact(node_id: int,scope: Scoped):
        data,key=workspace(scope)
        result=data.forget_impact(key,node_id)
        if result is None:
            raise HTTPException(404,"记忆不存在")
        return result

    @api.post("/preview")
    def preview(payload: Preview):
        snapshot=current()
        data,key=workspace(payload.scope,snapshot)
        result=data.preview(key,payload.subject,payload.text.strip(),snapshot.settings.cognition)
        if result is None:
            raise HTTPException(403,"预览身份必须来自该会话的有效授权消息")
        return clean(result)

    @api.put("/nodes/{node_id}/state")
    def state(node_id: int,payload: NodeChange):
        data,key=workspace(payload.scope)
        try:
            changed=data.memory.change(key,node_id,payload.updated_at,payload.state)
        except MemoryChangeError as exc:
            raise HTTPException(409,str(exc)) from None
        if not changed:
            raise HTTPException(409,"记忆已修改或不存在，请刷新")
        return {"ok":True}

    @api.put("/nodes/{node_id}/content")
    def edit(node_id: int,payload: ContentChange):
        data,key=workspace(payload.scope)
        value=payload.value.strip()
        # 编辑可保留普通中文引号，凭据和指令仍由同一规则校验。
        if not accepted_text(value.translate(str.maketrans("「」“”","    "))) or redactor()(value)!=value:
            raise HTTPException(422,"请输入普通记忆文本，移除凭据、敏感信息和指令")
        try:
            new=data.memory.edit(key,node_id,payload.updated_at,value)
        except MemoryChangeError as exc:
            raise HTTPException(409,str(exc)) from None
        if new is None:
            raise HTTPException(409,"记忆已修改或不存在，请刷新")
        return {"id":new}

    @api.put("/nodes/{node_id}/goal")
    def goal(node_id: int,payload: GoalChange):
        data,key=workspace(payload.scope)
        if not data.memory.goal(key,node_id,payload.updated_at,payload.state,payload.due_at):
            raise HTTPException(409,"目标已修改或不存在，请刷新")
        return {"ok":True}

    @api.post("/nodes/{node_id}/forget")
    def forget(node_id: int,payload: Forget):
        data,key=workspace(payload.scope)
        if node_id!=payload.confirmation:
            raise HTTPException(422,"请输入正确的节点编号以确认删除")
        if not data.memory.delete(key,node_id,payload.updated_at):
            raise HTTPException(409,"记忆已修改或不存在，请刷新")
        return {"ok":True}

    @api.post("/reflections")
    def reflect(payload: ReflectionRequest):
        snapshot=current()
        data,key=workspace(payload.scope,snapshot)
        settings=snapshot.settings.cognition
        if not settings.reflection:
            raise HTTPException(409,"M2 反思暂未启用")
        return {"node_id":Cognition(data.store,settings).reflect(key,payload.subject,payload.topic)}

    @api.get('/engine')
    def engine_config():
        cfg,revision=read_config(store)
        public=cfg.model_dump(); public['api_key']=''
        index=VectorIndex(store).status()
        index['errors']=[{'error':e['error'],'attempts':e['attempts'],'available':e['available']} for e in index['errors']]
        with closing(store.open_read()) as db:
            tasks=[dict(r) for r in db.execute('SELECT kind,state,count(*) count FROM memory_tasks GROUP BY kind,state')]
        return {'config':public,'revision':revision,'key_configured':bool(cfg.api_key),'index':index,'tasks':tasks}

    @api.put('/engine')
    def configure_engine(payload: EngineEdit):
        cfg=payload.config
        if not cfg.api_key and not payload.clear_api_key:
            cfg=cfg.model_copy(update={'api_key':read_config(store)[0].api_key})
        try: save_config(store,cfg,payload.revision)
        except EngineError: raise HTTPException(409,'记忆设置已更新，请刷新') from None
        return {'saved':True}

    @api.post('/engine/test')
    async def test_embedding():
        cfg,_=read_config(store)
        try:
            vectors=await Embeddings(cfg).embed(['大肥鱼记忆连接测试，不包含私人内容'])
        except EngineError as exc: raise HTTPException(502,str(exc)) from None
        return {'available':True,'model':cfg.model,'dimension':len(vectors[0])}

    @api.post('/engine/rebuild')
    def rebuild_index():
        VectorIndex(store).rebuild()
        return {'queued':True}

    @api.post('/engine/retry')
    def retry_index():
        VectorIndex(store).retry()
        return {'queued':True}

    @api.post('/engine/search')
    async def hybrid_search(payload: HybridQuery):
        data,key=workspace(payload.scope)
        if not accepted_text(payload.text): raise HTTPException(422,'请输入普通检索文本')
        result=await search(data.store,key,payload.subject,payload.text,history=payload.history)
        workspace(payload.scope)  # Permission can change while an external request is in flight.
        valid=[]
        for item in result['items']:
            details=data.memory.get(key,item['node']['id'])
            if details is None or details['updated_at']!=item['node']['updated_at']: continue
            item['sources']=details['sources']
            valid.append(item)
        result['items']=valid
        return clean(result)

    @api.post('/engine/tasks')
    def schedule_tasks(payload: TaskRequest):
        data,key=workspace(payload.scope)
        if payload.scope.dataset=='demo': raise HTTPException(409,'演示数据不调用后台模型')
        count=Growth(data.store).schedule(force=True,scope=key,kind=payload.kind)
        return {'queued':count}

    @api.get('/engine/nodes')
    def lifecycle_nodes(scope: Scoped,status: Literal['active','archived','soft_deleted','inactive']='active'):
        data,key=workspace(scope)
        with closing(data.store.open_read()) as db:
            rows=[dict(r) for r in db.execute('''SELECT n.id,n.value,n.updated_at,n.importance,n.confidence,l.*
                FROM memory_nodes n JOIN memory_lifecycle l ON l.node_id=n.id
                WHERE n.scope=? AND l.status=? ORDER BY n.updated_at DESC LIMIT 100''',(key,status))]
        return clean({'items':rows})

    @api.put('/engine/nodes/{node_id}')
    def lifecycle_change(node_id: int,payload: GrowthChange):
        data,key=workspace(payload.scope)
        try: Growth(data.store).transition(key,node_id,payload.updated_at,payload.action,payload.tier,manual=True)
        except EngineError as exc: raise HTTPException(409,str(exc)) from None
        return {'ok':True}

    return api

