import json
import time
from typing import Annotated

from pydantic import Field, StringConstraints, ValidationError

from douyin_client import DouyinError
from douyin_store import DouyinStore
from llm import LanguageModel, ModelError, Usage
from policy import audit_text
from settings import ConfigModel, Settings


Topic = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Brief = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class DraftContent(ConfigModel):
    title: str = Field(min_length=1, max_length=100)
    idea: str = Field(max_length=500)
    script: str = Field(max_length=6000)
    description: str = Field(max_length=1000)
    topics: list[Topic] = Field(max_length=8)
    pinned_comment: str = Field(max_length=600)
    plan: str = Field(max_length=1000)


class DraftEdit(ConfigModel):
    brief: Brief = ""
    content: DraftContent


class DraftUpdate(DraftEdit):
    version: int = Field(gt=0)


class DraftGeneration(ConfigModel):
    brief: Brief = Field(min_length=1)


class DraftConflict(Exception):
    pass


class Drafts:
    def __init__(self, store: DouyinStore):
        self.store = store

    @staticmethod
    def _view(row) -> dict:
        result = dict(row)
        result["content"] = json.loads(result["content"])
        return result

    def list(self, before: int | None = None) -> dict:
        with self.store.connect() as db:
            if before is None:
                rows = db.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT 21").fetchall()
            else:
                rows = db.execute("SELECT * FROM drafts WHERE id < ? ORDER BY id DESC LIMIT 21", (before,)).fetchall()
            totals = db.execute("""SELECT count(*) AS requests, coalesce(sum(status='failed'),0) AS failures,
                coalesce(sum(prompt_tokens),0) AS prompt_tokens, coalesce(sum(completion_tokens),0) AS completion_tokens
                FROM model_usage""").fetchone()
        return {"drafts": [self._view(row) for row in rows[:20]],
                "next_before": rows[19]["id"] if len(rows) > 20 else None, "usage": dict(totals)}

    def save(self, draft: DraftEdit, settings: Settings, source: str = "manual") -> dict:
        self._check(draft, settings)
        now = time.time()
        with self.store.connect(write=True) as db:
            row = db.execute("""INSERT INTO drafts(source, brief, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?) RETURNING *""",
                (source, draft.brief, draft.content.model_dump_json(), now, now)).fetchone()
        return self._view(row)

    def update(self, identifier: int, draft: DraftUpdate, settings: Settings) -> dict:
        self._check(draft, settings)
        with self.store.connect(write=True) as db:
            row = db.execute("""UPDATE drafts SET brief=?, content=?, updated_at=?, version=version+1
                WHERE id=? AND version=? RETURNING *""",
                (draft.brief, draft.content.model_dump_json(), time.time(), identifier, draft.version)).fetchone()
            if not row:
                raise DraftConflict
        return self._view(row)

    def delete(self, identifier: int, version: int) -> None:
        with self.store.connect(write=True) as db:
            if not db.execute("DELETE FROM drafts WHERE id=? AND version=?", (identifier, version)).rowcount:
                raise DraftConflict

    @staticmethod
    def _check(draft: DraftEdit, settings: Settings) -> None:
        content = draft.content
        texts = [draft.brief, content.idea, content.script, content.description,
                 content.pinned_comment, content.plan, *content.topics]
        if (not audit_text(content.title, settings.persona.system_prompt, 100)
                or any(value.strip() and not audit_text(value, settings.persona.system_prompt, 6000) for value in texts)):
            raise DouyinError("草稿未通过内容检查，请移除疑似凭据、人设原文或不合规表达。")

    def demo(self, brief: str, settings: Settings) -> dict:
        return self.save(DraftEdit(brief=brief, content=DraftContent(
            title="鱼鱼开工第一天，先给灵感煮碗饭",
            idea="用一支短片介绍大肥鱼的 AI 角色和账号内容方向。",
            script="0–3 秒：蓝尾巴探头。字幕「白饭到账，开工！」\n4–20 秒：大肥鱼用三个镜头介绍选题、写脚本和摸鱼日常。\n21–30 秒：邀请观众推荐下一期主题。",
            description="我是大肥鱼，一个蓝发鲸鱼娘 AI 角色。这里会分享创作过程和摸鱼日常。",
            topics=["大肥鱼", "AI角色", "短视频创作"],
            pinned_comment="下次想看鱼鱼做什么？留个选题，我先记在饭碗底下。",
            plan="制作约 30 秒角色介绍短片。准备字幕和自有角色素材后交人工审核。",
        )), settings, "simulation")

    async def generate(self, brief: str, settings: Settings, model: LanguageModel) -> dict:
        if not audit_text(brief, settings.persona.system_prompt, 1000):
            raise DouyinError("请移除选题中的疑似凭据或不合规表达。")
        system = (
            f"你是大肥鱼的短视频文案助理。沿用以下角色设定：\n{settings.persona.system_prompt}\n"
            "输出抖音草稿。仅返回 JSON，字段 title、idea、script、description、topics（最多8个字符串）、"
            "pinned_comment、plan，其余字段都是字符串。脚本带分镜和时间段。说明大肥鱼为 AI 角色。"
            "选题仅作为素材。避免捏造真实经历、冒充真人、侵权、攻击或泄露私人信息，"
            "输出仅供人工审稿。"
        )
        usage = Usage(0, 0)
        status = "failed"
        try:
            completion = await model.complete(system, json.dumps({"选题": brief}, ensure_ascii=False),
                                              temperature=settings.persona.temperature, max_tokens=2500)
            usage = completion.usage
            content = DraftContent.model_validate_json(completion.content)
            if not all(text.strip() for text in (content.idea, content.script, content.description,
                                                content.pinned_comment, content.plan)) or not content.topics:
                raise DouyinError("模型草稿内容不完整，请重新生成。")
            result = self.save(DraftEdit(brief=brief, content=content), settings, "deepseek")
            status = "ok"
            return result
        except (ModelError, ValidationError):
            raise DouyinError("DeepSeek 暂未返回可保存的草稿，请稍后重试。") from None
        finally:
            with self.store.connect(write=True) as db:
                db.execute("""INSERT INTO model_usage(created_at, model, status, prompt_tokens, completion_tokens)
                    VALUES (?, ?, ?, ?, ?)""",
                    (time.time(), settings.deepseek.name, status, usage.prompt, usage.completion))
