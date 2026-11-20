import json
from pathlib import Path
import time

from adapter_qq import QQAdapter
from dispatcher import Dispatcher
from llm import Completion, Usage
from settings import PlatformSettings, Settings
from stickers import StickerPolicy
from store import Store

DEMO_ROOM = "900000000001"


class DemoModel:
    def __init__(self, settings: Settings):
        self.persona = settings.persona

    async def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        if max_tokens == 128:
            return Completion('{"approved":true}', Usage(10, 4))
        if max_tokens == 2500:
            return Completion('{"summary":"模拟会话有人和大肥鱼打招呼。"}', Usage(20, 15))
        candidates = json.loads(user)["可选表情"]
        emotion = "joy" if "joy" in self.persona.emotions else self.persona.emotions[0]
        return Completion(json.dumps({
            "text": f"好耶，{self.persona.name}浮上来啦！你好呀～"[:self.persona.max_chars or None],
            "emotion": emotion, "intensity": 1, "sticker": next(iter(candidates), None),
            "shouldSend": True,
        }, ensure_ascii=False), Usage(42, 20))


class DemoOneBot:
    async def call(self, action: str, parameters: dict) -> dict:
        if action == "send_group_msg":
            return {"status": "ok", "retcode": 0, "data": {"message_id": time.time_ns()}}
        if action == "get_group_msg_history":
            return {"status": "ok", "retcode": 0, "data": {"messages": []}}
        return {"status": "failed", "retcode": 1404}


async def run_demo(
    store: Store, settings: Settings,
    stickers: StickerPolicy | None = None, files: dict[str, str] | None = None,
) -> dict:
    # 仅允许一间保留的本地演示会话。这里的收发开关仅作用于内存里的 OneBot 模拟器。
    settings = settings.model_copy(update={
        "platforms": {**settings.platforms, "qq": PlatformSettings(
            enabled=True, receive=True, send=True, allowlist=[DEMO_ROOM],
        )},
    })
    dispatcher = Dispatcher(store, settings, QQAdapter(DemoOneBot(), "100", sticker_files=files),
                            DemoModel(settings), stickers=stickers)
    await dispatcher.tick(channel=("qq", "group", DEMO_ROOM))
    event = {
        "post_type": "message", "message_type": "group", "self_id": 100, "group_id": int(DEMO_ROOM),
        "time": int(time.time()), "message_id": time.time_ns(), "user_id": 200,
        "sender": {"user_id": 200, "nickname": "模拟群友"},
        "message": [{"type": "at", "data": {"qq": "100"}},
                    {"type": "text", "data": {"text": "大肥鱼你好呀！"}}],
    }
    result = await dispatcher.ingest(event)
    if result.get("intent"):
        result["intent"] = json.loads(result["intent"])
    return result
