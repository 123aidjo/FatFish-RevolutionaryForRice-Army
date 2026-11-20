import json
import shutil
import sqlite3
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from adapter_douyin import DouyinAdapter
from douyin_client import DouyinClient
from douyin_content import DraftContent
from douyin_store import DouyinStore
from llm import Completion, Usage
from panel import create_app
from settings import PROJECT_ROOT
from store import SCHEMA_VERSION, Store


ORIGIN = "http://127.0.0.1:8000"
AUTH = {"Authorization": "Bearer test-douyin-panel", "Origin": ORIGIN}
CONFIG = {"client_key": "test-app-key", "client_secret": "test-app-secret",
          "redirect_uri": ORIGIN + "/douyin/callback"}
DRAFT = DraftContent(
    title="大肥鱼的第一次桌面旅行", idea="用鲸鱼娘 AI 角色介绍本地面板。",
    script="0–5 秒，大肥鱼从蓝色桌面角落冒出来。镜头二是她点开创作草稿并认真检查每一行字幕。",
    description="我是大肥鱼，一个 AI 角色。今天带你看我的面板。",
    topics=["大肥鱼", "AI角色"], pinned_comment="下期想看鱼鱼做什么？", plan="人工过稿后再准备视频素材。",
)


class MockModel:
    def __init__(self):
        self.output = DRAFT.model_dump_json()
        self.calls = []

    async def complete(self, system, user, *, temperature, max_tokens):
        self.calls.append((system, json.loads(user), max_tokens))
        return Completion(self.output, Usage(101, 59))


class MockPlatform:
    def __init__(self):
        self.requests = []
        self.profile_id = "account-one"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.method == "POST"
        assert req.url.host == "open.douyin.com"
        if req.url.path == "/oauth/access_token/":
            params = parse_qs(req.content.decode())
            assert params["grant_type"] == ["authorization_code"]
            assert params["client_secret"] == ["test-app-secret"]
            assert params["code"] == ["one-time-code"]
        elif req.url.path == "/oauth/refresh_token/":
            assert req.headers["content-type"].startswith("multipart/form-data")
            assert b"test-refresh-token" in req.content
        else:
            assert req.url.path == "/oauth/userinfo/"
            assert json.loads(req.content) == {"open_id": "account-one"}
            assert req.headers["access-token"] == "test-access-token"
            return httpx.Response(200, json={"data": {"error_code": "0", "open_id": self.profile_id,
                "nickname": "模拟鱼鱼", "avatar": "https://p.example.invalid/photo.jpg"}})
        return httpx.Response(200, json={"data": {"error_code": 0, "open_id": "account-one",
            "access_token": "test-access-token", "refresh_token": "test-refresh-token",
            "scope": "user_info", "expires_in": "86400", "refresh_expires_in": 172800}})


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("persona.toml", "platforms.toml"):
        shutil.copyfile(PROJECT_ROOT / name, root / name)
    data = tmp_path / "data"
    data.mkdir()
    (data / "panel.token").write_text("test-douyin-panel", encoding="ascii")
    return root, Store(data / "fishbot.db"), MockPlatform(), MockModel()


def browser(env):
    root, store, wire, model = env
    app = create_app(store, config_dir=root,
                     douyin_client=DouyinClient(httpx.MockTransport(wire)), douyin_model=model)
    return TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 55555))


def begin(client):
    response = client.post("/api/douyin/authorize", headers=AUTH)
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    url = urlsplit(response.json()["authorization_url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "open.douyin.com", "/platform/oauth/connect/")
    params = parse_qs(url.query)
    assert params["scope"] == ["user_info"]
    return params["state"][0]


def callback(client, state):
    return client.get("/douyin/callback", params={"state": state, "code": "one-time-code"},
                      follow_redirects=False)


def test_auth_origin_scope_restart_and_local_disconnect(env):
    with browser(env) as client:
        assert client.get("/api/douyin/account").status_code == 401
        assert client.put("/api/douyin/app", json=CONFIG, headers={**AUTH, "Origin": "https://elsewhere.test"}).status_code == 403
        initial = client.get("/api/douyin/account", headers=AUTH).json()
        assert initial["granted_scopes"] == [] and initial["mode"] == "draft"
        assert client.put("/api/douyin/app", json=CONFIG, headers=AUTH).status_code == 200
        state = begin(client)
        with browser(env) as other:
            assert "douyin=error" in callback(other, state).headers["location"]
        assert "douyin=error" in callback(client, "z" * 43).headers["location"]
        assert not env[2].requests
        assert "douyin=ok" in callback(client, state).headers["location"]
        assert "douyin=error" in callback(client, state).headers["location"]
        assert len(env[2].requests) == 2
    with browser(env) as client:
        account = client.get("/api/douyin/account", headers=AUTH)
        status = account.json()
        assert status["profile"]["nickname"] == "模拟鱼鱼" and status["profile"]["avatar"] is None
        assert status["connection"] == "verified" and status["granted_scopes"] == ["user_info"]
        assert all(secret not in account.text for secret in ("test-app-secret", "test-access-token", "test-refresh-token"))
        renewed = client.post("/api/douyin/refresh", headers=AUTH).json()
        assert renewed["expires_at"] >= status["expires_at"]
        assert renewed["refresh_expires_at"] <= status["refresh_expires_at"]
        assert client.delete("/api/douyin/account", headers=AUTH).json()["connection"] == "disconnected"
        assert client.post("/api/douyin/refresh", headers=AUTH).status_code == 422
        assert len(env[2].requests) == 4


def test_mismatched_account_and_invalid_redirect_are_blocked(env):
    with browser(env) as client:
        payload = {**CONFIG, "redirect_uri": "http://example.test/callback"}
        assert client.put("/api/douyin/app", json=payload, headers=AUTH).status_code == 422
        assert client.put("/api/douyin/app", json=CONFIG, headers=AUTH).status_code == 200
        state = begin(client)
        env[2].profile_id = "unexpected-account"
        assert "douyin=error" in callback(client, state).headers["location"]
        status = client.get("/api/douyin/account", headers=AUTH).json()
        assert status["connection"] == "disconnected" and "身份" in status["error"]


def test_draft_workbench_model_usage_and_database_isolation(env):
    with browser(env) as client:
        assert "douyin-drafts" in client.get("/").text
        path = "/api/douyin/drafts"
        assert client.post(path + "/demo", headers=AUTH, json={"brief": " "}).status_code == 422
        demo = client.post(path + "/demo", headers=AUTH, json={"brief": "模拟选题"}).json()
        assert demo["source"] == "simulation" and not env[3].calls
        generated = client.post(path + "/generate", headers=AUTH, json={"brief": "介绍大肥鱼"}).json()
        assert generated["source"] == "deepseek" and len(generated["content"]["script"]) > 40
        assert "Q 版蓝发女仆鲸鱼娘" in env[3].calls[0][0]
        assert env[3].calls[0][1] == {"选题": "介绍大肥鱼"}
        manual = client.post(path, headers=AUTH, json={"content": DRAFT.model_dump()}).json()
        data = {"version": manual["version"], "content": {**DRAFT.model_dump(), "title": "修订标题"}}
        assert client.put(f"{path}/{manual['id']}", headers=AUTH, json=data).json()["version"] == 2
        assert client.put(f"{path}/{manual['id']}", headers=AUTH, json=data).status_code == 409
        assert client.delete(f"{path}/{manual['id']}?version=2", headers=AUTH).status_code == 200
        env[3].output = "破碎的 JSON"
        assert client.post(path + "/generate", headers=AUTH, json={"brief": "测试错误"}).status_code == 422
        assert not env[2].requests
    with browser(env) as client:
        snapshot = client.get("/api/douyin/drafts", headers=AUTH).json()
        assert [draft["source"] for draft in snapshot["drafts"]] == ["deepseek", "simulation"]
        assert snapshot["usage"] == dict(requests=2, failures=1, prompt_tokens=202, completion_tokens=118)
    dbs = env[1], DouyinStore(env[1].path.parent)
    for db, version in zip(dbs, (SCHEMA_VERSION, 1), strict=True):
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
            contents = conn.execute("SELECT count(*) FROM sqlite_master WHERE name='drafts'").fetchone()[0]
            assert contents == int(version == 1)
    adapter = DouyinAdapter()
    assert adapter.normalize({}) is None and not adapter.capabilities().supports_reply


def test_douyin_sidecar_backup_and_transaction_rollback(env, tmp_path):
    store = DouyinStore(env[1].path.parent)
    with store.connect(write=True) as db:
        db.execute("INSERT INTO oauth_state VALUES ('check', 'original')")
    backup = tmp_path / "backup.db"
    with store.connect() as original, sqlite3.connect(backup) as copy:
        original.backup(copy)
    with pytest.raises(RuntimeError), store.connect(write=True) as db:
        db.execute("UPDATE oauth_state SET value='discarded' WHERE key='check'")
        raise RuntimeError
    with store.connect() as db:
        assert db.execute("SELECT value FROM oauth_state WHERE key='check'").fetchone()[0] == "original"
    with store.connect(write=True) as db:
        db.execute("UPDATE oauth_state SET value='new' WHERE key='check'")
    with sqlite3.connect(backup) as copy, sqlite3.connect(store.path) as original:
        copy.backup(original)
    with store.connect() as db:
        assert db.execute("SELECT value FROM oauth_state WHERE key='check'").fetchone()[0] == "original"
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
