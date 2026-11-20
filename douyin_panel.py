import asyncio
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from douyin_auth import AppConfig, CALLBACK_PATH, DouyinAuth, STATE_COOKIE, STATE_SECONDS
from douyin_client import DouyinClient, DouyinError
from douyin_content import DraftConflict, DraftEdit, DraftGeneration, DraftUpdate, Drafts
from douyin_store import DouyinStore
from llm import DeepSeekClient, LanguageModel


def local_origin(request: Request) -> str:
    return "http://" + request.headers["host"].lower()


def mount_douyin(
    app: FastAPI, api: APIRouter, directory: Path, current_settings, model_key,
    *, client: DouyinClient | None = None, model: LanguageModel | None = None,
) -> None:
    store = DouyinStore(directory)
    auth = DouyinAuth(store, client or DouyinClient())
    drafts = Drafts(store)
    account_lock = asyncio.Lock()
    generation_lock = asyncio.Lock()

    def failure(exc: DouyinError) -> HTTPException:
        return HTTPException(422, str(exc))

    @api.get("/douyin/account")
    def account():
        return auth.status()

    @api.put("/douyin/app")
    async def save_app(proposal: AppConfig):
        async with account_lock:
            try:
                auth.configure(proposal)
            except DouyinError as exc:
                raise failure(exc) from None
        return auth.status()

    @api.post("/douyin/authorize")
    async def authorize(request: Request, response: Response):
        async with account_lock:
            try:
                url, browser = auth.start(local_origin(request))
            except DouyinError as exc:
                raise failure(exc) from None
        response.set_cookie(STATE_COOKIE, browser, httponly=True, samesite="lax",
                            max_age=STATE_SECONDS, path=CALLBACK_PATH)
        return {"authorization_url": url}

    @app.get(CALLBACK_PATH)
    async def callback(request: Request):
        # OAuth 顶级导航不能依赖面板 Origin。仅接受同一浏览器、同一来源的一次性 state。
        params = request.query_params
        status = "error"
        try:
            state = params.getlist("state")
            code = params.getlist("code")
            if len(state) != 1 or len(state[0]) != 43 or len(code) > 1:
                raise DouyinError("抖音回调格式有误。")
            async with account_lock:
                await auth.complete(state[0], request.cookies.get(STATE_COOKIE, ""), local_origin(request),
                                    code[0] if code and not params.get("error") else "")
            status = "ok"
        except DouyinError:
            pass
        result = RedirectResponse(f"/?douyin={status}#douyin-account", status_code=303)
        if status == "ok":
            result.delete_cookie(STATE_COOKIE, path=CALLBACK_PATH)
        return result

    @api.post("/douyin/refresh")
    async def refresh():
        async with account_lock:
            try:
                await auth.refresh()
            except DouyinError as exc:
                raise failure(exc) from None
        return auth.status()

    @api.delete("/douyin/account")
    async def disconnect(response: Response):
        async with account_lock:
            auth.disconnect()
        response.delete_cookie(STATE_COOKIE, path=CALLBACK_PATH)
        return auth.status()

    @api.get("/douyin/drafts")
    def list_drafts(before: Annotated[int | None, Query(gt=0)] = None):
        return {**drafts.list(before), "deepseek_configured": bool(model or model_key())}

    @api.post("/douyin/drafts", status_code=201)
    def save(payload: DraftEdit):
        try:
            return drafts.save(payload, current_settings())
        except DouyinError as exc:
            raise failure(exc) from None

    @api.put("/douyin/drafts/{identifier}")
    def update(identifier: int, payload: DraftUpdate):
        try:
            return drafts.update(identifier, payload, current_settings())
        except DouyinError as exc:
            raise failure(exc) from None
        except DraftConflict:
            raise HTTPException(409, "草稿已被修改或删除，请刷新。") from None

    @api.delete("/douyin/drafts/{identifier}")
    def delete(identifier: int, version: Annotated[int, Query(gt=0)]):
        try:
            drafts.delete(identifier, version)
        except DraftConflict:
            raise HTTPException(409, "草稿已被修改或删除，请刷新。") from None
        return {"deleted": True}

    @api.post("/douyin/drafts/demo", status_code=201)
    def demo(payload: DraftGeneration):
        try:
            return drafts.demo(payload.brief, current_settings())
        except DouyinError as exc:
            raise failure(exc) from None

    @api.post("/douyin/drafts/generate", status_code=201)
    async def generate(payload: DraftGeneration):
        if generation_lock.locked():
            raise HTTPException(409, "已有抖音草稿在生成中。")
        async with generation_lock:
            settings = current_settings()
            try:
                if model:
                    return await drafts.generate(payload.brief, settings, model)
                key = model_key()
                if not key:
                    raise DouyinError("V2 尚未配置 DeepSeek API Key。")
                async with httpx.AsyncClient(trust_env=False) as transport:
                    llm = DeepSeekClient(settings.deepseek, key, transport, attempts=1, timeout=45)
                    return await drafts.generate(payload.brief, settings, llm)
            except DouyinError as exc:
                raise failure(exc) from None
