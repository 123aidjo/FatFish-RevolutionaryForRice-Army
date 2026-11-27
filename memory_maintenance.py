"""One lifespan-owned, cancellable maintenance loop per role."""
import asyncio
from contextlib import asynccontextmanager
import logging
import time

from memory_engine import VectorIndex, read_config
from memory_growth import Growth, MemoryModel


class MemoryMaintenance:
    def __init__(self,store,settings,live_qq=None):
        self.store,self.settings,self.live_qq=store,settings,live_qq
        self.task=None

    def permitted(self,scope):
        from cognitive_panel import scope_permitted
        return scope_permitted(scope,self.settings(),self.live_qq)

    async def loop(self):
        index=VectorIndex(self.store,permitted=self.permitted)
        growth=Growth(self.store,MemoryModel(self.store,self.settings),permitted=self.permitted)
        next_growth=0
        while True:
            interval=30
            try:
                cfg,_=read_config(self.store)
                interval=cfg.interval
                await index.batch()
                if time.monotonic()>=next_growth:
                    if cfg.growth_enabled:
                        growth.schedule()
                        await growth.batch()
                    if cfg.reflection_enabled:
                        growth.schedule(force=True,kind='reflection')
                        await growth.batch('reflection')
                    next_growth=time.monotonic()+cfg.task_interval
                # Explicit manual task requests also run with automatic scheduling disabled.
                await growth.batch()
                await growth.batch('reflection')
            except asyncio.CancelledError: raise
            except Exception:
                # Task-specific errors are persisted; do not log request text or credentials.
                logging.getLogger(__name__).warning('记忆后台维护暂时失败，将自动重试')
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def running(self):
        self.task=asyncio.create_task(self.loop())
        try: yield
        finally:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
