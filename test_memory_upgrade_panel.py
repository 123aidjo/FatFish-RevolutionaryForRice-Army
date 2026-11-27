import asyncio
from contextlib import closing
import shutil

from fastapi.testclient import TestClient
import pytest

from cognitive_store import CognitiveStore, SYSTEM_SCOPE, scope_for
from memory_engine import Embeddings, read_config, save_config, VectorIndex
from panel import create_app
from settings import PROJECT_ROOT
from store import Store
from test_memory_engine import add, Semantic
from test_cognition import fixture, learn


def test_upgrade_panel_config_secret_permissions_lifecycle(tmp_path,monkeypatch):
    project=tmp_path/'project'; project.mkdir()
    for name in ('platforms.toml','persona.toml'): shutil.copyfile(PROJECT_ROOT/name,project/name)
    store=Store(tmp_path/'memory.db'); store.initialize()
    token='fixture-memory-admin'; (tmp_path/'panel.token').write_text(token)
    node=add(store,'晚上喝咖啡后经常睡不着')
    auth={'Authorization':'Bearer '+token,'Origin':'http://127.0.0.1:8000'}
    scope={'platform':'system','kind':'role','channel':'fishbot-v2','mode':'live','dataset':'real'}
    async def embed(self,texts,**kwargs): return [[1.,0.,0.] for _ in texts]
    monkeypatch.setattr(Embeddings,'embed',embed)
    with TestClient(create_app(store,config_dir=project),base_url='http://127.0.0.1:8000',client=('127.0.0.1',55000)) as client:
        assert client.get('/api/cognition/engine').status_code==401
        initial=client.get('/api/cognition/engine',headers=auth).json()
        config={**initial['config'],'api_key':'fixture-private-key'}
        payload={'revision':initial['revision'],'config':config}
        assert client.put('/api/cognition/engine',headers=auth,json=payload).status_code==200
        assert client.put('/api/cognition/engine',headers=auth,json=payload).status_code==409
        current=client.get('/api/cognition/engine',headers=auth)
        assert 'fixture-private-key' not in current.text and current.json()['key_configured']
        assert client.post('/api/cognition/engine/test',headers=auth).json()['dimension']==3
        query={'scope':scope,'subject':'owner','text':'咖啡','history':False}
        assert client.post('/api/cognition/engine/search',headers=auth,json=query).json()['items']
        forbidden={**query,'scope':{**scope,'platform':'qq','kind':'dm','channel':'private'}}
        assert client.post('/api/cognition/engine/search',headers=auth,json=forbidden).status_code==403
        version=CognitiveStore(store).get(SYSTEM_SCOPE,node)['updated_at']
        change={'scope':scope,'updated_at':version,'action':'archive'}
        assert client.put('/api/cognition/engine/nodes/'+str(node),headers=auth,json=change).status_code==200
        assert not client.post('/api/cognition/engine/search',headers=auth,json=query).json()['items']
        query['history']=True
        assert client.post('/api/cognition/engine/search',headers=auth,json=query).json()['items']
        assert client.put('/api/cognition/engine/nodes/'+str(node),headers=auth,json=change).status_code==409
        path='/api/cognition/engine/nodes?platform=system&kind=role&channel=fishbot-v2&mode=live&status=archived'
        assert client.get(path,headers=auth).json()['items'][0]['status']=='archived'
        assert client.post('/api/cognition/engine/retry',headers=auth).status_code==200
        assert client.post('/api/cognition/engine/rebuild',headers=auth).status_code==200


def test_actual_dispatcher_injects_semantic_memory_and_survives_outage(fixture,monkeypatch):
    h,c=fixture
    event=learn(h,c,1,'我喜欢咖啡。')
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update={'embedding_enabled':True,'token_budget':1200}),revision)
    source=Semantic()
    asyncio.run(VectorIndex(h.store,source).batch())
    async def embed(self,texts,**kwargs): return await source.embed(texts,**kwargs)
    monkeypatch.setattr(Embeddings,'embed',embed)
    h.now+=10
    result=h.ingest(number=2,text='小鱼，我晚上应该避免喝什么东西？',moment=h.now)
    assert result['state']=='sent'
    _,payload=h.model.generations[-1]
    assert '咖啡' in str(payload['获准结构化记忆'])
    source.fail=True
    h.now+=30
    result=h.ingest(number=3,text='小鱼，记得我喜欢咖啡吗？',moment=h.now)
    assert result['state']=='sent'


def test_invalid_model_result_preserves_raw_message(fixture):
    from memory_growth import Extractor
    from test_qq_engine import raw
    h,_=fixture
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update={'llm_extraction':True}),revision)
    h.engine.record(raw(1,text='我喜欢咖啡。',moment=h.now))
    class Invalid:
        async def json(self,*args): return {'items':[{'job_id':1,'kind':'fact','quote':'没有出现的证据'}]}
    asyncio.run(Extractor(h.store,Invalid()).batch('simulation',lambda e:True))
    with closing(h.store.open_read()) as db:
        assert db.execute('SELECT count(*) FROM memory_nodes').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==1


def test_forgetting_while_embedding_query_is_in_flight(fixture,monkeypatch):
    from dataclasses import replace
    h,c=fixture
    event=learn(h,c,1,'我喜欢咖啡。')
    scope=scope_for(event,'simulation')
    identifier=CognitiveStore(h.store).list(scope)[0]['id']
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update={'embedding_enabled':True}),revision)
    asyncio.run(VectorIndex(h.store,Semantic()).batch())
    async def forget_then_embed(self,texts,**kwargs):
        CognitiveStore(h.store).delete(scope,identifier)
        return [[1.,0.,0.]]
    monkeypatch.setattr(Embeddings,'embed',forget_then_embed)
    result=asyncio.run(c.recall_async(replace(event,text='晚上应该避免喝什么？',memory_allowed=True),'simulation',allowed=True))
    assert result=={}


def test_extraction_checks_permission_again_after_model(fixture):
    from memory_growth import Extractor
    from test_memory_growth import ExtractModel
    from test_qq_engine import raw
    h,_=fixture; permission=[True]
    cfg,revision=read_config(h.store)
    save_config(h.store,cfg.model_copy(update={'llm_extraction':True}),revision)
    h.engine.record(raw(1,text='我喜欢咖啡。',moment=h.now))
    class Revoke(ExtractModel):
        async def json(self,instruction,payload):
            permission[0]=False
            return await super().json(instruction,payload)
    asyncio.run(Extractor(h.store,Revoke()).batch('simulation',lambda e:permission[0]))
    with closing(h.store.open_read()) as db:
        assert db.execute('SELECT count(*) FROM memory_nodes').fetchone()[0]==0
