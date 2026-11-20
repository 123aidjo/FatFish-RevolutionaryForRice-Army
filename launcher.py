"""WSL 中的 V2 服务启动与只针对 V2 的优雅停止。"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
import tomllib
from typing import NamedTuple

PROJECT = Path(__file__).resolve().parent
VENV_PYTHON = Path(sys.executable)
RUN_DIR = Path.home() / ".local/share/fishbot-v2-public/launcher"
RECORD = RUN_DIR / "service.json"
TOKEN_FORMAT = re.compile(r"[0-9a-f]{32}")
PORT = 8000


class LaunchError(Exception):
    pass


class Process(NamedTuple):
    pid: int
    start: str
    kind: str


def listener_pids() -> tuple[bool, set[int]]:
    output = subprocess.check_output(
        ["ss", "-H", "-ltnp", "sport", "=", f":{PORT}"], text=True, timeout=5,
    )
    return bool(output.strip()), {int(pid) for pid in re.findall(r"pid=(\d+)", output)}


def identity(pid: int) -> Process | None:
    proc = Path("/proc") / str(pid)
    try:
        args = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        executable = (proc / "exe").resolve(strict=True)
        cwd = (proc / "cwd").resolve(strict=True)
        start = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
        if proc.stat().st_uid != os.getuid() or cwd != PROJECT or executable != VENV_PYTHON.resolve(strict=True):
            return None
        if not args or Path(os.fsdecode(args[0])) != VENV_PYTHON:
            return None
        if len(args) == 2 and (cwd / os.fsdecode(args[1])).resolve() == PROJECT / "main.py":
            return Process(pid, start, "server")
        if (len(args) == 4 and (cwd / os.fsdecode(args[1])).resolve() == PROJECT / "launcher.py"
                and args[2] == b"serve" and TOKEN_FORMAT.fullmatch(os.fsdecode(args[3]))):
            return Process(pid, start, "starting")
    except (OSError, ValueError, IndexError):
        pass
    return None


def record_for(launch_id: str | None) -> Process | None:
    try:
        record = json.loads(RECORD.read_text(encoding="utf8"))
        if launch_id is not None and record["launch_id"] != launch_id:
            return None
        if record["boot"] != boot_id():
            return None
        proc = identity(record["pid"])
        if proc and proc.start == record["start"]:
            return proc
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


@contextmanager
def locked(name: str, *, blocking: bool = True):
    import fcntl

    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUN_DIR / name).open("a+b") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle, flags)
        yield handle


def preflight() -> None:
    required = ("main.py", "settings.py", "platforms.toml", "persona.toml", "panel.py",
                "index.html", "style.css", "app.js")
    if Path.cwd() != PROJECT or any(not (PROJECT / name).is_file() for name in required):
        raise LaunchError("V2 项目目录或必要文件缺失。")
    if Path(sys.executable) != VENV_PYTHON:
        raise LaunchError("V2 虚拟环境解释器路径不匹配。")
    try:
        with (PROJECT / "platforms.toml").open("rb") as source:
            port = tomllib.load(source).get("server", {}).get("port", PORT)
    except (OSError, ValueError):
        raise LaunchError("平台配置文件无法读取或格式无效。") from None
    if type(port) is not int or port != PORT:
        raise LaunchError("桌面启动使用 8000 端口，请检查 platforms.toml 的 server.port。")


def status() -> dict:
    bound, pids = listener_pids()
    if bound:
        processes = [identity(pid) for pid in pids]
        if len(processes) == 1 and processes[0] and processes[0].kind == "server":
            return {"state": "running", "pid": processes[0].pid}
        return {"state": "conflict"}
    proc = record_for(None)
    if proc:
        return {"state": "starting", "pid": proc.pid}
    return {"state": "stopped"}


def serve(launch_id: str) -> None:
    preflight()
    with locked("service.lock", blocking=False) as service_lock:
        with locked("control.lock"):
            if (RUN_DIR / ("cancel-" + launch_id)).exists():
                return
            if listener_pids()[0]:
                raise LaunchError("WSL 的 8000 端口已被占用。")
            proc = identity(os.getpid())
            if proc is None:
                raise LaunchError("无法确认启动进程身份。")
            temporary = RUN_DIR / ("record-" + launch_id)
            temporary.write_text(json.dumps({
                "pid": proc.pid, "start": proc.start, "boot": boot_id(), "launch_id": launch_id,
            }), encoding="utf8")
            os.replace(temporary, RECORD)
            path = RUN_DIR / "server.log"
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                os.replace(path, RUN_DIR / "server.previous.log")
            with path.open("ab", buffering=0) as log:
                log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] V2 启动，PID {proc.pid}\n".encode("utf8"))
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
        # 保持同一 PID，锁会随服务退出释放。Windows 的隐藏 WSL 进程等待这个服务结束。
        os.set_inheritable(service_lock.fileno(), True)
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(PROJECT / "main.py")])


def target(launch_id: str | None) -> Process | None:
    managed = record_for(launch_id)
    if managed or launch_id is not None:
        return managed
    bound, pids = listener_pids()
    if not bound:
        return None
    if len(pids) != 1:
        raise LaunchError("无法唯一确认 8000 端口进程属于 V2，未终止进程。")
    candidate = identity(next(iter(pids)))
    if candidate is None or candidate.kind != "server":
        raise LaunchError("8000 端口进程不属于可确认的 V2，未终止进程。")
    return candidate


def stop(launch_id: str | None = None) -> dict:
    with locked("control.lock"):
        if launch_id is not None:
            # 启动超时后取消本次请求，即使后台启动进程稍晚才进入 WSL。
            (RUN_DIR / ("cancel-" + launch_id)).touch(mode=0o600)
        proc = target(launch_id)
        if proc is None:
            return {"state": "stopped"}
        try:
            fd = os.pidfd_open(proc.pid)
        except ProcessLookupError:
            return {"state": "stopped"}
        try:
            current = identity(proc.pid)
            if not current or current.start != proc.start:
                raise LaunchError("服务进程身份已变化，未终止进程。")
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(20000):
                raise LaunchError("V2 尚未完成优雅退出，请稍后检查日志。")
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)
        if record_for(None) is None:
            RECORD.unlink(missing_ok=True)
        if status()["state"] == "running":
            raise LaunchError("仍检测到 V2 监听，请检查运行日志。")
        return {"state": "stopped", "pid": proc.pid}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("action", choices=("check", "serve", "stop", "abort"))
    parser.add_argument("launch_id", nargs="?")
    args = parser.parse_args()
    try:
        if args.action in {"serve", "abort"} and not TOKEN_FORMAT.fullmatch(args.launch_id or ""):
            raise LaunchError("启动编号无效。")
        if args.action == "serve":
            serve(args.launch_id)
            result = {"state": "stopped"}
        elif args.action == "check":
            preflight()
            result = status()
        else:
            result = stop(args.launch_id if args.action == "abort" else None)
    except (LaunchError, OSError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, LaunchError) else "WSL 进程管理失败，请检查 V2 目录与权限。"
        result = {"state": "error", "error": reason}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    if result["state"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
