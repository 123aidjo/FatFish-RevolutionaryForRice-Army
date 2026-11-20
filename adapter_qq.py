from dataclasses import replace
import math
import re
from typing import Protocol

from adapter_base import Capabilities, SendResult
from events import Event, ReplyIntent

CQ_CODE = re.compile(r"\[CQ:([a-z_]+)((?:,[^\[\]]*)?)\]")
ACCOUNT = re.compile(r"[1-9][0-9]{0,18}")
MESSAGE = re.compile(r"-?[0-9]{1,20}")


def account_id(value: object) -> str:
    value = str(value)
    return value if ACCOUNT.fullmatch(value) else ""


def message_id(value: object) -> str:
    value = str(value)
    return value if MESSAGE.fullmatch(value) else ""


def unescape(text: str, *, parameter: bool = False) -> str:
    if parameter:
        text = text.replace("&#44;", ",")
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def segments(message: object) -> list[tuple[str, dict]]:
    if isinstance(message, list):
        return [
            (item["type"], item["data"]) for item in message
            if isinstance(item, dict) and isinstance(item.get("type"), str)
            and isinstance(item.get("data"), dict)
        ]
    if not isinstance(message, str):
        return []
    result = []
    cursor = 0
    for match in CQ_CODE.finditer(message):
        if match.start() > cursor:
            result.append(("text", {"text": unescape(message[cursor:match.start()])}))
        fields = {}
        for field in match[2].split(",")[1:]:
            key, separator, value = field.partition("=")
            if separator:
                fields[key] = unescape(value, parameter=True)
        result.append((match[1], fields))
        cursor = match.end()
    if cursor < len(message):
        result.append(("text", {"text": unescape(message[cursor:])}))
    return result


class OneBotRPC(Protocol):
    async def call(self, action: str, parameters: dict) -> dict: ...


class QQAdapter:
    """OneBot 动作通过注入的 RPC 执行，阶段 2 只提供模拟传输。"""

    platform = "qq"
    mode = "simulation"

    def __init__(
        self, rpc: OneBotRPC, bot_id: str, owner_ids: tuple[str, ...] = (),
        sticker_files: dict[str, str] | None = None,
    ) -> None:
        if not account_id(bot_id):
            raise ValueError("机器人账号编号无效")
        self.rpc = rpc
        self.bot_id = bot_id
        self.owner_ids = frozenset(owner_ids)
        self.sticker_files = sticker_files or {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def capabilities(self) -> Capabilities:
        return Capabilities(True, True, bool(self.sticker_files), True, 0)

    def normalize(self, raw: dict) -> Event | None:
        if raw.get("post_type") != "message" or str(raw.get("self_id")) != self.bot_id:
            return None
        kind = {"group": "group", "private": "dm"}.get(str(raw.get("message_type")))
        author = self._author(raw)
        channel = self._channel(raw, kind)
        identifier = message_id(raw.get("message_id"))
        timestamp = raw.get("time")
        if (not kind or not author or not channel or not identifier
                or type(timestamp) not in (int, float) or not 0 < timestamp < 1e12):
            return None

        message = segments(raw.get("message", raw.get("raw_message")))
        sender = raw.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        name = sender.get("card") or sender.get("nickname") or author
        is_self = author == self.bot_id
        role = "SELF_UNKNOWN" if is_self else "OWNER" if author in self.owner_ids else "PEER"
        mentions = {str(data.get("qq")) for tag, data in message if tag == "at"}
        return Event(
            self.platform, channel, kind, identifier, author, str(name)[:120],
            "".join(data["text"] for tag, data in message
                    if tag == "text" and isinstance(data.get("text"), str)).strip(),
            self.bot_id in mentions,
            any(target == "all" or (account_id(target) and target != self.bot_id)
                for target in mentions),
            next((value for tag, data in message if tag == "reply"
                  and (value := message_id(data.get("id")))), None),
            [location for tag, data in message if tag == "image"
             and isinstance(location := data.get("url") or data.get("file"), str)],
            timestamp, is_self, role,
        )

    @staticmethod
    def _author(raw: dict) -> str:
        sender = raw.get("sender")
        fallback = sender.get("user_id") if isinstance(sender, dict) else None
        return account_id(raw.get("user_id", fallback))

    @staticmethod
    def _channel(raw: dict, kind: str | None) -> str:
        return account_id(raw.get("group_id") if kind == "group" else raw.get("user_id"))

    async def resolve_reply(self, event: Event) -> Event:
        if event.reply_to is None:
            return event
        # 引用段里的作者字段不能作为凭据。通过协议端查询，并核对会话与原消息 ID。
        result = await self.rpc.call("get_msg", {"message_id": int(event.reply_to)})
        data = result.get("data")
        if not self._ok(result) or not isinstance(data, dict):
            return event
        kind = "group" if event.channel_kind == "group" else "private"
        author = self._author(data)
        if kind == "group":
            same_channel = self._channel(data, "group") == event.channel_id
        else:
            # 私聊历史中的发送者和目标必须确认为本账号与当前对话方。
            same_channel = {author, account_id(data.get("target_id"))} == {
                self.bot_id, event.channel_id,
            }
        if (data.get("message_type") != kind or not same_channel
                or message_id(data.get("message_id")) != event.reply_to or not author):
            return event
        return replace(event, reply_author_id=author, mentions_self=event.mentions_self or author == self.bot_id)

    @staticmethod
    def _ok(result: dict) -> bool:
        return result.get("status") == "ok" and result.get("retcode") == 0

    async def send(self, reply: ReplyIntent) -> SendResult:
        channel = account_id(reply.channel_id)
        if not channel or reply.platform != self.platform or reply.channel_kind not in {"group", "dm"}:
            return SendResult("failed", "invalid_destination")
        content = []
        if reply.reply_to is not None:
            content.append({"type": "reply", "data": {"id": reply.reply_to}})
        content.append({"type": "text", "data": {"text": reply.text}})
        if reply.sticker_id is not None:
            image = self.sticker_files.get(reply.sticker_id)
            if not image:
                return SendResult("failed", "unknown_sticker")
            content.append({"type": "image", "data": {"file": image}})
        group = reply.channel_kind == "group"
        result = await self.rpc.call(
            "send_group_msg" if group else "send_private_msg",
            {"group_id" if group else "user_id": int(channel), "message": content},
        )
        data = result.get("data")
        if self._ok(result) and isinstance(data, dict) and (identifier := message_id(data.get("message_id"))):
            return SendResult("sent", "onebot_ack", identifier)
        code = result.get("retcode")
        if type(code) is int and code in {1400, 1401, 1403, 1404} and result.get("status") == "failed":
            return SendResult("failed", f"onebot_{code}")
        return SendResult("uncertain", f"onebot_{code}" if type(code) is int else "onebot_unknown")

    async def query_delivery(self, reply: ReplyIntent, since: float, until: float) -> str | None:
        if reply.channel_kind not in {"group", "dm"} or reply.reply_to is None or reply.sticker_id is not None:
            return None
        private = reply.channel_kind == "dm"
        action, peer = ("get_friend_msg_history", "user_id") if private else ("get_group_msg_history", "group_id")
        result = await self.rpc.call(action, {peer: int(reply.channel_id), "count": 50})
        data = result.get("data")
        if not self._ok(result) or not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        matches = set()
        for raw in data["messages"]:
            if not isinstance(raw, dict) or self._author(raw) != self.bot_id:
                continue
            moment = raw.get("time")
            if type(moment) not in (int, float) or not math.floor(since) <= moment <= until:
                continue
            if private and (raw.get("message_type") != "private"
                            or ("target_id" in raw and account_id(raw["target_id"]) != reply.channel_id)):
                continue
            if not private and self._channel(raw, "group") != reply.channel_id:
                continue
            content = segments(raw.get("message"))
            ids = [message_id(fields.get("id")) for tag, fields in content if tag == "reply"]
            body = "".join(str(fields.get("text", "")) for tag, fields in content if tag == "text")
            if ids == [reply.reply_to] and body == reply.text and (identifier := message_id(raw.get("message_id"))):
                matches.add(identifier)
        return next(iter(matches)) if len(matches) == 1 else None
