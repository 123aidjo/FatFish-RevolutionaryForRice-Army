"""Role-local decision settings. Existing chat interval and persona aliases stay authoritative."""
import hashlib
from contextlib import closing
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, field_validator
from settings import ConfigModel


class ReplyConfig(ConfigModel):
    mode: Literal['eco','balanced'] = 'eco'
    backoff: bool = True
    multiplier: float = Field(default=2,ge=1,le=10)
    max_interval: int = Field(default=32,ge=1,le=1000)
    max_wait: float = Field(default=120,ge=1,le=3600)
    conversation_ttl: float = Field(default=90,ge=0,le=1800)
    early_name: bool = False
    early_continuation: bool = False
    early_topic: bool = False
    early_interest: bool = False
    aggregate_seconds: float = Field(default=0,ge=0,le=30)
    semantic: bool = False
    semantic_interval: float = Field(default=30,ge=5,le=3600)
    semantic_timeout: float = Field(default=.5,ge=.1,le=3)
    interest_threshold: float = Field(default=.75,ge=0,le=1)
    topic_threshold: float = Field(default=.35,ge=0,le=1)
    continuation_threshold: float = Field(default=.8,ge=0,le=1)
    planner_same_model: bool = True
    planner_base_url: str = 'https://api.deepseek.com'
    planner_model: str = Field(default='deepseek-flash',min_length=1,max_length=200)
    planner_key: str = Field(default='',max_length=4000)
    planner_disable_thinking: bool = True
    planner_timeout: float = Field(default=8,ge=.1,le=60)
    planner_input_tokens: int = Field(default=800,ge=200,le=4000)
    planner_output_tokens: int = Field(default=64,ge=16,le=256)
    planner_messages: int = Field(default=10,ge=1,le=20)
    planner_failure: Literal['eco','skip'] = 'eco'

    @field_validator('planner_base_url')
    @classmethod
    def validate_url(cls,value):
        url=urlsplit(value)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.scheme not in {'http','https'} or (url.scheme=='http' and url.hostname not in {'localhost','127.0.0.1','::1'})):
            raise ValueError('远程 API 须为 HTTPS，本机可使用 HTTP，地址不能包含凭据')
        return value.rstrip('/')


def read_config(store):
    with closing(store.open_read()) as db:
        row=db.execute("SELECT value FROM panel_state WHERE key='reply_decision_config'").fetchone()
    raw=row[0] if row else ReplyConfig().model_dump_json()
    return ReplyConfig.model_validate_json(raw),hashlib.sha256(raw.encode()).hexdigest()


def save_config(store,cfg,revision):
    with store.transaction() as db:
        row=db.execute("SELECT value FROM panel_state WHERE key='reply_decision_config'").fetchone()
        raw=row[0] if row else ReplyConfig().model_dump_json()
        if hashlib.sha256(raw.encode()).hexdigest()!=revision: raise ValueError('config_conflict')
        db.execute("INSERT INTO panel_state VALUES('reply_decision_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(cfg.model_dump_json(),))
