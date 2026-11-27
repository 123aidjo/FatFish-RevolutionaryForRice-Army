from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import sqlite3
import time

from adapter_base import SendResult
from cognitive_schema import SCHEMA as COGNITIVE_SCHEMA
from events import Event, ReplyIntent
from policy import Decision, OWNER_CHECKIN, decide
from ratelimit import start_of_day
from settings import Limits
from stickers import StickerPolicy

SCHEMA_VERSION = 6

PANEL_SCHEMA = (
    """CREATE TABLE panel_audit (
        id INTEGER PRIMARY KEY, created_at REAL NOT NULL, action TEXT NOT NULL
    )""",
    "CREATE TABLE panel_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE panel_stickers (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)",
    "ALTER TABLE outbox ADD COLUMN generated_text TEXT",
)

STAGE_TWO_SCHEMA = (
    """
    CREATE TABLE conversations (
        platform TEXT NOT NULL, channel_kind TEXT NOT NULL, channel_id TEXT NOT NULL,
        ordinary_count INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        summary_cursor INTEGER NOT NULL DEFAULT 0,
        summary_at REAL,
        summary_checked_at REAL,
        PRIMARY KEY (platform, channel_kind, channel_id)
    )
    """,
    """
    CREATE TABLE memory_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL, channel_kind TEXT NOT NULL, channel_id TEXT NOT NULL,
        speaker TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX memory_by_channel ON memory_entries(platform, channel_kind, channel_id, id)",
    """
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL, channel_kind TEXT NOT NULL, channel_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
            ('evaluate','evaluating','pending','sending','sent','skipped','failed','uncertain')),
        trigger TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL CHECK(mode IN ('simulation','live')),
        intent TEXT, lease_until REAL, attempted_at REAL, attempt_until REAL,
        delivery_id TEXT, sticker_id TEXT, prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        UNIQUE (platform, channel_kind, channel_id, message_id),
        FOREIGN KEY (platform, channel_kind, channel_id, message_id)
            REFERENCES events(platform, channel_kind, channel_id, message_id)
    )
    """,
    "CREATE INDEX outbox_by_attempt ON outbox(mode, attempted_at)",
)


@dataclass(frozen=True, slots=True)
class SummaryWork:
    previous: str
    previous_at: float | None
    checked_at: float | None
    first_at: float
    count: int
    characters: int
    cursor: int
    entries: list[dict]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        mode = "rwc" if create else "rw"
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode={mode}", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _write(self, *, create: bool = False):
        with closing(self._connect(create=create)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write(create=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version not in {0, 1, 2, 3, 4, 5}:
                raise RuntimeError(f"SQLite 结构版本不受支持（{version}）")
            if version == 0:
                db.execute("""
                    CREATE TABLE events (
                        platform TEXT NOT NULL, channel_kind TEXT NOT NULL, channel_id TEXT NOT NULL,
                        message_id TEXT NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY (platform, channel_kind, channel_id, message_id)
                    )
                """)
            if version < 2:
                for sql in STAGE_TWO_SCHEMA:
                    db.execute(sql)
            if version < 3:
                for sql in PANEL_SCHEMA:
                    db.execute(sql)
            if version in {2, 3}:
                db.execute("ALTER TABLE outbox RENAME TO outbox_previous")
                db.execute("DROP INDEX outbox_by_attempt")
                db.execute(STAGE_TWO_SCHEMA[-2])
                db.execute(PANEL_SCHEMA[-1])
                db.execute("INSERT INTO outbox SELECT * FROM outbox_previous")
                db.execute("DROP TABLE outbox_previous")
                db.execute(STAGE_TWO_SCHEMA[-1])
            if version < 5:
                for sql in COGNITIVE_SCHEMA:
                    db.execute(sql)
            from memory_upgrade_schema import SCHEMA as UPGRADE_SCHEMA
            for sql in UPGRADE_SCHEMA:
                db.execute(sql)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def open_read(self) -> sqlite3.Connection:
        return self._connect()

    def transaction(self):
        return self._write()

    @staticmethod
    def record_panel_action(db: sqlite3.Connection, action: str) -> None:
        db.execute("INSERT INTO panel_audit(created_at, action) VALUES (?, ?)", (time.time(), action))

    def check_health(self) -> bool:
        try:
            with closing(self._connect()) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                db.execute("SELECT payload FROM events LIMIT 1").fetchone()
                db.execute("SELECT state FROM outbox LIMIT 1").fetchone()
                return version == SCHEMA_VERSION
        except sqlite3.Error:
            return False

    @staticmethod
    def _insert_event(db: sqlite3.Connection, event: Event) -> bool:
        cursor = db.execute("""
            INSERT INTO events (platform, channel_kind, channel_id, message_id, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (platform, channel_kind, channel_id, message_id) DO NOTHING
        """, (*event.key, json.dumps(asdict(event), ensure_ascii=False, allow_nan=False)))
        return cursor.rowcount == 1

    def save_event(self, event: Event) -> bool:
        with self._write() as db:
            return self._insert_event(db, event)

    def get_event(
        self, platform: str, channel_kind: str, channel_id: str, message_id: str
    ) -> Event | None:
        with closing(self._connect()) as db:
            row = db.execute("""
                SELECT payload FROM events
                WHERE platform = ? AND channel_kind = ? AND channel_id = ? AND message_id = ?
            """, (platform, channel_kind, channel_id, message_id)).fetchone()
        return Event(**json.loads(row[0])) if row else None

    def accept(self, event: Event, every: int, now: float, *, mode: str = "simulation",
               reply_block: str = "", defer_quote: bool = False, owner_checkin: bool = False,
               cognition: bool = False) -> int | None:
        with self._write() as db:
            if not self._insert_event(db, event):
                return None
            key = event.channel_key
            db.execute("""
                INSERT INTO conversations(platform, channel_kind, channel_id) VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING
            """, key)
            count = db.execute("""
                SELECT ordinary_count FROM conversations
                WHERE platform=? AND channel_kind=? AND channel_id=?
            """, key).fetchone()[0]
            if reply_block or owner_checkin:
                decision = Decision(reply_block or OWNER_CHECKIN, count)
            else:
                decision = decide(event, count, every)
            db.execute("""
                UPDATE conversations SET ordinary_count=?
                WHERE platform=? AND channel_kind=? AND channel_id=?
            """, (decision.next_count, *key))
            text = event.text or ("[图片]" if event.images else "")
            if text and not event.is_self and event.memory_allowed is not False:
                db.execute("INSERT INTO memory_entries VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
                           (*key, event.speaker, event.sender_name, text, now))
            needs_quote = (defer_quote and not reply_block and event.channel_kind == "group"
                           and event.reply_to and not event.mentions_self)
            trigger = "verify_quote" if needs_quote else decision.reason
            evaluate = bool(needs_quote or decision.evaluate)
            row = db.execute("""
                INSERT INTO outbox(platform, channel_kind, channel_id, message_id,
                                   state, trigger, detail, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (*event.key, "evaluate" if evaluate else "skipped", trigger,
                  "" if evaluate else decision.reason, mode))
            if cognition and event.memory_allowed is True and not event.is_self and event.sender_id and event.text.strip():
                scope = json.dumps([*key, mode], ensure_ascii=False, separators=(",", ":"))
                db.execute("""INSERT INTO memory_jobs(scope,mode,platform,channel_kind,channel_id,message_id,available)
                    VALUES (?,?,?,?,?,?,?)""", (scope, mode, *event.key, now))
            return row.lastrowid

    def classify_quote(self, job_id: int, lease: float, event: Event, every: int) -> Decision:
        with self._write() as db:
            count = db.execute("""SELECT ordinary_count FROM conversations
                WHERE platform=? AND channel_kind=? AND channel_id=?""", event.channel_key).fetchone()[0]
            decision = decide(event, count, every)
            updated = db.execute("""UPDATE outbox SET trigger=? WHERE id=?
                AND state='evaluating' AND lease_until=? AND trigger='verify_quote'""",
                (decision.reason, job_id, lease))
            if updated.rowcount:
                db.execute("UPDATE events SET payload=? WHERE platform=? AND channel_kind=? AND channel_id=? AND message_id=?",
                           (json.dumps(asdict(event), ensure_ascii=False, allow_nan=False), *event.key))
                db.execute("""UPDATE conversations SET ordinary_count=?
                    WHERE platform=? AND channel_kind=? AND channel_id=?""", (decision.next_count, *event.channel_key))
            return decision

    def discard_live_pending(self, channel: tuple[str, str, str], reason: str) -> int:
        with self._write() as db:
            return db.execute("""UPDATE outbox SET state='skipped', detail=?, lease_until=NULL
                WHERE mode='live' AND platform=? AND channel_kind=? AND channel_id=?
                AND state IN ('evaluate','evaluating','pending')""", (reason, *channel)).rowcount

    def skip_evaluation(self, job_id: int, reason: str) -> None:
        with self._write() as db:
            db.execute("UPDATE outbox SET state='skipped', detail=? WHERE id=? AND state='evaluate'", (reason, job_id))

    def get_job(self, job_id: int) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (job_id,)).fetchone()
            return dict(row)

    def jobs_in(self, *states: str, channel: tuple[str, str, str] | None = None,
                mode: str = "simulation") -> list[int]:
        placeholders = ",".join("?" for _ in states)
        scope = " AND platform=? AND channel_kind=? AND channel_id=?" if channel else ""
        with closing(self._connect()) as db:
            return [row[0] for row in db.execute(
                f"SELECT id FROM outbox WHERE state IN ({placeholders}) AND mode=?{scope} ORDER BY id",
                (*states, mode, *(channel or ())),
            )]

    def recover_expired(self, now: float, *, channel: tuple[str, str, str] | None = None,
                        mode: str = "simulation") -> list[int]:
        scope = " AND platform=? AND channel_kind=? AND channel_id=?" if channel else ""
        params = (now, mode, *(channel or ()))
        with self._write() as db:
            db.execute(f"""
                UPDATE outbox SET state='evaluate', lease_until=NULL
                WHERE state='evaluating' AND lease_until<=? AND mode=?{scope}
            """, params)
            return [row[0] for row in db.execute(f"""
                UPDATE outbox SET state='uncertain', detail='interrupted_send', lease_until=NULL
                WHERE state='sending' AND lease_until<=? AND mode=?{scope} RETURNING id
            """, params)]

    def claim_evaluation(self, job_id: int, lease: float) -> bool:
        with self._write() as db:
            return db.execute("""
                UPDATE outbox SET state='evaluating', lease_until=? WHERE id=? AND state='evaluate'
            """, (lease, job_id)).rowcount == 1

    def finish_evaluation(
        self, job_id: int, lease: float, intent: ReplyIntent | None, detail: str, usage: tuple[int, int],
        *, generated_text: str | None = None,
    ) -> None:
        with self._write() as db:
            db.execute("""
                UPDATE outbox SET state=?, intent=?, detail=?, prompt_tokens=?, completion_tokens=?, generated_text=?, lease_until=NULL
                WHERE id=? AND state='evaluating' AND lease_until=?
            """, ("pending" if intent else "skipped", json.dumps(asdict(intent), ensure_ascii=False) if intent else None,
                  detail, *usage, generated_text, job_id, lease))

    def skip_pending(self, job_id: int, reason: str) -> None:
        with self._write() as db:
            db.execute("UPDATE outbox SET state='skipped', detail=? WHERE id=? AND state='pending'",
                       (reason, job_id))

    def claim_send(
        self, job_id: int, now: float, timeout: float, limits: Limits, stickers: StickerPolicy,
    ) -> ReplyIntent | None:
        with self._write() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=? AND state='pending'", (job_id,)).fetchone()
            if row is None:
                return None
            intent = ReplyIntent(**json.loads(row["intent"]))
            key = (row["platform"], row["channel_kind"], row["channel_id"])
            reason = self._limit_reason(db, key, now, limits, row["mode"])
            if reason:
                db.execute("UPDATE outbox SET state='skipped', detail=? WHERE id=?", (reason, job_id))
                return None
            sticker_id = intent.sticker_id
            if sticker_id and self._sticker_blocked(db, key, now, sticker_id, stickers):
                intent = replace(intent, sticker_id=None)
            db.execute("""
                UPDATE outbox SET state='sending', attempted_at=?, attempt_until=?, lease_until=?,
                                  intent=?, sticker_id=?
                WHERE id=?
            """, (now, now + timeout + 2, now + timeout + 5,
                  json.dumps(asdict(intent), ensure_ascii=False), intent.sticker_id, job_id))
            return intent

    @staticmethod
    def _limit_reason(db: sqlite3.Connection, key: tuple[str, str, str], now: float, limits: Limits,
                      mode: str) -> str:
        counts = db.execute("""
            SELECT count(*), coalesce(sum(platform=? AND channel_kind=? AND channel_id=?), 0)
            FROM outbox WHERE attempted_at>=? AND mode=? AND state IN ('sending','sent','uncertain')
        """, (*key, start_of_day(now), mode)).fetchone()
        if counts[0] >= limits.global_daily:
            return "global_daily"
        if counts[1] >= limits.per_channel_daily:
            return "channel_daily"
        latest = db.execute("SELECT max(attempted_at) FROM outbox WHERE mode=?", (mode,)).fetchone()[0]
        return "minimum_interval" if latest is not None and now - latest < limits.minimum_interval else ""

    @staticmethod
    def _sticker_blocked(
        db: sqlite3.Connection, key: tuple[str, str, str], now: float,
        sticker_id: str, stickers: StickerPolicy,
    ) -> bool:
        if not stickers.enabled or not any(item.id == sticker_id for item in stickers.catalog):
            return True
        return db.execute("""
            SELECT 1 FROM outbox
            WHERE platform=? AND channel_kind=? AND channel_id=?
              AND state IN ('sending','sent','uncertain') AND sticker_id IS NOT NULL
              AND (attempted_at>? OR (sticker_id=? AND attempted_at>?))
            LIMIT 1
        """, (*key, now - stickers.min_interval, sticker_id, now - stickers.repeat_interval)).fetchone() is not None

    def finish_send(
        self, job_id: int, result: SendResult, now: float, name: str, *, reconciling: bool = False,
        record_memory: bool = True,
    ) -> None:
        expected = "uncertain" if reconciling else "sending"
        with self._write() as db:
            row = db.execute("""
                UPDATE outbox SET state=?, detail=?, delivery_id=?, lease_until=NULL
                WHERE id=? AND state=?
                RETURNING platform, channel_kind, channel_id, intent
            """, (result.status, result.detail, result.message_id, job_id, expected)).fetchone()
            if row is not None and result.status == "sent" and record_memory:
                text = json.loads(row["intent"])["text"]
                db.execute("INSERT INTO memory_entries VALUES (NULL, ?, ?, ?, 'BOT', ?, ?, ?)",
                           (row["platform"], row["channel_kind"], row["channel_id"], name, text, now))

    def context(self, key: tuple[str, str, str], limit: int) -> tuple[list[dict], str]:
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT speaker, name, substr(text, 1, 1500) AS text FROM memory_entries
                WHERE platform=? AND channel_kind=? AND channel_id=? ORDER BY id DESC LIMIT ?
            """, (*key, limit)).fetchall()
            state = db.execute("""
                SELECT summary FROM conversations WHERE platform=? AND channel_kind=? AND channel_id=?
            """, key).fetchone()
        return [dict(row) for row in reversed(rows)], state[0] if state else ""

    def known_channels(self) -> list[tuple[str, str, str]]:
        with closing(self._connect()) as db:
            return [tuple(row) for row in db.execute("SELECT platform, channel_kind, channel_id FROM conversations")]

    def summary_work(self, key: tuple[str, str, str]) -> SummaryWork | None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            state = db.execute("""
                SELECT * FROM conversations WHERE platform=? AND channel_kind=? AND channel_id=?
            """, key).fetchone()
            if state is None:
                return None
            pending_key = (*key, state["summary_cursor"])
            totals = db.execute("""
                SELECT count(*), coalesce(sum(length(text)), 0)
                FROM memory_entries
                WHERE platform=? AND channel_kind=? AND channel_id=? AND id>?
            """, pending_key).fetchone()
            if totals[0] == 0:
                return None
            rows = db.execute("""
                SELECT id, speaker, name, substr(text, 1, 6000) AS text, created_at
                FROM memory_entries
                WHERE platform=? AND channel_kind=? AND channel_id=? AND id>?
                ORDER BY id ASC LIMIT 100
            """, pending_key).fetchall()
        batch, characters = [], 0
        for row in rows:
            characters += len(row["text"])
            if batch and characters > 24000:
                break
            batch.append(row)
        return SummaryWork(
            state["summary"], state["summary_at"], state["summary_checked_at"],
            batch[0]["created_at"], totals[0], totals[1], batch[-1]["id"],
            [{"speaker": row["speaker"], "name": row["name"], "text": row["text"]} for row in batch],
        )

    def mark_summary_check(self, key: tuple[str, str, str], previous: float | None, now: float) -> bool:
        with self._write() as db:
            return db.execute("""
                UPDATE conversations SET summary_checked_at=?
                WHERE platform=? AND channel_kind=? AND channel_id=? AND summary_checked_at IS ?
            """, (now, *key, previous)).rowcount == 1

    def finish_summary(
        self, key: tuple[str, str, str], summary: str, cursor: int,
        checked_at: float, now: float, recent_limit: int,
    ) -> None:
        with self._write() as db:
            updated = db.execute("""
                UPDATE conversations SET summary=?, summary_cursor=?, summary_at=?
                WHERE platform=? AND channel_kind=? AND channel_id=? AND summary_cursor<? AND summary_checked_at=?
            """, (summary, cursor, now, *key, cursor, checked_at))
            if updated.rowcount:
                # 已摘要的旧内容可以从记忆窗口清掉；未摘要的内容保留到后续批次。
                db.execute("""
                    DELETE FROM memory_entries
                    WHERE platform=? AND channel_kind=? AND channel_id=? AND id<=?
                      AND id IN (
                          SELECT id FROM memory_entries
                          WHERE platform=? AND channel_kind=? AND channel_id=?
                          ORDER BY id DESC LIMIT -1 OFFSET ?
                      )
                """, (*key, cursor, *key, recent_limit))
