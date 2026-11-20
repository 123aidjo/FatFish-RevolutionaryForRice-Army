"""记忆工作台的按作用域查询与隔离演示。"""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import time

from cognition import Cognition
from cognitive_store import CognitiveStore, SYSTEM_SCOPE
from events import Event
from settings import CognitionSettings
from store import Store

DEMO_SCOPE = '["qq","group","demo-room","simulation"]'
NODE_KINDS = ("person","event","fact","preference","promise","autobiography","reflection")


class DemoMemory:
    def __init__(self):
        self._lock = Lock()
        self._folder = None
        self._store = None

    def close(self):
        with self._lock:
            if self._folder:
                self._folder.cleanup()
            self._folder = self._store = None

    def get(self):
        with self._lock:
            if self._store:
                return self._store
            self._folder = TemporaryDirectory(prefix="fishbot-memory-demo-")
            store = Store(Path(self._folder.name) / "demo.db")
            store.initialize()
            engine = Cognition(store, CognitionSettings(extraction=True, reflection=True))
            clock = time()
            phrases = (
                ("demo-xm","小明","我叫小明，我喜欢羽毛球。"),
                ("demo-peer","小明","我叫小明，我喜欢咖啡。"),
                ("demo-xm","小明","我喜欢咖啡。"),
                ("demo-xm","小明","我们之前想聊头像。"),
                ("demo-xm","小明","我们来聊新头像，我希望加蓝色的鲸尾。"),
                ("demo-xm","小明","等考试结束再告诉你。"),
                ("demo-xm","小明","今天练习了 Python 循环。"),
                ("demo-xm","小明","今天讨论了 Python 列表和排序。"),
                ("demo-xm","小明","我在学Python。"),
                ("demo-xm","小明","我已经不喝咖啡。"),
                ("demo-xm","小明","我爱喝咖啡。"),
            )
            for i,(actor,name,text) in enumerate(phrases,1):
                at = clock - (len(phrases) - i + 1) * 2800
                event = Event("qq","demo-room","group",str(i),actor,name,text,False,False,None,[],at,
                              memory_allowed=True)
                store.accept(event,0,at,mode="simulation",reply_block="demo",cognition=True)
            while engine.run_one("simulation",lambda event: True):
                pass
            engine.data.observe_launch("鲸小鱼","demo")
            self._store = store
            return store


class MemoryWorkspace:
    def __init__(self, store: Store):
        self.store = store
        self.memory = CognitiveStore(store)

    def stats(self, scopes: list[str]) -> dict:
        marks = ",".join("?" for _ in scopes)
        counts = {kind:0 for kind in NODE_KINDS}
        jobs = {state:0 for state in ("pending","running","done","failed","suppressed")}
        with closing(self.store.open_read()) as db, db:
            db.execute("BEGIN")
            rows = db.execute(f"""SELECT kind,count(*) count,max(updated_at) last
                FROM memory_nodes WHERE scope IN ({marks}) GROUP BY kind""", scopes).fetchall()
            for row in rows:
                counts[row["kind"]] = row["count"]
            jobs.update({row["state"]:row["count"] for row in db.execute(f"""SELECT state,count(*) count
                FROM memory_jobs WHERE scope IN ({marks}) GROUP BY state""", scopes)})
        return {"kinds":counts,"total":sum(counts.values()),"jobs":jobs,
                "last_updated":max((r["last"] for r in rows),default=None),
                "last_reflection":next((r["last"] for r in rows if r["kind"]=="reflection"),None),
                "recall_calls":None,"token_usage":None}

    def graph(self, scope: str, kind: str, limit: int, offset: int, anchor: int | None) -> dict | None:
        with closing(self.store.open_read()) as db, db:
            db.execute("BEGIN")
            if anchor is None:
                condition = "scope=?" + (" AND kind=?" if kind else "")
                params = (scope,kind) if kind else (scope,)
                total = db.execute(f"SELECT count(*) FROM memory_nodes WHERE {condition}",params).fetchone()[0]
                nodes = db.execute(f"""SELECT id,kind,topic,value,state FROM memory_nodes WHERE {condition}
                    ORDER BY (state='active') DESC,importance DESC,updated_at DESC,id DESC LIMIT ? OFFSET ?""",
                                   (*params,limit,offset)).fetchall()
            else:
                focus = db.execute("SELECT id,kind,topic,value,state FROM memory_nodes WHERE scope=? AND id=?",
                                   (scope,anchor)).fetchone()
                if focus is None:
                    return None
                connected = """WITH neighbors AS (
                    SELECT target_id id FROM memory_edges WHERE scope=? AND origin_id=?
                    UNION SELECT origin_id FROM memory_edges WHERE scope=? AND target_id=?
                ) SELECT n.id,n.kind,n.topic,n.value,n.state FROM neighbors JOIN memory_nodes n
                    ON n.id=neighbors.id AND n.scope=?"""
                params = (scope,anchor,scope,anchor,scope)
                if kind:
                    connected += " WHERE n.kind=?"
                    params += (kind,)
                total = db.execute("SELECT count(*) FROM ("+connected+")",params).fetchone()[0]
                nodes = [focus, *db.execute(connected+" ORDER BY n.importance DESC,n.id DESC LIMIT ? OFFSET ?",
                                           (*params,limit,offset)).fetchall()]
            ids = [r["id"] for r in nodes]
            marks = ",".join("?" for _ in ids)
            edges = db.execute(f"""SELECT origin_id,target_id,relation,confidence,at FROM memory_edges
                WHERE scope=? AND origin_id IN ({marks}) AND target_id IN ({marks})
                ORDER BY origin_id,target_id,relation""", (scope,*ids,*ids)).fetchall()
        return {"nodes":[dict(r,value=r["value"][:180]) for r in nodes],
                "edges":[dict(r) for r in edges],"offset":offset,"limit":limit,"total":total,"anchor":anchor}

    def people(self, scope: str, limit: int, offset: int) -> list[dict]:
        with closing(self.store.open_read()) as db:
            return [dict(row) for row in db.execute("""SELECT n.subject,count(*) AS memories,max(n.updated_at) AS latest,
                max(CASE WHEN n.kind='event' THEN n.created_at END) AS latest_event,
                (SELECT a.value FROM memory_nodes a WHERE a.scope=n.scope AND a.subject=n.subject AND a.kind='person'
                 AND a.state='active' ORDER BY a.updated_at DESC,a.id DESC LIMIT 1) AS alias
                FROM memory_nodes n WHERE n.scope=? AND n.subject<>'' GROUP BY n.subject
                ORDER BY latest DESC,n.subject LIMIT ? OFFSET ?""", (scope,limit,offset))]

    def timeline(self, scopes: list[str], views: tuple[str,...], subject: str, limit: int, offset: int) -> list[dict]:
        marks = ",".join("?" for _ in scopes)
        kinds = ",".join("?" for _ in views)
        where = f"n.scope IN ({marks}) AND (?='' OR n.subject=?)"
        params = (*scopes,subject,subject)
        history = ""
        values = (*params,*views)
        if not views or views == ("promise",):
            history = f"""
                UNION ALL SELECT n.id,n.scope,n.subject,n.kind,n.topic,n.value,n.state,n.origin,n.confidence,
                 r.at,'revision',r.action,r.id
                 FROM memory_revisions r JOIN memory_nodes n ON n.scope=r.scope AND n.id=r.node_id
                 WHERE {where} AND r.action<>'created'
                """
            values += params
            if views:
                history += " AND n.kind='promise'"
        types = f"n.kind IN ({kinds})" if views else "n.kind IN ('event','autobiography','promise')"
        with closing(self.store.open_read()) as db:
            rows = db.execute(f"""SELECT n.id,n.scope,n.subject,n.kind,n.topic,n.value,n.state,n.origin,n.confidence,
                n.created_at AS at,'node' AS entry,NULL AS action,0 AS revision_id
                FROM memory_nodes n WHERE {where} AND {types}
                {history} ORDER BY at DESC,revision_id DESC,id DESC LIMIT ? OFFSET ?""",
                              (*values,limit,offset)).fetchall()
        return [dict(row,value=row["value"][:260]) for row in rows]

    def source(self, scope: str, node_id: int, job_id: int) -> dict | None:
        with closing(self.store.open_read()) as db:
            row = db.execute("""SELECT j.platform,j.channel_kind,j.channel_id,j.message_id,e.payload
                FROM memory_sources m JOIN memory_jobs j ON j.scope=m.scope AND j.id=m.job_id
                JOIN events e ON (e.platform,e.channel_kind,e.channel_id,e.message_id)=
                 (j.platform,j.channel_kind,j.channel_id,j.message_id)
                WHERE m.scope=? AND m.node_id=? AND m.job_id=?""",(scope,node_id,job_id)).fetchone()
        if row is None:
            return None
        raw = json.loads(row["payload"])
        if raw.get("memory_allowed") is not True:
            return None
        return {"message_id":row["message_id"],"at":raw["timestamp"],"speaker":raw["speaker"],
                "subject":raw["sender_id"],"name":raw["sender_name"],"text":raw["text"][:1200],
                "truncated":len(raw["text"])>1200}

    def preview(self, scope: str, subject: str, text: str, config: CognitionSettings) -> dict | None:
        with closing(self.store.open_read()) as db:
            row = db.execute("""SELECT e.payload FROM memory_nodes n JOIN memory_sources m
                ON m.scope=n.scope AND m.node_id=n.id JOIN memory_jobs j ON j.scope=m.scope AND j.id=m.job_id
                JOIN events e ON (e.platform,e.channel_kind,e.channel_id,e.message_id)=
                 (j.platform,j.channel_kind,j.channel_id,j.message_id)
                WHERE n.scope=? AND n.subject=? AND n.state='active'
                AND json_extract(e.payload,'$.memory_allowed')=1 AND json_extract(e.payload,'$.sender_id')=?
                ORDER BY m.at DESC LIMIT 1""", (scope,subject,subject)).fetchone()
        if row is None:
            return None
        existing = Event(**json.loads(row["payload"]))
        event = replace(existing,text=text,images=[],reply_to=None,reply_author_id=None)
        config = config.model_copy(update={"recall":True})
        trace = {}
        Cognition(self.store,config).recall(event,json.loads(scope)[3],allowed=True,trace=trace)
        for item in trace["items"]:
            details = self.memory.get(json.dumps(item["scope"],ensure_ascii=False,separators=(",",":")),item["id"])
            item["sources"] = details["sources"] if details else []
        trace["count"] = len(trace["items"])
        trace["other_conversations_queried"] = False
        trace["token_budget"] = config.token_budget
        return trace

    def forget_impact(self, scope: str, node_id: int) -> dict | None:
        with closing(self.store.open_read()) as db:
            row = db.execute("SELECT slot FROM memory_nodes WHERE scope=? AND id=?",(scope,node_id)).fetchone()
            if row is None:
                return None
            affected = db.execute("""WITH nodes AS (SELECT id FROM memory_nodes WHERE scope=? AND slot=?)
              SELECT (SELECT count(*) FROM nodes) versions,
              (SELECT count(*) FROM memory_edges WHERE scope=? AND
               (origin_id IN (SELECT id FROM nodes) OR target_id IN (SELECT id FROM nodes))) edges,
              (SELECT count(DISTINCT e.target_id) FROM memory_edges e JOIN memory_nodes n
               ON n.scope=e.scope AND n.id=e.target_id WHERE e.scope=? AND e.relation='supports'
               AND n.kind='reflection' AND e.origin_id IN (SELECT id FROM nodes)) reflections,
              (SELECT count(DISTINCT job_id) FROM memory_sources WHERE scope=? AND node_id IN (SELECT id FROM nodes)) sources
              """, (scope,row["slot"],scope,scope,scope)).fetchone()
        return dict(affected)

