import asyncio
from contextlib import closing
import json
import shutil
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from panel import create_app
from settings import PROJECT_ROOT, load_settings
from reply_config import ReplyConfig
from reply_planner import plan
from usage_ledger import meter, stats
from store import Store
from test_qq_engine import Harness, raw


def test_panel_auth_config_aliases_stats_and_secret_preservation(tmp_path):
    project=tmp_path/'config';project.mkdir()
    for filename in ('platforms.toml','persona.toml'): shutil.copyfile(PROJECT_ROOT/filename,project/filename)
    store=Store(tmp_path/'data.db');store.initialize()
    (tmp_path/'panel.token').write_text('fixture-reply-token')
    headers={'Authorization':'Bearer fixture-reply-token','Origin':'http://127.0.0.1:8000'}
    with TestClient(create_app(store,config_dir=project),base_url='http://127.0.0.1:8000',client=('127.0.0.1',5555)) as client:
        assert client.get('/api/reply-decision').status_code==401
        initial=client.get('/api/reply-decision',headers=headers).json()
        payload={'revision':initial['revision'],'config':{**initial['config'],'mode':'balanced','planner_key':'fixture-private'}}
        assert client.put('/api/reply-decision',headers=headers,json=payload).status_code==200
        assert client.put('/api/reply-decision',headers=headers,json=payload).status_code==409
        current=client.get('/api/reply-decision',headers=headers)
        assert 'fixture-private' not in current.text and current.json()['key_configured']
        assert client.get('/api/reply-decision/stats?since=2&until=1',headers=headers).status_code==422
        assert client.get('/api/reply-decision/stats',headers=headers).json()['totals']['balanced']['sent']==0
        persona=client.get('/api/persona',headers=headers).json()
        selected={k:persona['persona'][k] for k in ('name','system_prompt','style','max_chars','temperature','emotions')}
        assert client.put('/api/persona',headers=headers,json={**selected,'revision':persona['revision'],'aliases':['测试昵称']}).status_code==200
        assert client.get('/api/persona',headers=headers).json()['persona']['aliases']==['测试昵称']


def test_planner_http_one_request_real_usage_and_estimated_usage(tmp_path,monkeypatch):
    h=Harness(tmp_path/'planner.db');event=h.engine.adapter.normalize(raw())
    client_type=httpx.AsyncClient;calls=[]
    def respond(request):
        body=json.loads(request.content);calls.append(body)
        assert body['thinking']=={'type':'disabled'} and body['max_tokens']==64 and 'tools' not in body
        result={'choices':[{'finish_reason':'stop','message':{'content':'{"shouldReply":false}'}}]}
        if len(calls)==1: result['usage']={'prompt_tokens':23,'completion_tokens':4}
        return httpx.Response(200,json=result)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client_type(**kw,transport=httpx.MockTransport(respond)))
    cfg=ReplyConfig(planner_same_model=False)
    for _ in range(2):
        with meter(h.store,'scope','simulation','balanced',stage='planner'):
            assert not asyncio.run(plan(cfg,None,h.settings.persona,'',[event],{'last_reply':0},h.now)).shouldReply
    assert len(calls)==2
    report=stats(h.store,'',0,time.time()+1)
    assert report['totals']['balanced']['estimated_requests']==1
    assert any(g['stage']=='planner' and g['calls']==2 for g in report['groups'])


def test_v6_upgrade_preserves_existing_outbox_and_memory(tmp_path):
    h=Harness(tmp_path/'old.db');h.ingest()
    with h.store.transaction() as db:
        before=db.execute('SELECT count(*) FROM events').fetchone()[0]
        for table in ('reply_pending','reply_rounds','reply_state','model_usage'): db.execute('DROP TABLE '+table)
        db.execute('PRAGMA user_version=6')
    h.store.initialize();h.store.initialize()
    with closing(h.store.open_read()) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==7
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==before
        assert db.execute("SELECT state FROM outbox WHERE id=1").fetchone()[0]=='sent'
