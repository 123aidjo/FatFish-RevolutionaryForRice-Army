import asyncio
import json

import httpx
import pytest

from llm import DeepSeekClient, ModelError, Usage
from settings import ModelSettings


def run_client(handler, *, attempts=2):
    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            model = DeepSeekClient(
                ModelSettings(name="deepseek-flash"), "mock-only", client,
                attempts=attempts, retry_delay=0,
            )
            return await model.complete("只输出 JSON", "你好", temperature=1.0, max_tokens=500)
    return asyncio.run(run())


def test_deepseek_json_request_and_usage_after_retry():
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == DeepSeekClient.ENDPOINT
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert request.headers["Authorization"] == "Bearer mock-only"
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "模拟限流"})
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 7},
        })

    result = run_client(handler)
    assert len(calls) == 2
    assert (result.content, result.usage) == ("{}", Usage(17, 7))


def test_deepseek_network_timeout_retries_only_configured_attempts():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("只供模拟", request=request)

    with pytest.raises(ModelError, match="deepseek_transport_error"):
        run_client(handler, attempts=2)
    assert len(calls) == 2


@pytest.mark.parametrize(("code", "body", "reason"), [
    (401, {"error": "要保密的服务端原文"}, "deepseek_http_401"),
    (200, {"choices": []}, "invalid_deepseek_response"),
    (200, {"choices": [{"finish_reason": "length", "message": {"content": "{\"text\":"}}]},
     "incomplete_model_content"),
])
def test_deepseek_protocol_failures_without_retry_or_body_logs(caplog, code, body, reason):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, json=body)

    with pytest.raises(ModelError, match=reason):
        run_client(handler)
    assert len(calls) == 1
    assert "要保密的服务端原文" not in caplog.text
    assert "mock-only" not in caplog.text
