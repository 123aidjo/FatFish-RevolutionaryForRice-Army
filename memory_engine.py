"""Persistent derived indexes and configuration for the existing cognitive memory."""
import asyncio
from array import array
from contextlib import closing
import hashlib
import json
import math
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import Field, field_validator, model_validator
from settings import ConfigModel


class Tier(ConfigModel):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    name: str = Field(min_length=1,max_length=40)
    order: int = Field(ge=0,le=10000)
    retention_days: int | None = Field(default=None,ge=1,le=36500)
    min_importance: int = Field(default=1,ge=1,le=5)
    min_confidence: float = Field(default=.65,ge=0,le=1)
    min_confirmations: int = Field(default=1,ge=1,le=1000)
    min_recalls: int = Field(default=0,ge=0,le=100000)


class EngineConfig(ConfigModel):
    embedding_enabled: bool = False
    base_url: str = 'http://127.0.0.1:11434/v1'
    model: str = Field(default='qwen3-embedding:4b',min_length=1,max_length=200)
    api_key: str = Field(default='',max_length=4000)
    dimensions: int | None = Field(default=None,ge=1,le=32768)
    generation: str = Field(default='initial',max_length=40)
    timeout: float = Field(default=10,ge=.1,le=60)
    query_timeout: float = Field(default=2,ge=.1,le=10)
    interval: int = Field(default=30,ge=1,le=86400)
    batch_size: int = Field(default=8,ge=1,le=32)
    keyword_weight: float = Field(default=.35,ge=0,le=1)
    semantic_weight: float = Field(default=.5,ge=0,le=1)
    graph_weight: float = Field(default=.15,ge=0,le=1)
    min_similarity: float = Field(default=.55,ge=0,le=1)
    min_score: float = Field(default=.18,ge=0,le=1)
    candidates: int = Field(default=40,ge=1,le=200)
    final_count: int = Field(default=6,ge=1,le=20)
    token_budget: int = Field(default=1600,ge=100,le=12000)
    llm_extraction: bool = False
    include_reflections: bool = False
    include_autobiography: bool = False
    growth_enabled: bool = False
    reflection_enabled: bool = False
    task_interval: int = Field(default=300,ge=10,le=86400)
    max_output_tokens: int = Field(default=1800,ge=100,le=8000)
    daily_requests: int = Field(default=24,ge=0,le=1000)
    daily_tokens: int = Field(default=100000,ge=0,le=5000000)
    archive_days: int | None = Field(default=None,ge=1,le=36500)
    allow_soft_delete: bool = False
    initial_tier: str = 'temporary'
    tiers: list[Tier] = Field(default_factory=lambda:[
        Tier(id='temporary',name='临时记忆',order=0,retention_days=1),
        Tier(id='short',name='短期记忆',order=10,retention_days=7,min_importance=2),
        Tier(id='medium',name='中期记忆',order=20,retention_days=30,min_importance=3),
        Tier(id='long',name='长期记忆',order=30,retention_days=365,min_importance=4,min_confirmations=2),
        Tier(id='core',name='核心记忆',order=40,min_importance=5,min_confidence=.85,min_confirmations=3),
    ],min_length=1,max_length=100)

    @field_validator('base_url')
    @classmethod
    def valid_url(cls,value):
        url=urlsplit(value)
        if (url.scheme not in ('http','https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or (url.scheme=='http' and url.hostname not in ('127.0.0.1','localhost','::1'))):
            raise ValueError('远程服务须使用HTTPS，本机可使用HTTP；地址不能含凭据')
        return value.rstrip('/')

    @model_validator(mode='after')
    def consistent(self):
        ids=[t.id for t in self.tiers]
        if len(ids)!=len(set(ids)) or self.initial_tier not in ids or len({t.order for t in self.tiers})!=len(ids):
            raise ValueError('档位标识/顺序不能重复，初始档位必须存在')
        if self.keyword_weight+self.semantic_weight+self.graph_weight<=0:
            raise ValueError('至少启用一种召回方式')
        return self

    def fingerprint(self):
        return hashlib.sha256(json.dumps([self.base_url,self.model,self.dimensions,self.generation]).encode()).hexdigest()


class EngineError(Exception): pass


def read_config(store):
    with closing(store.open_read()) as db:
        row=db.execute("SELECT value FROM panel_state WHERE key='memory_engine_config'").fetchone()
    raw=row[0] if row else EngineConfig().model_dump_json()
    return EngineConfig.model_validate_json(raw),hashlib.sha256(raw.encode()).hexdigest()


def save_config(store,config,revision):
    with store.transaction() as db:
        row=db.execute("SELECT value FROM panel_state WHERE key='memory_engine_config'").fetchone()
        raw=row[0] if row else EngineConfig().model_dump_json()
        if hashlib.sha256(raw.encode()).hexdigest()!=revision:
            raise EngineError('config_conflict')
        old=EngineConfig.model_validate_json(raw)
        now=time.time()
        tiers={t.id:t for t in config.tiers}
        # Removing a tier preserves every node and records the migration.
        for row in db.execute('SELECT node_id,tier,status FROM memory_lifecycle').fetchall():
            tier=tiers.get(row['tier'],tiers[config.initial_tier])
            previous=next((t for t in old.tiers if t.id==row['tier']),None)
            if previous != tier:
                expiry=now+tier.retention_days*86400 if tier.retention_days else None
                if row['status']=='archived':
                    expiry=now+config.archive_days*86400 if config.archive_days else None
                elif row['status']!='active':
                    expiry=None
                db.execute('UPDATE memory_lifecycle SET tier=?,expires=?,revision=? WHERE node_id=?',
                           (tier.id,expiry,now,row['node_id']))
                db.execute("INSERT INTO memory_revisions(scope,node_id,at,action,previous_state) SELECT scope,id,?,'tier_config_migration',? FROM memory_nodes WHERE id=?",(now,row['tier'],row['node_id']))
        db.execute("INSERT INTO panel_state VALUES('memory_engine_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(config.model_dump_json(),))


def text_hash(node):
    return hashlib.sha256((node['topic']+'\n'+node['value']).encode()).hexdigest()


class Embeddings:
    def __init__(self,config,transport=None):
        self.config,self.transport=config,transport

    async def embed(self,texts,timeout=None):
        cfg=self.config
        headers={'Authorization':'Bearer '+cfg.api_key} if cfg.api_key else {}
        body={'model':cfg.model,'input':texts,'encoding_format':'float'}
        if cfg.dimensions is not None: body['dimensions']=cfg.dimensions
        try:
            async with httpx.AsyncClient(timeout=timeout or cfg.timeout,follow_redirects=False,transport=self.transport) as client:
                response=await client.post(cfg.base_url+'/embeddings',headers=headers,json=body)
                response.raise_for_status()
                payload=response.json()
            rows=sorted(payload['data'],key=lambda r:r['index'])
            if [r['index'] for r in rows]!=list(range(len(texts))): raise ValueError()
            vectors=[]
            for row in rows:
                values=row['embedding']
                if (not isinstance(values,list) or not 1<=len(values)<=32768
                        or any(type(v) not in (int,float) or not math.isfinite(v) for v in values)):
                    raise ValueError()
                if cfg.dimensions is not None and len(values)!=cfg.dimensions:
                    raise EngineError('embedding_dimension_mismatch')
                norm=math.sqrt(sum(v*v for v in values))
                if not math.isfinite(norm) or norm<=0: raise ValueError()
                vectors.append([v/norm for v in values])
            if len({len(v) for v in vectors})!=1: raise ValueError()
            return vectors
        except EngineError: raise
        except Exception:
            raise EngineError('embedding_unavailable_or_invalid') from None


class VectorIndex:
    def __init__(self,store,client=None,clock=time.time,permitted=lambda scope:True):
        self.store,self.client,self.clock=store,client,clock
        self.permitted=permitted

    async def batch(self):
        cfg,_=read_config(self.store)
        if not cfg.embedding_enabled: return 0
        now=self.clock(); generation=cfg.fingerprint(); lease=uuid4().hex
        with self.store.transaction() as db:
            rows=db.execute('''SELECT n.* FROM memory_nodes n JOIN memory_vectors v ON v.node_id=n.id
                JOIN memory_lifecycle l ON l.node_id=n.id
                WHERE (n.state='active' OR l.status='archived') AND (v.state<>'running' OR v.available<=?) AND
                (v.generation<>? OR (v.state IN ('pending','error','running') AND v.available<=?))
                ORDER BY v.available,n.id''',(now,generation,now)).fetchall()
            rows=[r for r in rows if self.permitted(r['scope'])][:cfg.batch_size]
            for row in rows:
                db.execute("UPDATE memory_vectors SET state='running',lease=?,available=?,attempts=attempts+1,generation=? WHERE node_id=?",(lease,now+cfg.timeout+30,generation,row['id']))
        if not rows: return 0
        try:
            vectors=await (self.client or Embeddings(cfg)).embed([r['topic']+'\n'+r['value'] for r in rows])
            with self.store.transaction() as db:
                if read_config(self.store)[0].fingerprint()!=generation: return 0
                known=db.execute("SELECT dimension FROM memory_vectors WHERE generation=? AND state='ready' LIMIT 1",(generation,)).fetchone()
                if known and known[0]!=len(vectors[0]): raise EngineError('embedding_dimension_changed')
                for node,vector in zip(rows,vectors):
                    current=db.execute('SELECT * FROM memory_nodes WHERE id=?',(node['id'],)).fetchone()
                    if current and self.permitted(current['scope']) and text_hash(current)==text_hash(node):
                        db.execute("UPDATE memory_vectors SET state='ready',vector=?,dimension=?,content_hash=?,lease=NULL,error=NULL,attempts=0,updated_at=? WHERE node_id=? AND lease=?",
                                   (array('f',vector).tobytes(),len(vector),text_hash(node),now,node['id'],lease))
        except asyncio.CancelledError: raise
        except Exception as exc:
            reason=str(exc) if isinstance(exc,EngineError) else 'embedding_failed'
            with self.store.transaction() as db:
                db.execute("UPDATE memory_vectors SET state='error',lease=NULL,error=?,available=?+min(3600,30*attempts) WHERE lease=?",(reason,now,lease))
        return len(rows)

    def status(self):
        cfg,_=read_config(self.store)
        with closing(self.store.open_read()) as db:
            eligible="FROM memory_vectors v JOIN memory_nodes n ON n.id=v.node_id JOIN memory_lifecycle l ON l.node_id=n.id WHERE (n.state='active' OR l.status='archived')"
            states={r[0]:r[1] for r in db.execute('SELECT v.state,count(*) '+eligible+' GROUP BY v.state')}
            ready=db.execute("SELECT count(*),max(v.dimension) "+eligible+" AND v.generation=? AND v.state='ready'",(cfg.fingerprint(),)).fetchone()
            stale=db.execute('SELECT count(*) '+eligible+' AND v.generation<>?',(cfg.fingerprint(),)).fetchone()[0]
            errors=[dict(r) for r in db.execute("SELECT v.node_id,v.error,v.attempts,v.available "+eligible+" AND v.state='error' LIMIT 20")]
        return {'states':states,'current_ready':ready[0],'needs_rebuild':stale,'detected_dimension':ready[1],'errors':errors}

    def rebuild(self):
        cfg,revision=read_config(self.store)
        save_config(self.store,cfg.model_copy(update={'generation':uuid4().hex}),revision)

    def retry(self):
        with self.store.transaction() as db:
            db.execute("UPDATE memory_vectors SET available=0,state='pending',lease=NULL WHERE state='error'")


async def search(store,scope,subject,query,*,history=False,client=None):
    """Caller must authorize scope; SQL also bounds every candidate by scope AND subject."""
    from cognition import keywords
    cfg,_=read_config(store)
    query_vector=None; degraded=False
    if cfg.embedding_enabled:
        try:
            async with asyncio.timeout(cfg.query_timeout):
                query_vector=(await (client or Embeddings(cfg)).embed([query],timeout=cfg.query_timeout))[0]
        except Exception:
            degraded=True
    results=await asyncio.to_thread(_search_rank,store,scope,subject,query,history,query_vector,cfg)
    return {'items':results,'degraded':degraded}


def _search_rank(store,scope,subject,query,history,query_vector,cfg):
    from cognition import keywords
    terms=keywords(query)
    # Chinese bigrams improve exact phrasing recall without any model.
    import re
    for word in re.findall(r'[\u4e00-\u9fff]{2,}',query):
        terms.extend(word[i:i+2] for i in range(len(word)-1))
    terms=list(dict.fromkeys(terms))[:40]
    ranked={}; now=time.time()
    with closing(store.open_read()) as db:
        rows=db.execute('''SELECT n.*,l.status lifecycle_status,l.recalls,l.tier,l.expires,
            v.vector,v.dimension,v.generation,v.content_hash,v.state index_state
            FROM memory_nodes n JOIN memory_lifecycle l ON l.node_id=n.id
            LEFT JOIN memory_vectors v ON v.node_id=n.id WHERE n.scope=? AND n.subject=?
            AND (n.state='active' OR (? AND l.status='archived'))''',(scope,subject,history))
        # Streaming exact cosine scan avoids a new native dependency on Windows.
        for row in rows:
            node=dict(row); text=node['topic']+' '+node['value']
            hits=sum(t in text.casefold() for t in terms)
            lexical=min(1,hits/max(1,min(4,len(terms))))
            semantic=0.
            if (query_vector is not None and node['generation']==cfg.fingerprint() and node['index_state']=='ready'
                    and node['dimension']==len(query_vector) and node['content_hash']==text_hash(node)):
                vec=array('f'); vec.frombytes(node['vector'])
                cosine=sum(a*b for a,b in zip(vec,query_vector))
                if cosine>=cfg.min_similarity: semantic=max(0,min(1,cosine))
            if not (lexical or semantic): continue
            weights=cfg.keyword_weight+(cfg.semantic_weight if query_vector is not None else 0)
            relevance=(cfg.keyword_weight*lexical+cfg.semantic_weight*semantic)/max(weights,.0001)
            if relevance<cfg.min_score: continue
            quality=.8+.08*(node['importance']/5)+.08*node['confidence']+.04/(1+max(0,now-node['updated_at'])/86400/30)
            ranked[node['id']]={'node':node,'score':relevance*quality,'channels':[k for k,v in [('keyword',lexical),('semantic',semantic)] if v],
                'keyword':lexical,'semantic':semantic,'graph':0.}
        seeds=sorted(ranked.values(),key=lambda r:r['score'],reverse=True)[:cfg.candidates]
        ranked={r['node']['id']:r for r in seeds}
        if cfg.graph_weight>0:
            for seed in seeds[:5]:
                for row in db.execute('''SELECT n.* FROM memory_edges e JOIN memory_nodes n
                    ON n.id=CASE WHEN e.origin_id=? THEN e.target_id ELSE e.origin_id END
                    WHERE e.scope=? AND (e.origin_id=? OR e.target_id=?) AND n.scope=? AND n.subject=? AND n.state='active'
                    AND e.relation IN ('supports','related_to','belongs_to','participated_in') LIMIT ?''',
                    (seed['node']['id'],scope,seed['node']['id'],seed['node']['id'],scope,subject,cfg.candidates)):
                    strength=seed['score']*cfg.graph_weight/(cfg.graph_weight+.2)
                    if strength<cfg.min_score: continue
                    item=ranked.setdefault(row['id'],{'node':dict(row),'score':strength,'channels':[],'keyword':0.,'semantic':0.,'graph':0.})
                    item['graph']=max(item['graph'],strength)
                    if 'graph' not in item['channels']: item['channels'].append('graph')
                    item['score']=max(item['score'],strength)
    results=sorted(ranked.values(),key=lambda r:r['score'],reverse=True)[:cfg.final_count]
    for result in results:
        for key in ('vector','content_hash','generation','dimension','index_state'):
            result['node'].pop(key,None)
    return results
