import asyncio
from contextlib import closing
import json

import pytest

from adapter_qq import QQAdapter
from dispatcher import Dispatcher
from live_qq import BOT, CHANNEL, GROUP, LiveQQ, get_value, save_value
from onebot_ws import OneBotSocket
from ratelimit import start_of_day
from settings import Limits, PlatformSettings, load_settings
from store import Store
from test_qq_engine import MockDeepSeek, MockOneBot, NOW, REPLY, ok


OWNER = "9990000003"
DM = ("qq", "dm", OWNER)
DAYTIME = start_of_day(NOW) + 14 * 3600


def packet(author=OWNER, *, at=DAYTIME):
    return {"post_type": "message", "message_type": "private", "self_id": int(BOT),
            "message_id": 1, "user_id": int(author), "time": at,
            "sender": {"user_id": int(author), "nickname": "群友也可冒用此昵称"},
            "message": [{"type": "text", "data": {"text": "今天忙完啦"}}]}


def engine_for(store, rpc):
    cfg = load_settings(database_path=store.path)
    cfg = cfg.model_copy(update={"limits": Limits(minimum_interval=0), "platforms": {
        **cfg.platforms, "qq": PlatformSettings(enabled=True, receive=True, send=True,
                                               allowlist=[f"group:{GROUP}", f"dm:{OWNER}"]),
    }})
    adapter = QQAdapter(rpc, BOT, owner_ids=(OWNER,))
    adapter.mode = "live"
    return Dispatcher(store, cfg, adapter, MockDeepSeek(), clock=lambda: DAYTIME)


def test_owner_dm_replies_without_mention_and_isolates_group_memory(tmp_path):
    from test_memory_m1 import group_raw
    store = Store(tmp_path / "fishbot.db")
    store.initialize()
    rpc = MockOneBot()
    engine = engine_for(store, rpc)
    engine.record(group_raw(2, moment=DAYTIME, mention=False))
    assert engine.record(packet("987654321")) is None
    job = engine.record(packet())
    assert engine.record(packet()) is None
    asyncio.run(engine.tick(channel=DM, summarize=False))
    assert store.get_job(job)["state"] == "sent"
    assert rpc.calls[-1] == ("send_private_msg", {
        "user_id": int(OWNER),
        "message": [{"type": "text", "data": {"text": REPLY}}],
    })
    assert json.loads(store.get_job(job)["intent"])["reply_to"] is None
    prompt, inputs = engine.model.generations[0]
    assert "语气温柔" in prompt
    assert [item["text"] for item in inputs["最近对话"]] == ["今天忙完啦"]
    assert len(store.context(CHANNEL, 32)[0]) == 1
    assert [item["speaker"] for item in store.context(DM, 32)[0]] == ["OWNER", "BOT"]


def test_unquoted_owner_reply_timeout_stays_uncertain_without_resending(tmp_path):
    store = Store(tmp_path / "fishbot.db")
    store.initialize()
    rpc = MockOneBot()
    rpc.delivery = TimeoutError()
    engine = engine_for(store, rpc)
    job = engine.record(packet())
    asyncio.run(engine.tick(channel=DM, summarize=False))
    assert store.get_job(job)["state"] == "uncertain"

    restarted = engine_for(store, rpc)
    assert asyncio.run(restarted.reconcile(job))["state"] == "uncertain"
    asyncio.run(restarted.tick(channel=DM, summarize=False))
    assert len(rpc.sends) == 1


def test_wire_restricts_private_events_and_replies_to_owner():
    async def checks():
        rpc = OneBotSocket("ws://mock", "", int(GROUP), allowed_owner=int(OWNER))
        assert rpc._allowed_message(packet())
        assert not rpc._allowed_message(packet("987654321"))
        assert not rpc._allowed_message({**packet(), "post_type": "message_sent"})
        rpc.send_enabled = True
        assert (await rpc.call("send_private_msg", {"user_id": 987654321}))["retcode"] == 1403
        with pytest.raises(ConnectionError, match="未连接"):
            await rpc.call("send_private_msg", {"user_id": int(OWNER)})

    asyncio.run(checks())


@pytest.mark.parametrize("eligible", [True, False])
def test_scheduled_checkin_respects_quiet_time_and_never_duplicates(tmp_path, monkeypatch, eligible):
    class FriendMock(MockOneBot):
        async def call(self, action, parameters):
            if action == "get_friend_list":
                return ok([{"user_id": int(OWNER)}])
            return await super().call(action, parameters)

    store = Store(tmp_path / "fishbot.db")
    store.initialize()
    owner_settings = load_settings(database_path=store.path).model_copy(update={
        "qq_owner_id": OWNER, "qq_owner_dm": True, "qq_owner_record_memory": True,
    })
    monkeypatch.setattr("live_qq.load_settings", lambda **kwargs: owner_settings)
    rpc = FriendMock()
    runtime = LiveQQ(store)
    engine = engine_for(store, rpc)
    schedule = f"live_owner_checkin:{OWNER}"
    asyncio.run(runtime._schedule_owner_checkin(rpc, DM, DAYTIME))
    assert int(get_value(store, schedule)) >= DAYTIME + 3600
    if not eligible:
        engine.record(packet(at=DAYTIME - 10))
    save_value(store, schedule, str(int(DAYTIME - 1)))
    asyncio.run(runtime._schedule_owner_checkin(rpc, DM, DAYTIME))
    asyncio.run(LiveQQ(store)._schedule_owner_checkin(rpc, DM, DAYTIME))
    with closing(store.open_read()) as conn:
        count = conn.execute("SELECT count(*) FROM outbox WHERE trigger='owner_checkin'").fetchone()[0]
    assert count == int(eligible)
    if eligible:
        asyncio.run(engine.tick(channel=DM, summarize=False))
        prompt = engine.model.generations[0][0]
        assert "本机计划向已授权的 OWNER 私聊问候" in prompt and "不催回复" in prompt
        assert [item["type"] for item in rpc.sends[0]["message"]] == ["text"]
        assert [item["speaker"] for item in store.context(DM, 32)[0]] == ["BOT"]
        assert int(get_value(store, schedule)) >= start_of_day(DAYTIME) + 86400
