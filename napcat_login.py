"""本机 NapCat WebUI 登录状态与二维码刷新。"""

import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from local_runtime import apply_environment, local_url
apply_environment()


WEBUI_URL = local_url(os.getenv("FISHBOT_NAPCAT_WEBUI_URL", "http://127.0.0.1:16099/webui/"), "http")
_webui = urlsplit(WEBUI_URL)
API_URL = f"{_webui.scheme}://{_webui.netloc}/api/"


class NapCatLogin:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.opener = build_opener(ProxyHandler({}))
        self.credential = ""
        self.next_refresh = 0.0

    def post(self, route, payload):
        headers = {"Content-Type": "application/json"}
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential
        request = Request(API_URL + route, json.dumps(payload).encode(), headers, method="POST")
        with self.opener.open(request, timeout=3) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise OSError("NapCat WebUI 返回无效回包")
        return data.get("data")

    def login_state(self):
        # 管理凭据保留在进程内，状态提示只使用预设文字。
        try:
            if not self.credential:
                token = json.loads(self.config_path.read_text(encoding="utf-8-sig"))["token"]
                digest = hashlib.sha256((token + ".napcat").encode()).hexdigest()
                auth = self.post("auth/login", {"hash": digest})
                self.credential = auth.get("Credential") if isinstance(auth, dict) else ""
            status = self.post("QQLogin/CheckLoginStatus", {}) if self.credential else None
        except (OSError, ValueError, KeyError, TypeError):
            self.credential = ""
            raise OSError("无法读取 NapCat 登录状态，请检查 WebUI 地址、配置文件或登录密码。") from None
        if not isinstance(status, dict):
            self.credential = ""
            raise OSError("V2 登录状态暂不可用")
        return status

    @staticmethod
    def phase(status):
        if status.get("isLogin") and status.get("coreReady"):
            return "online"
        if status.get("qrLoginAccepted"):
            return "initializing"
        return status.get("loginPhase", "")

    @classmethod
    def login_blocker(cls, status):
        phase = cls.phase(status)
        error = str(status.get("loginError") or "")
        if phase not in {"online", "initializing"} and "安全风险" in error and "恢复账号使用" in error:
            return "QQ 限制了本次登录。请在最新版手机 QQ 中登录此账号，按提示恢复账号使用后再扫码。"
        if phase == "waiting_qrcode" and "身份已失效" in error:
            return "QQ 登录身份已失效，请重新扫码并在手机确认。"
        return ""

    def refresh_expired(self, status, modified_at):
        """只在等待扫码且二维码过期时刷新，刷新前再核对一次状态。"""
        expired = "过期" in str(status.get("loginError") or "")
        stale = modified_at and time.time() - modified_at >= 110
        if self.phase(status) != "waiting_qrcode" or not (expired or stale):
            return self.phase(status), False
        if time.monotonic() >= self.next_refresh:
            latest = self.login_state()
            phase = self.phase(latest)
            if phase != "waiting_qrcode" or latest.get("qrcodeurl") != status.get("qrcodeurl"):
                return phase, True
            self.next_refresh = time.monotonic() + 15
            self.post("QQLogin/RefreshQRcode", {})
        return "refreshing", True
