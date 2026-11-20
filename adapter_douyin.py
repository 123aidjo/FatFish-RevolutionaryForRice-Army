from adapter_base import Capabilities, SendResult
from events import Event, ReplyIntent


class DouyinAdapter:
    """创作和账号授权可独立使用。作品评论、私信、直播事件尚未接通。"""

    platform = "douyin"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def normalize(self, raw: dict) -> Event | None:
        return None

    async def send(self, reply: ReplyIntent) -> SendResult:
        return SendResult("skipped", "douyin_outbound_unavailable")

    def capabilities(self) -> Capabilities:
        return Capabilities(False, False, False, False, 0)
