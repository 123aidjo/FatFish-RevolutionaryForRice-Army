import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

if __name__ == "__main__":
    import os
    from role_profiles import active_id
    os.environ["FISHBOT_PROFILE_ID"] = active_id()

from live_qq import LiveQQ
from panel import create_app, panel_token
from settings import load_settings
from simulation import run_demo
from store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="大肥鱼 V2")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--simulate", action="store_true", help="在隔离的 SQLite 库运行一次模拟")
    commands.add_argument("--panel-token", action="store_true", help="在本机终端查看面板访问令牌")
    args = parser.parse_args()
    if args.simulate:
        with TemporaryDirectory(prefix="fishbot-v2-") as folder:
            path = Path(folder) / "simulation.db"
            store = Store(path)
            store.initialize()
            result = asyncio.run(run_demo(store, load_settings(database_path=path)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    settings = load_settings()
    store = Store(settings.database_path)
    if args.panel_token:
        store.initialize()
        print(panel_token(store, store.path.parent))
        return
    app = create_app(store, live_qq=LiveQQ(store))
    from role_profiles import worker_lock
    with worker_lock():
        uvicorn.run(app, host="127.0.0.1", port=settings.server.port,
                    log_level=settings.log_level.lower(), proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
