from dataclasses import replace
import subprocess
import sys

from events import Event
from settings import PROJECT_ROOT
from store import Store


def example_event() -> Event:
    return Event("qq", "room", "group", "1", "2", "测试者", "测试中文",
                 False, True, None, [], 42.5)


def test_scoped_deduplication(tmp_path):
    store = Store(tmp_path / "fishbot.db")
    store.initialize()
    event = example_event()
    assert store.save_event(event)
    assert not store.save_event(replace(event, text="重复事件"))
    assert store.save_event(replace(event, platform="bilibili"))
    assert store.save_event(replace(event, channel_id="another-room"))
    assert store.save_event(replace(event, channel_kind="dm"))
    assert store.get_event(event.platform, event.channel_kind, event.channel_id, event.message_id) == event
    assert store.get_event(event.platform, event.channel_kind, event.channel_id, "absent") is None


def test_fresh_process_recovers_saved_events(tmp_path):
    # 子进程写入后退出，再通过新的连接验证恢复和持久化去重。
    path = tmp_path / "目录 带空格" / "fishbot.db"
    subprocess.run(
        [sys.executable, "-c", """
import sys
from pathlib import Path
from store import Store
from test_store import example_event
store = Store(Path(sys.argv[1]))
store.initialize()
assert store.save_event(example_event())
""", str(path)],
        cwd=PROJECT_ROOT,
        check=True,
    )
    store = Store(path)
    store.initialize()
    assert store.check_health()
    assert store.get_event("qq", "group", "room", "1") == example_event()
    assert not store.save_event(example_event())
