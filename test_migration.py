import json
import sqlite3

from store import Store


def test_phase_one_database_migrates_without_losing_messages(tmp_path):
    path = tmp_path / "phase-one.db"
    original = {
        "platform": "qq", "channel_id": "30", "channel_kind": "group", "message_id": "1",
        "sender_id": "20", "sender_name": "鱼友", "text": "旧库的一条测试消息",
        "mentions_self": False, "mentions_others": False, "reply_to": None,
        "images": [], "timestamp": 10.0,
    }
    db = sqlite3.connect(path)
    with db:
        db.execute("""
            CREATE TABLE events (
                platform TEXT NOT NULL, channel_kind TEXT NOT NULL, channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (platform, channel_kind, channel_id, message_id)
            )
        """)
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", ("qq", "group", "30", "1", json.dumps(original)))
        db.execute("PRAGMA user_version=1")
    db.close()

    store = Store(path)
    store.initialize()
    store.initialize()
    assert store.check_health()
    event = store.get_event("qq", "group", "30", "1")
    assert event.text == original["text"]
    assert (event.speaker, event.is_self, event.reply_author_id) == ("PEER", False, None)
    assert not store.save_event(event)
    assert store.jobs_in("pending", "sending", "uncertain") == []
