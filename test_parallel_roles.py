import json
import socket

import httpx
import pytest

from desktop import RoleServices, Service
from role_profiles import worker_lock
from test_role_profiles import roles, select


def test_parallel_processes_ports_cookies_and_independent_stop(roles,monkeypatch):
    a=roles.create('并行甲'); b=roles.create('并行乙')
    services=RoleServices()
    # Occupied configured port belongs to an unrelated application.
    with socket.socket() as unrelated:
        unrelated.bind(('127.0.0.1',0)); unrelated.listen()
        port=unrelated.getsockname()[1]
        for identifier in (a,b):
            path=roles.config(identifier)/'platforms.toml'
            path.write_text(path.read_text(encoding='utf-8').replace('port = 8000',f'port = {port}'),encoding='utf-8')
        select(roles,a,monkeypatch)
        first=services.get(a); second=services.get(b)
        try:
            first.start()
            pid=first.process.pid
            select(roles,b,monkeypatch)
            second.start()
            assert first.running() and second.running() and first.process.pid==pid
            assert len({first.port,second.port,port})==3
            assert len(services.running())==2
            # One browser cookie jar, two localhost ports: both logins must coexist.
            with httpx.Client(trust_env=False) as client:
                for identifier,service in ((a,first),(b,second)):
                    token=(roles.data(identifier)/'panel.token').read_text().strip()
                    response=client.post(service.url+'api/session',headers={
                        'Authorization':'Bearer '+token,'Origin':service.url.rstrip('/')})
                    assert response.status_code==200
                for name,service in (('并行甲',first),('并行乙',second)):
                    response=client.get(service.url+'api/persona')
                    assert response.status_code==200
                    assert name in response.text
                assert len(client.cookies)==2
                assert client.delete(first.url+'api/session',headers={'Origin':first.url.rstrip('/')}).status_code==200
                assert client.get(second.url+'api/persona').status_code==200
            # Explicit duplicate role is rejected by an OS lock even on a different port.
            duplicate=Service(a)
            with pytest.raises(ValueError,match='启动失败'):
                duplicate.start()
            assert not duplicate.running() and first.running()
            select(roles,a,monkeypatch)
            with pytest.raises(ValueError,match='已有服务'):
                with worker_lock():
                    pytest.fail('duplicate lock acquired')
            second_pid=second.process.pid
            first.stop()
            assert second.running() and second.process.pid==second_pid
            with httpx.Client(trust_env=False) as client:
                assert client.get(second.url+'health').status_code==200
            with socket.create_connection(('127.0.0.1',port),timeout=1):
                pass
            # Runtime port assignment never rewrites the saved role configuration.
            assert f'port = {port}' in (roles.config(b)/'platforms.toml').read_text()
        finally:
            services.stop_all()
        assert not services.running()


def test_duplicate_qq_connection_blocked_without_starting_process(roles):
    a=roles.create('连接甲'); b=roles.create('连接乙')
    services=RoleServices()
    class Running:
        def running(self): return True
    services.services[a]=Running()
    services.connections[a]={'qq_bot_id':'12345','onebot_url':'ws://127.0.0.1:3001'}
    folder=roles.data(b); folder.mkdir(parents=True)
    for config in ({'qq_bot_id':'12345','onebot_url':'ws://127.0.0.1:3002'},
                   {'qq_bot_id':'67890','onebot_url':'ws://127.0.0.1:3001'}):
        (folder/'desktop.local.json').write_text(json.dumps(config),encoding='utf-8')
        with pytest.raises(ValueError,match='相同 QQ'):
            services.check_connection(b)


def test_stop_all_attempts_remaining_roles_after_one_failure():
    services=RoleServices(); stopped=[]
    class Broken:
        def stop(self): raise ValueError('still stopping')
    class Healthy:
        def stop(self): stopped.append(True)
    services.services={'a':Broken(),'b':Healthy()}
    with pytest.raises(ValueError,match='部分角色'):
        services.stop_all()
    assert stopped==[True]
