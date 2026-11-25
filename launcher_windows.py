"""Windows 桌面启动器。实际服务单独运行在 WSL 中。"""

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import datetime
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

DISTRO = os.getenv("FISHBOT_WSL_DISTRO", "Ubuntu-24.04")
PROJECT = os.getenv("FISHBOT_WSL_PROJECT", "")
PYTHON = os.getenv("FISHBOT_WSL_PYTHON", "")
HOST = "127.0.0.1"
PORT = 8000
PANEL = f"http://{HOST}:{PORT}/"


class LaunchError(Exception):
    pass


def wsl_executable(path: Path) -> Path:
    if not path.is_file():
        raise LaunchError("找不到 WSL。请检查 Windows 子系统是否已安装。")
    return path


def command(*arguments: str) -> list[str]:
    return ["-d", DISTRO, "--cd", PROJECT, "--exec", PYTHON, f"{PROJECT}/launcher.py", *arguments]


def invoke(wsl: Path, args: list[str], timeout: int = 25) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(wsl), *args], capture_output=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise LaunchError("WSL 无法运行或命令超时，请先在终端检查 Ubuntu。") from None


def distros(wsl: Path, *, running: bool = False) -> set[str]:
    arguments = ["--list", "--quiet"]
    if running:
        arguments.append("--running")
    result = invoke(wsl, arguments)
    if result.returncode and not running:
        raise LaunchError("无法列出 WSL 发行版，请运行 wsl --list --verbose 检查。")
    raw = result.stdout
    encoding = "utf-16-le" if b"\0" in raw else "utf8"
    return {line.strip().lstrip("\ufeff") for line in raw.decode(encoding, errors="replace").splitlines()}


def control(wsl: Path, *arguments: str) -> dict:
    result = invoke(wsl, command(*arguments), timeout=30)
    try:
        reply = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise LaunchError("无法运行 Ubuntu 中的 V2 启动器。请检查 E 盘挂载、项目目录和虚拟环境。") from None
    if reply["state"] == "error":
        raise LaunchError(reply["error"])
    return reply


def healthy() -> bool:
    connection = http.client.HTTPConnection(HOST, PORT, timeout=1.2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        result = json.loads(response.read(1024))
        return isinstance(result, dict) and all(result.get(key) == value for key, value in {
            "service": "fishbot-v2-public", "status": "ok", "database": "ok",
        }.items())
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


@contextmanager
def exclusive(*, wait_ms: int = 90000):
    # Windows 命名互斥只串行化本项目的启动和停止。
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, r"Local\FishbotV2PublicLauncher")
    if not handle:
        raise LaunchError("无法建立大肥鱼 V2 启停锁。")
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, wait_ms) in (0, 0x80)
        if not acquired:
            raise LaunchError("另一个 V2 启停操作还在执行，请稍后重试。")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def write_log(directory: Path, message: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "launcher.log").open("a", encoding="utf8") as log:
        log.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def start(wsl_path: Path, directory: Path, *, browser: bool) -> None:
    if healthy():
        write_log(directory, "复用已通过健康检查的 V2。")
    elif port_open():
        raise LaunchError("Windows 的 8000 端口已有服务，但未通过 V2 健康检查。请查看端口占用或 V2 日志。")
    else:
        wsl = wsl_executable(wsl_path)
        if DISTRO not in distros(wsl):
            raise LaunchError("找不到 Ubuntu-24.04。请检查 wsl --list --verbose。")
        probe = control(wsl, "check")
        if probe["state"] == "conflict":
            raise LaunchError("Ubuntu 的 8000 端口被其他服务占用。请在 Ubuntu 中运行 ss -ltnp 查看。")
        launch_id = None
        process = None
        try:
            if probe["state"] == "stopped":
                launch_id = uuid4().hex
                write_log(directory, "正在后台启动 V2 服务。")
                with (directory / "startup-worker.log").open("wb") as worker_log:
                    process = subprocess.Popen(
                        [str(wsl), *command("serve", launch_id)],
                        stdin=subprocess.DEVNULL, stdout=worker_log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
            deadline = time.monotonic() + 35
            while not healthy():
                if process and process.poll() is not None:
                    raise LaunchError("V2 启动进程已退出。请检查 WSL 服务日志。")
                if time.monotonic() >= deadline:
                    raise LaunchError("V2 健康检查超时。请检查服务日志、SQLite 权限和 WSL 本机端口转发。")
                time.sleep(0.3)
        except (OSError, LaunchError):
            if launch_id:
                try:
                    control(wsl, "abort", launch_id)
                except LaunchError:
                    write_log(directory, "无法确认本次启动已取消，请查看 WSL 服务日志。")
            raise
        write_log(directory, "V2 健康检查通过。")
    if browser:
        os.startfile(PANEL)
        write_log(directory, "已请求默认浏览器打开控制面板。")


def stop(wsl_path: Path, directory: Path) -> None:
    wsl = wsl_executable(wsl_path)
    if DISTRO not in distros(wsl):
        raise LaunchError("找不到 Ubuntu-24.04，无法确认 V2 运行状态。")
    if DISTRO not in distros(wsl, running=True):
        write_log(directory, "Ubuntu 未运行，没有执行停止操作。")
        return
    result = control(wsl, "stop")
    if result["state"] != "stopped" or healthy():
        raise LaunchError("仍检测到 V2 健康响应。请检查是否另有 Windows 调试实例。")
    message = (f"V2 进程 {result['pid']} 已优雅退出。"
               if "pid" in result else "未找到需要停止的 V2 进程。")
    write_log(directory, message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    wsl = Path(os.environ["SystemRoot"]) / "System32/wsl.exe"
    directory = Path(os.environ["LOCALAPPDATA"]) / "FishbotV2Public/launcher"
    global PROJECT, PYTHON
    try:
        wsl_executable(wsl)
        if not PROJECT:
            result = invoke(wsl, ["-d", DISTRO, "--exec", "wslpath", "-a", str(Path(__file__).resolve().parent)])
            if result.returncode != 0 or not result.stdout.strip():
                raise LaunchError("无法转换工作区路径，请设置 FISHBOT_WSL_PROJECT。")
            PROJECT = result.stdout.decode("utf8").strip()
        if not PYTHON:
            result = invoke(wsl, ["-d", DISTRO, "--exec", "printenv", "HOME"])
            if result.returncode != 0 or not result.stdout.strip():
                raise LaunchError("无法读取 WSL 主目录，请设置 FISHBOT_WSL_PYTHON。")
            PYTHON = result.stdout.decode("utf8").strip() + "/.local/share/fishbot-v2-public/venv/bin/python"
        with exclusive():
            if args.action == "start":
                start(wsl, directory, browser=not args.no_browser)
            else:
                stop(wsl, directory)
    except (LaunchError, OSError) as exc:
        reason = str(exc) if isinstance(exc, LaunchError) else "无法运行启动器或默认浏览器，请检查 Windows 配置。"
        try:
            write_log(directory, reason)
        except OSError:
            pass
        details = (f"{reason}\n\nWindows 日志：{directory / 'launcher.log'}"
                   "\nWSL 服务日志：~/.local/share/fishbot-v2-public/launcher/server.log")
        import sys

        print(details, file=sys.stderr)
        if not args.quiet:
            ctypes.windll.user32.MessageBoxW(None, details, "大肥鱼 V2", 0x30)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
