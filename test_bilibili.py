import asyncio
from contextlib import closing
from datetime import datetime
import json
import shutil
import tomllib

from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import ValidationError

from bilibili_browser import BEIJING, BrowseError, BrowseSettings, BrowseStore, Browser
from bilibili_sources import BiliSources, BiliVision, allowed_url
from llm import Completion
from panel import create_app
from settings import PROJECT_ROOT
from store import Store


TEXT = '这是测试字幕中的真实片段，介绍如何观察星空中的星座以及它们随季节变化的位置。'
IDS = ['BV'+str(i).zfill(10) for i in range(1,9)]
NOW = datetime(2026,11,26,12,30,tzinfo=BEIJING).timestamp()


class Sources:
    def __init__(self,path):
        self.path = path/'credential.local.json'
        self.calls = []

    async def recommendations(self):
        return [{'bvid':b,'title':'<b>星空</b>','duration':60} for b in IDS[:4]]

    async def search(self,keyword):
        return [{'bvid':b,'title':'星空','duration':'01:00'} for b in IDS[2:]]

    async def read(self,bvid,config):
        self.calls.append(bvid)
        return {'bvid':bvid,'title':'星空','text':TEXT,'source':'字幕抽样','coverage':'仅文字','duration':60}

    async def close(self):
        pass


class Model:
    def __init__(self):
        self.calls = 0
        self.bad_quote = False

    async def complete(self,system,user,**kwargs):
        self.calls += 1
        payload = json.loads(user)
        if 'exploration' in payload:
            result = {'keyword':'星空'}
        elif 'limits' in payload:
            result = {k:[r['bvid'] for r in payload[k]] for k in ('recommended','searched')}
            result['visual'] = IDS[0]
        else:
            result = {'items':[{'bvid':m['bvid'],'quote':'没有根据'*15 if self.bad_quote else TEXT,
                'summary':'字幕介绍星空观察','interest':.8,'topics':['天文']} for m in payload['materials']]}
        return Completion(json.dumps(result,ensure_ascii=False))


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path/'fishbot.db'); store.initialize()
    storage = BrowseStore(store,lambda:NOW); storage.initialize()
    config = BrowseSettings(enabled=True,visual=1)
    storage.configure(config,storage.config()[1])
    sources,model = Sources(tmp_path),Model()
    return storage,sources,model


def test_round_dedup_grounding_visual_sync_and_restart(setup):
    storage,sources,model = setup
    visuals = []
    async def vision(bvid,config):
        visuals.append(bvid)
        return {'description':'测试帧','frames':[1,2,3]}
    browser = Browser(storage,sources,model,lambda:'对天文好奇',vision)
    result = asyncio.run(browser.run())
    assert result['state']=='completed'
    assert len(result['read'])==6 and len(set(sources.calls))==6
    assert visuals==[IDS[0]] and model.calls==3
    assert storage.status()['usage']['requests']==4
    assert storage.interests()[0]['topic']=='天文'
    assert all(r['synced'] for r in storage.history())
    storage.sync()
    with closing(storage.store.open_read()) as db:
        assert db.execute("SELECT count(*) FROM memory_nodes WHERE kind='autobiography'").fetchone()[0]==7
    restarted = Browser(BrowseStore(storage.store,lambda:NOW),sources,model,lambda:'')
    with pytest.raises(BrowseError,match='daily_round_limit'):
        asyncio.run(restarted.run())


def test_concurrent_reservation_and_abandoned_round(setup):
    storage,_,_ = setup
    identifier = storage.reserve(storage.config()[0])
    with pytest.raises(BrowseError,match='already_running'):
        BrowseStore(storage.store,lambda:NOW).reserve(storage.config()[0])
    storage.clock = lambda:NOW+700
    with pytest.raises(BrowseError,match='daily_round_limit'):
        storage.reserve(storage.config()[0])
    assert storage.status()['rounds'][0]['state']=='interrupted'
    storage.clock = lambda:NOW+86400
    storage.reserve(storage.config()[0])
    assert any(r['id']==identifier and r['state']=='interrupted' for r in storage.status()['rounds'])


def test_cancel_keeps_round_quota(setup):
    storage,sources,model = setup
    async def cancelled(*args,**kwargs):
        raise asyncio.CancelledError()
    model.complete = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Browser(storage,sources,model,lambda:'').run())
    assert storage.status()['rounds'][0]['state']=='interrupted'
    assert storage.status()['today_rounds']==1
    assert storage.status()['usage']['requests']==1


def test_failed_model_budget_no_refund(setup):
    storage,sources,model = setup
    config = storage.config()[0].model_copy(update={'requests_per_day':1})
    storage.configure(config,storage.config()[1])
    result = asyncio.run(Browser(storage,sources,model,lambda:'').run())
    assert result['state']=='failed' and result['errors']==['daily_model_budget']
    assert model.calls==1 and storage.status()['usage']['requests']==1


def test_no_material_not_a_watch_and_untrusted_summary(setup):
    storage,sources,model = setup
    async def no_material(bvid,config):
        return {'bvid':bvid,'text':'只有标题'}
    sources.read = no_material
    result = asyncio.run(Browser(storage,sources,model,lambda:'').run())
    assert result['state']=='empty' and not storage.history() and not storage.interests()


def test_unsupported_assessment_cannot_grow_interests(setup):
    storage,sources,model = setup; model.bad_quote=True
    result = asyncio.run(Browser(storage,sources,model,lambda:'').run())
    assert result['state']=='completed'
    assert len(storage.history())==6 and not storage.interests()
    assert 'ungrounded_assessment' in result['errors']


def test_windows_config_revision_and_preview_limits(setup):
    storage,sources,model = setup
    for windows in (['24:01-25:00'],['22:00-06:00'],['12:30-12:30']):
        with pytest.raises(ValidationError):
            BrowseSettings(windows=windows)
    config = BrowseSettings(enabled=True,share_preview=True)
    old = storage.config()[1]
    storage.configure(config,old)
    with pytest.raises(BrowseError,match='config_conflict'):
        storage.configure(config,old)
    assert not config.in_window(datetime(2026,11,26,10,tzinfo=BEIJING).timestamp())
    assert config.in_window(datetime(2026,11,28,10,tzinfo=BEIJING).timestamp())
    asyncio.run(Browser(storage,sources,model,lambda:'').run())
    assert len(storage.status()['previews'])==1


@pytest.mark.parametrize('url',['https://bilibili.com.evil.invalid/a','http://hdslb.com/a',
    'https://user:secret@hdslb.com/a','https://127.0.0.1/a','https://hdslb.com:444/a'])
def test_media_hosts(url):
    with pytest.raises(BrowseError):
        allowed_url(url)
    assert allowed_url('//aisubtitle.hdslb.com/a')=='https://aisubtitle.hdslb.com/a'


def test_public_template_and_daily_budget_boundary(setup):
    template=tomllib.loads((PROJECT_ROOT/'bilibili.example.toml').read_text(encoding='utf-8'))
    assert BrowseSettings.model_validate(template)==BrowseSettings()
    storage,_,_=setup
    config=BrowseSettings(requests_per_day=1,tokens_per_day=1000)
    storage.budget(config,1000)
    with pytest.raises(BrowseError,match='daily_model_budget'):
        storage.budget(config,1)
    storage.clock=lambda:NOW+86400
    storage.budget(config,1000)
    assert storage.status()['usage']['requests']==1


def test_qr_login_persists_only_allowed_cookies(tmp_path,monkeypatch):
    from bilibili_api import user
    async def self_info(credential):
        return {'mid':12345}
    monkeypatch.setattr(user,'get_self_info',self_info)
    def respond(request):
        if request.url.path.endswith('/generate'):
            return httpx.Response(200,json={'code':0,'data':{'url':'https://passport.bilibili.com/qrcode/test','qrcode_key':'fixture-key'}})
        return httpx.Response(200,json={'code':0,'data':{'code':0}},headers=[
            ('set-cookie','SESSDATA=fixture-session; Path=/'),('set-cookie','bili_jct=fixture-csrf; Path=/'),
            ('set-cookie','DedeUserID=12345; Path=/'),('set-cookie','irrelevant=discard; Path=/')])
    async def exercise():
        sources=BiliSources(tmp_path,httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        await sources.login()
        assert sources.qr_image.startswith(b'\x89PNG')
        assert await sources.poll_login()=='connected'
        saved=json.loads(sources.path.read_text())
        assert set(saved)=={'SESSDATA','bili_jct','DedeUserID'}
        assert not sources.qr_key and not sources.qr_image
        await sources.close()
    asyncio.run(exercise())


def test_source_real_material_fallback_and_duration(tmp_path,monkeypatch):
    from bilibili_api import video
    class Video:
        def __init__(self,**kwargs): pass
        async def get_info(self):
            return {'bvid':IDS[0],'title':'星空','duration':60,'cid':1,'videos':1}
        async def get_subtitle(self,**kwargs):
            return {'subtitles':[]}
        async def get_ai_conclusion(self,**kwargs):
            return {'model_result':{'summary':TEXT}}
    monkeypatch.setattr(video,'Video',Video)
    async def exercise():
        sources = BiliSources(tmp_path)
        result = await sources.read(IDS[0],BrowseSettings())
        assert result['text']==TEXT and result['source']=='B站 AI 摘要'
        with pytest.raises(BrowseError,match='unsupported_video'):
            await sources.read(IDS[0],BrowseSettings(max_duration=30))
        await sources.close()
    asyncio.run(exercise())


def test_panel_auth_config_and_disabled_no_io(tmp_path):
    root=tmp_path/'project'; root.mkdir()
    for name in ('persona.toml','platforms.toml'):
        shutil.copyfile(PROJECT_ROOT/name,root/name)
    store=Store(tmp_path/'fishbot.db')
    (tmp_path/'panel.token').write_text('test-bili-panel')
    sources,model=Sources(tmp_path),Model()
    app=create_app(store,config_dir=root,bili_sources=sources,bili_model=model)
    headers={'Authorization':'Bearer test-bili-panel','Origin':'http://127.0.0.1:8000'}
    with TestClient(app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',55000)) as client:
        assert client.get('/api/bilibili').status_code==401
        data=client.get('/api/bilibili',headers=headers).json()
        assert not data['config']['enabled'] and model.calls==0
        assert client.post('/api/bilibili/run',headers=headers).status_code==409
        edit={'revision':data['revision'],'config':{**data['config'],'rounds_per_day':2}}
        assert client.put('/api/bilibili/config',headers=headers,json=edit).status_code==200
        assert client.put('/api/bilibili/config',headers=headers,json=edit).status_code==409
        assert client.put('/api/bilibili/config',headers={**headers,'Origin':'https://evil.invalid'},json=edit).status_code==403
        assert client.get('/api/bilibili/history',headers=headers).json()=={'items':[]}


def test_ffmpeg_extracts_frames_without_external_install(tmp_path,monkeypatch):
    import subprocess
    import imageio_ffmpeg
    from bilibili_api import video
    path=tmp_path/'fixture.mp4'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-loglevel','error','-f','lavfi','-i',
        'color=c=blue:s=64x64:d=2','-c:v','libx264',str(path)],check=True,capture_output=True)
    class Video:
        def __init__(self,**kwargs): pass
        async def get_info(self): return {'duration':2,'videos':1}
        async def get_download_url(self,**kwargs): return {'dash':{'video':[{'baseUrl':'https://video.bilivideo.com/test','bandwidth':1}]}}
    monkeypatch.setattr(video,'Video',Video)
    for key,value in {'FISHBOT_VISION_ENDPOINT':'https://vision.example.invalid/chat/completions',
                      'FISHBOT_VISION_MODEL':'fixture','FISHBOT_VISION_KEY':'fixture-key'}.items():
        monkeypatch.setenv(key,value)
    requests=[]
    def respond(request):
        payload=json.loads(request.content); requests.append(payload)
        assert len(payload['messages'][0]['content'])==4
        return httpx.Response(200,json={'choices':[{'message':{'content':'三个蓝色画面'}}]})
    original=httpx.AsyncClient
    async def exercise():
        sources=BiliSources(tmp_path)
        async def download(url,limit): return path.read_bytes()
        sources.download=download
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
        result=await BiliVision(sources)(IDS[0],BrowseSettings())
        assert len(result['frames'])==3 and len(requests)==1
        await sources.close()
    asyncio.run(exercise())
