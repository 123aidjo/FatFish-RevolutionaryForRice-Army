"""Local desktop settings; never stored alongside source code."""

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


DEFAULTS = {
    "qq_bot_id": "", "qq_group_id": "", "onebot_url": "ws://127.0.0.1:3001",
    "onebot_token": "", "webui_url": "http://127.0.0.1:6099/webui/",
    "webui_config": "", "napcat_launcher": "", "model_key": "",
}
ENVIRONMENT = {
    "qq_bot_id": "FISHBOT_QQ_BOT_ID", "qq_group_id": "FISHBOT_QQ_GROUP_ID",
    "onebot_url": "FISHBOT_ONEBOT_URL", "onebot_token": "FISHBOT_ONEBOT_TOKEN",
    "webui_url": "FISHBOT_NAPCAT_WEBUI_URL", "webui_config": "FISHBOT_NAPCAT_WEBUI_CONFIG",
    "model_key": "DEEPSEEK_API_KEY",
}


def data_directory() -> Path:
    from role_profiles import Profiles, active_id
    return Profiles().data(active_id())


def local_url(value: str, scheme: str) -> str:
    try:
        url = urlsplit(value)
        if (url.scheme != scheme or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.query or url.fragment
                or not url.port or not 1 <= url.port <= 65535):
            raise ValueError
    except ValueError:
        raise ValueError("连接地址必须是带端口的本机地址，不能包含密码或查询参数。") from None
    return value


def validate(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError("本地启动配置含未知字段。")
    result = {**DEFAULTS, **values}
    if any(not isinstance(value, str) for value in result.values()):
        raise ValueError("本地启动配置格式无效。")
    result = {key: value.strip() for key, value in result.items()}
    for key in ("qq_bot_id", "qq_group_id"):
        if result[key] and not re.fullmatch(r"[1-9][0-9]{4,11}", result[key]):
            raise ValueError("QQ 号和授权群号需为 5–12 位数字，首位不能为 0。")
    local_url(result["onebot_url"], "ws")
    local_url(result["webui_url"], "http")
    for key in ("onebot_token", "model_key"):
        if any(character.isspace() for character in result[key]):
            raise ValueError("密钥和 Token 不能包含空白字符。")
    return result


def read_config() -> dict:
    from role_profiles import isolated
    path = data_directory() / "desktop.local.json"
    try:
        values = validate(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else dict(DEFAULTS)
        # Explicit isolated data directories and legacy WSL deployments remain independent.
        if not os.getenv("FISHBOT_DATA_DIR") and os.getenv("FISHBOT_USE_WSL") != "1" and not isolated():
            from bundled_runtime import settings
            values.update(settings(Path(__file__).resolve().parent))
        return validate(values)
    except (ValueError, TypeError):
        raise ValueError("本地启动配置无效，请检查 desktop.local.json；未覆盖原文件。") from None


def save_config(values: dict) -> None:
    values = validate(values)
    folder = data_directory()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(values, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, folder / "desktop.local.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_environment() -> None:
    from role_profiles import isolated, child_environment
    if isolated():
        clean = child_environment()
        for key in set(os.environ) - set(clean):
            os.environ.pop(key, None)
        for key, value in read_config().items():
            if key in ENVIRONMENT and value:
                os.environ[ENVIRONMENT[key]] = value
        vision = data_directory() / "environment.local.json"
        if vision.exists():
            values = json.loads(vision.read_text(encoding="utf-8"))
            allowed = {"FISHBOT_VISION_ENDPOINT", "FISHBOT_VISION_MODEL", "FISHBOT_VISION_KEY"}
            if not isinstance(values, dict) or set(values) - allowed or any(not isinstance(v,str) for v in values.values()):
                raise ValueError("角色视觉模型配置无效。")
            os.environ.update(values)
        return
    # Existing command-line/WSL environment always wins over desktop defaults.
    bundled = Path(__file__).resolve().parent / "runtime/napcat/node.exe"
    if not (data_directory() / "desktop.local.json").exists() and (
            not bundled.is_file() or os.getenv("FISHBOT_DATA_DIR") or os.getenv("FISHBOT_USE_WSL") == "1"):
        return
    for key, value in read_config().items():
        if key in ENVIRONMENT and value:
            os.environ.setdefault(ENVIRONMENT[key], value)
