import asyncio
from contextlib import closing
import json

import pytest

from cognitive_store import CognitiveStore, SYSTEM_SCOPE
from memory_engine import EngineError, EngineConfig, Tier, read_config, save_config, search
from memory_growth import Extractor, Growth
from test_memory_engine import memory, add, Semantic
from test_cognition import fixture
from test_qq_engine import raw


class ExtractModel:
    async def json(self,instruction,payload):
        items=[]
        for event in payload['messages']:
            prior=event['existing']; changed='现在' in event['text']
            items.append({'job_id':event['job_id'],'kind':'preference','topic':'咖啡偏好','value':event['text'],
                'quote':event['text'],'importance':4,'epistemic':'self_report',
                'operation':'revise' if changed else 'confirm' if prior else 'new',
                'target_id':prior[0]['id'] if prior else None})
        return {'items':items}


def test_llm_extract_deduplicate_revision_and_unrelated_events(fixture):
    h,_=fixture
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update={'llm_extraction':True}),revision)
    extractor=Extractor(h.store,ExtractModel())
    for number,text in enumerate(['我喜欢美式咖啡。','我喜欢美式咖啡。','我现在更喜欢拿铁。'],1):
        h.now+=10
        h.engine.record(raw(number,text=text,moment=h.now))
        assert asyncio.run(extractor.batch('simulation',lambda e:True))
    with closing(h.store.open_read()) as db:
        nodes=[dict(r) for r in db.execute('SELECT * FROM memory_nodes')]
        assert len(nodes)==2 and {n['state'] for n in nodes}=={'active','superseded'}
        assert '拿铁' in next(n for n in nodes if n['state']=='active')['value']
        assert db.execute("SELECT count(*) FROM memory_edges WHERE relation='supersedes'").fetchone()[0]==1


class Evaluator:
    def __init__(self): self.action='promote'; self.tier='high'; self.calls=0; self.fail=False
    async def json(self,instruction,payload):
        self.calls+=1
        if self.fail: raise RuntimeError('fixture-failure')
        if 'node' in payload:
            return {'action':self.action,'tier':self.tier,'quote':payload['node']['value'],'reason':'多次真实证据'}
        return {'summary':'用户可能对晚间咖啡因较敏感。','evidence':[{'id':r['id'],'quote':r['value']} for r in payload['memories']]}


def configure(store):
    cfg,revision=read_config(store)
    tiers=[Tier(id='low',name='短',order=0,retention_days=1),Tier(id='middle',name='中',order=1,retention_days=7),
           Tier(id='high',name='永久',order=3,min_importance=4)]
    save_config(store,cfg.model_copy(update={'tiers':tiers,'initial_tier':'low','growth_enabled':True,'allow_soft_delete':True}),revision)


def test_expiry_review_cross_tier_archive_restore_and_delete_tier(memory):
    configure(memory); node=add(memory,'晚上喝咖啡后经常睡不着')
    with memory.transaction() as db:
        db.execute("UPDATE memory_lifecycle SET tier='low',expires=10 WHERE node_id=?",(node,))
    model=Evaluator(); clock=[200.]
    growth=Growth(memory,model,lambda:clock[0])
    assert growth.schedule()==1
    assert CognitiveStore(memory).get(SYSTEM_SCOPE,node)['state']=='active'
    asyncio.run(growth.batch())
    current=CognitiveStore(memory).get(SYSTEM_SCOPE,node)
    assert current['lifecycle']['tier']=='high' and current['lifecycle']['expires'] is None
    clock[0]+=1
    growth.transition(SYSTEM_SCOPE,node,current['updated_at'],'demote','low')
    clock[0]+=1
    current=CognitiveStore(memory).get(SYSTEM_SCOPE,node)
    growth.transition(SYSTEM_SCOPE,node,current['updated_at'],'archive')
    assert not asyncio.run(search(memory,SYSTEM_SCOPE,'owner','咖啡',client=Semantic()))['items']
    assert asyncio.run(search(memory,SYSTEM_SCOPE,'owner','咖啡',history=True,client=Semantic()))['items']
    current=CognitiveStore(memory).get(SYSTEM_SCOPE,node); clock[0]+=1
    growth.transition(SYSTEM_SCOPE,node,current['updated_at'],'restore','low')
    cfg,revision=read_config(memory)
    save_config(memory,cfg.model_copy(update={'tiers':[cfg.tiers[2]],'initial_tier':'high'}),revision)
    current=CognitiveStore(memory).get(SYSTEM_SCOPE,node)
    assert current['lifecycle']['tier']=='high' and current['state']=='active'
    assert len(current['revisions'])>=5
    CognitiveStore(memory).delete(SYSTEM_SCOPE,node)
    with memory.transaction() as db:
        result=CognitiveStore(memory)._put(db,SYSTEM_SCOPE,'owner',CognitiveStore.slot('owner','fact','晚上喝咖啡后经常睡不着'),
            'fact','习惯','晚上喝咖啡后经常睡不着',500,4,.9,'message','new-source')
    assert result is None


def test_tasks_retry_after_restart_and_reflection_has_grounded_sources(memory):
    configure(memory)
    first=add(memory,'晚上喝咖啡后睡不着。'); second=add(memory,'再次在晚上喝含咖啡因饮料后失眠。')
    model=Evaluator(); model.fail=True
    growth=Growth(memory,model,clock=lambda:1000)
    growth.schedule(force=True,kind='reflection')
    asyncio.run(growth.batch('reflection'))
    with closing(memory.open_read()) as db:
        assert db.execute("SELECT count(*) FROM memory_tasks WHERE state='error'").fetchone()[0]==2
    model.fail=False
    resumed=Growth(memory,model,clock=lambda:2000)
    asyncio.run(resumed.batch('reflection'))
    reflected=CognitiveStore(memory).list(SYSTEM_SCOPE,kind='reflection')
    assert len(reflected)==1 and reflected[0]['origin']=='inference' and reflected[0]['confidence']==.65
    detail=CognitiveStore(memory).get(SYSTEM_SCOPE,reflected[0]['id'])
    assert {e['origin_id'] for e in detail['edges'] if e['relation']=='supports'}=={first,second}
    views=CognitiveStore(memory).self_state(SYSTEM_SCOPE)['grounded_views']
    assert [v['id'] for v in views]==[reflected[0]['id']]
    assert not CognitiveStore(memory).self_state('["qq","dm","unrelated","live"]')['grounded_views']
    calls=model.calls
    resumed.schedule(force=True,kind='reflection'); asyncio.run(resumed.batch('reflection'))
    assert model.calls==calls
    CognitiveStore(memory).delete(SYSTEM_SCOPE,reflected[0]['id'])
    assert not CognitiveStore(memory).self_state(SYSTEM_SCOPE)['grounded_views']
    resumed.schedule(force=True,kind='reflection'); asyncio.run(resumed.batch('reflection'))
    assert not CognitiveStore(memory).list(SYSTEM_SCOPE,kind='reflection')


def test_llm_batches_do_not_mix_people_or_conversations(fixture):
    h,_=fixture
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update={'llm_extraction':True}),revision)
    for number,author,room in [(1,200,300),(2,201,300),(3,200,301),(4,200,300)]:
        h.engine.record(raw(number,text='我喜欢咖啡。',author=author,room=room,moment=h.now))
    class Capture(ExtractModel):
        def __init__(self): self.batches=[]
        async def json(self,instruction,payload):
            self.batches.append([m['job_id'] for m in payload['messages']])
            return await super().json(instruction,payload)
    model=Capture(); worker=Extractor(h.store,model)
    for _ in range(3): assert asyncio.run(worker.batch('simulation',lambda e:True))
    assert model.batches==[[1,4],[2],[3]]
    assert not asyncio.run(worker.batch('simulation',lambda e:True))


def test_evaluator_cannot_bypass_promotion_or_restore_archived(memory):
    configure(memory); identifier=add(memory,'晚上喝咖啡后经常睡不着')
    with memory.transaction() as db:
        db.execute("UPDATE memory_lifecycle SET tier='low' WHERE node_id=?",(identifier,))
    model=Evaluator(); model.action='retain'
    growth=Growth(memory,model,clock=lambda:2000)
    node=CognitiveStore(memory).get(SYSTEM_SCOPE,identifier)
    with pytest.raises(EngineError,match='retain_cannot_change_tier'):
        growth.transition(SYSTEM_SCOPE,identifier,node['updated_at'],'retain','high')
    growth.transition(SYSTEM_SCOPE,identifier,node['updated_at'],'archive')
    node=CognitiveStore(memory).get(SYSTEM_SCOPE,identifier)
    growth.transition(SYSTEM_SCOPE,identifier,node['updated_at'],'retain','low')
    assert CognitiveStore(memory).get(SYSTEM_SCOPE,identifier)['lifecycle']['status']=='archived'
