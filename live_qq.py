"""真实 QQ 连接，只接收并回复授权群。"""

import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import httpx

from local_runtime import apply_environment, local_url
apply_environment()

from adapter_qq import QQAdapter
from cognition import Cognition
from cognitive_store import CognitiveStore
from dispatcher import Dispatcher
from events import Event
from llm import DeepSeekClient, ModelError, parse_reply
from napcat_login import NapCatLogin, WEBUI_URL
from onebot_ws import OneBotSocket
from ratelimit import start_of_day
from settings import PlatformSettings, Settings, load_settings
from store import Store

_BOT_ENV = os.getenv("FISHBOT_QQ_BOT_ID", "").strip()
_GROUP_ENV = os.getenv("FISHBOT_QQ_GROUP_ID", "").strip()
BOT = _BOT_ENV or "9990000001"
GROUP = _GROUP_ENV or "9990000002"


def live_ids_configured() -> bool:
    return all(x.isascii() and x.isdecimal() and 5 <= len(x) <= 12 and x[0] != "0"
               for x in (_BOT_ENV, _GROUP_ENV))
CHANNEL = ("qq", "group", GROUP)
WS_URL = local_url(os.getenv("FISHBOT_ONEBOT_URL", "ws://127.0.0.1:13001"), "ws")
log = logging.getLogger(__name__)
DAY = 86400
CHECKIN_START = 10 * 3600
CHECKIN_END = 21 * 3600
CHECKIN_QUIET = 8 * 3600


def owner_channel(settings: Settings) -> tuple[str, str, str] | None:
    if settings.qq_owner_dm and settings.qq_owner_id and settings.qq_owner_id != BOT:
        return "qq", "dm", settings.qq_owner_id
    return None


def next_checkin_slot(earliest: float) -> int:
    day = start_of_day(earliest)
    begin = max(int(earliest), int(day) + CHECKIN_START)
    end = int(day) + CHECKIN_END
    if begin >= end:
        begin, end = int(day) + DAY + CHECKIN_START, end + DAY
    return begin + secrets.randbelow(end - begin)


def read_key(directory: Path) -> str:
    configured = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if configured:
        return configured
    from role_profiles import isolated
    paths = (directory / ".env",) if isolated() else (directory / ".env", Path(__file__).parent / ".env")
    for path in paths:
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                name, separator, value = line.partition("=")
                if separator and name.strip() == "DEEPSEEK_API_KEY":
                    return value.strip().strip('"\'')
    return ""


def get_value(store: Store, key: str) -> str:
    with closing(store.open_read()) as db:
        row = db.execute("SELECT value FROM panel_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""


def save_value(store: Store, key: str, value: str):
    with store.transaction() as db:
        db.execute("INSERT INTO panel_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))


@asynccontextmanager
async def consumer_lock(directory: Path):
    # 锁只保护真实 QQ 消费者，不锁住业务数据库。进程退出后由操作系统释放。
    with (directory / "qq-consumer.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
        else:
            yield True


def token_for(directory: Path) -> str:
    explicit = os.getenv("FISHBOT_ONEBOT_TOKEN", "")
    if explicit:
        return explicit
    config = directory / "napcat/config"
    for path in (config / f"onebot11_{BOT}.json", config / "onebot11.json"):
        if not path.is_file():
            continue
        entries = json.loads(path.read_text(encoding="utf-8-sig")).get("network", {}).get("websocketServers", [])
        for entry in entries:
            expected_port = str(urlsplit(WS_URL).port)
            # The original Docker setup maps host 13001 to container 3001.
            ports = {expected_port, "3001"} if expected_port == "13001" else {expected_port}
            if entry.get("enable") and str(entry.get("port")) in ports:
                return entry.get("token") or entry.get("accessToken") or ""
    return ""


async def authenticate(rpc: OneBotSocket) -> dict:
    result = await rpc.call("get_login_info", {})
    data = result.get("data")
    if result.get("retcode") != 0 or not isinstance(data, dict):
        raise ConnectionError("QQ 尚未登录")
    if str(data.get("user_id")) != BOT:
        raise ConnectionError("QQ 账号与授权账号不符")
    await require_online(rpc)
    return data


async def require_online(rpc: OneBotSocket) -> None:
    for _ in range(2):
        try:
            async with asyncio.timeout(4):
                status = await rpc.call("get_status", {})
        except TimeoutError:
            continue
        data = status.get("data")
        if status.get("retcode") == 0 and isinstance(data, dict):
            if data.get("online") is True:
                return
            if data.get("online") is False:
                raise ConnectionError("QQ 账号未在线")
    raise ConnectionError("OneBot 未返回有效的 QQ 在线状态")


def verification(key: str, name: str) -> str:
    return hmac.digest(key.encode(), ("fishbot-deepseek:" + name).encode(), hashlib.sha256).hex()


class LiveQQ:
    def __init__(self, store: Store):
        self.store = store
        self.directory = store.path.parent
        self.napcat_login = NapCatLogin(Path(os.getenv("FISHBOT_NAPCAT_WEBUI_CONFIG")
                                             or self.directory / "napcat/config/webui.json"))
        self.connected = False
        self.group_ready = False
        self.nickname = ""
        self.error = ""
        self.napcat = False
        self.task = None
        self.receiving = False
        self.storage_error = False
        self.receive_errors = 0
        self.last_received_at = None
        self.last_recovered_at = None
        self.last_error = ""
        self.failure_hold = False
        self.start_cutoff = 0
        self.wake = asyncio.Event()

    def enabled(self) -> bool:
        return get_value(self.store, "live_qq_enabled") == "1"

    def proactive(self) -> bool:
        return get_value(self.store, "live_qq_proactive") == "1"

    def key(self) -> str:
        return read_key(self.directory)

    def verified(self, key: str, model: str) -> bool:
        return bool(key and get_value(self.store, "live_deepseek_verified") == verification(key, model))

    def sending(self, model: str) -> bool:
        return (self.enabled() and self.connected and self.group_ready and self.verified(self.key(), model)
                and not self.failure_hold
                and not self.storage_error
                and not get_value(self.store, "live_send_paused"))

    def memory_allowed(self, channel: tuple[str, str, str] = CHANNEL) -> bool:
        settings = load_settings(database_path=self.store.path)
        if channel == CHANNEL:
            return settings.platforms["qq"].record_memory
        return channel == owner_channel(settings) and settings.qq_owner_record_memory

    def reply_cutoff(self) -> float:
        saved = get_value(self.store, "live_reply_after")
        return max(self.start_cutoff, int(saved) if saved.isdigit() else 0)

    def set_reply_enabled(self, enabled: bool, model: str) -> None:
        if enabled and (not self.connected or not self.group_ready or not self.enabled()
                        or not self.verified(self.key(), model) or self.storage_error):
            raise ValueError("QQ、授权群、数据库和已核验模型均就绪后才能恢复回复。")
        with self.store.transaction() as db:
            if enabled and db.execute("""SELECT 1 FROM outbox WHERE mode='live' AND platform='qq'
                    AND state='sending' LIMIT 1""").fetchone():
                raise ValueError("仍有发送中的任务，请等待该任务落账后再恢复。")
            db.execute("""INSERT INTO panel_state VALUES('live_reply_after',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(int(time.time()) + 1),))
            if enabled:
                db.execute("DELETE FROM panel_state WHERE key='live_send_paused'")
            else:
                db.execute("""INSERT INTO panel_state VALUES('live_send_paused','manual_pause')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""")
            self.store.record_panel_action(db, "qq_resume" if enabled else "qq_pause")
        if enabled:
            self.failure_hold = False
            self.last_recovered_at = time.time()
        self.wake.set()

    async def reserve(self, request: httpx.Request):
        # 每个真实 HTTP 尝试都计数，重试也消耗额度。
        proactive = self.proactive()
        name = f"live_model_calls:{int(start_of_day(time.time()))}" if proactive else "live_model_trials"
        maximum = max(0, 2 * load_settings().limits.global_daily) if proactive else 5
        with self.store.transaction() as db:
            row = db.execute("SELECT value FROM panel_state WHERE key=?", (name,)).fetchone()
            count = int(row[0]) if row else 0
            if count >= maximum:
                raise ModelError("live_model_budget")
            db.execute("INSERT INTO panel_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (name, str(count + 1)))

    def rpc(self) -> OneBotSocket:
        dm = owner_channel(load_settings(database_path=self.store.path))
        return OneBotSocket(WS_URL, token_for(self.directory), int(GROUP),
                            allowed_owner=int(dm[2]) if dm else None)

    def snapshot(self, model: str) -> dict:
        key = self.key()
        settings = load_settings(database_path=self.store.path)
        dm = owner_channel(settings)
        checkin_at = get_value(self.store, f"live_owner_checkin:{dm[2]}") if dm and settings.qq_owner_checkins else ""
        with closing(self.store.open_read()) as db:
            uncertain = db.execute("""SELECT count(*) FROM outbox WHERE mode='live'
                AND platform='qq' AND state='uncertain'""").fetchone()[0]
        return {"enabled": self.enabled(), "connected": self.connected,
                "logged_in": self.connected, "account": BOT, "nickname": self.nickname,
                "napcat": self.napcat, "authorized_send": self.sending(model),
                "allowlist": [GROUP], "proactive": self.proactive(),
                "owner_dm": dm[2] if dm else None,
                "owner_record_memory": bool(dm and settings.qq_owner_record_memory),
                "owner_checkins": bool(dm and settings.qq_owner_checkins),
                "owner_checkin_at": int(checkin_at) if checkin_at.isdigit() else None,
                "configured": bool(key), "verified": self.verified(key, model),
                "webui_url": WEBUI_URL,
                "receiving": self.receiving and not self.storage_error,
                "receive_errors": self.receive_errors, "record_memory": self.memory_allowed(),
                "storage_error": self.storage_error, "uncertain": uncertain,
                "last_received_at": self.last_received_at, "last_recovered_at": self.last_recovered_at,
                "last_error": self.last_error,
                "error": self.error or get_value(self.store, "live_send_paused")}

    @asynccontextmanager
    async def running(self):
        if self.task and not self.task.done():
            raise RuntimeError("QQ 消费者已运行")
        async with consumer_lock(self.directory) as acquired:
            if not acquired:
                self.error = "已有 V2 QQ 消费者在运行"
                yield
                return
            settings = load_settings(database_path=self.store.path)
            engine = Cognition(self.store, settings.cognition)
            self.task = asyncio.create_task(self.loop())
            worker = asyncio.create_task(engine.worker("live", self._cognitive_allowed))
            try:
                yield
            finally:
                self.task.cancel()
                worker.cancel()
                await asyncio.gather(self.task, worker, return_exceptions=True)

    def _cognitive_allowed(self, event: Event) -> bool:
        settings = load_settings(database_path=self.store.path)
        from memory_engine import read_config
        return ((settings.cognition.extraction or read_config(self.store)[0].llm_extraction) and self.enabled()
                and event.channel_key in (CHANNEL, owner_channel(settings)) and self.memory_allowed(event.channel_key))

    @staticmethod
    def _webui_available() -> bool:
        try:
            with build_opener(ProxyHandler({})).open(WEBUI_URL, timeout=2) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    async def _connection_error(self, exc: Exception) -> str:
        if isinstance(exc, sqlite3.Error):
            return "QQ 持久化暂不可用"
        if isinstance(exc, ConnectionError) and str(exc) in {
            "QQ 账号与授权账号不符", "QQ 尚未加入授权群",
        }:
            return str(exc)
        if not self.napcat:
            return "V2 NapCat 本机服务暂不可用，正在重连"
        try:
            status = await asyncio.to_thread(self.napcat_login.login_state)
        except (OSError, ValueError, KeyError, TypeError):
            return "无法读取 NapCat 登录状态，OneBot 正在重连"
        blocker = self.napcat_login.login_blocker(status)
        if blocker:
            return blocker
        phase = self.napcat_login.phase(status)
        return {
            "waiting_qrcode": "QQ 正在等待扫码登录",
            "qrcode_scanned": "已收到 QQ 扫码，请在手机上确认登录",
            "initializing": "QQ 已获登录确认，正在初始化 OneBot",
            "reconnecting": "NapCat 正在尝试恢复 QQ 会话",
            "online": "NapCat 显示 QQ 已登录，V2 正在重连 OneBot",
        }.get(phase, "OneBot 正在重连，NapCat 尚未报告 QQ 在线")

    async def loop(self):
        if not live_ids_configured():
            return
        delay = 2
        while True:
            started = time.monotonic()
            try:
                self.napcat = await asyncio.to_thread(self._webui_available)
                async with self.rpc().opened() as rpc:
                    account = await authenticate(rpc)
                    self.connected = True
                    self.nickname = str(account.get("nickname") or "鲸小鱼")[:60]
                    self.error = ""
                    await self.consume(rpc)
                    if rpc.closed.is_set():
                        raise ConnectionError("OneBot 连接已中断")
            except (OSError, ValueError, TimeoutError, ModelError, sqlite3.Error) as exc:
                self.connected = self.group_ready = self.receiving = False
                self.error = await self._connection_error(exc)
            finally:
                if self.error:
                    if self.last_error != self.error:
                        log.warning("%s", self.error)
                    self.last_error = self.error
                self.connected = False
                self.group_ready = False
                self.receiving = False
                self.nickname = ""
            if time.monotonic() - started >= 60:
                delay = 2
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def consume(self, rpc: OneBotSocket):
        group = await rpc.call("get_group_info", {"group_id": int(GROUP)})
        group_data = group.get("data")
        if group.get("retcode") != 0 or not isinstance(group_data, dict) or str(group_data.get("group_id")) != GROUP:
            raise ConnectionError("QQ 尚未加入授权群")
        settings = load_settings(database_path=self.store.path)
        dm = owner_channel(settings)
        channels = (CHANNEL, dm) if dm else (CHANNEL,)
        self.start_cutoff = int(time.time()) + 1
        for channel in channels:
            self.store.discard_live_pending(channel, "reply_window_closed")
        settings = settings.model_copy(update={"platforms": {
            **settings.platforms,
            "qq": PlatformSettings(enabled=True, receive=True, send=True,
                                   allowlist=[f"{kind}:{identifier}" for _, kind, identifier in channels]),
        }})
        async with httpx.AsyncClient(trust_env=False, event_hooks={"request": [self.reserve]}) as client:
            owner_ids = (settings.qq_owner_id,) if settings.qq_owner_id else ()
            adapter = QQAdapter(rpc, BOT, owner_ids=owner_ids)
            adapter.mode = "live"
            dispatcher = Dispatcher(self.store, settings, adapter, None, proactive_enabled=self.proactive,
                                    reply_enabled=lambda: self.sending(settings.deepseek.name),
                                    reply_since=self.reply_cutoff, memory_enabled=self.memory_allowed)
            for channel in channels:
                for job in self.store.recover_expired(float("inf"), channel=channel, mode="live"):
                    self._on_result(await dispatcher.reconcile(job))
            self.group_ready = True
            lost = 0
            if self.last_error:
                self.last_recovered_at = time.time()
            sender = asyncio.create_task(self._send_loop(rpc, dispatcher, client, channels))
            try:
                while not rpc.closed.is_set() or not rpc.events.empty():
                    if sender.done():
                        sender.result()
                    self.receiving = rpc.receive_enabled = self.enabled() and not rpc.closed.is_set()
                    if rpc.dropped_events > lost:
                        self.receive_errors += rpc.dropped_events - lost
                        lost = rpc.dropped_events
                        self.last_error = "QQ 接收队列已满，部分消息未入库"
                    try:
                        raw = await asyncio.wait_for(rpc.events.get(), timeout=1)
                    except TimeoutError:
                        continue
                    await self._persist(dispatcher, raw, rpc.closed)
            finally:
                self.receiving = rpc.receive_enabled = rpc.send_enabled = False
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def _persist(self, dispatcher: Dispatcher, raw: dict, closed: asyncio.Event):
        delay = 0.25
        event = dispatcher.adapter.normalize(raw)
        if event is None:
            return
        allow_memory = dispatcher.memory_enabled(event.channel_key)
        while True:
            try:
                job = await asyncio.to_thread(dispatcher.record, raw, allow_memory=allow_memory)
            except sqlite3.Error:
                if not self.storage_error:
                    self.last_error = "QQ 消息写库失败，正在重试原事件"
                    log.warning("%s", self.last_error)
                    self.receive_errors += 1
                self.storage_error = True
                if closed.is_set():
                    # 断线后仍保留手中的原事件，有限重试失败后由面板提示丢失。
                    if delay >= 2:
                        self.last_error = "QQ 断线后写库仍失败，部分消息未入库"
                        log.warning("%s", self.last_error)
                        raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 4)
            else:
                if self.storage_error:
                    self.last_recovered_at = time.time()
                    log.info("QQ 写库已恢复")
                self.storage_error = False
                if job is not None:
                    self.last_received_at = time.time()
                    self.wake.set()
                return

    async def _send_loop(self, rpc: OneBotSocket, dispatcher: Dispatcher, client: httpx.AsyncClient,
                         channels: tuple[tuple[str, str, str], ...]):
        refresh = 0.0
        model = dispatcher.settings.deepseek
        model_config = ("", 0)
        while not rpc.closed.is_set():
            self.wake.clear()
            try:
                if refresh <= time.monotonic():
                    rpc.send_enabled = False
                    await require_online(rpc)
                    refresh = time.monotonic() + 30
                if self.failure_hold and not get_value(self.store, "live_send_paused"):
                    save_value(self.store, "live_send_paused", "storage_error")
                key, attempts = self.key(), 2 if self.proactive() else 1
                if model_config != (key, attempts):
                    model_config = key, attempts
                    dispatcher.model = DeepSeekClient(model, key, client, attempts=attempts) if key else None
                    dispatcher.memory.model = dispatcher.model
                rpc.send_enabled = self.sending(model.name) and dispatcher.model is not None
                summarize = self.last_received_at is not None and self.last_received_at >= self.reply_cutoff()
                for channel in channels:
                    if channel[1] == "dm" and dispatcher.settings.qq_owner_checkins and rpc.send_enabled:
                        await self._schedule_owner_checkin(rpc, channel, time.time())
                    await dispatcher.tick(channel=channel, on_result=self._on_result, summarize=summarize)
            except sqlite3.Error:
                if not self.failure_hold:
                    log.warning("QQ 回复落账异常，已暂停后续发送")
                self.last_error = "QQ 回复落账异常，需检查待核查任务"
                self.failure_hold = True
                rpc.send_enabled = False
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=3)
            except TimeoutError:
                pass

    async def _schedule_owner_checkin(self, rpc: OneBotSocket, channel: tuple[str, str, str], now: float):
        """每个自然日最多一个问候任务。进程恢复时已有的任务继续走 Outbox 安全规则。"""
        state_key = f"live_owner_checkin:{channel[2]}"
        scheduled = get_value(self.store, state_key)
        if not scheduled.isdigit():
            save_value(self.store, state_key, str(next_checkin_slot(now + 3600)))
            return
        day = start_of_day(now)
        if now < int(scheduled) or now < self.reply_cutoff() or not day + CHECKIN_START <= now < day + CHECKIN_END:
            return
        with closing(self.store.open_read()) as db:
            last = db.execute("""SELECT max(json_extract(payload, '$.timestamp')) FROM events e
                JOIN outbox o USING(platform,channel_kind,channel_id,message_id)
                WHERE e.platform=? AND e.channel_kind=? AND e.channel_id=?
                AND o.mode='live' AND json_extract(payload, '$.speaker')='OWNER'""", channel).fetchone()[0]
        if last is not None and now - last < CHECKIN_QUIET:
            return
        next_day = day + (1 + secrets.randbelow(3)) * DAY
        save_value(self.store, state_key, str(next_checkin_slot(next_day)))
        try:
            result = await rpc.call("get_friend_list", {})
        except (OSError, TimeoutError):
            log.warning("主人 QQ 好友身份暂无法核对，本轮问候已跳过")
            return
        friends = result.get("data")
        if (result.get("retcode") != 0 or not isinstance(friends, list)
                or not any(isinstance(friend, dict) and str(friend.get("user_id")) == channel[2] for friend in friends)):
            return
        event = Event("qq", channel[2], "dm", f"owner-checkin:{int(day)}", BOT, "大肥鱼",
                      "本机计划的主动问候", False, False, None, [], now,
                      is_self=True, speaker="SELF_UNKNOWN", memory_allowed=self.memory_allowed(channel))
        self.store.accept(event, 0, now, mode="live", owner_checkin=True)

    def _on_result(self, result: dict) -> None:
        if (result["state"] == "sent" and result.get("mode") == "live"
                and result["detail"] == "onebot_ack" and result.get("delivery_id")):
            try:
                self._record_cognitive_reply(result)
            except sqlite3.Error:
                log.warning("M2 暂未记录回复里程碑")
        if result["state"] == "uncertain" or result["detail"] in {
            "onebot_1403", "deepseek_http_402", "deepseek_http_401", "deepseek_http_429", "live_model_budget",
        }:
            save_value(self.store, "live_send_paused", result["detail"])

    def _record_cognitive_reply(self, result: dict) -> None:
        CognitiveStore(self.store).system_fact(
            "首次确认回复", "M2 就绪后，首次观察到一条 QQ 回复已确认送达。",
            source=f"outbox:{result['id']}", at=result["attempted_at"],
        )


async def console(action: str):
    if not live_ids_configured():
        raise ModelError('Set FISHBOT_QQ_BOT_ID and FISHBOT_QQ_GROUP_ID before connecting to OneBot.')
    settings = load_settings()
    store = Store(settings.database_path)
    store.initialize()
    runtime = LiveQQ(store)
    if action == "status":
        async with runtime.rpc().opened() as rpc:
            account = await authenticate(rpc)
            print(json.dumps({"online": True, "account": str(account["user_id"]),
                              "nickname": str(account.get("nickname", ""))[:60]}, ensure_ascii=True))
        return
    if action == "proactive":
        with closing(store.open_read()) as db:
            row = db.execute("""
                SELECT id FROM outbox WHERE mode='live' AND platform='qq' AND channel_id=?
                AND trigger='direct' AND state='sent' AND delivery_id IS NOT NULL LIMIT 1
            """, (GROUP,)).fetchone()
        if row is None:
            raise ModelError("还没有已确认发送的真实直接回复")
        save_value(store, "live_qq_proactive", "1")
        print("主动接话已启用")
        return
    key = runtime.key()
    if not key:
        raise ModelError("V2 尚未配置 DeepSeek API Key")
    async with runtime.rpc().opened() as rpc:
        await authenticate(rpc)
        group = await rpc.call("get_group_info", {"group_id": int(GROUP)})
        details = group.get("data")
        if group.get("retcode") != 0 or not isinstance(details, dict) or str(details.get("group_id")) != GROUP:
            raise ConnectionError("QQ 尚未加入授权群")
    async with httpx.AsyncClient(trust_env=False, event_hooks={"request": [runtime.reserve]}) as client:
        model = DeepSeekClient(settings.deepseek, key, client, attempts=1)
        completion = await model.complete(
            "只返回 JSON 对象：text 为简短中文问候，emotion 为 joy，intensity 为 1，sticker 为 null，shouldSend 为 true。",
            "你好", temperature=0, max_tokens=128,
        )
        reply = parse_reply(completion.content, 40)
        if not reply.text or not reply.should_send or completion.usage.prompt <= 0 or completion.usage.completion <= 0:
            raise ModelError("模型回复或 Token 统计无效")
    save_value(store, "live_deepseek_verified", verification(key, settings.deepseek.name))
    save_value(store, "live_qq_enabled", "1")
    print(json.dumps({"verified": True, "model": settings.deepseek.name,
                      "prompt_tokens": completion.usage.prompt,
                      "completion_tokens": completion.usage.completion}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "verify", "proactive"))
    try:
        asyncio.run(console(parser.parse_args().action))
    except (OSError, ValueError, TimeoutError, ModelError) as error:
        message = str(error) if isinstance(error, (ConnectionError, ModelError)) else type(error).__name__
        raise SystemExit(message) from None
