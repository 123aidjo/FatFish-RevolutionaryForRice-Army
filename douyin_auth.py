import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator

from douyin_client import DouyinClient, DouyinError, authorization_url
from douyin_store import DouyinStore
from settings import ConfigModel


CALLBACK_PATH = "/douyin/callback"
STATE_COOKIE = "fishbot_douyin_flow"
STATE_SECONDS = 600
SCOPE = re.compile(r"[a-zA-Z0-9_.]{1,50}")
AVATAR_DOMAINS = ("douyinpic.com", "douyincdn.com")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AppConfig(ConfigModel):
    client_key: str = Field(pattern=r"^[A-Za-z0-9_-]{3,150}$")
    client_secret: SecretStr | None = None
    redirect_uri: str = Field(max_length=500)

    @field_validator("redirect_uri")
    @classmethod
    def check_redirect(cls, value: str) -> str:
        url = urlsplit(value)
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or not url.path or any(char.isspace() for char in value)
                or (url.scheme != "https" and not (local and url.scheme == "http"))
                or (local and url.path != CALLBACK_PATH)):
            raise ValueError("请填写官方控制台登记的回调地址")
        return value


def get(db, key: str):
    row = db.execute("SELECT value FROM oauth_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def put(db, key: str, value) -> None:
    db.execute("INSERT INTO oauth_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))


def expiry(data: dict, field: str, now: float) -> float:
    seconds = str(data.get(field, ""))
    if not re.fullmatch(r"[0-9]{1,8}", seconds) or not int(seconds):
        raise DouyinError("抖音返回的令牌有效期无效，请重新授权。")
    return now + int(seconds)


def token_field(data: dict, name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,2048}", value):
        raise DouyinError("抖音返回的授权信息不完整，请重新授权。")
    return value


class DouyinAuth:
    def __init__(self, store: DouyinStore, client: DouyinClient):
        self.store = store
        self.client = client

    def status(self) -> dict:
        with self.store.connect() as db:
            config, grant, error = (get(db, key) for key in ("app", "grant", "error"))
        active = bool(grant and grant["expires_at"] > time.time())
        return {
            "mode": "draft", "connection": "verified" if active else "expired" if grant else "disconnected",
            "app_configured": bool(config), "app_review": "unknown",
            "redirect_uri": config["redirect_uri"] if config else "",
            "requested_scopes": ["user_info"], "granted_scopes": grant["scopes"] if grant else [],
            "profile": grant["profile"] if grant else None, "error": error,
            "expires_at": grant["expires_at"] if grant else None,
            "refresh_expires_at": grant["refresh_expires_at"] if grant else None,
            "can_refresh": bool(grant and grant["refresh_expires_at"] > time.time()),
        }

    def configure(self, proposal: AppConfig):
        with self.store.connect(write=True) as db:
            previous = get(db, "app")
            change_key = previous is None or previous["client_key"] != proposal.client_key
            supplied = proposal.client_secret.get_secret_value().strip() if proposal.client_secret else ""
            if not supplied and change_key:
                raise DouyinError("首次配置或更换应用时，请填写 Client Secret。")
            secret = supplied or previous["client_secret"]
            if not re.fullmatch(r"[!-~]{1,250}", secret):
                raise DouyinError("Client Secret 的格式无效。")
            config = dict(client_key=proposal.client_key, client_secret=secret, redirect_uri=proposal.redirect_uri)
            if config != previous:
                if get(db, "grant"):
                    raise DouyinError("请先断开当前账号，再修改应用配置。")
                put(db, "app", config)
                db.execute("DELETE FROM oauth_state WHERE key IN ('flow','error')")

    def start(self, local_origin: str) -> tuple[str, str]:
        with self.store.connect(write=True) as db:
            config = get(db, "app")
            if not config:
                raise DouyinError("请先配置已审核应用的 Client Key、Client Secret 和回调地址。")
            redirect = config["redirect_uri"]
            if urlsplit(redirect).hostname in {"127.0.0.1", "localhost", "::1"} and redirect != local_origin + CALLBACK_PATH:
                raise DouyinError("请从与已登记回调地址相同的本机面板地址发起授权。")
            state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            put(db, "flow", {"state": digest(state), "browser": digest(browser),
                             "expires_at": time.time() + STATE_SECONDS, "origin": local_origin})
            db.execute("DELETE FROM oauth_state WHERE key='error'")
        return authorization_url(config["client_key"], redirect, state), browser

    def _consume_flow(self, state: str, browser: str, origin: str) -> dict:
        with self.store.connect(write=True) as db:
            flow, config = get(db, "flow"), get(db, "app")
            if (not flow or not config or flow["expires_at"] <= time.time() or flow["origin"] != origin
                    or not hmac.compare_digest(flow["state"], digest(state))
                    or not hmac.compare_digest(flow["browser"], digest(browser))):
                raise DouyinError("授权回调已过期或与当前浏览器不匹配。请重新发起授权。")
            db.execute("DELETE FROM oauth_state WHERE key='flow'")
        return config

    async def _verified_grant(self, data: dict, now: float, prior: dict | None) -> dict:
        open_id = token_field(data, "open_id")
        if prior and open_id != prior["open_id"]:
            raise DouyinError("返回的账号与当前账号不同。切换账号前请先断开当前连接。")
        access = token_field(data, "access_token")
        refresh = token_field(data, "refresh_token")
        scope = data.get("scope")
        scopes = sorted({item.strip() for item in scope.split(",") if SCOPE.fullmatch(item.strip())}) if isinstance(scope, str) else []
        if "user_info" not in scopes:
            raise DouyinError("抖音没有返回 user_info 授权。")
        expires_at = expiry(data, "expires_in", now)
        refresh_expires_at = expiry(data, "refresh_expires_in", now)
        profile = await self.client.profile(access, open_id)
        if profile.get("open_id") != open_id or not isinstance(profile.get("nickname"), str):
            raise DouyinError("抖音返回的账号身份无法核对。")
        avatar = profile.get("avatar")
        try:
            url = urlsplit(avatar) if isinstance(avatar, str) and len(avatar) <= 1024 else None
        except ValueError:
            url = None
        if (not url or url.scheme != "https" or url.username or url.password
                or not any((url.hostname or "").endswith("." + suffix) for suffix in AVATAR_DOMAINS)):
            avatar = None
        nickname = profile["nickname"]
        if any(value in nickname or (avatar and value in avatar) for value in (access, refresh)):
            raise DouyinError("抖音返回的账号资料格式异常。")
        return {"open_id": open_id, "access_token": access, "refresh_token": refresh,
                "scopes": scopes, "expires_at": expires_at, "refresh_expires_at": refresh_expires_at,
                "profile": {"nickname": nickname[:100], "avatar": avatar, "verified_at": time.time()}}

    async def complete(self, state: str, browser: str, origin: str, code: str):
        config = self._consume_flow(state, browser, origin)
        with self.store.connect() as db:
            prior = get(db, "grant")
        try:
            if not code or not re.fullmatch(r"[!-~]{1,1024}", code):
                raise DouyinError("本次授权没有返回授权码，请确认后重新发起。")
            now = time.time()
            data = await self.client.exchange(config["client_key"], config["client_secret"], code)
            grant = await self._verified_grant(data, now, prior)
        except DouyinError as exc:
            self._record_error(exc)
            raise
        self._save_grant(grant)

    async def refresh(self):
        with self.store.connect() as db:
            config, prior = get(db, "app"), get(db, "grant")
        try:
            now = time.time()
            if not config or not prior or prior["refresh_expires_at"] <= now:
                raise DouyinError("当前没有可用的刷新令牌，请重新授权。")
            data = await self.client.refresh(config["client_key"], prior["refresh_token"])
            grant = await self._verified_grant(data, now, prior)
            grant["refresh_expires_at"] = min(grant["refresh_expires_at"], prior["refresh_expires_at"])
        except DouyinError as exc:
            self._record_error(exc)
            raise
        self._save_grant(grant)

    def disconnect(self):
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM oauth_state WHERE key IN ('grant','flow','error')")

    def _record_error(self, exc: DouyinError) -> None:
        with self.store.connect(write=True) as db:
            put(db, "error", str(exc))

    def _save_grant(self, grant: dict) -> None:
        with self.store.connect(write=True) as db:
            put(db, "grant", grant)
            db.execute("DELETE FROM oauth_state WHERE key='error'")
