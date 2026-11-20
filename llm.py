import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from settings import ModelSettings

log = logging.getLogger(__name__)
Emotion = Literal["joy", "teasing", "sleepy", "flustered", "sad", "smug", "neutral", "annoyed"]


class ModelError(Exception):
    pass


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str
    emotion: Emotion
    intensity: Annotated[int, Field(ge=0, le=3)]
    sticker: str | None
    should_send: bool = Field(alias="shouldSend")


class SummaryReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str


@dataclass(frozen=True, slots=True)
class Usage:
    prompt: int = 0
    completion: int = 0


@dataclass(frozen=True, slots=True)
class Completion:
    content: str
    usage: Usage = Usage()


class LanguageModel(Protocol):
    async def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion: ...


def parse_reply(content: str, max_chars: int) -> ModelReply:
    if not content.strip():
        raise ModelError("empty_model_content")
    try:
        reply = ModelReply.model_validate_json(content)
    except ValidationError:
        raise ModelError("invalid_model_json") from None
    reply.text = reply.text.strip()
    if reply.should_send and not reply.text:
        raise ModelError("empty_reply")
    if max_chars and len(reply.text) > max_chars:
        raise ModelError("reply_too_long")
    return reply


def parse_summary(content: str, limit: int) -> str:
    try:
        return SummaryReply.model_validate_json(content).summary.strip()[:limit]
    except ValidationError:
        raise ModelError("invalid_summary_json") from None


def token_count(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else 0


class DeepSeekClient:
    ENDPOINT = "https://api.deepseek.com/chat/completions"

    def __init__(
        self, settings: ModelSettings, api_key: str, client: httpx.AsyncClient,
        *, timeout: float = 15, attempts: int = 2, retry_delay: float = 0.5,
    ) -> None:
        if not api_key.strip() or not 1 <= attempts <= 3 or not 0 < timeout <= 60 or retry_delay < 0:
            raise ValueError("DeepSeek 请求参数无效")
        self.settings = settings
        self._key = api_key
        self.client = client
        self.timeout = timeout
        self.attempts = attempts
        self.retry_delay = retry_delay

    async def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        payload = {
            "model": self.settings.name, "thinking": {"type": self.settings.thinking},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False,
        }
        for attempt in range(self.attempts):
            started = time.monotonic()
            try:
                async with asyncio.timeout(self.timeout):
                    response = await self.client.post(
                        self.ENDPOINT, json=payload, headers={"Authorization": f"Bearer {self._key}"},
                        timeout=self.timeout, follow_redirects=False,
                    )
                response.raise_for_status()
                result = self._decode(response)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                reason, retry = f"deepseek_http_{status}", status == 429 or 500 <= status <= 599
            except (httpx.TransportError, TimeoutError):
                reason, retry = "deepseek_transport_error", True
            except ModelError as exc:
                reason, retry = str(exc), False
            else:
                log.info("DeepSeek 请求成功 用时 %.2fs 输入 %d 输出 %d",
                         time.monotonic() - started, result.usage.prompt, result.usage.completion)
                return result
            # 日志仅保留受控错误码、耗时和次数。
            log.warning("DeepSeek 请求失败 %s 第 %d 次 用时 %.2fs", reason, attempt + 1, time.monotonic() - started)
            if not retry or attempt + 1 == self.attempts:
                raise ModelError(reason) from None
            await asyncio.sleep(self.retry_delay * (2 ** attempt))
        raise AssertionError("未完成 DeepSeek 请求")

    @staticmethod
    def _decode(response: httpx.Response) -> Completion:
        try:
            result = response.json()
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if choice["finish_reason"] != "stop":
                raise ModelError("incomplete_model_content")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise TypeError
            usage = result.get("usage") or {}
            return Completion(content, Usage(token_count(usage, "prompt_tokens"),
                                             token_count(usage, "completion_tokens")))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("invalid_deepseek_response") from None
