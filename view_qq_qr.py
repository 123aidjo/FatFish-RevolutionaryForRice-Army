"""只在本机显示 V2 独立 NapCat 的 QQ 扫码图，二维码更新时自动刷新。"""

import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import os
import subprocess

from PIL import Image
from napcat_login import NapCatLogin


WEBUI_CONFIG = os.getenv("FISHBOT_NAPCAT_WEBUI_CONFIG", "")
DISTRO = os.getenv("FISHBOT_WSL_DISTRO", "Ubuntu-24.04")
CONTAINER = os.getenv("FISHBOT_NAPCAT_CONTAINER", "fishbot-napcat")


def show():
    import tkinter as tk

    if not WEBUI_CONFIG:
        raise SystemExit('Set FISHBOT_NAPCAT_WEBUI_CONFIG or use the NapCat WebUI.')

    root = tk.Tk()
    root.title("大肥鱼 V2 · 独立 QQ 扫码")
    root.geometry("420x455")
    label = tk.Label(root, text="用手机 QQ 扫码并确认登录鲸小鱼", font=("Microsoft YaHei UI", 14))
    label.pack(pady=(20, 8))
    picture = tk.Label(root)
    picture.pack()
    status = tk.Label(root, text="正在读取 V2 二维码…", wraplength=390)
    status.pack(pady=8)
    wsl = Path(os.environ["SystemRoot"]) / "System32/wsl.exe"
    login = NapCatLogin(Path(WEBUI_CONFIG))
    worker = ThreadPoolExecutor(max_workers=1)
    pending = None
    previous = b""
    awaiting_confirmation = False
    interrupted = False

    def read_status():
        state = login.login_state()
        phase = login.phase(state)
        blocker = login.login_blocker(state)
        if phase != "waiting_qrcode":
            return phase, b"", blocker
        result = subprocess.run([
            str(wsl), "-d", DISTRO, "--exec", "docker", "-H",
            "unix:///var/run/docker.sock", "exec", CONTAINER,
            "sh", "-c", 'stat -c %Y /app/napcat/cache/qrcode.png && cat /app/napcat/cache/qrcode.png',
        ], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=3)
        stamp, _, png = result.stdout.partition(b"\n") if result.returncode == 0 else (b"", b"", b"")
        phase, discard = login.refresh_expired(state, int(stamp) if stamp.isdigit() else None)
        return phase, b"" if discard else png, blocker

    def refresh():
        nonlocal previous, pending, awaiting_confirmation, interrupted
        if pending is None:
            pending = worker.submit(read_status)
        if not pending.done():
            root.after(200, refresh)
            return
        try:
            phase, png, blocker = pending.result()
            if phase in {"qrcode_scanned", "initializing"}:
                awaiting_confirmation = True
                interrupted = False
            elif phase in {"reconnecting", "waiting_qrcode"} and awaiting_confirmation:
                awaiting_confirmation = False
                interrupted = True
            messages = {
                "qrcode_scanned": "V2 已收到扫码，请在手机 QQ 上确认登录。",
                "initializing": "登录已获确认，正在等待 QQ 和 OneBot 就绪…",
                "reconnecting": "QQ 登录连接已断开，正在重连…",
                "refreshing": "二维码已过期，正在自动换码…",
                "online": "QQ 已在线，V2 正在核对账号。",
            }
            if phase == "waiting_qrcode" and png and png != previous:
                with Image.open(BytesIO(png)) as source:
                    width = source.width
                    qr = source.resize((width * (320 // width or 1),) * 2, Image.Resampling.NEAREST)
                    canvas = Image.new("RGB", (qr.width + 24, qr.height + 24), "white")
                    canvas.paste(qr, (12, 12))
                    data = BytesIO()
                    canvas.save(data, format="PNG")
                photo = tk.PhotoImage(data=base64.b64encode(data.getvalue()))
                picture.configure(image=photo)
                picture.image = photo
                previous = png
            if phase != "waiting_qrcode" or not png:
                picture.configure(image="")
                previous = b""
            waiting = "上次扫码后登录中断。请使用当前二维码。" if interrupted else "扫码后请在手机上确认。"
            status.configure(text=blocker or messages.get(phase, waiting if png else "正在等待 V2 二维码…"))
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            picture.configure(image="")
            previous = b""
            status.configure(text="暂时无法读取 V2 登录状态，正在重试…")
        pending = None
        root.after(1000, refresh)

    root.after(5, refresh)
    try:
        root.mainloop()
    finally:
        worker.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    show()
