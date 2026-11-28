import asyncio
from contextlib import closing
from dataclasses import replace
import json
import time

import pytest

from cognitive_store import scope_for
from llm import Completion, ModelError, Usage, parse_reply
from reply_config import read_config, save_config
from reply_planner import brief
from usage_ledger import stats
from test_qq_engine import Harness, raw


@pytest.fixture
def h(tmp_path): return Harness(tmp_path/'decision.db')


def configure(h,**fields):
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update=fields),revision)


def ordinary(h,number,text='大家在讨论天气',**kwargs):
    h.now+=1
    return h.ingest(number=number,mention=None,text=text,moment=h.now,**kwargs)


class Planner:
    def __init__(self,reply=False,target=None,fail=False): self.reply,self.target,self.fail=reply,target,fail; self.calls=[]
    async def complete(self,system,user,**kwargs):
        self.calls.append((system,json.loads(user),kwargs))
        if self.fail: raise ModelError('fixture_failure')
        result={'shouldReply':self.reply}
        if self.reply: result['targetMessageId']=self.target or json.loads(user)['messages'][0]['id']
        return Completion(json.dumps(result),Usage(70,8))


def test_minimal_silence_backoff_cap_isolation_restart_and_no_new_messages(h):
    h.model.output='{"shouldSend":false}'
    configure(h,max_interval=16)
    for n in range(1,29): ordinary(h,n)
    assert len(h.model.generations)==3  # 4 + 8 + 16
    assert not h.model.reviews and not h.rpc.sends
    for _ in range(4): h.now+=200; h.tick()
    assert len(h.model.generations)==3
    h.restart()
    for n in range(29,45): ordinary(h,n)
    assert len(h.model.generations)==4
    for n in range(45,49): ordinary(h,n,room=301)
    assert len(h.model.generations)==5
    assert parse_reply('{"shouldSend":false}',40).should_send is False


def test_name_interrupts_backoff_and_direct_never_calls_planner(h):
    configure(h,mode='balanced',early_name=True)
    planner=Planner(); h.engine.planner_model=planner
    for n in range(1,5): ordinary(h,n)
    assert len(planner.calls)==1 and not h.model.generations
    ordinary(h,5,'小鱼，最近有什么有趣的经历？')
    assert len(planner.calls)==2
    h.now+=1
    result=h.ingest(number=6,text='直接问你',moment=h.now)
    assert result['state']=='sent' and len(planner.calls)==2


def test_balanced_silence_skips_memory_reply_review_and_budget(h,monkeypatch):
    configure(h,mode='balanced')
    planner=Planner();h.engine.planner_model=planner
    async def forbidden(*args,**kwargs): raise AssertionError('full recall on silent plan')
    monkeypatch.setattr(h.engine.cognition,'recall_async',forbidden)
    for n in range(1,5): result=ordinary(h,n)
    assert result['detail']=='planner_declined'
    assert len(planner.calls)==1 and not h.model.generations and not h.model.reviews
    system,body,parameters=planner.calls[0]
    assert len((system+json.dumps(body,ensure_ascii=False,separators=(',',':'))).encode())+128<=800
    assert parameters['max_tokens']==64 and 'tools' not in body
    totals=stats(h.store,'',0,time.time()+10)['totals']['balanced']
    assert totals['planner_average_tokens']==78 and totals['silent_average_tokens']==78


def test_planner_selects_earlier_target_and_eco_rejects_foreign_target(h):
    configure(h,mode='balanced',planner_input_tokens=2000);h.engine.planner_model=Planner(True,'1')
    for n in range(1,5): result=ordinary(h,n)
    assert result['state']=='sent' and h.rpc.sends[-1]['message'][0]['data']['id']=='1'
    assert h.model.generations[-1][1]['当前消息']['正文']=='大家在讨论天气'
    configure(h,mode='eco')
    h.model.output=json.dumps({'text':'正常文本','emotion':'joy','intensity':1,'sticker':None,'shouldSend':True,'targetMessageId':'foreign'})
    for n in range(5,9): result=ordinary(h,n)
    assert result['detail']=='invalid_reply_target' and len(h.rpc.sends)==1


@pytest.mark.parametrize('fallback',['eco','skip'])
def test_planner_failure_does_not_count_as_silence(h,fallback):
    configure(h,mode='balanced',planner_failure=fallback)
    h.engine.planner_model=Planner(fail=True)
    for n in range(1,5): result=ordinary(h,n)
    assert len(h.engine.planner_model.calls)==1
    assert result['state']==('sent' if fallback=='eco' else 'skipped')
    with closing(h.store.open_read()) as db: assert db.execute('SELECT silence FROM reply_state').fetchone()[0]==0


def test_aggregation_max_wait_and_concurrent_ingress_survive_restart(h):
    configure(h,aggregate_seconds=3,max_wait=15)
    for n in range(1,7): ordinary(h,n)
    assert not h.model.generations
    h.now+=3;h.tick()
    assert len(h.model.generations)==1
    h.restart()
    ordinary(h,7);h.now+=15;h.tick()
    assert len(h.model.generations)==2
    h.now+=15;h.tick();assert len(h.model.generations)==2


def test_messages_arriving_during_generation_are_queued_and_stale_reply_is_blocked(h):
    original=h.model.complete;inserted=[]
    async def complete(*args,**kwargs):
        if not inserted:
            for n in range(5,9):
                h.now+=1;h.engine.record(raw(n,mention=None,text='完全换题：数据库迁移索引修复',moment=h.now));inserted.append(n)
        return await original(*args,**kwargs)
    h.model.complete=complete
    for n in range(1,5): result=ordinary(h,n,'篮球比赛十分精彩')
    assert result['detail']=='stale_decision' and not h.rpc.sends
    h.tick()
    assert len(h.model.generations)==2 and len(h.rpc.sends)==1


def test_semantic_batch_cache_and_failure_fallback(h,monkeypatch):
    from memory_engine import Embeddings
    configure(h,semantic=True,early_interest=True,semantic_interval=5)
    calls=[]
    async def vectors(self,texts,**kwargs): calls.append(texts); return [[1.,0.] for _ in texts]
    monkeypatch.setattr(Embeddings,'embed',vectors)
    ordinary(h,1,'没有直接关键词的兴趣表达')
    assert len(calls)==1 and len(h.model.generations)==1
    ordinary(h,2,'下一句话')
    assert len(calls)==1
    h.now+=5;ordinary(h,3,'下一段不同的新文字')
    assert len(calls)==2 and len(calls[1])<=2  # persona interest vector reused
    async def fail(*args,**kwargs): raise RuntimeError('offline')
    monkeypatch.setattr(Embeddings,'embed',fail)
    h.now+=5
    for n in range(4,8): ordinary(h,n,'普通对话')
    assert len(h.model.generations)>=3


def test_interrupted_planner_is_not_called_again(h):
    configure(h,mode='balanced',planner_failure='skip')
    for n in range(1,5): h.engine.record(raw(n,mention=None,moment=h.now))
    queued=asyncio.run(h.engine.dynamic.schedule())
    with h.store.transaction() as db: db.execute('UPDATE reply_rounds SET planner_started=1 WHERE job_id=?',(queued[0],))
    planner=Planner(True);h.engine.planner_model=planner;h.tick()
    assert not planner.calls and not h.rpc.sends


def test_disabled_memory_limits_planner_and_replyer_to_current_message(h):
    configure(h,mode='balanced',planner_input_tokens=2000)
    planner=Planner(True);h.engine.planner_model=planner
    h.engine.memory_enabled=lambda key:False
    for n in range(1,5): result=ordinary(h,n,'本轮当前话题')
    assert result['state']=='sent'
    assert [m['id'] for m in planner.calls[0][1]['messages']]==['4']
    assert [m['id'] for m in h.model.generations[-1][1]['候选回复消息']]==['4']
    assert h.model.generations[-1][1]['最近对话']==[]
