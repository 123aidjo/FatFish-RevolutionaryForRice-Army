from dataclasses import asdict

from adapter_base import Capabilities, SendResult
from events import Event, ReplyIntent


def test_event_preserves_reply_and_images():
    event = Event("qq", "room", "group", "1", "2", "小鱼", "你好",
                  True, False, "previous", ["https://example.test/image"], 1.5)
    assert Event(**asdict(event)) == event
    assert event.reply_to == "previous"
    assert event.images == ["https://example.test/image"]


def test_adapter_result_can_report_uncertain():
    reply = ReplyIntent("qq", "room", "你好", "joy", None, "1", False)
    caps = Capabilities(True, True, False, False, 40)
    assert reply.reply_to == "1"
    assert len(reply.text) <= caps.max_chars
    assert SendResult("uncertain", "发送超时").message_id is None
