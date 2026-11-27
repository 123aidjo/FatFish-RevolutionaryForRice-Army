"""分域记忆、证据、修订与任务的一致性写入。"""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from secrets import token_hex
import sqlite3
from time import time
from typing import Literal

from events import Event
from store import Store


SYSTEM_SCOPE = '["system","role","fishbot-v2","live"]'
MAX_ATTEMPTS = 3


class MemoryChangeError(ValueError):
    pass


def scope_for(event: Event, mode: str) -> str:
    return json.dumps([*event.channel_key, mode], ensure_ascii=False, separators=(",", ":"))


def digest(*parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: Literal["person", "event", "fact", "preference", "promise", "autobiography"]
    topic: str
    value: str
    proof: str
    importance: int = 3
    correction: bool = False
    due_at: float | None = None
    withdraw: bool = False
    instance: str = ""
    origin: str = "message"
    confidence: float | None = None
    epistemic: str = "self_report"


class CognitiveStore:
    def __init__(self, store: Store, clock=time):
        self.store = store
        self.clock = clock

    def claim(self, mode: str, *, scope: str | None = None, subject: str | None = None) -> tuple[dict, Event] | None:
        now = self.clock()
        with self.store.transaction() as db:
            db.execute("""UPDATE memory_jobs SET state='failed',lease=NULL,error='retry_exhausted'
                WHERE mode=? AND state='running' AND available<=? AND attempts>=?""",
                       (mode, now, MAX_ATTEMPTS))
            job = db.execute("""UPDATE memory_jobs SET state='running', attempts=attempts+1, available=?, lease=?
                WHERE id=(SELECT j.id FROM memory_jobs j JOIN events e
                    USING(platform,channel_kind,channel_id,message_id)
                    WHERE j.mode=? AND j.state IN ('pending','running')
                    AND j.available<=? AND j.attempts<? AND (? IS NULL OR j.scope=?)
                    AND (? IS NULL OR json_extract(e.payload,'$.sender_id')=?)
                    ORDER BY j.available,j.id LIMIT 1) RETURNING *""",
                             (now + 60, token_hex(16), mode, now, MAX_ATTEMPTS,
                              scope, scope, subject, subject)).fetchone()
        if job is None:
            return None
        event = self.store.get_event(*(job[key] for key in ("platform", "channel_kind", "channel_id", "message_id")))
        return (dict(job), event) if event else None

    def fail(self, job: dict) -> None:
        now = self.clock()
        state = "failed" if job["attempts"] >= MAX_ATTEMPTS else "pending"
        with self.store.transaction() as db:
            db.execute("""UPDATE memory_jobs SET state=?, available=?, lease=NULL,error='extraction_failed'
                WHERE id=? AND lease=? AND state='running'""",
                       (state, now + 2 ** job["attempts"], job["id"], job["lease"]))

    @staticmethod
    def _revision(db, scope, node, at, action, previous=None, evidence=None):
        db.execute("""INSERT INTO memory_revisions(scope,node_id,at,action,previous_state,evidence_node)
            VALUES (?,?,?,?,?,?)""", (scope, node, at, action, previous, evidence))

    @staticmethod
    def _edge(db, scope, origin, target, relation, at, confidence):
        db.execute("INSERT OR IGNORE INTO memory_edges VALUES (?,?,?,?,?,?)",
                   (scope, origin, target, relation, at, confidence))

    @staticmethod
    def _self_state(db, scope, key, value, now):
        db.execute("""INSERT INTO memory_self_state VALUES (?,?,?,?)
            ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                   (scope, key, str(value), now))

    @staticmethod
    def slot(subject: str, kind: str, topic: str) -> str:
        return digest(subject, kind, normalize(topic))

    def _put(self, db, scope, subject, slot, kind, topic, value, at, importance, confidence, origin,
             source_key, job_id=None, correction=False, due_at=None):
        if db.execute("SELECT 1 FROM memory_suppression WHERE scope=? AND fingerprint=?",
                      (scope, slot)).fetchone():
            return None
        old = db.execute("""SELECT * FROM memory_nodes WHERE scope=? AND slot=? AND state='active'
            ORDER BY updated_at DESC LIMIT 1""", (scope, slot)).fetchone()
        same = db.execute("SELECT * FROM memory_nodes WHERE scope=? AND slot=? AND value=?",
                          (scope, slot, value)).fetchone()
        changes = old is not None and old["value"] != value
        newer_correction = changes and correction and at >= old["updated_at"]
        state = "conflict" if changes and not newer_correction else "active"
        if same is not None:
            node = same["id"]
            if newer_correction and same["state"] != "inactive":
                db.execute("UPDATE memory_nodes SET state='active',updated_at=? WHERE id=?", (at, node))
                self._revision(db, scope, node, at, "reactivated", same["state"], old["id"])
            elif same["state"] == "active":
                db.execute("UPDATE memory_nodes SET updated_at=max(updated_at,?) WHERE id=?", (at, node))
        else:
            node = db.execute("""INSERT INTO memory_nodes(scope,subject,slot,kind,topic,value,state,origin,
                importance,confidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (scope, subject, slot, kind, topic, value, state, origin,
                               importance, confidence, at, at)).lastrowid
            self._revision(db, scope, node, at, "created" if state == "active" else "conflict",
                           evidence=old["id"] if changes else None)
        if changes and old["id"] != node:
            if newer_correction and (same is None or same["state"] != "inactive"):
                db.execute("UPDATE memory_nodes SET state='superseded' WHERE id=?", (old["id"],))
                self._revision(db, scope, old["id"], at, "superseded", "active", node)
                db.execute("""UPDATE memory_goals SET state='cancelled',updated_at=?
                    WHERE node_id=? AND state IN ('proposed','active')""", (at, old["id"]))
                self._edge(db, scope, node, old["id"], "supersedes", at, confidence)
                self._invalidate_dependents(db, scope, {old["id"]})
            else:
                self._edge(db, scope, node, old["id"], "contradicts", at, confidence)
        db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?,?,?,?,?)",
                   (scope, node, source_key, job_id, at))
        if kind == "promise" and same is None:
            db.execute("INSERT INTO memory_goals VALUES (?,?, 'proposed', ?, ?, 0, ?)",
                       (scope, node, due_at, value, at))
        return node

    def finish(self, job: dict, event: Event, candidates: list[Candidate], allowed: bool) -> None:
        with self.store.transaction() as db:
            current = db.execute("SELECT state FROM memory_jobs WHERE id=? AND lease=?",
                                 (job["id"], job["lease"])).fetchone()
            if current is None or current["state"] != "running":
                return
            scope = scope_for(event, job["mode"])
            authorized = (allowed and event.memory_allowed is True and scope == job["scope"]
                          and not event.is_self and event.speaker in {"OWNER", "PEER"} and event.sender_id)
            if not authorized:
                db.execute("UPDATE memory_jobs SET state='suppressed',lease=NULL WHERE id=?", (job["id"],))
                return
            source = digest(event.sender_id, normalize(event.text))
            ids = []
            for item in candidates:
                slot = self.slot(event.sender_id, item.kind, item.topic + item.instance)
                if item.withdraw:
                    self._delete_slots(db, scope, {slot}, self.clock())
                    continue
                node = self._put(db, scope, event.sender_id, slot, item.kind, item.topic, item.value,
                                 event.timestamp, item.importance, item.confidence if item.confidence is not None else (0.95 if item.kind == "person" else 0.85),
                                 item.origin, source, job["id"], item.correction, item.due_at)
                if node:
                    db.execute('UPDATE memory_lifecycle SET epistemic=? WHERE node_id=?',(item.epistemic,node))
                    ids.append((node, item.kind, item.topic))
                    if item.kind in {"event", "autobiography"}:
                        self._self_state(db, scope, "last_experience", node, self.clock())
            person = db.execute("""SELECT id FROM memory_nodes WHERE scope=? AND subject=? AND kind='person'
                AND state='active' ORDER BY updated_at DESC LIMIT 1""", (scope, event.sender_id)).fetchone()
            for node, kind, topic in ids:
                if person and person["id"] != node:
                    relation = "participated_in" if kind == "event" else "belongs_to"
                    self._edge(db, scope, node, person["id"], relation, event.timestamp, 0.85)
                related = db.execute("""SELECT id FROM memory_nodes WHERE scope=? AND subject=?
                    AND state='active' AND topic=? AND id<>? ORDER BY updated_at DESC LIMIT 4""",
                                     (scope, event.sender_id, topic, node))
                for other in related:
                    self._edge(db, scope, node, other["id"], "related_to", event.timestamp, 0.85)
            db.execute("UPDATE memory_jobs SET state='done',lease=NULL,error=NULL WHERE id=? AND state='running'",
                       (job["id"],))

    def system_fact(self, key: str, value: str, *, at: float | None = None, source: str = "system") -> int | None:
        """由本地可信入口记录已观察到的事实，不重建旧历史。"""
        at = self.clock() if at is None else at
        slot = self.slot("", "autobiography", key)
        with self.store.transaction() as db:
            old = db.execute("SELECT id FROM memory_nodes WHERE scope=? AND slot=?", (SYSTEM_SCOPE, slot)).fetchone()
            if old:
                return old["id"]
            return self._put(db, SYSTEM_SCOPE, "", slot, "autobiography", key, value, at, 5, 1.0, "system", source)

    def observe_launch(self, persona_name: str, revision: str):
        """首次见到的名字只记录为观测，后续可信配置变更才记更名。"""
        now = self.clock()
        with self.store.transaction() as db:
            row = db.execute("SELECT value FROM memory_self_state WHERE scope=? AND key='persona'",
                             (SYSTEM_SCOPE,)).fetchone()
            previous = json.loads(row[0]) if row else None
            state = {"name": persona_name, "revision": revision}
            if previous == state:
                return
            if previous is None:
                topic, value = "M2 启动", f"M2 首次记录当前角色名称为「{persona_name}」。"
            elif previous["name"] != persona_name:
                topic, value = "角色更名", f"正式名称由「{previous['name']}」更新为「{persona_name}」。"
            else:
                topic, value = "人设版本", f"人设配置更新为 {revision[:12]}。"
            self._put(db, SYSTEM_SCOPE, "", digest(topic, revision, str(now)), "autobiography", topic,
                      value, now, 5, 1.0, "system", digest(topic, revision, str(now)))
            self._self_state(db, SYSTEM_SCOPE, "persona", json.dumps(state, ensure_ascii=False), now)

    def add_reflection(self, scope: str, subject: str, topic: str, evidence: list[int],
                       interval: int) -> int | None:
        now = self.clock()
        if len(set(evidence)) < 2:
            return None
        with self.store.transaction() as db:
            key = "reflection:" + digest(subject)
            marker = db.execute("SELECT updated_at FROM memory_self_state WHERE scope=? AND key=?",
                                (scope, key)).fetchone()
            if marker and now - marker[0] < interval:
                return None
            ids = ",".join("?" for _ in evidence)
            rows = db.execute(f"""SELECT n.id,j.message_id,m.source_key FROM memory_nodes n
                JOIN memory_sources m ON m.scope=n.scope AND m.node_id=n.id
                JOIN memory_jobs j ON j.scope=m.scope AND j.id=m.job_id
                WHERE n.scope=? AND n.subject=? AND n.topic=? AND n.state='active'
                AND n.kind='event' AND n.id IN ({ids}) AND j.state='done'""",
                              (scope, subject, topic, *evidence)).fetchall()
            if len({r["source_key"] for r in rows}) < 2 or len({r["id"] for r in rows}) < 2:
                return None
            sources = sorted({r["id"] for r in rows})
            fingerprint = digest(*(str(node) for node in sources))
            if db.execute("SELECT 1 FROM memory_sources WHERE scope=? AND source_key=?",
                          (scope, fingerprint)).fetchone():
                return None
            node = self._put(db, scope, subject, self.slot(subject, "reflection", topic), "reflection", topic,
                             f"候选总结：近期多次谈到{topic}。", now, 3, 0.7, "inference",
                             fingerprint)
            if node:
                for source in sources:
                    self._edge(db, scope, source, node, "supports", now, 0.7)
                self._self_state(db, scope, key, node, now)
            return node

    @staticmethod
    def _invalidate_dependents(db, scope, initial: set[int]) -> set[int]:
        affected = set(initial)
        pending = set(initial)
        while pending:
            marks = ",".join("?" for _ in pending)
            found = {r[0] for r in db.execute(f"""SELECT e.target_id FROM memory_edges e
                JOIN memory_nodes n ON n.scope=e.scope AND n.id=e.target_id
                WHERE e.scope=? AND e.relation='supports' AND n.kind='reflection' AND e.origin_id IN ({marks})""",
                                            (scope, *pending))}
            pending = found - affected
            affected.update(pending)
        derived = affected - initial
        if derived:
            marks = ",".join("?" for _ in derived)
            db.execute(f"DELETE FROM memory_nodes WHERE scope=? AND id IN ({marks})", (scope, *derived))
            db.execute(f"DELETE FROM memory_self_state WHERE scope=? AND value IN ({marks})",
                       (scope, *(str(node) for node in derived)))
        return affected

    def _delete_slots(self, db, scope, slots, now):
        jobs = set()
        if scope != SYSTEM_SCOPE:
            platform, kind, channel, mode = json.loads(scope)
        for slot in slots:
            db.execute("INSERT OR IGNORE INTO memory_suppression VALUES (?,?)", (scope, slot))
            nodes = {n[0] for n in db.execute("SELECT id FROM memory_nodes WHERE scope=? AND slot=?", (scope, slot))}
            if not nodes:
                continue
            affected = self._invalidate_dependents(db, scope, nodes)
            for node in nodes:
                for source in db.execute("""SELECT j.id,e.payload FROM memory_sources m JOIN memory_jobs j
                    ON j.scope=m.scope AND j.id=m.job_id JOIN events e
                    ON (e.platform,e.channel_kind,e.channel_id,e.message_id)=(j.platform,j.channel_kind,j.channel_id,j.message_id)
                    WHERE m.scope=? AND m.node_id=?""", (scope, node)):
                    jobs.add(source["id"])
                    raw = json.loads(source["payload"])
                    db.execute("""DELETE FROM memory_entries WHERE (platform,channel_kind,channel_id)=(?,?,?)
                        AND speaker=? AND name=? AND text=?""",
                               (platform, kind, channel, raw["speaker"], raw["sender_name"], raw["text"]))
            db.execute("DELETE FROM memory_nodes WHERE scope=? AND slot=?", (scope, slot))
            for node in affected:
                db.execute("DELETE FROM memory_self_state WHERE scope=? AND value=?", (scope, str(node)))
        if jobs:
            db.executemany("UPDATE memory_jobs SET state='suppressed',lease=NULL WHERE id=?", ((job,) for job in jobs))
            db.execute("""UPDATE conversations SET summary='',summary_at=NULL,summary_checked_at=NULL
                WHERE (platform,channel_kind,channel_id)=(?,?,?)""", (platform, kind, channel))

    def delete(self, scope: str, node_id: int, version: float | None = None) -> bool:
        with self.store.transaction() as db:
            node = db.execute("SELECT slot,updated_at FROM memory_nodes WHERE scope=? AND id=?", (scope, node_id)).fetchone()
            if node is None or (version is not None and version != node["updated_at"]):
                return False
            self._delete_slots(db, scope, {node["slot"]}, self.clock())
            self.store.record_panel_action(db, "cognitive_forget")
            return True

    def clear_channel(self, key: tuple[str, str, str]):
        with self.store.transaction() as db:
            for mode in ("live", "simulation"):
                scope = json.dumps([*key, mode], ensure_ascii=False, separators=(",", ":"))
                slots = {r[0] for r in db.execute("SELECT slot FROM memory_nodes WHERE scope=?", (scope,))}
                self._delete_slots(db, scope, slots, self.clock())
                db.execute("""UPDATE memory_jobs SET state='suppressed',lease=NULL WHERE scope=?
                    AND state IN ('pending','running')""", (scope,))

    def change(self, scope: str, node_id: int, updated_at: float, state: str) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM memory_nodes WHERE scope=? AND id=?", (scope, node_id)).fetchone()
            if old is None or old["updated_at"] != updated_at:
                return False
            if state in {"active","inactive"}:
                if state == "active" and db.execute("""SELECT 1 FROM memory_nodes
                    WHERE scope=? AND slot=? AND state='active' AND id<>?""",
                                                    (scope, old["slot"], node_id)).fetchone():
                    raise MemoryChangeError("该事实已有当前版本")
                db.execute("UPDATE memory_nodes SET state=?,updated_at=? WHERE id=?", (state, now, node_id))
                if state == "inactive":
                    self._invalidate_dependents(db, scope, {node_id})
                self._revision(db, scope, node_id, now, state, old["state"])
                self.store.record_panel_action(db, "cognitive_state")
            return True

    def edit(self, scope: str, node_id: int, version: float, value: str) -> int | None:
        """保留旧节点与来源，管理员的改写独立标注。"""
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM memory_nodes WHERE scope=? AND id=?", (scope, node_id)).fetchone()
            if old is None or old["updated_at"] != version:
                return None
            if old["origin"] != "message" or old["state"] != "active":
                raise MemoryChangeError("只能修订有效的消息记忆")
            if old["value"] == value:
                return node_id
            if db.execute("SELECT 1 FROM memory_nodes WHERE scope=? AND slot=? AND value=?",
                          (scope, old["slot"], value)).fetchone():
                raise MemoryChangeError("同一记忆已有这项内容的历史版本")
            now = self.clock()
            confidence = min(old["confidence"], .75)
            new = db.execute("""INSERT INTO memory_nodes(scope,subject,slot,kind,topic,value,state,origin,
                importance,confidence,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'active','message',?,?,?,?)""",
                             (scope,old["subject"],old["slot"],old["kind"],old["topic"],value,
                              old["importance"],confidence,old["created_at"],now)).lastrowid
            db.execute("UPDATE memory_nodes SET state='superseded',updated_at=? WHERE id=?", (now,node_id))
            db.execute("""INSERT INTO memory_sources SELECT scope,?,source_key,job_id,at
                FROM memory_sources WHERE scope=? AND node_id=?""", (new,scope,node_id))
            db.execute("""INSERT INTO memory_goals SELECT scope,?,state,due_at,?,0,?
                FROM memory_goals WHERE scope=? AND node_id=?""", (new,value,now,scope,node_id))
            db.execute("""UPDATE memory_goals SET state='cancelled',updated_at=? WHERE scope=? AND node_id=?
                AND state IN ('active','proposed')""", (now,scope,node_id))
            for edge in db.execute("""SELECT * FROM memory_edges WHERE scope=? AND (origin_id=? OR target_id=?)
                AND relation IN ('related_to','participated_in','belongs_to')""", (scope,node_id,node_id)).fetchall():
                self._edge(db,scope,new if edge["origin_id"]==node_id else edge["origin_id"],
                           new if edge["target_id"]==node_id else edge["target_id"],
                           edge["relation"],now,edge["confidence"])
            self._edge(db,scope,new,node_id,"supersedes",now,confidence)
            self._revision(db,scope,node_id,now,"superseded","active",new)
            self._revision(db,scope,new,now,"admin_edit",evidence=node_id)
            self._invalidate_dependents(db,scope,{node_id})
            self.store.record_panel_action(db, "cognitive_edit")
            return new

    def goal(self, scope: str, node_id: int, version: float, state: str, due_at: float | None) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM memory_goals WHERE scope=? AND node_id=?", (scope, node_id)).fetchone()
            if old is None or old["updated_at"] != version:
                return False
            db.execute("UPDATE memory_goals SET state=?,due_at=?,updated_at=? WHERE scope=? AND node_id=?",
                       (state, due_at, now, scope, node_id))
            self._revision(db, scope, node_id, now, "goal_" + state, old["state"])
            self.store.record_panel_action(db, "cognitive_goal")
            return True

    def get(self, scope: str, node_id: int) -> dict | None:
        with closing(self.store.open_read()) as db:
            with db:
                db.execute("BEGIN")
                row = db.execute("SELECT * FROM memory_nodes WHERE scope=? AND id=?", (scope, node_id)).fetchone()
                if row is None:
                    return None
                node = dict(row)
                node["sources"] = [dict(r) for r in db.execute("""SELECT j.platform,j.channel_kind,j.channel_id,
                    j.message_id,j.id AS job_id,m.at,n.origin,CASE WHEN n.origin='system' THEN m.source_key END AS system_ref
                    FROM memory_sources m JOIN memory_nodes n
                    ON n.scope=m.scope AND n.id=m.node_id LEFT JOIN memory_jobs j ON j.scope=m.scope AND j.id=m.job_id
                    WHERE m.scope=? AND m.node_id=? ORDER BY m.at LIMIT 50""", (scope, node_id))]
                node["edges"] = [dict(r) for r in db.execute("""SELECT origin_id,target_id,relation,confidence,at
                    FROM memory_edges WHERE scope=? AND (origin_id=? OR target_id=?) ORDER BY at DESC LIMIT 100""",
                                                             (scope, node_id, node_id))]
                node["revisions"] = [dict(r) for r in db.execute("""SELECT at,action,previous_state,evidence_node
                    FROM memory_revisions WHERE scope=? AND node_id=? ORDER BY id DESC LIMIT 50""", (scope, node_id))]
                goal = db.execute("SELECT * FROM memory_goals WHERE scope=? AND node_id=?", (scope, node_id)).fetchone()
                node["goal"] = dict(goal) if goal else None
                lifecycle = db.execute('SELECT * FROM memory_lifecycle WHERE node_id=?',(node_id,)).fetchone()
                node['lifecycle'] = dict(lifecycle) if lifecycle else None
            return node

    def scopes(self) -> list[dict]:
        with closing(self.store.open_read()) as db:
            return [{"scope": json.loads(row["scope"]), "nodes": row["nodes"]}
                    for row in db.execute("SELECT scope,count(*) nodes FROM memory_nodes GROUP BY scope ORDER BY scope")]

    def self_state(self, scope: str) -> dict:
        with closing(self.store.open_read()) as db:
            views = [dict(row) for row in db.execute("""SELECT n.id,n.subject,n.value,n.confidence,n.updated_at
                FROM memory_self_state s JOIN memory_nodes n ON n.scope=s.scope AND CAST(n.id AS TEXT)=s.value
                WHERE s.scope=? AND s.key LIKE 'grounded_view:%' AND n.state='active'
                AND n.kind='reflection' AND EXISTS (SELECT 1 FROM memory_edges e
                    WHERE e.scope=n.scope AND e.target_id=n.id AND e.relation='supports')
                ORDER BY n.updated_at DESC LIMIT 20""", (scope,))]
            last = db.execute("SELECT value FROM memory_self_state WHERE scope=? AND key='last_experience'",
                              (scope,)).fetchone()
            state = db.execute("SELECT value,updated_at FROM memory_self_state WHERE scope=? AND key='persona'",
                               (SYSTEM_SCOPE,)).fetchone()
            goals = [dict(row) for row in db.execute("""SELECT g.node_id,g.state,g.due_at,g.condition,g.may_execute
                FROM memory_goals g JOIN memory_nodes n ON n.scope=g.scope AND n.id=g.node_id
                WHERE g.scope=? AND n.state='active' AND g.state IN ('active','proposed')
                AND (g.due_at IS NULL OR g.due_at>=?) ORDER BY g.updated_at DESC LIMIT 20""",
                                                   (scope, self.clock()))]
        topics = list(dict.fromkeys(row["topic"] for row in self.list(scope, kind="event", state="active", limit=12)))
        return {"observed_persona": {**json.loads(state["value"]), "observed_at": state["updated_at"]} if state else None,
                "recent_topics": topics, "last_experience_id": int(last["value"]) if last else None,
                "open_goals": goals, "grounded_views": views}

    def list(self, scope: str, *, q="", kind="", state="", subject="", since=0., until=None,
             confidence=0., sort="recent", limit=50, offset=0):
        terms, params = ["scope=?"], [scope]
        for key, val in (("kind", kind), ("state", state), ("subject", subject)):
            if val:
                terms.append(key + "=?")
                params.append(val)
        if q:
            terms.append("""(instr(lower(value),lower(?))>0 OR instr(lower(topic),lower(?))>0
                OR (subject<>'' AND EXISTS (SELECT 1 FROM memory_nodes a WHERE a.scope=memory_nodes.scope
                AND a.subject=memory_nodes.subject AND a.kind='person' AND a.state='active'
                AND instr(lower(a.value),lower(?))>0)))""")
            params.extend((q, q, q))
        terms.append("confidence>=?")
        params.append(confidence)
        terms.append("created_at>=?")
        params.append(since)
        if until is not None:
            terms.append("created_at<=?")
            params.append(until)
        with closing(self.store.open_read()) as db:
            ordering = {"recent":"updated_at DESC", "importance":"importance DESC,updated_at DESC",
                        "oldest":"created_at ASC"}[sort]
            return [dict(row) for row in db.execute(f"""SELECT * FROM memory_nodes WHERE {' AND '.join(terms)}
                ORDER BY {ordering},id DESC LIMIT ? OFFSET ?""", (*params, limit, offset))]

