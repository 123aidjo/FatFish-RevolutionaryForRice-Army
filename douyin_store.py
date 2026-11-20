from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3


class DouyinStore:
    """使用独立 SQLite 文件，沿用 V2 数据目录及备份策略。"""

    def __init__(self, directory: Path):
        self.directory = directory / "douyin"
        self.path = self.directory / "operations.db"

    @contextmanager
    def connect(self, *, write: bool = False):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise sqlite3.DatabaseError("抖音运营库版本不受支持")
            if version == 0 or write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                if version == 0:
                    db.execute("CREATE TABLE IF NOT EXISTS oauth_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("""CREATE TABLE IF NOT EXISTS drafts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL DEFAULT 1,
                        source TEXT NOT NULL CHECK (source IN ('manual','simulation','deepseek')),
                        brief TEXT NOT NULL, content TEXT NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
                    db.execute("""CREATE TABLE IF NOT EXISTS model_usage (
                        id INTEGER PRIMARY KEY, created_at REAL NOT NULL, model TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('ok','failed')),
                        prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL)""")
                    db.execute("PRAGMA user_version=1")
                yield db
