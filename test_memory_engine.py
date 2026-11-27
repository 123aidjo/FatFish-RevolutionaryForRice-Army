import asyncio
from contextlib import closing
import json
import httpx
import pytest

from cognitive_store import CognitiveStore, SYSTEM_SCOPE
from memory_engine import EngineConfig, EngineError, Embeddings, VectorIndex, read_config, save_config, search
from store import Store


@pytest.fixture
def memory(tmp_path):
    store=Store(tmp_path/'memory.db'); store.initialize()
    cfg,revision=read_config(store)
    save_config(store,cfg.model_copy(update={'embedding_enabled':True}),revision)
    return store


def add(store,value,subject='owner',scope=SYSTEM_SCOPE):
    data=CognitiveStore(store)
    with store.transaction() as db:
        return data._put(db,scope,subject,data.slot(subject,'fact',value),'fact','习惯',value,100,4,.9,'message',value)


class Semantic:
    def __init__(self): self.fail=False; self.calls=0
    async def embed(self,texts,**kwargs):
        self.calls+=1
        if self.fail: raise EngineError('embedding_unavailable_or_invalid')
        return [[1.,0.,0.] if ('咖啡' in text or '晚上应该避免' in text) else [0.,1.,0.] for text in texts]


def test_semantic_scope_incremental_edit_archive_recovery(memory):
    first=add(memory,'晚上喝咖啡后经常睡不着')
    add(memory,'晚上喝咖啡后失眠，私聊秘密',scope='["qq","dm","private","live"]')
    source=Semantic(); index=VectorIndex(memory,source)
    assert asyncio.run(index.batch())==2
    assert asyncio.run(index.batch())==0
    result=asyncio.run(search(memory,SYSTEM_SCOPE,'owner','我晚上应该避免喝什么东西',client=source))
    assert [r['node']['id'] for r in result['items']]==[first]
    assert 'semantic' in result['items'][0]['channels']
    assert not asyncio.run(search(memory,SYSTEM_SCOPE,'owner','月球质量',client=source))['items']
    assert not asyncio.run(search(memory,SYSTEM_SCOPE,'other','晚上应该避免',client=source))['items']
    with memory.transaction() as db:
        db.execute("UPDATE memory_nodes SET value='新的天气观察',updated_at=200 WHERE id=?",(first,))
    assert asyncio.run(index.batch())==1
    with memory.transaction() as db: db.execute("UPDATE memory_nodes SET state='inactive',updated_at=201 WHERE id=?",(first,))
    assert not asyncio.run(search(memory,SYSTEM_SCOPE,'owner','天气',client=source))['items']
    with memory.transaction() as db: db.execute("UPDATE memory_nodes SET state='active',updated_at=202 WHERE id=?",(first,))
    assert asyncio.run(search(memory,SYSTEM_SCOPE,'owner','天气',client=source))['items']
    CognitiveStore(memory).delete(SYSTEM_SCOPE,first)
    with closing(memory.open_read()) as db: assert db.execute('SELECT 1 FROM memory_vectors WHERE node_id=?',(first,)).fetchone() is None


def test_embedding_failure_fallback_and_retry(memory):
    add(memory,'主人喜欢咖啡')
    source=Semantic(); source.fail=True
    index=VectorIndex(memory,source)
    asyncio.run(index.batch())
    assert index.status()['states']['error']==1
    result=asyncio.run(search(memory,SYSTEM_SCOPE,'owner','咖啡',client=source))
    assert result['degraded'] and result['items']
    source.fail=False; index.retry(); asyncio.run(index.batch())
    assert index.status()['current_ready']==1
    index.rebuild()
    assert index.status()['current_ready']==0
    asyncio.run(index.batch()); assert index.status()['current_ready']==1


def test_embedding_protocol_dimensions_and_invalid_values():
    cfg=EngineConfig(dimensions=3)
    def respond(req):
        assert req.url.path=='/v1/embeddings'
        assert json.loads(req.content)['model']=='qwen3-embedding:4b'
        return httpx.Response(200,json={'data':[{'index':0,'embedding':[1,0,0]}]})
    assert asyncio.run(Embeddings(cfg,httpx.MockTransport(respond)).embed(['test']))==[[1.,0.,0.]]
    with pytest.raises(EngineError,match='dimension'):
        asyncio.run(Embeddings(cfg.model_copy(update={'dimensions':4}),httpx.MockTransport(respond)).embed(['test']))


def test_migration_v5_preserves_nodes_and_is_repeatable(tmp_path):
    import sqlite3
    from cognitive_schema import SCHEMA
    path=tmp_path/'old.db'; db=sqlite3.connect(path)
    db.execute('CREATE TABLE events(platform TEXT,channel_kind TEXT,channel_id TEXT,message_id TEXT,payload TEXT,PRIMARY KEY(platform,channel_kind,channel_id,message_id))')
    db.execute('CREATE TABLE panel_state(key TEXT PRIMARY KEY,value TEXT)')
    for sql in SCHEMA: db.execute(sql)
    db.execute("INSERT INTO memory_nodes VALUES(1,?,'owner','slot','fact','咖啡','测试历史记忆','active','message',4,.9,1,1)",(SYSTEM_SCOPE,))
    db.execute('PRAGMA user_version=5'); db.commit(); db.close()
    store=Store(path); store.initialize(); store.initialize()
    with closing(store.open_read()) as db:
        assert db.execute('SELECT value FROM memory_nodes').fetchone()[0]=='测试历史记忆'
        assert db.execute('SELECT count(*) FROM memory_vectors').fetchone()[0]==1
