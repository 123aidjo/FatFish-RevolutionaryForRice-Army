import asyncio
import json
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

import desktop
import local_runtime


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("FISHBOT_DATA_DIR", str(tmp_path / "data"))
    for name in local_runtime.ENVIRONMENT.values():
        # Register restoration even when the variable was initially absent:
        # apply_environment intentionally writes os.environ in the process.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_local_configuration_stays_outside_repository_and_environment_wins(isolated, monkeypatch):
    local_runtime.save_config({"qq_bot_id": "123456", "qq_group_id": "654321", "model_key": "test-key"})
    path = isolated / "data" / "desktop.local.json"
    assert json.loads(path.read_text())["model_key"] == "test-key"
    assert not (isolated / "data" / "fishbot.db").exists()
    monkeypatch.setenv("FISHBOT_QQ_BOT_ID", "987654")
    local_runtime.apply_environment()
    import os
    assert os.environ["FISHBOT_QQ_BOT_ID"] == "987654"
    assert os.environ["FISHBOT_QQ_GROUP_ID"] == "654321"
    assert os.environ["DEEPSEEK_API_KEY"] == "test-key"


@pytest.mark.parametrize("changes", [
    {"webui_url": "http://example.com:6099/webui/"},
    {"onebot_url": "ws://secret@localhost:3001"},
    {"onebot_url": "ws://127.0.0.1:3001?token=secret"},
    {"qq_bot_id": "not-an-id"}, {"model_key": "a\nb"}, {"unknown": "x"},
])
def test_invalid_config_preserves_previous_file(isolated, changes):
    local_runtime.save_config({})
    path = isolated / "data" / "desktop.local.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        local_runtime.save_config(changes)
    assert path.read_bytes() == before


def test_corrupted_config_is_reported_and_not_overwritten(isolated):
    path = isolated / "data" / "desktop.local.json"
    path.parent.mkdir()
    path.write_text("invalid")
    with pytest.raises(ValueError):
        local_runtime.read_config()
    assert path.read_text() == "invalid"


def test_napcat_discovery_is_read_only_and_keeps_multiple_candidates(tmp_path):
    for name in ("NapCat-one", "NapCat-two"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "napcat.bat").write_text("test")
        (folder / "webui.json").write_text('{"token":"do-not-return"}')
    result = desktop.discover_napcat([tmp_path])
    assert len(result["launchers"]) == len(result["configs"]) == 2
    assert "do-not-return" not in repr(result)
    assert all(Path(path).is_file() for paths in result.values() for path in paths)


def test_napcat_bad_config_and_auth_failure_are_safe(tmp_path, monkeypatch):
    from napcat_login import NapCatLogin
    path = tmp_path / "webui.json"
    path.write_text('{"unexpected":"secret-value"}')
    login = NapCatLogin(path)
    with pytest.raises(OSError) as error:
        login.login_state()
    assert "secret-value" not in str(error.value)
    assert login.credential == ""


def test_import_existing_napcat_uses_actual_ports_without_rewriting(tmp_path):
    config = tmp_path / "napcat" / "config"
    config.mkdir(parents=True)
    webui = config / "webui.json"
    webui.write_text(json.dumps({"port": 6099, "token": "test-webui"}))
    onebot = config / "onebot11_123456.json"
    onebot.write_text(json.dumps({"network": {"websocketServers": [
        {"enable": True, "port": 7998, "token": "test-onebot"}]}}))
    (tmp_path / "napcat.bat").write_text("test")
    before = {path: path.read_bytes() for path in (webui, onebot)}
    result = desktop.import_napcat_config(webui, dict(local_runtime.DEFAULTS))
    assert result["onebot_url"] == "ws://127.0.0.1:7998"
    assert result["onebot_token"] == "test-onebot"
    assert result["qq_bot_id"] == "123456"
    assert result["qq_group_id"] == ""
    assert result["napcat_launcher"] == str(tmp_path / "napcat.bat")
    assert all(path.read_bytes() == content for path, content in before.items())


def test_wrong_group_does_not_call_model_or_enable_replies(isolated, monkeypatch):
    from contextlib import asynccontextmanager
    import live_qq
    from store import Store
    from settings import default_database_path

    class RPC:
        @asynccontextmanager
        async def opened(self):
            yield self

        async def call(self, action, params):
            assert action == "get_group_info"
            return {"retcode": 1, "data": None}

    async def auth(rpc):
        return {}

    monkeypatch.setattr(live_qq, "live_ids_configured", lambda: True)
    monkeypatch.setattr(live_qq, "authenticate", auth)
    monkeypatch.setattr(live_qq.LiveQQ, "rpc", lambda self: RPC())
    monkeypatch.setattr(live_qq.LiveQQ, "key", lambda self: "test-key")
    monkeypatch.setattr(live_qq.DeepSeekClient, "complete", lambda *args, **kwargs: pytest.fail("must not call model"))
    with pytest.raises(ConnectionError, match="授权群"):
        asyncio.run(live_qq.console("verify"))
    store = Store(default_database_path())
    assert live_qq.get_value(store, "live_qq_enabled") == ""


def test_native_service_uses_configured_port_and_stops_only_owned_process(isolated, monkeypatch):
    project = isolated / "project"
    project.mkdir()
    for source in desktop.PROJECT.iterdir():
        if source.suffix in {".py", ".toml", ".html", ".js", ".css", ".png", ".ico"}:
            shutil.copyfile(source, project / source.name)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    config = project / "platforms.toml"
    config.write_text(config.read_text(encoding="utf8").replace("port = 8000", f"port = {port}"), encoding="utf8")
    monkeypatch.setattr(desktop, "PROJECT", project)
    service = desktop.Service()
    assert service.url == f"http://127.0.0.1:{port}/"
    try:
        assert "已启动" in service.start()
        original = service.process.pid
        service.start()
        assert service.process.pid == original
        assert (isolated / "data" / "panel.token").exists()
    finally:
        service.stop()
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", port))
        unrelated.listen()
        with pytest.raises(ValueError, match="已被使用"):
            service.start()
        service.stop()
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
