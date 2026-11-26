"""Authenticated management and one scheduler owned by the FastAPI lifespan."""
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException, Response
from pydantic import Field

from bilibili_browser import BrowseError, BrowseSettings, BrowseStore, Browser
from bilibili_sources import BiliSources, BiliVision, vision_ready
from llm import DeepSeekClient
from settings import ConfigModel


class BrowseEdit(ConfigModel):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    config: BrowseSettings


class RuntimeModel:
    def __init__(self, directory, settings, http):
        self.directory, self.settings, self.http = directory, settings, http

    async def complete(self, *args, **kwargs):
        from live_qq import read_key
        key = read_key(self.directory)
        if not key:
            raise BrowseError('model_key_missing')
        model = DeepSeekClient(self.settings().deepseek,key,self.http,timeout=60,attempts=1)
        return await model.complete(*args,**kwargs)


class BrowseRuntime:
    def __init__(self, store, directory, settings, sources=None, model=None, vision=None):
        self.storage = BrowseStore(store)
        self.directory, self.settings = directory, settings
        self.sources, self.model, self.vision = sources, model, vision
        self.browser = None
        self.manual = None
        self.worker = None

    @asynccontextmanager
    async def running(self):
        self.storage.initialize()
        async with httpx.AsyncClient(timeout=60) as http:
            self.sources = self.sources or BiliSources(self.directory)
            model = self.model or RuntimeModel(self.directory,self.settings,http)
            self.browser = Browser(self.storage,self.sources,model,
                lambda:self.settings().persona.system_prompt,self.vision or BiliVision(self.sources))
            self.worker = asyncio.create_task(self.browser.loop())
            try:
                yield
            finally:
                tasks = [t for t in (self.manual,self.worker) if t is not None]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await self.sources.close()


def mount_bilibili(api, runtime):
    @api.get('/bilibili')
    def status():
        return {**runtime.storage.status(), 'credential_saved':runtime.sources.path.exists(),
                'vision_ready':vision_ready(), 'running':runtime.browser.lock.locked(),
                'worker_alive':runtime.worker is not None and not runtime.worker.done()}

    @api.put('/bilibili/config')
    def configure(edit: BrowseEdit):
        try:
            runtime.storage.configure(edit.config,edit.revision)
        except BrowseError:
            raise HTTPException(409,'配置已变化，请刷新后再保存') from None
        return {'saved':True}

    @api.get('/bilibili/history')
    def history():
        return {'items':runtime.storage.history()}

    @api.post('/bilibili/run',status_code=202)
    async def run():
        config,_ = runtime.storage.config()
        if runtime.browser.lock.locked() or (runtime.manual is not None and not runtime.manual.done()):
            raise HTTPException(409,'已有浏览任务正在运行')
        if not config.enabled or not config.in_window(runtime.storage.clock()):
            raise HTTPException(409,'请先开启浏览，并在允许时段内运行')
        if runtime.storage.status()['today_rounds'] >= config.rounds_per_day:
            raise HTTPException(409,'今日轮次已用完（失败或中断也占额度）')
        async def execute():
            try:
                await runtime.browser.run()
            except BrowseError:
                pass  # A simultaneous background reservation can win; never bypass it.
        runtime.manual = asyncio.create_task(execute())
        return {'accepted':True}

    @api.post('/bilibili/login')
    async def login():
        try:
            async with asyncio.timeout(40):
                await runtime.sources.login()
        except Exception:
            raise HTTPException(502,'生成 B 站二维码失败，请检查网络后重试') from None
        return {'expires_in':180}

    @api.get('/bilibili/login/image')
    def image():
        if not runtime.sources.qr_image:
            raise HTTPException(404,'请先生成二维码')
        return Response(runtime.sources.qr_image,media_type='image/png')

    @api.post('/bilibili/login/check')
    async def check():
        try:
            async with asyncio.timeout(40):
                return {'state':await runtime.sources.poll_login()}
        except Exception:
            raise HTTPException(502,'登录校验失败，请重新扫码或检查网络') from None
