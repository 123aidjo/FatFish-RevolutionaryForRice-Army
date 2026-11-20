from io import BytesIO
import shutil
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from events import Event
from panel import create_app
from settings import PROJECT_ROOT, load_settings
from store import Store

ORIGIN = "http://127.0.0.1:8000"
TEST_TOKEN = "test-local-token-for-pytest"


def client_for(store, config):
    return TestClient(create_app(store, config_dir=config), base_url=ORIGIN,
                      client=("127.0.0.1", 54321))


@pytest.fixture
def environment(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("persona.toml", "platforms.toml"):
        shutil.copyfile(PROJECT_ROOT / name, cfg / name)
    store = Store(tmp_path / "data" / "fishbot.db")
    store.path.parent.mkdir()
    (store.path.parent / "panel.token").write_text(TEST_TOKEN, encoding="ascii")
    return store, cfg


@pytest.fixture
def panel(environment):
    with client_for(*environment) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}", "Origin": ORIGIN})
        yield client


def persona_payload(panel):
    response = panel.get("/api/persona").json()
    return {"revision": response["revision"], **{
        key: value for key, value in response["persona"].items() if key != "aliases"
    }}


def test_remember_browser_session_and_revoke_on_logout(environment):
    with client_for(*environment) as first:
        auth = {"Authorization": f"Bearer {TEST_TOKEN}", "Origin": ORIGIN}
        assert first.post("/api/session", headers={**auth, "Origin": "http://example.test"}).status_code == 403
        response = first.post("/api/session", headers=auth)
        cookie = response.headers["set-cookie"]
        assert response.status_code == 200
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=2592000" in cookie
        assert TEST_TOKEN not in cookie
        saved_cookie = dict(first.cookies)
    with client_for(*environment) as reopened:
        reopened.cookies.update(saved_cookie)
        assert reopened.get("/api/status").status_code == 200
        assert reopened.delete("/api/session").status_code == 403
        assert reopened.delete("/api/session", headers={"Origin": ORIGIN}).status_code == 200
        reopened.cookies.update(saved_cookie)
        assert reopened.get("/api/status").status_code == 401


def event(number, room="300", platform="qq", text="你好", moment=None):
    return Event(platform, room, "group", str(number), "100", "测试者", text, True, False, None, [],
                 time.time() if moment is None else moment)


def test_page_and_assets(panel):
    response = panel.get("/")
    assert response.status_code == 200
    for label in ("总览", "平台管理", "AI 人设", "记忆管理", "表情包", "消息日志", "系统设置"):
        assert label in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert panel.get("/assets/app.js").status_code == 200
    assert panel.get("/assets/style.css").status_code == 200
    assert 'src="/assets/whale-girl.png"' in response.text
    mascot = panel.get("/assets/whale-girl.png")
    assert mascot.status_code == 200
    assert mascot.headers["content-type"] == "image/png"
    assert mascot.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert TEST_TOKEN not in response.text


def test_status_reports_real_zeros_and_unavailable_connections(panel, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    status = panel.get("/api/status").json()
    assert status["service"] == "running" and status["database"]
    assert status["mode"] == "simulation"
    assert status["deepseek"]["verified"] is False
    assert status["deepseek"]["configured"] is False
    assert {item["id"] for item in status["platforms"]} == {"qq", "bilibili", "douyin"}
    assert all(not item["connected"] and not item["send"] for item in status["platforms"])
    assert set(status["stats"].values()) == {0}
    assert status["logs"] == []
    assert len(status["revision"]) == 64


def test_bearer_required_and_remote_hosts_denied(environment, panel):
    assert panel.get("/api/status", headers={"Authorization": "Bearer incorrect"}).status_code == 401
    assert panel.get("/api/status", headers={"Host": "example.test"}).status_code == 403
    store, cfg = environment
    with TestClient(create_app(store, config_dir=cfg), base_url=ORIGIN,
                    client=("192.0.2.19", 45678)) as remote:
        assert remote.get("/").status_code == 403
        assert remote.get("/api/status", headers=panel.headers).status_code == 403


def test_origin_is_checked_on_reads_and_writes(environment, panel):
    assert panel.get("/api/status", headers={"Origin": "http://example.test"}).status_code == 403
    for source in ("http://127.0.0.1:9000", "null", ""):
        response = panel.put("/api/persona", json=persona_payload(panel), headers={"Origin": source})
        assert response.status_code == 403
    with client_for(*environment) as other:
        assert other.get("/api/status").status_code == 401
        assert other.post("/api/runtime", json={"action": "pause"},
                          headers={"Authorization": f"Bearer {TEST_TOKEN}"}).status_code == 403


def test_persona_read_save_restart_and_restore(environment, panel):
    store, config = environment
    original = persona_payload(panel)
    assert original["name"] == "鲸小鱼"
    assert original["max_chars"] == 0
    assert panel.get("/api/persona").json()["persona"]["aliases"] == ["大肥鱼", "小鱼"]
    assert "人格配置版本" in panel.get("/assets/app.js").text
    changed = panel.put("/api/persona", json={**original, "name": "蓝色大肥鱼",
                         "style": "短句", "max_chars": 32, "emotions": ["joy", "neutral"]})
    assert changed.status_code == 200, changed.text
    saved = load_settings(config, database_path=store.path)
    assert (saved.persona.name, saved.persona.style, saved.persona.max_chars) == ("蓝色大肥鱼", "短句", 32)
    with client_for(*environment) as restarted:
        restarted.headers.update(panel.headers)
        current = persona_payload(restarted)
        assert current["name"] == "蓝色大肥鱼"
        assert current["revision"] != original["revision"]
        assert current["emotions"] == ["joy", "neutral"]
        restored = restarted.post("/api/settings/restore", json={"revision": current["revision"]})
        assert restored.status_code == 200, restored.text
        assert persona_payload(restarted)["name"] == original["name"]
        assert persona_payload(restarted)["max_chars"] == 0
        assert restored.json()["revision"] != original["revision"]
        assert restarted.put("/api/persona", json=original).status_code == 409


@pytest.mark.parametrize("invalid", [{"max_chars": -1}, {"temperature": 2.5}, {"name": "  "},
                                     {"emotions": ["wrong"]}, {"system_prompt": "API key: sk-private-demo-99"}])
def test_invalid_persona_is_rejected_without_overwrite(environment, panel, invalid):
    _, config = environment
    original = (config / "persona.toml").read_bytes()
    result = panel.put("/api/persona", json={**persona_payload(panel), **invalid})
    assert result.status_code == 422
    assert (config / "persona.toml").read_bytes() == original
    assert "sk-private-demo-99" not in result.text


def test_parallel_edit_does_not_overwrite_shared_revision(panel):
    persona = persona_payload(panel)
    settings = panel.get("/api/settings").json()
    settings.pop("can_restore")
    assert panel.put("/api/settings", json={**settings, "participation_every": 8, "log_level": "WARNING",
                     "model": "deepseek-v4-pro"}).status_code == 200
    assert panel.put("/api/persona", json={**persona, "name": "过期编辑"}).status_code == 409
    latest = panel.get("/api/settings").json()
    assert (latest["model"], latest["participation_every"], latest["log_level"]) == ("deepseek-v4-pro", 8, "WARNING")


def test_platform_preferences_cannot_enable_real_send(panel):
    snapshot = panel.get("/api/platforms").json()
    assert all(not item["send"] and not item["authorized_send"] for item in snapshot["platforms"])
    edit = {"revision": snapshot["revision"], "enabled": True, "receive": True, "allowlist": ["300"]}
    assert panel.put("/api/platforms/qq", json={**edit, "send": True}).status_code == 422
    assert panel.put("/api/platforms/qq", json={**edit, "send": False}).status_code == 200
    qq = next(item for item in panel.get("/api/platforms").json()["platforms"] if item["id"] == "qq")
    assert qq["enabled"] and qq["receive"] and qq["allowlist"] == ["300"]
    assert not qq["send"] and not qq["connected"]
    assert panel.post("/api/runtime", json={"action": "live"}).status_code == 422


def test_memory_isolate_and_confirm_clear(environment, panel):
    store, _ = environment
    moment = time.time()
    store.accept(event(1, room="300", text="记忆甲"), 4, moment)
    store.accept(event(1, room="301", text="记忆乙"), 4, moment)
    work = store.summary_work(("qq", "group", "300"))
    assert store.mark_summary_check(("qq", "group", "300"), None, moment)
    store.finish_summary(("qq", "group", "300"), "摘要甲", work.cursor, moment, moment, 32)
    channels = panel.get("/api/memory").json()["channels"]
    assert len(channels) == 2 and channels[0]["summary_status"] == "ready"
    first = {"platform": "qq", "kind": "group", "channel": "300"}
    second = {**first, "channel": "301"}
    result = panel.get("/api/memory", params=first).json()
    assert result["summary"] == "摘要甲" and len(result["entries"]) == 1
    assert result["entries"][0]["text"] == "记忆甲" and result["entries"][0]["created_at"] == moment
    assert panel.post("/api/memory/clear", json={**first, "confirmation": "301"}).status_code == 422
    assert panel.post("/api/memory/clear", json={**first, "confirmation": "300"}).status_code == 200
    assert panel.get("/api/memory", params=first).json() == {"entries": [], "summary": ""}
    assert panel.get("/api/memory", params=second).json()["entries"][0]["text"] == "记忆乙"
    assert store.get_event("qq", "group", "300", "1") is not None


def png():
    image = BytesIO()
    Image.new("RGBA", (24, 16), "#369fe8").save(image, "PNG")
    return image.getvalue()


def test_sticker_upload_preview_and_edit(environment, panel):
    store, _ = environment
    response = panel.post("/api/stickers", headers={"Content-Type": "image/png"}, content=png())
    assert response.status_code == 201, response.text
    ident = response.json()["id"]
    preview = panel.get(f"/api/stickers/{ident}/image")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
    assert (store.path.parent / "stickers" / f"{ident}.png").is_file()
    metadata = {"name": "水花", "enabled": True, "emotions": ["joy"], "keywords": ["你好"], "contexts": ["鱼"]}
    assert panel.put(f"/api/stickers/{ident}", json=metadata).status_code == 200
    assert panel.get("/api/stickers").json()["items"] == [{"id": ident, **metadata}]
    assert panel.get(f"/api/stickers/{ident}/image", headers={"Authorization": ""}).status_code == 401


@pytest.mark.parametrize(("mime", "body", "status"), [
    ("image/png", b"not a png", 422),
    ("image/jpeg", png(), 422),
    ("image/svg+xml", b"<svg/>", 422),
    ("image/png", b"x" * (4 * 1024 * 1024 + 1), 413),
])
def test_untrusted_sticker_bytes_are_rejected(panel, mime, body, status):
    assert panel.post("/api/stickers", headers={"Content-Type": mime}, content=body).status_code == status
    assert panel.get("/api/stickers").json()["items"] == []


def test_message_filters_and_audit_detail(environment, panel):
    store, _ = environment
    moment = time.time()
    identifier = store.accept(event(1, text="待审核", moment=moment), 4, moment)
    assert store.claim_evaluation(identifier, moment + 200)
    store.finish_evaluation(identifier, moment + 200, None, "content_rejected", (3, 4), generated_text="候选")
    store.save_event(event(1, platform="douyin", moment=moment - 500))
    response = panel.get("/api/messages", params={"platform": "qq", "state": "skipped",
                                                 "since": moment - 1, "until": moment + 1})
    assert response.status_code == 200, response.text
    result = response.json()["items"]
    assert len(result) == 1 and result[0]["reply"] == "候选" and result[0]["audit_skipped"]
    assert result[0]["mode"] == "simulation" and result[0]["detail"] == "内容审核跳过"
    assert panel.get("/api/messages", params={"state": "received", "platform": "douyin"}).json()["items"][0]["mode"] == "unknown"
    assert panel.get("/api/messages", params={"since": moment + 10}).json()["items"] == []
    assert panel.get("/api/messages", params={"since": moment + 1, "until": moment}).status_code == 422


def test_sensitive_values_are_not_returned(environment, panel, monkeypatch):
    store, _ = environment
    monkeypatch.setenv("DEEPSEEK_API_KEY", "only-test-credential")
    status = panel.get("/api/status")
    assert status.json()["deepseek"]["configured"]
    assert not status.json()["deepseek"]["verified"] and "only-test-credential" not in status.text
    store.save_event(event(1, text="only-test-credential " + TEST_TOKEN + " sk-synthetic-secret"))
    assert "only-test-credential" not in panel.get("/api/messages").text
    assert TEST_TOKEN not in panel.get("/api/messages").text
    assert "sk-synthetic-secret" not in panel.get("/api/messages").text
    assert "only-test-credential" not in panel.get("/api/logs").text


def test_pause_resume_runs_only_simulated_work(environment, panel):
    assert panel.post("/api/runtime", json={"action": "pause"}).json()["paused"]
    assert panel.get("/api/status").json()["paused"]
    assert panel.post("/api/runtime", json={"action": "simulate"}).status_code == 409
    assert panel.post("/api/runtime", json={"action": "resume"}).json()["paused"] is False
    output = panel.post("/api/runtime", json={"action": "simulate"})
    assert output.status_code == 200, output.text
    assert output.json()["mode"] == "simulation" and output.json()["state"] == "sent"
    status = panel.get("/api/status").json()
    assert status["stats"]["sent"] == 0
    assert status["stats"]["received"] == status["stats"]["simulated_sent"] == 1
    assert panel.get("/api/messages").json()["items"][0]["mode"] == "simulation"


def test_manual_demo_preserves_jobs_from_other_rooms(environment, panel):
    store, _ = environment
    job = store.accept(event(1, room="301"), 4, time.time())
    assert panel.post("/api/runtime", json={"action": "simulate"}).status_code == 200
    assert store.get_job(job)["state"] == "evaluate"
