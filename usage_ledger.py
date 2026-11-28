"""Content-free stage accounting. Unknown consumption is not labelled as a real zero."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
import time

_meter=ContextVar('reply_usage_meter',default=None)


@contextmanager
def meter(store,scope,mode,decision_mode,job_id=None,stage='replyer'):
    token=_meter.set((store,scope,mode,decision_mode,job_id,stage))
    try: yield
    finally: _meter.reset(token)


@contextmanager
def stage(name):
    current=_meter.get()
    token=_meter.set((*current[:-1],name) if current else None)
    try: yield
    finally: _meter.reset(token)


def write(db,scope,mode,decision_mode,event,*,job_id=None,stage='rules',calls=0,prompt=0,completion=0,
          estimated=False,unknown=False,latency=0,at=None):
    db.execute('''INSERT INTO model_usage(at,scope,mode,decision_mode,job_id,stage,event,calls,prompt,completion,estimated,unknown,latency)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(time.time() if at is None else at,scope,mode,decision_mode,job_id,stage,event,calls,prompt,completion,int(estimated),int(unknown),latency))


def emit(event='completed',*,stage_name=None,**fields):
    current=_meter.get()
    if current:
        store,scope,mode,decision_mode,job_id,name=current
        if stage_name=='embedding' and (name.startswith('memory_') or name.startswith('embedding_')):
            stage_name=name
        with store.transaction() as db:
            write(db,scope,mode,decision_mode,event,job_id=job_id,stage=stage_name or name,**fields)


def stats(store,scope,since,until):
    where='at>=? AND at<=?'; args=[since,until]
    if scope: where+=' AND scope=?'; args.append(scope)
    with closing(store.open_read()) as db:
        rows=[dict(r) for r in db.execute(f'''SELECT decision_mode,stage,event,count(*) records,sum(calls) calls,
            sum(prompt) prompt,sum(completion) completion,sum(estimated) estimated_requests,
            sum(unknown) unknown_requests,sum(latency) latency FROM model_usage WHERE {where}
            GROUP BY decision_mode,stage,event''',args)]
        hours=[dict(r) for r in db.execute(f'''SELECT CAST(at/3600 AS INTEGER)*3600 hour,
            decision_mode,sum(prompt+completion) tokens FROM model_usage WHERE {where}
            GROUP BY hour,decision_mode ORDER BY hour''',args)]
        rounds=[dict(r) for r in db.execute('''SELECT r.decision_mode,r.outcome,r.job_id,
            coalesce(sum(u.prompt+u.completion),0) tokens FROM reply_rounds r
            LEFT JOIN model_usage u ON u.job_id=r.job_id
            WHERE r.started>=? AND r.started<=? AND (?='' OR r.scope=?)
            GROUP BY r.job_id''',(since,until,scope,scope))]
    totals={}
    for mode in ('eco','balanced'):
        selected=[r for r in rows if r['decision_mode']==mode]
        chat=[r for r in selected if not r['stage'].startswith('memory_')]
        tokens=sum(r['prompt']+r['completion'] for r in chat)
        planners=[r for r in chat if r['stage']=='planner']
        calls=sum(r['calls'] for r in planners)
        sent=sum(r['records'] for r in chat if r['event']=='sent')
        silent=[r for r in rounds if r['decision_mode']==mode and r['outcome']=='silent']
        totals[mode]={'tokens':tokens,'calls':sum(r['calls'] for r in chat),'sent':sent,
            'planner_average_tokens':sum(r['prompt']+r['completion'] for r in planners)/calls if calls else None,
            'silent_average_tokens':sum(r['tokens'] for r in silent)/len(silent) if silent else None,
            'tokens_per_sent':tokens/sent if sent else None,
            'unknown_requests':sum(r['unknown_requests'] for r in chat),
            'estimated_requests':sum(r['estimated_requests'] for r in chat)}
    return {'groups':rows,'hourly':hours,'totals':totals,'since':since,'until':until}
