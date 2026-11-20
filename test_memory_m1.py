import asyncio
from contextlib import asynccontextmanager, closing
import shutil
import sqlite3
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from adapter_qq import QQAdapter
from dispatcher import Dispatcher
from events import ReplyIntent
from live_qq import BOT, CHANNEL, GROUP, LiveQQ, consumer_lock, get_value, save_value, verification
from onebot_ws import OneBotSocket
from panel import create_app
from settings import Limits, PROJECT_ROOT, PlatformSettings, load_settings
from stickers import StickerPolicy
from store import Store
from test_qq_engine import MockDeepSeek, MockOneBot, NOW, REPLY, ok, raw


def group_raw(number, *, moment=NOW, group=GROUP, mention=True):
    packet = raw(number, room=int(group), mention=BOT if mention else None, moment=moment)
    packet["self_id"] = int(BOT)
    return packet


def fake_dispatcher(store, rpc, *, reply=lambda: True, cutoff=lambda: 0, memory=lambda key: True):
    adapter = QQAdapter(rpc, BOT)
    adapter.mode = "live"
    config = load_settings(database_path=store.path)
    config = config.model_copy(update={
        "platforms": {**config.platforms, "qq": PlatformSettings(
            enabled=True, send=True, receive=True, allowlist=[GROUP])},
        "limits": Limits(minimum_interval=0),
    })
    return Dispatcher(store, config, adapter, MockDeepSeek(), clock=lambda: NOW,
                      reply_enabled=reply, reply_since=cutoff, memory_enabled=memory)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "fishbot.db")
    result.initialize()
    return result


def test_paused_receives_deduplicates_and_never_replays(store):
    rpc = MockOneBot()
    engine = fake_dispatcher(store, rpc, reply=lambda: False)
    job = engine.record(group_raw(1))
    assert store.get_job(job)["detail"] == "sending_disabled_or_not_allowed"
    assert store.context(CHANNEL, 32)[0][0]["speaker"] == "PEER"
    assert engine.model.generations == [] and rpc.sends == []
    restarted = fake_dispatcher(Store(store.path), rpc, cutoff=lambda: NOW)
    assert restarted.record(group_raw(1)) is None
    assert restarted.record(group_raw(2, group="99999")) is None
    older = restarted.record(group_raw(3, moment=NOW - 1))
    assert store.get_job(older)["detail"] == "reply_window_closed"
    asyncio.run(restarted.tick(channel=CHANNEL, summarize=False))
    assert not rpc.sends
    recent = restarted.record(group_raw(4))
    asyncio.run(restarted.tick(channel=CHANNEL, summarize=False))
    assert store.get_job(recent)["state"] == "sent" and len(rpc.sends) == 1
    with closing(store.open_read()) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 3


def test_memory_permission_is_independent_and_persisted(store):
    engine = fake_dispatcher(store, MockOneBot(), memory=lambda key: False)
    job = engine.record(group_raw(1))
    asyncio.run(engine.tick(channel=CHANNEL))
    assert store.get_job(job)["state"] == "sent"
    assert store.get_event(*CHANNEL, "1").memory_allowed is False
    assert engine.model.generations[0][1]["最近对话"] == []
    assert store.context(CHANNEL, 32) == ([], "")
    restarted = fake_dispatcher(Store(store.path), MockOneBot())
    assert restarted.record(group_raw(1)) is None
    assert store.get_event(*CHANNEL, "1").memory_allowed is False


def test_sqlite_insert_rollback_and_retry_original_event(store):
    runtime = LiveQQ(store)
    engine = fake_dispatcher(store, MockOneBot(), reply=lambda: False)

    async def scenario():
        with store.transaction() as db:
            db.execute("""CREATE TRIGGER force_failure BEFORE INSERT ON outbox
                BEGIN SELECT RAISE(ABORT, 'test'); END""")
        pending = asyncio.create_task(runtime._persist(engine, group_raw(1), asyncio.Event()))
        try:
            async with asyncio.timeout(2):
                while not runtime.storage_error:
                    await asyncio.sleep(0.01)
            assert store.get_event(*CHANNEL, "1") is None
            assert store.context(CHANNEL, 32) == ([], "")
            assert runtime.receive_errors == 1
        finally:
            with store.transaction() as db:
                db.execute("DROP TRIGGER force_failure")
            await asyncio.wait_for(pending, 3)

    asyncio.run(scenario())
    assert not runtime.storage_error and runtime.last_recovered_at and runtime.last_received_at
    assert store.get_event(*CHANNEL, "1")
    assert len(store.context(CHANNEL, 32)[0]) == 1


def test_socket_receive_does_not_depend_on_send_and_filters_scope(store):
    async def scenario():
        rpc = OneBotSocket("ws://mock", "", int(GROUP))

        async def frames():
            import json
            yield json.dumps(group_raw(1))
            yield json.dumps(group_raw(2, group="98765"))

        rpc.receive_enabled = True
        rpc.socket = frames()
        await rpc._read()
        assert (await rpc.events.get())["message_id"] == 1
        assert rpc.events.empty()
        assert (await rpc.call("send_group_msg", {"group_id": int(GROUP)}))["retcode"] == 1403

    asyncio.run(scenario())


def test_live_worker_records_while_paused_even_without_model_credentials(store, monkeypatch):
    class ConnectedMock(MockOneBot):
        def __init__(self):
            super().__init__()
            self.events = asyncio.Queue()
            self.closed = asyncio.Event()
            self.dropped_events = 0
            self.receive_enabled = self.send_enabled = False

        async def call(self, action, parameters):
            if action == "get_status":
                return ok({"online": True})
            if action == "get_group_info":
                return ok({"group_id": int(GROUP)})
            return await super().call(action, parameters)

    runtime = LiveQQ(store)
    monkeypatch.setattr(runtime, "key", lambda: "")
    runtime.connected = True
    save_value(store, "live_qq_enabled", "1")
    save_value(store, "live_send_paused", "manual_pause")

    async def scenario():
        rpc = ConnectedMock()
        worker = asyncio.create_task(runtime.consume(rpc))
        try:
            async with asyncio.timeout(2):
                while not rpc.receive_enabled:
                    await asyncio.sleep(0.01)
                await rpc.events.put(group_raw(99, moment=runtime.start_cutoff))
                while not runtime.last_received_at:
                    await asyncio.sleep(0.01)
            assert rpc.receive_enabled and not rpc.send_enabled
        finally:
            rpc.closed.set()
            await asyncio.wait_for(worker, 3)
        assert not rpc.sends

    asyncio.run(scenario())
    assert store.get_job(1)["detail"] == "sending_disabled_or_not_allowed"
    assert store.get_event(*CHANNEL, "99").memory_allowed is True
    assert len(store.context(CHANNEL, 32)[0]) == 1


def test_quote_lookup_happens_after_durable_admission(store):
    rpc = MockOneBot()
    engine = fake_dispatcher(store, rpc)
    rpc.quotes[99] = ok({**group_raw(99), "user_id": int(BOT)})
    packet = group_raw(1, mention=False)
    packet["message"].insert(0, {"type": "reply", "data": {"id": "99"}})
    job = engine.record(packet)
    assert store.get_event(*CHANNEL, "1").reply_author_id is None
    assert not rpc.calls
    asyncio.run(engine.tick(channel=CHANNEL, summarize=False))
    assert store.get_job(job)["state"] == "sent"
    assert store.get_event(*CHANNEL, "1").reply_author_id == BOT


def test_live_crash_recovery_keeps_old_uncertain_and_does_not_send(store):
    child = """
import os, sys
from pathlib import Path
from test_memory_m1 import *
s = Store(Path(sys.argv[1]))
engine = fake_dispatcher(s, MockOneBot())
for number in (1, 2, 3):
    job = engine.record(group_raw(number))
    if number == 3:
        break
    assert s.claim_evaluation(job, NOW + 60)
    s.finish_evaluation(job, NOW + 60, ReplyIntent("qq", GROUP, REPLY, "joy", None, str(number), False), "", (1, 1))
    if number == 1:
        assert s.claim_send(job, NOW, 10, Limits(minimum_interval=0), StickerPolicy())
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", child, str(store.path)], capture_output=True, timeout=10)
    assert result.returncode == 17, result.stderr
    assert store.discard_live_pending(CHANNEL, "reply_window_closed") == 2
    assert store.recover_expired(float("inf"), channel=CHANNEL, mode="live") == [1]
    engine = fake_dispatcher(store, MockOneBot(), reply=lambda: False)
    asyncio.run(engine.reconcile(1))
    asyncio.run(engine.tick(channel=CHANNEL, summarize=False))
    assert store.get_job(1)["state"] == "uncertain"
    assert store.get_job(1)["detail"] == "interrupted_send"
    assert store.get_job(2)["state"] == store.get_job(3)["state"] == "skipped"
    assert not engine.adapter.rpc.sends


def test_panel_status_and_independent_controls(store, tmp_path, monkeypatch):
    config = tmp_path / "conf"
    config.mkdir()
    for name in ("platforms.toml", "persona.toml"):
        shutil.copyfile(PROJECT_ROOT / name, config / name)
    settings = load_settings(config, database_path=store.path)
    runtime = LiveQQ(store)
    monkeypatch.setattr("live_qq.load_settings", lambda **kwargs: load_settings(config, **kwargs))
    monkeypatch.setattr(runtime, "key", lambda: "local-test-only")

    @asynccontextmanager
    async def simulated_running():
        yield

    monkeypatch.setattr(runtime, "running", simulated_running)
    runtime.receiving = runtime.connected = runtime.group_ready = True
    save_value(store, "live_qq_enabled", "1")
    save_value(store, "live_send_paused", "send_timeout")
    save_value(store, "live_deepseek_verified", verification(runtime.key(), settings.deepseek.name))
    (tmp_path / "panel.token").write_text("test-m1-token", encoding="ascii")
    origin = "http://127.0.0.1:8000"
    headers = {"Authorization": "Bearer test-m1-token", "Origin": origin}
    app = create_app(store, config_dir=config, live_qq=runtime)
    with TestClient(app, base_url=origin, client=("127.0.0.1", 8765)) as client:
        status = client.get("/api/status", headers=headers).json()
        qq = next(item for item in status["platforms"] if item["id"] == "qq")
        assert qq["connected"] and qq["receiving"] and qq["record_memory"] and not qq["authorized_send"]
        payload = {"enabled": False, "revision": status["revision"]}
        assert client.put("/api/platforms/qq/memory", headers={**headers, "Origin": "https://foreign.test"},
                          json=payload).status_code == 403
        saved = client.put("/api/platforms/qq/memory", headers=headers, json=payload).json()
        assert client.put("/api/platforms/qq/replies", headers=headers,
                          json={"enabled": True, "revision": status["revision"]}).status_code == 409
        resumed = client.put("/api/platforms/qq/replies", headers=headers,
                             json={"enabled": True, "revision": saved["revision"]})
        assert resumed.status_code == 200
        updated = client.get("/api/platforms", headers=headers).json()["platforms"][0]
        assert updated["receiving"] and updated["authorized_send"] and not updated["record_memory"]
        assert not get_value(store, "live_send_paused") and get_value(store, "live_reply_after")


def test_consumer_exclusion_and_backup(store, tmp_path):
    async def scenario():
        async with consumer_lock(store.path.parent) as first:
            async with consumer_lock(store.path.parent) as second:
                assert first and not second
        async with consumer_lock(store.path.parent) as third:
            assert third
    asyncio.run(scenario())
    fake_dispatcher(store, MockOneBot(), reply=lambda: False).record(group_raw(1))
    backup = tmp_path / "backup.db"
    with closing(store.open_read()) as source, closing(sqlite3.connect(backup)) as copy:
        source.backup(copy)
        assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA foreign_key_check").fetchall() == []
    restored = Store(backup)
    assert restored.get_event(*CHANNEL, "1")
    assert restored.get_job(1)["state"] == "skipped"
