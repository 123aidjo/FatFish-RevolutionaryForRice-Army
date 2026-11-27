"""受限的记忆提取、排序与本地反思，独立于 QQ 回复发送。"""

import asyncio
from contextlib import closing
from datetime import date, datetime, time as midnight, timedelta, timezone
import json
import logging
import re
import sqlite3
from typing import Callable

from cognitive_store import Candidate, CognitiveStore, SYSTEM_SCOPE, digest, normalize, scope_for
from events import Event
from policy import audit_text
from settings import CognitionSettings
from store import Store


log = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
TOPICS = (
    ("Python", ("python", "编程", "写代码")),
    ("头像", ("头像", "鲸尾图标")),
    ("考试", ("考试", "复试", "录取", "成绩")),
    ("羽毛球", ("羽毛球",)),
    ("咖啡", ("咖啡",)),
)
SENSITIVE = re.compile(
    r"手机号|电话|住址|住在|我住|身份证|银行卡|工资|密码|病历|真实姓名|家庭地址|联系方式|微信|出生日期|验证码|密钥|"
    r"\d{5,}|(?:https?://|www\.)|[\w.+-]+@[\w.-]+\.\w+|"
    r"忽略.{0,12}(指令|权限|规则)|公开.{0,12}(记忆|秘密|私聊)|"
    r"(修改|改写|覆盖|替换).{0,12}(人设|权限|规则|提示词)|转发|聊天记录", re.IGNORECASE,
)
ALIAS = re.compile(r"(?:^|[。！？；，,])\s*我(现在叫|改名叫|叫)([\w\u4e00-\u9fff]{1,14})(?=$|[。！？；，,\s])")
PREFERENCE = re.compile(
    r"(?:^|[。！？；，,])\s*我(最喜欢|喜欢|爱喝|爱吃|已经不喝|不再喜欢|不再喝|不喜欢|不喝)"
    r"([\w\u4e00-\u9fff]{1,14}?)(了)?(?=$|[。！？；，,\s])"
)
WITHDRAW = re.compile(r"^(?:别记住|忘掉|不要记住)我(?:喜欢|不喝|不喜欢)([\w\u4e00-\u9fff]{1,14})[。！]?$")
PROMISE = re.compile(r"(?:^|[。！？；，,])\s*(我会[^。！？；，,]{2,70}(?:告诉你|反馈给你)|"
                     r"我答应[^。！？；，,]{2,70}|等[^。！？；，,]{1,48}(?:再告诉你|再发给你))(?=$|[。！？；，,])")
FACT = re.compile(r"(?:^|[。！？；，,])\s*(我(?:在学|正在学|已经通过|刚参加了)[\w\u4e00-\u9fff]{1,28})(?=$|[。！？；，,\s])")
RECALL_CUE = re.compile(r"记得|回忆|以前|过去|上次|之前|喜欢什么|怎么称呼|叫啥|叫什么|约了|约定|后来")
IDENTITY_CUE = re.compile(r"改名|更名|什么时候.*叫|人设版本|首次启动|第一次.*回复|自传")
WORD = re.compile(r"[a-z][a-z0-9]{1,25}|[\u4e00-\u9fff]{2,12}", re.I)


def accepted_text(text: str) -> bool:
    return bool(audit_text(text, "", 900) and not SENSITIVE.search(text)
                and not any(mark in text for mark in ("「", "“", chr(96) * 3, "[CQ:")))


def topics_for(text: str) -> list[str]:
    text = text.casefold()
    return [topic for topic, form in TOPICS if any(word in text for word in form)]


def explicit_due(text: str) -> float | None:
    found = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", text)
    if found:
        try:
            when = date.fromisoformat(found[1])
        except ValueError:
            return None
        return datetime.combine(when, midnight.max, CHINA).timestamp()
    return None


def extract(event: Event) -> list[Candidate]:
    """首版只提取可由原句直接复核的有限类型。"""
    text = event.text.strip()
    if not accepted_text(text):
        return []
    withdraw = WITHDRAW.fullmatch(text)
    if withdraw:
        return [Candidate("preference", withdraw[1], "", withdraw[0], withdraw=True)]
    result = []
    alias = ALIAS.search(text)
    if alias:
        result.append(Candidate("person", "称呼", f"发言者自称「{alias[2]}」。", alias[0], 4, alias[1] != "叫"))
    preference = PREFERENCE.search(text)
    if preference:
        op, obj, changed = preference.groups()
        correction = "不" in op and (bool(changed) or op.startswith(("已经","不再")))
        result.append(Candidate("preference", obj, f"发言者表示{op}{obj}。", preference[0], 4, correction))
    fact = FACT.search(text)
    if fact:
        topic = next(iter(topics_for(fact[1])), "自述")
        result.append(Candidate("fact", topic, f"发言者自述：{fact[1]}。", fact[1]))
    promise = PROMISE.search(text)
    if promise:
        proof = promise[1]
        result.append(Candidate("promise", next(iter(topics_for(proof)), "约定"), proof, proof,
                                5, due_at=explicit_due(proof)))
    if not (preference or promise or RECALL_CUE.search(text) or "?" in text or "？" in text):
        for topic in topics_for(text):
            quote = text[:96]
            summary = f"获准消息谈到{topic}" + (f"：{quote}" if not fact and not alias else "。")
            result.append(Candidate("event", topic, summary, quote, 3,
                                    instance=digest(normalize(text))))
    if result and event.channel_kind == "dm":
        result.append(Candidate("autobiography", "私聊互动", "M2 在此私聊记录了有来源的互动。",
                                result[0].proof, 3))
    return result[:8]


def validated(event: Event, proposed) -> list[Candidate]:
    """提取器只提供候选，必须能由本地证据规则重新构造。"""
    approved = extract(event)
    if not isinstance(proposed, (tuple, list)) or len(proposed) > 8:
        raise ValueError("无效记忆候选")
    if any(not isinstance(item, Candidate) or item not in approved or item.proof not in event.text
           for item in proposed):
        raise ValueError("候选超出当前消息支持范围")
    return list(proposed)


def keywords(text: str) -> list[str]:
    known = topics_for(text)
    words = [match.group() for match in WORD.finditer(text)]
    return list(dict.fromkeys(normalize(word) for word in [*known, *words]))[:8]


class Cognition:
    def __init__(self, store: Store, config: CognitionSettings):
        self.data = CognitiveStore(store)
        self.config = config

    async def recall_async(self, event, mode, *, allowed):
        from memory_engine import read_config, search
        cfg,_=read_config(self.data.store)
        if not cfg.embedding_enabled:
            trace={}
            result=await asyncio.to_thread(self.recall,event,mode,allowed=allowed,trace=trace)
            self.record_recalls([r['id'] for r in trace.get('items',[])])
            return result
        if not (allowed and event.memory_allowed is True and not event.is_self
                and event.speaker in {'OWNER','PEER'} and event.sender_id and accepted_text(event.text)):
            return {}
        if IDENTITY_CUE.search(event.text) and (cfg.include_autobiography or self.config.include_autobiography):
            original=Cognition(self.data.store,self.config.model_copy(update={'recall':True,'include_autobiography':True}))
            trace={}
            result=await asyncio.to_thread(original.recall,event,mode,allowed=allowed,trace=trace)
            if result:
                self.record_recalls([r['id'] for r in trace.get('items',[])])
                return result
        scope=scope_for(event,mode)
        found=await search(self.data.store,scope,event.sender_id,event.text,history=bool(RECALL_CUE.search(event.text)))
        result={'关联记忆':[]}; used=[]; now=self.data.clock()
        with closing(self.data.store.open_read()) as db:
            for candidate in found['items']:
                # Re-read after network I/O: edits/forgetting/archiving may have occurred meanwhile.
                row=db.execute('SELECT n.*,l.status lifecycle_status,l.epistemic FROM memory_nodes n JOIN memory_lifecycle l ON l.node_id=n.id WHERE n.scope=? AND n.subject=? AND n.id=?',
                               (scope,event.sender_id,candidate['node']['id'])).fetchone()
                if row is None or row['updated_at']!=candidate['node']['updated_at']:
                    continue
                if row['state']!='active' and not (RECALL_CUE.search(event.text) and row['lifecycle_status']=='archived'):
                    continue
                policy = Cognition(self.data.store,self.config.model_copy(update={
                    'include_reflections':self.config.include_reflections or cfg.include_reflections,
                    'include_autobiography':self.config.include_autobiography or cfg.include_autobiography}))
                if not policy._injectable(db,row,now): continue
                item={'记忆':row['value'],'类型':row['kind'],'置信度':row['confidence'],
                      '性质':{'inference':'推测，不能作为确定事实','opinion':'用户主观观点','observation':'有来源的观察','self_report':'有来源的自述'}[row['epistemic']],
                      '时间':row['updated_at'],'来源节点':row['id'],'历史归档':row['lifecycle_status']=='archived'}
                proposed={'关联记忆':[*result['关联记忆'],item]}
                if len(json.dumps(proposed,ensure_ascii=False).encode())<=cfg.token_budget:
                    result=proposed; used.append(row['id'])
                if len(used)>=cfg.final_count: break
        self.record_recalls(used)
        return result if used else {}

    def record_recalls(self, used):
        now=self.data.clock()
        if used:
            with self.data.store.transaction() as db:
                for identifier in used:
                    db.execute('UPDATE memory_lifecycle SET recalls=recalls+1,last_recall=? WHERE node_id=? AND (last_recall IS NULL OR last_recall<?)',(now,identifier,now-60))

    def run_one(self, mode: str, allowed: Callable[[Event], bool], extractor=extract) -> bool:
        if not self.config.extraction:
            return False
        work = self.data.claim(mode)
        if work is None:
            return False
        job, event = work
        try:
            authorized = event.memory_allowed is True and allowed(event)
            candidates = validated(event, extractor(event)) if authorized else []
            still_allowed = authorized and allowed(event)
            self.data.finish(job, event, candidates, still_allowed)
            if self.config.reflection and still_allowed:
                for topic in {c.topic for c in candidates if c.kind == "event"}:
                    self.reflect(scope_for(event, mode), event.sender_id, topic)
        except (ValueError, TypeError, OSError, sqlite3.Error, RuntimeError):
            self.data.fail(job)
        return True

    async def worker(self, mode: str, allowed: Callable[[Event], bool]):
        from memory_engine import read_config
        from memory_growth import Extractor, MemoryModel
        from settings import load_settings
        extractor = Extractor(self.data.store,MemoryModel(self.data.store,lambda:load_settings(database_path=self.data.store.path)))
        while True:
            cfg = None
            try:
                cfg,_=read_config(self.data.store)
                if cfg.llm_extraction:
                    found = await extractor.batch(mode,allowed)
                else:
                    found = await asyncio.to_thread(self.run_one, mode, allowed)
            except sqlite3.Error:
                log.warning("M2 当前无法写入，将重试")
                found = False
            await asyncio.sleep(cfg.interval if cfg and cfg.llm_extraction else (0.25 if found else 3))

    def reflect(self, scope: str, subject: str, topic: str) -> int | None:
        if not self.config.reflection:
            return None
        rows = self.data.list(scope, kind="event", state="active", subject=subject, q=topic, limit=12)
        return self.data.add_reflection(scope, subject, topic,
                                        [n["id"] for n in rows if n["topic"] == topic],
                                        self.config.reflection_interval)

    @staticmethod
    def working(event: Event, direct: bool) -> dict:
        return {"主题": topics_for(event.text), "当前待回答的问题": event.text[:160] if direct else "",
                "会话状态": "处理当前消息" if direct else "评估接话"}

    def recall(self, event: Event, mode: str, *, allowed: bool, trace: dict | None = None) -> dict:
        cfg = self.config
        if trace is not None:
            trace.update(items=[], excluded={"policy":0,"budget":0,"limit":0}, estimated_tokens_upper=0)
        if not (cfg.recall and allowed and event.memory_allowed is True and not event.is_self
                and event.speaker in {"OWNER", "PEER"} and event.sender_id and accepted_text(event.text)):
            if trace is not None:
                trace["input_rejected"] = True
            return {}
        scope = scope_for(event, mode)
        terms = keywords(event.text)
        reflective_query = bool(RECALL_CUE.search(event.text))
        identity_query = cfg.include_autobiography and IDENTITY_CUE.search(event.text)
        if not terms and not reflective_query and not identity_query:
            return {}
        with closing(self.data.store.open_read()) as db, db:
            db.execute("BEGIN")
            candidates = self._candidates(db, scope, event.sender_id, terms, reflective_query)
            if identity_query:
                candidates.extend(db.execute("""SELECT * FROM memory_nodes WHERE scope=?
                    AND kind='autobiography' AND origin='system' AND state='active'
                    ORDER BY updated_at DESC LIMIT 20""", (SYSTEM_SCOPE,)))
            now = self.data.clock()
            ranked = self._rank(candidates, event.text, terms, now)
            expanded = self._expand(db, scope, event.sender_id, [row["id"] for _,row in ranked[:3]
                                                                 if row["scope"]==scope])
            seen = {row["id"] for _,row in ranked}
            ranked.extend((None,row) for row in expanded if row["id"] not in seen)
            result = {"主题": topics_for(event.text), "关联记忆": []}
            for score,row in ranked:
                if not self._injectable(db, row, now):
                    if trace is not None:
                        trace["excluded"]["policy"] += 1
                    continue
                items = result["关联记忆"]
                if len(items) >= cfg.max_results:
                    if trace is None:
                        break
                    trace["excluded"]["limit"] += 1
                    continue
                edited = db.execute("SELECT 1 FROM memory_revisions WHERE scope=? AND node_id=? AND action='admin_edit'",
                                    (row["scope"],row["id"])).fetchone()
                item = {"记忆": row["value"], "类型": row["kind"],
                        "性质": "管理员修订，原消息仅作参考" if edited else {
                            "message":"已授权消息自述或话题","inference":"推测","system":"可查系统事实"}[row["origin"]],
                        "时间": datetime.fromtimestamp(row["created_at"],CHINA).isoformat(timespec="minutes")}
                combined = {**result, "关联记忆": [*items,item]}
                estimate = len(json.dumps(combined,ensure_ascii=False).encode())
                if estimate <= cfg.token_budget:
                    items.append(item)
                    if trace is not None:
                        trace["items"].append({"id":row["id"],"scope":json.loads(row["scope"]),
                                               "score":round(score,2) if score is not None else None,
                                               "confidence":row["confidence"],"memory":item})
                        trace["estimated_tokens_upper"] = estimate
                elif trace is not None:
                    trace["excluded"]["budget"] += 1
            return result if result["关联记忆"] else {}

    def _injectable(self, db, row, now):
        cfg = self.config
        if row["confidence"] < cfg.min_confidence:
            return False
        if row["kind"] == "reflection":
            return cfg.include_reflections
        if row["kind"] == "autobiography":
            return cfg.include_autobiography
        if row["kind"] == "promise":
            goal = db.execute("SELECT state,due_at FROM memory_goals WHERE scope=? AND node_id=?",
                              (row["scope"],row["id"])).fetchone()
            return (goal is not None and goal["state"] in {"proposed","active"}
                    and (goal["due_at"] is None or goal["due_at"] >= now))
        return True

    @staticmethod
    def _rank(candidates, query, terms, now):
        scored = []
        for row in candidates:
            text = normalize(row["topic"] + " " + row["value"])
            matches = sum(word in text for word in terms)
            if row["scope"] == SYSTEM_SCOPE:
                if ("改名" in query or "更名" in query) and row["topic"] != "角色更名":
                    continue
                if "第一次" in query and "回复" in query and row["topic"] != "首次确认回复":
                    continue
                matches += 3
            days = max(0, (now - row["updated_at"]) / 86400)
            score = matches * 6 + row["importance"] + 2 * row["confidence"]
            if "喜欢什么" in query and row["kind"] == "preference":
                score += 9
            if row["kind"] not in {"promise","person"}:
                score += 2 / (1 + days / 14)
            scored.append((score, row))
        return sorted(scored,key=lambda pair:(pair[0],pair[1]["updated_at"]),reverse=True)

    def _candidates(self, db, scope, subject, terms, fallback):
        base = """SELECT n.* FROM memory_nodes n WHERE n.scope=? AND n.subject=? AND n.state='active'
            AND n.confidence>=?"""
        ranked = []
        if terms:
            clauses = " OR ".join("(instr(lower(n.topic),?)>0 OR instr(lower(n.value),?)>0)" for _ in terms)
            ranked = list(db.execute(base + f" AND ({clauses}) ORDER BY n.updated_at DESC LIMIT 96",
                                     (scope,subject,self.config.min_confidence,*(v for t in terms for v in (t,t)))))
        if fallback:
            extra = db.execute(base + " ORDER BY n.updated_at DESC LIMIT 32",
                               (scope,subject,self.config.min_confidence))
            known = {r["id"] for r in ranked}
            ranked.extend(r for r in extra if r["id"] not in known)
        return ranked

    @staticmethod
    def _expand(db, scope, subject, seeds):
        if not seeds:
            return []
        marks = ",".join("?" for _ in seeds)
        return list(db.execute(f"""SELECT DISTINCT n.* FROM memory_edges e JOIN memory_nodes n
            ON n.scope=e.scope AND n.id=CASE WHEN e.origin_id IN ({marks}) THEN e.target_id ELSE e.origin_id END
            WHERE e.scope=? AND (e.origin_id IN ({marks}) OR e.target_id IN ({marks}))
            AND e.relation IN ('supports','related_to','belongs_to','participated_in')
            AND n.subject=? AND n.state='active' ORDER BY n.updated_at DESC LIMIT 12""",
                               (*seeds,scope,*seeds,*seeds,subject)))

