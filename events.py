from dataclasses import dataclass
from typing import Literal

Speaker = Literal["OWNER", "BOT", "PEER", "SELF_UNKNOWN"]


@dataclass(frozen=True, slots=True)
class Event:
    platform: str
    channel_id: str
    channel_kind: str
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    mentions_self: bool
    mentions_others: bool
    reply_to: str | None
    images: list[str]
    timestamp: float
    is_self: bool = False
    speaker: Speaker = "PEER"
    reply_author_id: str | None = None
    memory_allowed: bool | None = None

    @property
    def channel_key(self) -> tuple[str, str, str]:
        return self.platform, self.channel_kind, self.channel_id

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (*self.channel_key, self.message_id)


@dataclass(frozen=True, slots=True)
class ReplyIntent:
    platform: str
    channel_id: str
    text: str
    emotion: str
    sticker_id: str | None
    reply_to: str | None
    proactive: bool
    channel_kind: str = "group"
