"""Bounded ordinary-chat batches; original Outbox remains the only sending authority."""
import asyncio
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import math
import re

from cognitive_store import scope_for
from events import Event
from policy import DIRECT, PROACTIVE, decide
from reply_config import read_config
from usage_ledger import meter, write, emit


def terms(text):
    result=set(re.findall(r'[a-z][a-z0-9_]{2,}',text.lower()))
    for word in re.findall(r'[\u4e00-\u9fff]{2,}',text):
        result.update(word[i:i+2] for i in range(len(word)-1))
    return result-{'这个','那个','什么','怎么','我们','你们','一个','可以','不是','没有','就是'}


def overlap(left,right):
    a,b=terms(left),terms(right)
    return len(a&b)/max(1,len(a|b))


def interest_summary(store,settings,scope,allow_memory=True):
    # Slicing is deterministic and cached by the embedding layer; no summarization model.
    text=settings.persona.system_prompt[:180]+' '+settings.persona.style[:100]
    if allow_memory:
        with closing(store.open_read()) as db:
            rows=db.execute("""SELECT n.value FROM memory_self_state s JOIN memory_nodes n
                ON n.scope=s.scope AND CAST(n.id AS TEXT)=s.value WHERE s.scope=?
                AND s.key LIKE 'grounded_view:%' AND n.state='active' ORDER BY n.updated_at DESC LIMIT 3""",(scope,))
            text+=' '+' '.join(r[0][:100] for r in rows)
    return text[:500]


class DynamicDecision:
    def __init__(self,store,settings,mode,clock):
        self.store,self.settings,self.mode,self.clock=store,settings,mode,clock
        self.vector_cache={}; self.semantic_at={}

    def accepted(self,db,event,job_id,cfg,now,blocked=False,proactive=True):
        if event.channel_kind!='group': return
        scope=scope_for(event,self.mode)
        db.execute('INSERT OR IGNORE INTO reply_state(scope) VALUES(?)',(scope,))
        write(db,scope,self.mode,cfg.mode,'received',job_id=job_id,at=now)
        decision=decide(event,0,1)
        if blocked:
            write(db,scope,self.mode,cfg.mode,'rule_skipped',job_id=job_id,at=now); return
        if decision.reason==DIRECT:
            db.execute('UPDATE reply_state SET silence=0 WHERE scope=?',(scope,)); return
        if not proactive:
            write(db,scope,self.mode,cfg.mode,'rule_skipped',job_id=job_id,at=now); return
        if decision.reason==PROACTIVE:
            db.execute('INSERT OR IGNORE INTO reply_pending VALUES(?,?,?)',(job_id,scope,now))
            db.execute("UPDATE outbox SET trigger='counting',detail='counting',state='skipped' WHERE id=? AND trigger<>'verify_quote'",(job_id,))
        else:
            write(db,scope,self.mode,cfg.mode,'rule_skipped',job_id=job_id,at=now)

    def state(self,scope):
        with closing(self.store.open_read()) as db:
            row=db.execute('SELECT * FROM reply_state WHERE scope=?',(scope,)).fetchone()
        return dict(row) if row else {'silence':0,'last_reply':0,'last_sender':'','topic':''}

    def threshold(self,cfg,state):
        base=self.settings.chat.participation_every
        exponent=min(state['silence'],32) if cfg.backoff else 0
        return max(base,min(max(base,cfg.max_interval),math.ceil(base*cfg.multiplier**exponent)))

    async def semantic(self,scope,text,interests,previous,cfg):
        from memory_engine import Embeddings, read_config as embedding_config
        now=self.clock()
        if not cfg.semantic: return {}
        if now-self.semantic_at.get(scope,-float('inf'))<cfg.semantic_interval:
            return {}  # Cached vectors do not authorize a different batch to use an old signal.
        self.semantic_at[scope]=now
        config,_=embedding_config(self.store)
        generation=config.fingerprint()
        texts=[interests,text]+([previous] if previous else [])
        keys=[(scope,generation,hashlib.sha256(t.encode()).hexdigest()) for t in texts]
        missing=list(dict.fromkeys(t for t,k in zip(texts,keys) if k not in self.vector_cache))
        try:
            if missing:
                with meter(self.store,scope,self.mode,cfg.mode,stage='embedding_trigger'):
                    async with asyncio.timeout(cfg.semantic_timeout):
                        vectors=await Embeddings(config).embed(missing,timeout=cfg.semantic_timeout)
                for value,vector in zip(missing,vectors):
                    self.vector_cache[(scope,generation,hashlib.sha256(value.encode()).hexdigest())]=vector
            vectors=[self.vector_cache[k] for k in keys]
            if len({len(v) for v in vectors})!=1: return {}
            cosine=lambda a,b:sum(x*y for x,y in zip(a,b))
            result={'interest':cosine(vectors[0],vectors[1])>=cfg.interest_threshold}
            if previous:
                similarity=cosine(vectors[1],vectors[2])
                result.update(topic=similarity<cfg.topic_threshold,continuation=similarity>=cfg.continuation_threshold)
            if len(self.vector_cache)>128: self.vector_cache.clear()
            if len(self.semantic_at)>128: self.semantic_at={scope:now}
            return result
        except Exception:
            with self.store.transaction() as db:
                write(db,scope,self.mode,cfg.mode,'embedding_fallback',at=now)
            return {}

    async def schedule(self,channel=None,permitted=lambda key:True,memory_permitted=lambda key:True):
        cfg,_=read_config(self.store); now=self.clock()
        with closing(self.store.open_read()) as db:
            scopes=[r[0] for r in db.execute('SELECT DISTINCT scope FROM reply_pending')]
        queued=[]
        for scope in scopes:
            parts=json.loads(scope)
            if parts[3]!=self.mode or (channel and tuple(parts[:3])!=channel): continue
            key=tuple(parts[:3])
            if not permitted(key): continue
            with closing(self.store.open_read()) as db:
                rows=[dict(r) for r in db.execute('''SELECT p.*,o.message_id,e.payload FROM reply_pending p
                    JOIN outbox o ON o.id=p.job_id JOIN events e USING(platform,channel_kind,channel_id,message_id)
                    WHERE p.scope=? ORDER BY p.job_id''',(scope,))]
            if not rows: continue
            # Drop only expired decision candidates; original messages and Outbox history remain.
            expired=[r['job_id'] for r in rows if now-Event(**json.loads(r['payload'])).timestamp>self.settings.chat.max_event_age]
            if expired:
                with self.store.transaction() as db:
                    db.executemany('DELETE FROM reply_pending WHERE job_id=?',[(x,) for x in expired])
                rows=[r for r in rows if r['job_id'] not in expired]
            if not rows: continue
            with closing(self.store.open_read()) as db:
                busy=db.execute("""SELECT 1 FROM outbox WHERE platform=? AND channel_kind=? AND channel_id=?
                    AND mode=? AND state IN ('evaluate','evaluating','pending','sending','uncertain') LIMIT 1""",(*key,self.mode)).fetchone()
            if busy: continue
            state=self.state(scope); event=Event(**json.loads(rows[-1]['payload']))
            memory_allowed=event.memory_allowed is True and memory_permitted(key)
            text=(' '.join(json.loads(r['payload'])['text'] for r in rows[-3:]) if memory_allowed else event.text)[:400]
            interests=interest_summary(self.store,self.settings,scope,memory_allowed)
            signal=await self.semantic(scope,text,interests,state['topic'] if memory_allowed else '',cfg)
            name=any(alias and alias in text for alias in [self.settings.persona.name,*self.settings.persona.aliases])
            continuation=(now-state['last_reply']<=cfg.conversation_ttl and event.sender_id==state['last_sender']
                          and (signal.get('continuation') or overlap(text,state['topic'])>=.15))
            changed=bool(state['topic'] and (signal.get('topic') or (len(terms(text))>=3 and overlap(text,state['topic'])==0)))
            interesting=signal.get('interest') or len(terms(text)&terms(interests))>=3
            early=next((kind for kind,enabled,hit in [('name',cfg.early_name,name),('continuation',cfg.early_continuation,continuation),
                ('topic',cfg.early_topic,changed),('interest',cfg.early_interest,interesting)] if enabled and hit),'')
            waited=now-rows[0]['created_at']>=cfg.max_wait
            due=early or waited or len(rows)>=self.threshold(cfg,state)
            if not due or (now-rows[-1]['created_at']<cfg.aggregate_seconds and not waited): continue
            identifier=rows[-1]['job_id']; candidates=[r['job_id'] for r in rows[-20:]]
            with self.store.transaction() as db:
                # Recheck after embedding I/O and atomically own this exact batch.
                if db.execute('SELECT 1 FROM reply_pending WHERE job_id=?',(identifier,)).fetchone() is None: continue
                if db.execute("SELECT 1 FROM outbox WHERE platform=? AND channel_kind=? AND channel_id=? AND mode=? AND state IN ('evaluate','evaluating','pending','sending','uncertain')",(*key,self.mode)).fetchone(): continue
                db.execute("UPDATE outbox SET state='evaluate',trigger='proactive',detail='' WHERE id=? AND state='skipped'",(identifier,))
                db.execute('INSERT INTO reply_rounds(job_id,scope,decision_mode,candidates,started,reason) VALUES(?,?,?,?,?,?)',
                    (identifier,scope,cfg.mode,json.dumps(candidates),now,early or ('max_wait' if waited else 'count')))
                db.execute('DELETE FROM reply_pending WHERE scope=? AND job_id<=?',(scope,identifier))
                db.execute('UPDATE reply_state SET last_decision=?,silence=CASE WHEN ? THEN 0 ELSE silence END WHERE scope=?',(now,bool(early),scope))
                write(db,scope,self.mode,cfg.mode,'triggered',job_id=identifier,at=now)
                # Count messages batched away rather than claiming every deferred message is permanently skipped.
                for r in rows[:-1]: write(db,scope,self.mode,cfg.mode,'rule_skipped',job_id=r['job_id'],at=now)
            queued.append(identifier)
        return queued

    def candidates(self,job_id):
        with closing(self.store.open_read()) as db:
            row=db.execute('SELECT candidates FROM reply_rounds WHERE job_id=?',(job_id,)).fetchone()
            if not row: return []
            result=[]
            for identifier in json.loads(row[0]):
                source=db.execute('SELECT e.payload FROM outbox o JOIN events e USING(platform,channel_kind,channel_id,message_id) WHERE o.id=?',(identifier,)).fetchone()
                if source: result.append(Event(**json.loads(source[0])))
            return result

    def stale(self,job_id,event):
        now=self.clock()
        if now-event.timestamp>self.settings.chat.max_event_age: return True
        with closing(self.store.open_read()) as db:
            newer=db.execute('''SELECT o.trigger,e.payload FROM outbox o JOIN events e USING(platform,channel_kind,channel_id,message_id)
                WHERE o.id>? AND o.platform=? AND o.channel_kind=? AND o.channel_id=? AND o.mode=? ORDER BY o.id DESC LIMIT 6''',
                (job_id,*event.channel_key,self.mode)).fetchall()
        if any(r['trigger'] in {DIRECT,'verify_quote'} for r in newer): return True
        text=' '.join(json.loads(r['payload'])['text'] for r in newer)
        return len(newer)>=3 and len(terms(text))>=5 and overlap(text,event.text)==0

    def finish(self,job_id,detail,intent,event):
        cfg,_=read_config(self.store); scope=scope_for(event,self.mode); now=self.clock()
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM reply_rounds WHERE job_id=?',(job_id,)).fetchone()
            if row and row['finished'] is None:
                silent=detail in {'model_declined','planner_declined'}
                outcome='reply' if intent else 'silent' if silent else 'error_or_skip'
                db.execute('UPDATE reply_rounds SET finished=?,outcome=?,target=? WHERE job_id=?',(now,outcome,intent.reply_to if intent else None,job_id))
                if silent: db.execute('UPDATE reply_state SET silence=min(32,silence+1) WHERE scope=?',(scope,))
                db.execute('UPDATE reply_state SET topic=? WHERE scope=?',(event.text[:400] if event.memory_allowed is True else '',scope))
                write(db,scope,self.mode,row['decision_mode'],outcome,job_id=job_id,at=now)

    def sent(self,job_id,event):
        cfg,_=read_config(self.store); scope=scope_for(event,self.mode)
        with self.store.transaction() as db:
            round_mode=db.execute('SELECT decision_mode FROM reply_rounds WHERE job_id=?',(job_id,)).fetchone()
            db.execute('UPDATE reply_state SET silence=0,last_reply=?,last_sender=?,topic=? WHERE scope=?',
                       (self.clock(),event.sender_id,event.text[:400] if event.memory_allowed is True else '',scope))
            if not db.execute("SELECT 1 FROM model_usage WHERE job_id=? AND event='sent'",(job_id,)).fetchone():
                write(db,scope,self.mode,round_mode[0] if round_mode else cfg.mode,'sent',job_id=job_id,at=self.clock())
