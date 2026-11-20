"""本机 NapCat OneBot v11 正向连接，分离 API 回包与事件。"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)


class OneBotSocket:
    def __init__(self, url: str, token: str, allowed_group: int, *, allowed_owner: int | None = None):
        self.url = url
        self.token = token
        self.allowed_group = allowed_group
        self.allowed_owner = allowed_owner
        self.socket = None
        self.events = asyncio.Queue(256)
        self.pending = {}
        self.closed = asyncio.Event()
        self.receive_enabled = False
        self.send_enabled = False
        self.dropped_events = 0

    @asynccontextmanager
    async def opened(self):
        headers = {"Authorization": "Bearer " + self.token} if self.token else None
        try:
            async with connect(self.url, additional_headers=headers, proxy=None, open_timeout=5,
                               close_timeout=2, max_size=4 * 1024 * 1024) as socket:
                self.socket = socket
                reader = asyncio.create_task(self._read())
                try:
                    yield self
                finally:
                    self.receive_enabled = False
                    self.send_enabled = False
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                    self.socket = None
        except WebSocketException as exc:
            raise ConnectionError(type(exc).__name__) from None

    async def _read(self):
        try:
            async for message in self.socket:
                try:
                    payload = json.loads(message)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                echo = payload.get("echo")
                if isinstance(echo, str) and (future := self.pending.get(echo)) is not None:
                    if not future.done():
                        future.set_result(payload)
                elif self.receive_enabled and self._allowed_message(payload):
                    try:
                        self.events.put_nowait(payload)
                    except asyncio.QueueFull:
                        self.dropped_events += 1
                        log.warning("QQ 消息队列已满，忽略新事件")
        except (OSError, WebSocketException):
            pass
        finally:
            self.receive_enabled = False
            self.send_enabled = False
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("OneBot 连接中断"))

    def _allowed_message(self, payload: dict) -> bool:
        if payload.get("post_type") != "message":
            return False
        kind = payload.get("message_type")
        return ((kind == "group" and str(payload.get("group_id")) == str(self.allowed_group))
                or (kind == "private" and self.allowed_owner is not None
                    and str(payload.get("user_id")) == str(self.allowed_owner)))

    async def call(self, action: str, parameters: dict) -> dict:
        if action.startswith("send_"):
            to_group = action == "send_group_msg" and parameters.get("group_id") == self.allowed_group
            to_owner = (action == "send_private_msg" and self.allowed_owner is not None
                        and parameters.get("user_id") == self.allowed_owner)
            if not self.send_enabled or not (to_group or to_owner):
                return {"status": "failed", "retcode": 1403}
        if self.socket is None or self.closed.is_set():
            raise ConnectionError("OneBot 未连接")
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.socket.send(json.dumps({"action": action, "params": parameters, "echo": echo}))
            return await asyncio.wait_for(future, 8)
        except WebSocketException:
            raise ConnectionError("OneBot 连接中断") from None
        finally:
            self.pending.pop(echo, None)
