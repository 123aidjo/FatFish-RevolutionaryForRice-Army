import asyncio
from contextlib import asynccontextmanager, closing
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from cognitive_panel import routes as cognition_routes
from cognitive_store import CognitiveStore, digest
from cognitive_workspace import DemoMemory
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from adapter_base import Platform
from douyin_panel import mount_douyin
from bilibili_panel import BrowseRuntime, mount_bilibili
from panel_config import ConfigConflict, NoBackup, PanelConfig, atomic_write
from panel_data import PanelData, reason_label
from panel_images import InvalidImage, MAX_BYTES, import_image
from settings import ConfigModel, EMOTIONS, Emotion, Limits, PROJECT_ROOT, StickerSettings
from simulation import run_demo
from stickers import Sticker, StickerPolicy
from store import Store

IDENTIFIER = re.compile(r"[0-9a-f]{32}")
HOST = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", re.I)
CHANNEL = re.compile(r"(?:group:|dm:|live:)?[A-Za-z0-9_-]{1,80}")
Status = Literal["received", "evaluate", "evaluating", "pending", "sending", "sent", "skipped", "failed", "uncertain"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Tag = Annotated[str, Field(min_length=1, max_length=60)]
log = logging.getLogger("fishbot.panel")
SESSION_COOKIE = "fishbot_panel_session"
SESSION_PREFIX = "panel_session:"
SESSION_SECONDS = 30 * 86400


class Versioned(ConfigModel):
    revision: Revision


class PersonaEdit(Versioned):
    name: str = Field(min_length=1, max_length=50)
    system_prompt: str = Field(min_length=1, max_length=10000)
    style: str = Field(max_length=1000)
    max_chars: int = Field(ge=0, le=1000)
    temperature: float = Field(ge=0, le=2)
    emotions: list[Emotion] = Field(min_length=1, max_length=8)


class SettingsEdit(Versioned):
    model: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    participation_every: int = Field(ge=1, le=1000)
    log_level: LogLevel
    limits: Limits
    stickers: StickerSettings


class PlatformEdit(Versioned):
    enabled: bool
    receive: bool
    send: bool = False
    allowlist: list[str] = Field(max_length=200)


class LiveQQEdit(Versioned):
    enabled: bool


class StickerEdit(ConfigModel):
    name: str = Field(min_length=1, max_length=60)
    enabled: bool
    emotions: list[Emotion] = Field(max_length=8)
    keywords: list[Tag] = Field(max_length=20)
    contexts: list[Tag] = Field(max_length=20)


class ClearMemory(ConfigModel):
    platform: Platform
    kind: Literal["group", "dm", "live"]
    channel: str = Field(min_length=1, max_length=100)
    confirmation: str = Field(max_length=100)


class RuntimeEdit(ConfigModel):
    action: Literal["pause", "resume", "simulate"]


def panel_token(store: Store, directory: Path) -> str:
    path = directory / "panel.token"
    with store.transaction():
        if not path.exists():
            atomic_write(path, (secrets.token_urlsafe(32) + "\n").encode("ascii"))
        return path.read_text(encoding="ascii").strip()


def error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def local_request(request: Request) -> bool:
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback
                    and HOST.fullmatch(request.headers.get("host", "")))
    except ValueError:
        return False


def activate_logging(level: str) -> None:
    for module in ("fishbot.panel", "dispatcher", "memory", "llm"):
        logging.getLogger(module).setLevel(level)


def create_app(
    store: Store, *, config_dir: Path | None = None, data_dir: Path | None = None,
    live_qq=None, douyin_client=None, douyin_model=None, bili_sources=None, bili_model=None, bili_vision=None,
) -> FastAPI:
    from role_profiles import config_directory
    config_dir = config_dir or config_directory()
    directory = data_dir or store.path.parent
    from role_profiles import active_id
    role_id = active_id()
    session_cookie = SESSION_COOKIE if role_id == 'default' else SESSION_COOKIE + '_' + role_id
    image_dir = directory / "stickers"
    config = PanelConfig(store, config_dir, directory)
    runtime_lock = asyncio.Lock()
    data = PanelData(store, lambda: (app.state.token,
                                    live_qq.key() if live_qq else os.environ.get("DEEPSEEK_API_KEY", "")))
    cognition = CognitiveStore(store)
    demo_memory = DemoMemory()
    browsing = BrowseRuntime(store, directory, lambda: config.read().settings,
                             bili_sources, bili_model, bili_vision)
    from memory_maintenance import MemoryMaintenance
    maintenance = MemoryMaintenance(store,lambda:config.read().settings,live_qq)

    def observe_persona(snapshot):
        persona = snapshot.settings.persona
        cognition.observe_launch(data.redact(persona.name), digest(persona.model_dump_json()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        app.state.token = panel_token(store, directory)
        try:
            async with browsing.running(), maintenance.running():
                if live_qq is None:
                    yield
                else:
                    observe_persona(config.read())
                    async with live_qq.running():
                        yield
        finally:
            demo_memory.close()

    app = FastAPI(title="大肥鱼 V2", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.url.path != "/health" and not local_request(request):
            return JSONResponse({"detail": "面板只允许从本机访问"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: https://*.douyinpic.com https://*.douyincdn.com; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "输入格式或范围无效，请检查表单后再提交。"}, status_code=422)

    @app.get("/health")
    def health(response: Response) -> dict[str, str]:
        ready = store.check_health()
        if not ready:
            response.status_code = 503
        return {"service": "fishbot-v2-public", "status": "ok" if ready else "unavailable",
                "database": "ok" if ready else "unavailable",
                **({'instance':os.environ['FISHBOT_SERVICE_INSTANCE']} if os.getenv('FISHBOT_SERVICE_INSTANCE') else {})}

    @app.get("/")
    def index():
        return FileResponse(PROJECT_ROOT / "index.html", media_type="text/html")

    @app.get("/assets/{name}")
    def asset(name: str):
        media = {"style.css": "text/css", "app.js": "text/javascript", "memory-workspace.js": "text/javascript",
                 "whale-girl.png": "image/png"}.get(name)
        if not media:
            raise error(404, "资源不存在")
        return FileResponse(PROJECT_ROOT / name, media_type=media)

    def verify_origin(request: Request):
        origin = request.headers.get("origin")
        expected = f"http://{request.headers['host'].lower()}"
        if ((origin is not None and origin != expected)
                or (request.method not in {"GET", "HEAD"} and origin != expected)):
            raise error(403, "请求来源不符合本机面板地址")
    def bearer_valid(request: Request) -> bool:
        supplied = request.headers.get("authorization", "")
        return hmac.compare_digest(supplied.encode("utf-8"), f"Bearer {app.state.token}".encode("utf-8"))

    def session_key(value: str) -> str:
        return SESSION_PREFIX + hmac.digest(app.state.token.encode(), value.encode(), "sha256").hex()

    def authorize(request: Request):
        verify_origin(request)
        if bearer_valid(request):
            return
        value = request.cookies.get(session_cookie, "")
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            with closing(store.open_read()) as db:
                entry = db.execute("SELECT value FROM panel_state WHERE key=?", (session_key(value),)).fetchone()
            if entry and int(entry[0]) > time.time():
                return
        raise error(401, "请输入有效的本机访问令牌")

    @app.post("/api/session")
    def start_session(request: Request, response: Response):
        verify_origin(request)
        if not bearer_valid(request):
            raise error(401, "请输入有效的本机访问令牌")
        now = int(time.time())
        value = secrets.token_urlsafe(32)
        old = request.cookies.get(session_cookie)
        with store.transaction() as db:
            db.execute("DELETE FROM panel_state WHERE key LIKE ? AND CAST(value AS INTEGER)<=?",
                       (SESSION_PREFIX + "%", now))
            if old:
                db.execute("DELETE FROM panel_state WHERE key=?", (session_key(old),))
            db.execute("INSERT INTO panel_state VALUES (?,?)", (session_key(value), str(now + SESSION_SECONDS)))
        response.set_cookie(session_cookie, value, httponly=True, samesite="strict",
                            max_age=SESSION_SECONDS, path="/api")
        return {"ok": True}

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @api.delete("/session")
    def end_session(request: Request, response: Response):
        value = request.cookies.get(session_cookie)
        if value:
            with store.transaction() as db:
                db.execute("DELETE FROM panel_state WHERE key=?", (session_key(value),))
        response.delete_cookie(session_cookie, path="/api", httponly=True, samesite="strict")
        return {"ok": True}

    def current():
        try:
            return config.read()
        except (OSError, ValueError):
            raise error(503, "配置文件无法读取或格式无效") from None

    def configuration_result(operation):
        try:
            result = operation()
            activate_logging(result.settings.log_level)
            if live_qq is not None:
                observe_persona(result)
            return {"revision": result.revision, "can_restore": result.can_restore}
        except ConfigConflict:
            raise error(409, "配置已经修改，请重新加载后再保存") from None
        except NoBackup:
            raise error(404, "暂无可恢复的上一次配置") from None
        except (OSError, ValueError):
            raise error(422, "配置无法保存，请检查输入和文件权限") from None

    def state(key: str) -> str | None:
        with closing(store.open_read()) as db:
            row = db.execute("SELECT value FROM panel_state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def platform_view(snapshot, actual=None):
        errors = data.logs()
        platforms = [{
            "id": identifier, "enabled": item.enabled, "receive": item.receive, "send": False,
            "connection": "unavailable", "connected": False, "authorized_send": False,
            "allowlist": [data.redact(value) for value in item.allowlist], "limits": snapshot.settings.limits.model_dump(),
            "recent_error": next((entry for entry in errors if entry.get("platform") == identifier), None),
        } for identifier, item in snapshot.settings.platforms.items()]
        if live_qq:
            if actual is None:
                actual = live_qq.snapshot(snapshot.settings.deepseek.name)
            qq = next(item for item in platforms if item["id"] == "qq")
            qq.update({key: actual[key] for key in (
                "connected", "authorized_send", "allowlist", "napcat", "logged_in",
                "account", "nickname", "webui_url", "proactive",
                "receiving", "record_memory", "storage_error", "receive_errors", "uncertain",
                "last_received_at", "last_recovered_at", "last_error",
                "owner_dm", "owner_record_memory", "owner_checkins", "owner_checkin_at",
            )})
            qq.update(enabled=actual["enabled"], receive=actual["receiving"],
                      send=actual["authorized_send"],
                      connection="connected" if actual["connected"] else "waiting_login")
            if actual["error"]:
                value = actual["error"]
                qq["recent_error"] = {"text": data.redact(reason_label(value) if value.isascii() else value),
                                      "mode": "live"}
        return platforms

    @api.get("/status")
    def status():
        snap = current()
        actual = live_qq.snapshot(snap.settings.deepseek.name) if live_qq else None
        return {
            "service": "running", "mode": "live" if actual and actual["enabled"] else "simulation",
            "database": store.check_health(),
            "revision": snap.revision, "can_restore": snap.can_restore,
            "paused": state("paused") == "1", "simulation_busy": runtime_lock.locked(),
            "deepseek": {"configured": actual["configured"] if actual else bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
                         "verified": actual["verified"] if actual else False,
                         "model": data.redact(snap.settings.deepseek.name), "thinking": "disabled"},
            "platforms": platform_view(snap, actual), "stats": data.stats(), "logs": data.logs(),
        }

    @api.get("/platforms")
    def platforms():
        snap = current()
        return {"revision": snap.revision, "platforms": platform_view(snap)}

    @api.put("/platforms/{platform}")
    def save_platform(platform: Platform, payload: PlatformEdit):
        if live_qq and live_qq.enabled() and platform == "qq":
            raise error(409, "真实 QQ 的授权群由本机联调配置管理")
        if payload.send:
            raise error(422, "阶段 3 未开放真实发送")
        allowlist = list(dict.fromkeys(value.strip() for value in payload.allowlist))
        if not all(CHANNEL.fullmatch(value) and data.redact(value) == value for value in allowlist):
            raise error(422, "白名单编号格式无效")

        def apply(values):
            values["platforms"][platform] = {
                **values["platforms"][platform],
                "enabled": payload.enabled, "receive": payload.receive, "send": False, "allowlist": allowlist,
            }
            return values

        return configuration_result(lambda: config.update(payload.revision, "platforms.toml", apply, "save_platform"))

    @api.put("/platforms/qq/memory")
    def live_qq_memory(payload: LiveQQEdit):
        if not live_qq or not live_qq.enabled():
            raise error(409, "真实 QQ 尚未启用")

        def apply(values):
            values["platforms"]["qq"]["record_memory"] = payload.enabled
            return values

        return configuration_result(lambda: config.update(
            payload.revision, "platforms.toml", apply, "qq_memory_policy",
        ))

    @api.put("/platforms/qq/replies")
    async def live_qq_replies(payload: LiveQQEdit):
        if not live_qq or not live_qq.enabled():
            raise error(409, "真实 QQ 尚未启用")
        snap = current()
        if snap.revision != payload.revision:
            raise error(409, "配置已经修改，请重新加载后再保存")
        try:
            live_qq.set_reply_enabled(payload.enabled, snap.settings.deepseek.name)
        except ValueError as exc:
            raise error(409, str(exc)) from None
        return {"enabled": payload.enabled}

    @api.get("/persona")
    def persona():
        snap = current()
        result = snap.settings.persona.model_dump()
        for key in ("name", "system_prompt", "style"):
            result[key] = data.redact(result[key])
        result["aliases"] = [data.redact(value) for value in result["aliases"]]
        return {"revision": snap.revision, "can_restore": snap.can_restore,
                "persona": result, "emotions": EMOTIONS}

    @api.put("/persona")
    def save_persona(payload: PersonaEdit):
        details = payload.model_dump(exclude={"revision"})
        if not all(details[key].strip() for key in ("name", "system_prompt")):
            raise error(422, "名称和人设提示词不能为空")
        if any(data.redact(details[key]) != details[key] for key in ("name", "style", "system_prompt")):
            raise error(422, "人设字段包含凭据信息，请移除后再保存")
        details["emotions"] = list(dict.fromkeys(details["emotions"]))
        return configuration_result(lambda: config.update(
            payload.revision, "persona.toml", lambda values: {**values, **details}, "save_persona",
        ))

    @api.get("/settings")
    def settings():
        snap = current()
        saved = snap.settings
        return {"revision": snap.revision, "can_restore": snap.can_restore, "model": data.redact(saved.deepseek.name),
                "participation_every": saved.chat.participation_every, "log_level": saved.log_level,
                "limits": saved.limits.model_dump(), "stickers": saved.stickers.model_dump()}

    @api.put("/settings")
    def save_settings(payload: SettingsEdit):
        if data.redact(payload.model) != payload.model:
            raise error(422, "模型名称无效")

        def apply(values):
            values["deepseek"] = {**values.get("deepseek", {}), "name": payload.model}
            values["chat"] = {**values.get("chat", {}), "participation_every": payload.participation_every}
            values["limits"] = payload.limits.model_dump()
            values["stickers"] = payload.stickers.model_dump()
            values["log_level"] = payload.log_level
            return values

        return configuration_result(lambda: config.update(payload.revision, "platforms.toml", apply, "save_settings"))

    @api.post("/settings/restore")
    def restore(payload: Versioned):
        return configuration_result(lambda: config.restore(payload.revision))

    @api.get("/memory")
    def memory(
        platform: Platform | None = None, kind: Literal["group", "dm", "live"] | None = None,
        channel: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    ):
        if platform is None and kind is None and channel is None:
            return {"channels": data.channels()}
        if not all((platform, kind, channel)):
            raise error(422, "请选择完整会话")
        key = (platform, kind, channel)
        if key not in store.known_channels():
            raise error(404, "会话不存在")
        return data.memory(key, current().settings.memory.recent_messages)

    @api.post("/memory/clear")
    async def clear_memory(payload: ClearMemory):
        if payload.confirmation != payload.channel:
            raise error(422, "请输入正确的会话编号以确认清理")
        async with runtime_lock:
            if not data.clear_memory((payload.platform, payload.kind, payload.channel)):
                raise error(404, "会话不存在")
            cognition.clear_channel((payload.platform, payload.kind, payload.channel))
        return {"ok": True}

    @api.get("/stickers")
    def stickers():
        return {"items": data.stickers(), "emotions": EMOTIONS}

    @api.post("/stickers", status_code=201)
    async def upload_sticker(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BYTES:
                raise error(413, "图片不得超过 4 MB")
            body.extend(chunk)
        try:
            identifier = import_image(store, image_dir, body, request.headers.get("content-type", ""))
        except InvalidImage:
            raise error(422, "图片无效。请上传符合大小要求的静态 PNG、JPEG 或 WebP") from None
        return {"id": identifier}

    @api.get("/stickers/{identifier}/image")
    def sticker_image(identifier: str):
        if not IDENTIFIER.fullmatch(identifier):
            raise error(404, "图片不存在")
        path = image_dir / f"{identifier}.png"
        if not path.is_file():
            raise error(404, "图片不存在")
        return FileResponse(path, media_type="image/png")

    @api.put("/stickers/{identifier}")
    def edit_sticker(identifier: str, payload: StickerEdit):
        values = payload.model_dump()
        if not payload.name.strip() or any(
            data.redact(value) != value for value in (payload.name, *payload.keywords, *payload.contexts)
        ):
            raise error(422, "表情包名称或标签无效")
        if not IDENTIFIER.fullmatch(identifier) or not data.edit_sticker(identifier, values):
            raise error(404, "表情包不存在")
        return {"ok": True}

    @api.get("/messages")
    def messages(
        platform: Platform | None = None, state: Status | None = None,
        since: Annotated[float | None, Query(ge=0, le=1e12)] = None,
        until: Annotated[float | None, Query(ge=0, le=1e12)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        if since is not None and until is not None and not since < until:
            raise error(422, "结束时间应晚于开始时间")
        return {"items": data.messages(platform, state, since, until, limit)}

    @api.get("/logs")
    def logs():
        return {"items": data.logs()}

    @api.post("/runtime")
    async def runtime(payload: RuntimeEdit):
        if live_qq and live_qq.enabled():
            raise error(409, "真实 QQ 已启用，请通过本机停止脚本暂停服务")
        if payload.action == "simulate":
            if runtime_lock.locked():
                raise error(409, "另一个本地任务正在运行")
            async with runtime_lock:
                if state("paused") == "1":
                    raise error(409, "模拟任务已暂停，请先恢复")
                snap = current()
                items = [item for item in data.stickers() if item["enabled"]]
                files = {item["id"]: str(image_dir / f"{item['id']}.png") for item in items}
                rules = StickerPolicy(
                    **snap.settings.stickers.model_dump(),
                    catalog=tuple(Sticker(item["id"], tuple(item["emotions"]), tuple(item["keywords"]),
                                          tuple(item["contexts"])) for item in items),
                )
                try:
                    job = await run_demo(store, snap.settings, rules, files)
                except Exception:
                    with store.transaction() as db:
                        store.record_panel_action(db, "simulation_error")
                    log.warning("本地模拟任务异常")
                    raise error(500, "本地模拟任务失败，请查看运行记录") from None
                with store.transaction() as db:
                    store.record_panel_action(db, "simulate")
                return {"mode": "simulation", "state": job["state"], "detail": reason_label(job["detail"])}
        with store.transaction() as db:
            paused = payload.action == "pause"
            db.execute("INSERT INTO panel_state VALUES ('paused',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if paused else "0",))
            store.record_panel_action(db, payload.action)
        return {"mode": "simulation", "paused": paused}

    mount_bilibili(api, browsing)
    mount_douyin(app, api, directory, lambda: current().settings,
                 lambda: live_qq.key() if live_qq else os.environ.get("DEEPSEEK_API_KEY", "").strip(),
                 client=douyin_client, model=douyin_model)
    api.include_router(cognition_routes(store, current, demo_memory, live_qq=live_qq, redactor=data.redactor))
    app.include_router(api)
    return app
