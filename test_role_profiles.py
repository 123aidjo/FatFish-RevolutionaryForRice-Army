import json
from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient

from local_runtime import data_directory, read_config, save_config, apply_environment
from role_profiles import Profiles, active_id, config_directory, child_environment, switch_service
from settings import PROJECT_ROOT, load_settings
from store import Store
from panel import create_app, panel_token
from panel_config import PanelConfig
from bilibili_browser import BrowseStore


@pytest.fixture
def roles(tmp_path,monkeypatch):
    monkeypatch.setenv('FISHBOT_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.delenv('FISHBOT_PROFILE_ID',raising=False)
    project=tmp_path/'project'; project.mkdir()
    for name in ('persona.toml','platforms.toml'):
        shutil.copyfile(PROJECT_ROOT/name,project/name)
    manager=Profiles(project=project); manager.initialize()
    return manager


def select(manager,identifier,monkeypatch):
    manager.activate(identifier)
    monkeypatch.setenv('FISHBOT_PROFILE_ID',identifier)


def test_default_preserved_and_roles_have_independent_configs(roles,monkeypatch):
    legacy=roles.root/'fishbot.db'; legacy.write_bytes(b'legacy-preserved')
    a=roles.create('角色甲','理性助手'); b=roles.create('角色乙',prompt='只属于乙的自定义人设')
    select(roles,a,monkeypatch)
    assert data_directory()==roles.data(a)
    assert config_directory()==roles.config(a)
    assert load_settings().persona.name=='角色甲'
    (config_directory()/'persona.toml').write_text((config_directory()/'persona.toml').read_text(encoding='utf-8').replace('角色甲','修改甲'),encoding='utf-8')
    select(roles,b,monkeypatch)
    assert load_settings().persona.name=='角色乙'
    assert load_settings().persona.system_prompt=='只属于乙的自定义人设'
    assert not (data_directory()/'fishbot.db').exists()
    assert legacy.read_bytes()==b'legacy-preserved'
    assert roles.data('default')==roles.root


def test_database_tokens_browse_and_credentials_isolated(roles,monkeypatch):
    tokens=[]
    for name in ('甲','乙'):
        identifier=roles.create(name)
        select(roles,identifier,monkeypatch)
        folder=data_directory(); store=Store(folder/'fishbot.db'); store.initialize()
        browse=BrowseStore(store); browse.initialize()
        assert browse.status()['today_rounds']==0
        assert read_config()['model_key']==''
        save_config({'model_key':'fixture-'+name})
        with store.transaction() as db:
            db.execute("INSERT INTO panel_state VALUES('only_this_role',?)",(name,))
        tokens.append(panel_token(store,folder))
        public=(roles.project/'persona.toml').read_bytes()
        snapshot=PanelConfig(store,config_directory(),folder).read()
        assert snapshot.settings.persona.name==name
        assert (roles.project/'persona.toml').read_bytes()==public
        app=create_app(store)
        with TestClient(app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',55000)) as client:
            assert client.get('/api/persona',headers={'Authorization':'Bearer '+tokens[-1]}).status_code==200
            if len(tokens)==2:
                assert client.get('/api/persona',headers={'Authorization':'Bearer '+tokens[0]}).status_code==401
    assert tokens[0]!=tokens[1]


def test_worker_environment_and_local_model_configuration(roles,monkeypatch):
    identifier=roles.create('隔离')
    select(roles,identifier,monkeypatch)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture-old')
    monkeypatch.setenv('FISHBOT_QQ_GROUP_ID','12345')
    monkeypatch.setenv('FISHBOT_VISION_KEY','fixture-old-vision')
    env=child_environment()
    assert 'DEEPSEEK_API_KEY' not in env and 'FISHBOT_QQ_GROUP_ID' not in env
    assert 'FISHBOT_VISION_KEY' not in env and env['FISHBOT_PROFILE_ID']==identifier
    save_config({'model_key':'fixture-new'})
    apply_environment()
    import os
    assert os.environ['DEEPSEEK_API_KEY']=='fixture-new'
    assert 'FISHBOT_QQ_GROUP_ID' not in os.environ
    assert read_config()['napcat_launcher']==''


class Service:
    def __init__(self,fail=None): self.on=True; self.events=[]; self.fail=fail
    def running(self): return self.on
    def stop(self): self.events.append(('stop',active_id())); self.on=False
    def start(self):
        self.events.append(('start',active_id()))
        if active_id()==self.fail: raise ValueError('fixture-start-failure')
        self.on=True


def test_switch_stops_old_worker_and_rolls_back_on_failure(roles,monkeypatch):
    a=roles.create('甲'); b=roles.create('乙')
    select(roles,a,monkeypatch)
    service=Service()
    switch_service(service,b,roles)
    assert service.events==[('stop',a),('start',b)]
    assert active_id()==b and service.log==roles.data(b)/'launcher/native-server.log'
    service.fail=a
    with pytest.raises(ValueError): switch_service(service,a,roles)
    assert active_id()==b and roles.read()['active']==b and service.running()


def test_worker_pin_and_invalid_profiles(roles,monkeypatch):
    a=roles.create('甲'); b=roles.create('乙')
    select(roles,a,monkeypatch)
    roles.activate(b)
    assert active_id()==a and data_directory()==roles.data(a)
    with pytest.raises(ValueError): roles.activate('../other')
    with pytest.raises(ValueError): roles.create('甲')
    with pytest.raises(ValueError): roles.create('')
    roles.rename(b,'新乙')
    assert roles.read()['roles'][b]=='新乙'


def test_native_switch_restarts_process_in_new_data_directory(roles,monkeypatch):
    import socket
    from desktop import Service as NativeService
    a=roles.create('实际进程甲'); b=roles.create('实际进程乙')
    with socket.socket() as free:
        free.bind(('127.0.0.1',0))
        port=free.getsockname()[1]
    for identifier in (a,b):
        path=roles.config(identifier)/'platforms.toml'
        path.write_text(path.read_text(encoding='utf-8').replace('port = 8000',f'port = {port}'),encoding='utf-8')
    select(roles,a,monkeypatch)
    service=NativeService()
    try:
        service.start()
        old=service.process
        token_a=(roles.data(a)/'panel.token').read_text()
        switch_service(service,b,roles)
        assert old.poll() is not None and service.running()
        assert (roles.data(b)/'panel.token').read_text()!=token_a
        assert (roles.data(a)/'fishbot.db').exists() and (roles.data(b)/'fishbot.db').exists()
        assert service.log.is_relative_to(roles.data(b))
    finally:
        service.stop()
