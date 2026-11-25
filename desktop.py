"""Windows desktop entry: local service, settings and NapCat login without a terminal."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import tomllib
from urllib.parse import urlsplit

from local_runtime import data_directory, read_config, save_config, validate

PROJECT = Path(__file__).resolve().parent
NAPCAT_GUIDE = "https://napneko.icu/guide/boot/Shell"


def panel_port():
    with (PROJECT / "platforms.toml").open("rb") as stream:
        port = tomllib.load(stream).get("server", {}).get("port", 8000)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("platforms.toml 中的 server.port 无效。")
    return port


def webui_ready(url):
    parsed = urlsplit(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
    try:
        conn.request("GET", parsed.path or "/webui/")
        response = conn.getresponse()
        response.read(1024)
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def discover_napcat(roots=None):
    """Bounded, read-only discovery. Never print or import credentials."""
    if roots is None:
        roots = [PROJECT.parent, Path.home() / "Downloads", Path.home() / "Desktop"]
        if os.getenv("APPDATA"):
            manager = Path(os.environ["APPDATA"]) / "MaiBotOneKeyDesktop"
            if manager.is_dir():
                roots = list(manager.glob("*/modules/napcat")) + roots
        for folder in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.getenv(folder):
                base = Path(os.environ[folder])
                roots.extend([base / "NapCat", base / "NapCatQQ", base / "Tencent" / "QQ"])
    found = {"launchers": [], "configs": []}
    visited = 0
    for root in roots:
        if not root.is_dir():
            continue
        for directory, children, files in os.walk(root, followlinks=False):
            visited += 1
            depth = len(Path(directory).relative_to(root).parts)
            children[:] = [name for name in children if not name.startswith(".")
                           and name.lower() not in {"node_modules", "cache", "logs", "venv", "__pycache__"}]
            if depth >= 5 or visited > 1200:
                children[:] = []
            for filename in files:
                path = Path(directory) / filename
                if filename.lower() == "webui.json":
                    found["configs"].append(str(path))
                if filename.lower() in {"napcat.bat", "launcher.bat", "napcatwinbootmain.exe"}:
                    if "napcat" in str(path).lower():
                        found["launchers"].append(str(path))
            if visited > 1200:
                break
    return found


def import_napcat_config(path: Path, values: dict) -> dict:
    """Read existing credentials into the local form; never edit NapCat files."""
    result = dict(values)
    try:
        webui = json.loads(path.read_text(encoding="utf-8-sig"))
        port = int(webui["port"])
        if not 1 <= port <= 65535:
            raise ValueError
        result.update(webui_config=str(path.resolve()), webui_url=f"http://127.0.0.1:{port}/webui/")
        account = result.get("qq_bot_id", "")
        configs = list(path.parent.glob(f"onebot11_{account}.json")) if account else list(path.parent.glob("onebot11_*.json"))
        if not configs and (path.parent / "onebot11.json").is_file():
            configs = [path.parent / "onebot11.json"]
        if len(configs) == 1:
            match = re.fullmatch(r"onebot11_([1-9][0-9]{4,11})", configs[0].stem)
            if match and not account:
                result["qq_bot_id"] = match[1]
            config = json.loads(configs[0].read_text(encoding="utf-8-sig"))
            servers = [item for item in config.get("network", {}).get("websocketServers", []) if item.get("enable")]
            if len(servers) == 1:
                result["onebot_url"] = f"ws://127.0.0.1:{int(servers[0]['port'])}"
                result["onebot_token"] = servers[0].get("token") or servers[0].get("accessToken") or ""
        if not result.get("napcat_launcher"):
            # Prefer the installation's normal entry, never a kill/quick-login script.
            for base in (path.parent.parent.parent, path.parent.parent):
                for name in ("napcat.bat", "launcher.bat"):
                    launcher = base / name
                    if launcher.is_file():
                        result["napcat_launcher"] = str(launcher)
                        break
                if result.get("napcat_launcher"):
                    break
        return validate(result)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("无法读取 NapCat 配置，请选择有效的 webui.json。多个账号或多个 WS 服务需手动填写对应地址和 Token。") from None


class Service:
    def __init__(self):
        self.process = None
        self.port = panel_port()
        self.log = data_directory() / "launcher" / "native-server.log"

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running():
            return "服务已运行。"
        self.port = panel_port()
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                raise ValueError(f"端口 {self.port} 已被使用。请先停止原服务，或修改 platforms.toml 的端口。未终止任何现有进程。")
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("ab") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(PROJECT / "desktop_worker.py")], cwd=PROJECT,
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not self.running():
                    raise ValueError("服务启动失败，请点击“查看服务日志”。")
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                try:
                    conn.request("GET", "/health")
                    response = conn.getresponse()
                    payload = json.loads(response.read(2048))
                    if response.status == 200 and payload.get("service") == "fishbot-v2-public" and payload.get("database") == "ok":
                        return "服务已启动。首次进入面板请复制访问令牌，QQ 登录请按下方步骤操作。"
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                finally:
                    conn.close()
                time.sleep(.2)
            raise ValueError("服务启动超时，请查看日志。")
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self.process is not None:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                raise ValueError("服务仍在退出中，请稍后重试。未强制结束进程。") from None
            self.process = None
        return "本窗口启动的服务已停止；NapCat 保持原状。"


def show():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    root.title("大肥鱼 V2 · 启动与 QQ 登录")
    root.geometry(f"{min(1100, root.winfo_screenwidth() - 80)}x{min(1000, root.winfo_screenheight() - 100)}")
    root.minsize(700, 500)
    root.report_callback_exception = lambda *_: messagebox.showerror(
        "操作失败", "无法完成此操作，请检查文件是否存在以及系统默认浏览器/文件关联。")
    service = Service()
    pool = ThreadPoolExecutor(max_workers=1)
    pending = None
    callback = None
    controls = []
    status = tk.StringVar(value="填写本机配置后保存，点击启动服务。已有 NapCat 可直接复用。")
    try:
        config = read_config()
    except (OSError, ValueError) as exc:
        messagebox.showerror("读取配置失败", str(exc))
        root.destroy()
        return
    values = {key: tk.StringVar(value=value) for key, value in config.items()}
    canvas = tk.Canvas(root, highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    frame = ttk.Frame(canvas, padding=16)
    content = canvas.create_window((0, 0), window=frame, anchor="nw")
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content, width=event.width))
    frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
    root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
    ttk.Label(frame, text="大肥鱼 V2", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
    ttk.Label(frame, textvariable=status, wraplength=840).pack(anchor="w", pady=10)

    def poll():
        nonlocal pending, callback
        if pending is not None and pending.done():
            task, done = pending, callback
            pending = callback = None
            for button in controls:
                button.configure(state="normal")
            try:
                result = task.result()
                if done:
                    done(result)
                elif isinstance(result, str):
                    status.set(result)
            except Exception as exc:
                # Do not expose raw subprocess output or network response bodies.
                text = str(exc) if isinstance(exc, ValueError) else "操作失败，请检查配置、端口和服务日志。"
                status.set(text)
                messagebox.showerror("操作未完成", text)
        try:
            if root.winfo_exists():
                root.after(150, poll)
        except tk.TclError:
            pass

    def run(task, done=None):
        nonlocal pending, callback
        if pending is not None:
            return
        status.set("正在处理，请稍候…")
        for button in controls:
            button.configure(state="disabled")
        callback = done
        pending = pool.submit(task)

    def button(parent, text, command):
        control = ttk.Button(parent, text=text, command=command)
        control.pack(side="left", padx=3, pady=5)
        controls.append(control)
        return control

    def save():
        if service.running():
            raise ValueError("请先停止本窗口的服务，再保存配置并重新启动。")
        save_config({key: value.get() for key, value in values.items()})
        status.set("已保存到本机用户数据目录，不会写入项目。")

    def safe_save():
        try:
            save()
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", str(exc) if isinstance(exc, ValueError) else "本地目录无法写入。")

    def start():
        try:
            if not service.running():
                save()
            run(service.start)
        except (OSError, ValueError) as exc:
            messagebox.showerror("启动失败", str(exc) if isinstance(exc, ValueError) else "无法保存本机配置。")

    def open_panel():
        if not service.running():
            messagebox.showinfo("服务未启动", "先点击“启动服务”。")
            return
        os.startfile(service.url)

    def copy_secret(path=None, field=None):
        try:
            secret = (json.loads(Path(path).read_text(encoding="utf-8-sig"))[field] if field
                      else (data_directory() / "panel.token").read_text(encoding="ascii").strip())
            if not isinstance(secret, str) or not secret:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            messagebox.showerror("无法复制", "请先启动相应服务，并确认配置文件有效。")
            return
        root.clipboard_clear()
        root.clipboard_append(secret)
        status.set("已复制到剪贴板，请粘贴到对应登录框。60 秒后将清理未被替换的剪贴板内容。")
        def clear():
            try:
                if root.clipboard_get() == secret:
                    root.clipboard_clear()
            except tk.TclError:
                pass
        root.after(60000, clear)

    actions = ttk.Frame(frame)
    actions.pack(fill="x")
    button(actions, "启动服务", start)
    button(actions, "打开控制面板", open_panel)
    button(actions, "复制面板访问令牌", copy_secret)
    button(actions, "停止服务", lambda: run(service.stop))
    button(actions, "查看服务日志", lambda: os.startfile(str(service.log)) if service.log.exists()
           else messagebox.showinfo("日志", "尚未生成服务日志。"))

    form = ttk.LabelFrame(frame, text="本机 QQ 接入配置", padding=10)
    form.pack(fill="x", pady=10)
    form.columnconfigure(1, weight=1)
    labels = {"qq_bot_id": "机器人 QQ 号", "qq_group_id": "允许接收的群号",
              "onebot_url": "OneBot 正向 WS 地址", "onebot_token": "OneBot Token",
              "webui_url": "NapCat WebUI 地址", "webui_config": "NapCat webui.json 文件",
              "napcat_launcher": "NapCat 启动文件（可选）", "model_key": "DeepSeek API Key（启用回复时需要）"}
    for index, (key, label) in enumerate(labels.items()):
        ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", pady=4)
        entry = ttk.Entry(form, textvariable=values[key], show="*" if key in {"model_key", "onebot_token"} else "")
        entry.grid(row=index, column=1, sticky="ew", padx=8)
        if key in {"webui_config", "napcat_launcher"}:
            def choose(name=key):
                selected = filedialog.askopenfilename(title="选择已有 NapCat 文件", filetypes=[
                    ("配置文件", "*.json"), ("启动文件", "*.bat *.exe"), ("所有文件", "*.*")])
                if selected:
                    values[name].set(selected)
            ttk.Button(form, text="选择…", command=choose).grid(row=index, column=2)

    def discovered(result):
        # Never silently choose between several installations or overwrite user entries.
        for kind, key in (("launchers", "napcat_launcher"), ("configs", "webui_config")):
            if len(result[kind]) == 1 and not values[key].get():
                values[key].set(result[kind][0])
        count = len(result["launchers"]) + len(result["configs"])
        messagebox.showinfo("检测结果", ("找到以下候选，请核对并保存：\n" + "\n".join(result["launchers"] + result["configs"]))
                            if count else "常见目录中未找到 NapCat。若安装在其他位置，请用“选择…”指定文件；也可查看官方安装说明。")
        status.set("检测完成；多个候选请手动选择，现有 NapCat 文件未改动。")

    config_actions = ttk.Frame(frame)
    config_actions.pack(fill="x")
    button(config_actions, "检测已有 NapCat", lambda: run(discover_napcat, discovered))
    def import_existing():
        try:
            imported = import_napcat_config(Path(values["webui_config"].get()),
                                           {key: value.get() for key, value in values.items()})
            for key, value in imported.items():
                values[key].set(value)
            status.set("已读取现有 NapCat 配置，请核对账号、补充授权群后保存。没有修改 NapCat 文件。")
        except ValueError as exc:
            messagebox.showerror("读取失败", str(exc))
    button(config_actions, "读取已有 NapCat 配置", import_existing)
    button(config_actions, "保存本机配置", safe_save)
    button(config_actions, "官方安装说明", lambda: os.startfile(NAPCAT_GUIDE))

    guide = ttk.LabelFrame(frame, text="QQ 登录步骤", padding=10)
    guide.pack(fill="x", pady=8)
    ttk.Label(guide, wraplength=820, justify="left", text=(
        "1. 保存配置。点击“打开 QQ 扫码登录”：复用正在运行的 NapCat，或启动你选择的文件。\n"
        "2. 在 NapCat WebUI 登录管理后台，进入 QQ 登录并用手机扫码确认；需要时复制 WebUI 密码。\n"
        "3. 在 NapCat 网络配置中新建并启用“WebSocket 服务端（正向 WS）”，地址与 Token 与上方一致，消息格式选 array。\n"
        "4. 启动大肥鱼服务后检查连接。确认账号、群和模型配置正确，再点击“验证模型并启用回复”。\n"
        "扫码本身不会开启新的自动回复授权；以前已启用的授权会保留。设置只在重启服务后生效。"
    )).pack(anchor="w")

    def login_qq():
        try:
            current = validate({key: value.get() for key, value in values.items()})
            launcher = current["napcat_launcher"]
            if launcher and (not Path(launcher).is_file() or Path(launcher).suffix.lower() not in {".bat", ".exe"}):
                raise ValueError("请选择已有的 NapCat .bat 或 .exe 启动文件。")
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        def open_login():
            url = current["webui_url"]
            if not webui_ready(url):
                if not launcher:
                    raise ValueError("NapCat 尚未运行。请先选择已有启动文件，或通过官方安装说明安装 NapCat。")
                os.startfile(launcher, cwd=str(Path(launcher).parent))
                deadline = time.monotonic() + 45
                while not webui_ready(url):
                    if time.monotonic() >= deadline:
                        raise ValueError("NapCat 未在指定地址就绪，请核对它的启动窗口和 WebUI 端口；不会重复启动。")
                    time.sleep(1)
            return url
        run(open_login, lambda url: (os.startfile(url), status.set("已打开 NapCat QQ 登录页。扫码后请在手机确认。")))

    def verify():
        if not service.running():
            messagebox.showinfo("服务未启动", "请保存配置并启动服务，再验证连接。")
            return
        if not messagebox.askyesno("启用 QQ 自动回复", "将核验机器人账号及授权群，并调用一次 DeepSeek（产生少量用量）。成功后允许对新消息自动回复。继续？"):
            return
        def task():
            result = subprocess.run([sys.executable, "-X", "utf8", "live_qq.py", "verify"], cwd=PROJECT,
                                    capture_output=True, timeout=90,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ValueError("验证未通过。请检查 QQ 已登录、机器人在授权群、OneBot 地址及 Token、模型密钥和余额。未授予新的回复权限。")
            return "账号、群和模型验证通过，QQ 自动回复已启用。请在控制面板查看接收和回复状态。"
        run(task)

    qq_actions = ttk.Frame(frame)
    qq_actions.pack(fill="x")
    button(qq_actions, "打开 QQ 扫码登录", login_qq)
    button(qq_actions, "复制 WebUI 密码", lambda: copy_secret(values["webui_config"].get(), "token"))
    button(qq_actions, "验证模型并启用回复", verify)
    button(qq_actions, "查看 QQ 状态", lambda: os.startfile(service.url + "#platforms") if service.running()
           else messagebox.showinfo("服务未启动", "请先启动服务。"))
    ttk.Label(frame, text="关闭此窗口会停止它启动的大肥鱼服务；不会退出 QQ 或停止 NapCat。", wraplength=820).pack(anchor="w", pady=8)

    def close():
        if pending is not None:
            messagebox.showinfo("正在处理", "请等待当前操作结束后再关闭。")
            return
        if service.running() and not messagebox.askyesno("退出大肥鱼", "关闭窗口并停止本窗口启动的服务？"):
            return
        run(service.stop, lambda _: (pool.shutdown(wait=False), root.destroy()))

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(150, poll)
    root.mainloop()


if __name__ == "__main__":
    try:
        from launcher_windows import exclusive
        with exclusive(wait_ms=0):
            show()
    except Exception as exc:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "启动窗口无法打开，请检查 Python 环境、配置文件或是否已有启动窗口。", "大肥鱼 V2", 0x30)
        raise SystemExit(1) from None
