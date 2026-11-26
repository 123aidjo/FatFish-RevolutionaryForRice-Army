"""A native service owned by the desktop launcher, stopped when its pipe closes."""

import asyncio
import sys
import threading

from local_runtime import apply_environment


def main():
    apply_environment()
    import uvicorn
    from live_qq import LiveQQ
    from panel import create_app
    from settings import load_settings
    from store import Store

    settings = load_settings()
    store = Store(settings.database_path)
    app = create_app(store, live_qq=LiveQQ(store))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.server.port,
                                         log_level=settings.log_level.lower(), access_log=False,
                                         proxy_headers=False))

    def watch_parent():
        # No HTTP shutdown endpoint and no termination of an unrelated PID.
        sys.stdin.buffer.read(1)
        server.should_exit = True

    threading.Thread(target=watch_parent, daemon=True).start()
    asyncio.run(server.serve())


if __name__ == "__main__":
    from role_profiles import worker_lock
    with worker_lock():
        main()
