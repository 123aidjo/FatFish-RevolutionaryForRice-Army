from dataclasses import dataclass
from typing import Literal, Protocol

from events import Event, ReplyIntent

Platform = Literal["qq", "bilibili", "douyin"]
PLATFORMS = ("qq", "bilibili", "douyin")
SendStatus = Literal["sent", "skipped", "failed", "uncertain"]


@dataclass(frozen=True, slots=True)
class Capabilities:
    supports_mention: bool
    supports_reply: bool
    supports_sticker: bool
    supports_image: bool
    max_chars: int  # 0 表示适配器不额外限制字数。


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    detail: str = ""
    message_id: str | None = None


class Adapter(Protocol):
    """由各平台模块单独实现，核心仅使用标准事件与回复意图。"""

    platform: Platform

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def normalize(self, raw: dict) -> Event | None: ...
    async def send(self, reply: ReplyIntent) -> SendResult: ...
    def capabilities(self) -> Capabilities: ...
