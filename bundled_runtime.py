"""Project-local NapCat binaries, with fresh per-installation credentials."""
import json
from pathlib import Path
import secrets


def settings(project: Path) -> dict:
    root = project / "runtime" / "napcat"
    if not (root / "node.exe").is_file() or not (root / "napcat" / "napcat.mjs").is_file():
        return {}
    config = root / "napcat" / "config"
    config.mkdir(parents=True, exist_ok=True)
    defaults = {
        "webui.json": {"host": "127.0.0.1", "port": 16099, "token": secrets.token_urlsafe(32)},
        "onebot11.json": {"network": {"websocketServers": [{
            "name": "Fatfish", "enable": True, "host": "127.0.0.1", "port": 13001,
            "messagePostFormat": "array", "reportSelfMessage": False, "token": secrets.token_urlsafe(32),
        }], "httpServers": [], "httpClients": [], "websocketClients": []}},
    }
    for name, value in defaults.items():
        path = config / name
        try:
            with path.open("x", encoding="utf8") as stream:
                json.dump(value, stream, indent=2)
        except FileExistsError:
            pass
    try:
        webui = json.loads((config / "webui.json").read_text(encoding="utf-8-sig"))
        onebot = json.loads((config / "onebot11.json").read_text(encoding="utf-8-sig"))
        server = next(item for item in onebot["network"]["websocketServers"] if item.get("enable"))
        return {"napcat_launcher": str(root / "napcat.bat"), "webui_config": str(config / "webui.json"),
                "webui_url": f"http://127.0.0.1:{int(webui['port'])}/webui/",
                "onebot_url": f"ws://127.0.0.1:{int(server['port'])}", "onebot_token": server.get("token", "")}
    except (ValueError, KeyError, TypeError, StopIteration):
        raise ValueError("项目内 NapCat 配置无效，原文件已保留。") from None
