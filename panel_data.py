from contextlib import closing
import json
import re
import time

from ratelimit import start_of_day

SECRET_PATTERN = re.compile(
    r"(?i)(?:\bsk-[\w-]{8,}|\bBearer\s+[\w.-]{6,}|"
    r"(?:api[_ -]?key|token|令牌|密钥)\s*[:=]\s*[^\s,;，；]{4,})"
)
REASONS = {
    "counting": "累计普通消息", "direct": "直接触发", "proactive": "主动接话",
    "owner_checkin": "主人私聊主动问候",
    "mentions_others": "仅提及他人", "no_trigger": "未达到触发条件",
    "unknown_reply": "引用作者待确认", "model_declined": "模型选择不回复",
    "content_rejected": "内容审核跳过", "invalid_review": "审核结果无效",
    "empty_model_content": "模型返回空内容", "invalid_model_json": "模型回复格式错误",
    "reply_too_long": "回复超过长度上限", "empty_reply": "回复为空",
    "model_timeout": "模型超时", "onebot_ack": "OneBot 确认",
    "history_confirmed": "历史记录已确认", "onebot_1200": "发送结果待核查（1200）",
    "interrupted_send": "发送过程曾中断", "send_timeout": "发送超时",
    "transport_error": "传输异常", "platform_or_allowlist": "平台或白名单限制",
    "sending_disabled_or_not_allowed": "发送未授权", "global_daily": "达到全局每日限额",
    "channel_daily": "达到会话每日限额", "minimum_interval": "发送间隔不足",
    "invalid_or_self": "已过滤自身事件或无效事件", "duplicate": "重复消息",
    "reply_window_closed": "早于本次回复窗口，已保留消息记录",
    "manual_pause": "自动回复已手动暂停", "storage_error": "写库失败，需人工检查发送状态",
    "simulation_error": "模拟任务异常", "emotion_not_enabled": "模型使用了未启用的情绪",
}
ACTIONS = {
    "save_persona": "保存人设", "save_settings": "保存系统设置", "save_platform": "保存平台配置",
    "restore_config": "恢复上一次配置", "clear_memory": "清理会话记忆",
    "import_sticker": "导入表情包", "edit_sticker": "编辑表情包",
    "pause": "暂停本地模拟", "resume": "恢复本地模拟", "simulate": "完成本地模拟",
    "simulation_error": "模拟任务异常",
    "qq_pause": "暂停 QQ 自动回复", "qq_resume": "恢复 QQ 新消息回复",
    "qq_memory_policy": "修改 QQ 群记忆记录权限",
}
CATALOG_DEFAULT = {"name": "", "enabled": False, "emotions": [], "keywords": [], "contexts": []}


def reason_label(code: str | None) -> str:
    if not code:
        return ""
    return REASONS.get(code, "平台或模型返回异常")


class PanelData:
    def __init__(self, store, secrets):
        self.store = store
        self.secrets = secrets

    def redactor(self):
        secrets = tuple(secret for secret in self.secrets() if secret)
        def redact(value: str) -> str:
            for secret in secrets:
                value = value.replace(secret, "［已隐藏］")
            return SECRET_PATTERN.sub("［已隐藏］", value)
        return redact

    def redact(self, value: str) -> str:
        return self.redactor()(value)

    def messages(self, platform, status, since, until, limit):
        filters, params = [], []
        for value, clause in (
            (platform, "e.platform=?"), (status, "coalesce(o.state, 'received')=?"),
            (since, "json_extract(e.payload, '$.timestamp')>=?"),
            (until, "json_extract(e.payload, '$.timestamp')<?"),
        ):
            if value is not None:
                filters.append(clause)
                params.append(value)
        where = " AND ".join(filters) or "1"
        with closing(self.store.open_read()) as db:
            rows = db.execute(f"""
                SELECT e.platform,e.channel_kind,e.channel_id,e.message_id,e.payload,
                       o.trigger,o.detail,o.mode,o.intent,o.generated_text,coalesce(o.state,'received') AS state
                FROM events e LEFT JOIN outbox o USING(platform,channel_kind,channel_id,message_id)
                WHERE {where}
                ORDER BY json_extract(e.payload, '$.timestamp') DESC,e.rowid DESC LIMIT ?
            """, (*params, limit)).fetchall()
        result = []
        for row in rows:
            event = json.loads(row["payload"])
            intent = json.loads(row["intent"]) if row["intent"] else {}
            result.append({
                "platform": row["platform"], "channel_kind": row["channel_kind"],
                "channel_id": row["channel_id"], "timestamp": event["timestamp"],
                "sender": self.redact(event["sender_name"]), "text": self.redact(event["text"]),
                "reply": self.redact(row["generated_text"] or intent.get("text", "")), "state": row["state"],
                "mode": row["mode"] or "unknown", "trigger": reason_label(row["trigger"]),
                "audit_skipped": row["detail"] in {"content_rejected", "invalid_review"},
                "detail": reason_label(row["detail"]),
            })
        return result

    def stats(self):
        start = start_of_day(time.time())
        with closing(self.store.open_read()) as db:
            row = db.execute("""
                SELECT count(*) FILTER (WHERE o.trigger IS NOT 'owner_checkin') AS received,
                  count(coalesce(o.generated_text, o.intent)) AS generated,
                  coalesce(sum(o.state='sent' AND o.mode='live'),0) AS sent,
                  coalesce(sum(o.state='sent' AND o.mode='simulation'),0) AS simulated_sent,
                  coalesce(sum(o.mode='live' AND o.trigger='proactive'),0) AS proactive,
                  coalesce(sum(o.state='skipped'),0) AS skipped,
                  coalesce(sum(o.state='failed'),0) AS failed,
                  coalesce(sum(o.state='uncertain'),0) AS uncertain
                FROM events e LEFT JOIN outbox o USING(platform,channel_kind,channel_id,message_id)
                WHERE json_extract(e.payload, '$.timestamp')>=?
            """, (start,)).fetchone()
        return dict(row)

    def logs(self):
        with closing(self.store.open_read()) as db:
            actions = db.execute("SELECT created_at, action FROM panel_audit ORDER BY id DESC LIMIT 30").fetchall()
            failures = db.execute("""
                SELECT platform, mode, detail, attempted_at FROM outbox
                WHERE state IN ('failed','uncertain') ORDER BY attempted_at DESC,id DESC LIMIT 30
            """).fetchall()
        items = []
        for row in actions:
            action = row["action"]
            item = {"timestamp": row["created_at"], "level": "info", "text": ACTIONS.get(action, "面板操作")}
            if action in {"simulate", "simulation_error"}:
                item["mode"] = "simulation"
            if action == "simulation_error":
                item["level"] = "warning"
            items.append(item)
        items += [{"timestamp": row["attempted_at"], "level": "warning",
                   "text": reason_label(row["detail"]), "platform": row["platform"], "mode": row["mode"]}
                  for row in failures]
        return sorted(items, key=lambda item: item["timestamp"] or 0, reverse=True)[:30]

    def channels(self):
        with closing(self.store.open_read()) as db:
            return [dict(row) for row in db.execute("""
                SELECT c.platform,c.channel_kind,c.channel_id,c.summary_at,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM memory_entries m
                    WHERE m.platform=c.platform AND m.channel_kind=c.channel_kind
                      AND m.channel_id=c.channel_id AND m.id>c.summary_cursor
                  ) THEN 'pending' WHEN c.summary_at IS NOT NULL THEN 'ready' ELSE 'empty' END AS summary_status
                FROM conversations c ORDER BY c.platform,c.channel_kind,c.channel_id
            """)]

    def memory(self, key, limit):
        with closing(self.store.open_read()) as db:
            entries = db.execute("""
                SELECT speaker, name, substr(text, 1, 1500) AS text, created_at
                FROM memory_entries WHERE platform=? AND channel_kind=? AND channel_id=?
                ORDER BY id DESC LIMIT ?
            """, (*key, limit)).fetchall()
            state = db.execute("""
                SELECT summary FROM conversations WHERE platform=? AND channel_kind=? AND channel_id=?
            """, key).fetchone()
        return {"entries": [{**dict(item), "text": self.redact(item["text"]),
                             "name": self.redact(item["name"])} for item in reversed(entries)],
                "summary": self.redact(state["summary"] if state else "")}

    def clear_memory(self, key) -> bool:
        with self.store.transaction() as db:
            updated = db.execute("""
                UPDATE conversations SET summary='',summary_cursor=0, summary_at=NULL,summary_checked_at=NULL
                WHERE platform=? AND channel_kind=? AND channel_id=?
            """, key)
            if not updated.rowcount:
                return False
            db.execute("DELETE FROM memory_entries WHERE platform=? AND channel_kind=? AND channel_id=?", key)
            self.store.record_panel_action(db, "clear_memory")
            return True

    def stickers(self):
        with closing(self.store.open_read()) as db:
            return [{"id": row["id"], **CATALOG_DEFAULT, **json.loads(row["metadata"])}
                    for row in db.execute("SELECT id,metadata FROM panel_stickers ORDER BY rowid DESC")]

    def edit_sticker(self, identifier, values) -> bool:
        with self.store.transaction() as db:
            updated = db.execute("UPDATE panel_stickers SET metadata=? WHERE id=?",
                                 (json.dumps(values, ensure_ascii=False), identifier))
            if updated.rowcount:
                self.store.record_panel_action(db, "edit_sticker")
            return bool(updated.rowcount)
