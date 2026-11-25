import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.request import urlopen

from PIL import Image
import pytest

import launcher_windows as launcher
from draw_fish import SIZES
from settings import PROJECT_ROOT

WINDOWS = sys.platform == "win32"
WINDOWS_ONLY = pytest.mark.skipif(not WINDOWS, reason="需要真实 Windows 桌面和 PowerShell")
POWERSHELL = "powershell.exe"


def test_original_icon_contains_every_size_and_transparency():
    with Image.open(PROJECT_ROOT / "fish.ico") as icon:
        assert icon.ico.sizes() == {(side, side) for side in SIZES}
        for side in SIZES:
            layer = icon.ico.getimage((side, side)).convert("RGBA")
            assert layer.getpixel((0, 0))[3] == 0
            assert layer.getchannel("A").getbbox()
    with Image.open(PROJECT_ROOT / "fish-preview.png") as preview:
        assert preview.mode == "RGBA" and preview.size == (256, 256)
        assert preview.getpixel((0, 0))[3] == 0


def test_missing_wsl_shows_chinese_error(tmp_path):
    with pytest.raises(launcher.LaunchError, match="找不到 WSL"):
        launcher.wsl_executable(tmp_path / "wsl.exe")


def test_fixed_launch_arguments_have_no_live_or_secret_switches(monkeypatch):
    monkeypatch.setattr(launcher, "PROJECT", "/tmp/fishbot-src")
    assert launcher.PANEL == "http://127.0.0.1:8000/"
    assert launcher.command("serve", "0" * 32)[-3:] == [
        "/tmp/fishbot-src/launcher.py", "serve", "0" * 32,
    ]


@WINDOWS_ONLY
@pytest.mark.skipif(os.environ.get("FISHBOT_VERIFY_DESKTOP") != "1", reason="需要本仓库创建的桌面快捷方式")
def test_windows_shortcut_points_to_v2():
    script = r"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$path = Join-Path $desktop ('{0}{1}{2} V2 {3}{4}{5}.lnk' -f [char]22823,[char]32933,[char]40060,[char]24320,[char]28304,[char]29256)
if (-not (Test-Path -LiteralPath $path)) { throw 'Expected V2 shortcut is missing' }
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
@{target=$link.TargetPath; icon=$link.IconLocation; arguments=$link.Arguments;
  directory=$link.WorkingDirectory} | ConvertTo-Json -Compress
"""
    result = subprocess.check_output([POWERSHELL, "-NoProfile", "-Command", script], text=True)
    shortcut = json.loads(result)
    assert Path(shortcut["target"]).name.lower() == "powershell.exe"
    assert Path(shortcut["directory"]) == PROJECT_ROOT
    assert shortcut["icon"] == f"{PROJECT_ROOT / 'fish.ico'},0"
    assert shortcut["arguments"].endswith(f'-File "{PROJECT_ROOT / "start.ps1"}"')
    assert "token" not in shortcut["arguments"].lower()
    assert "http" not in shortcut["arguments"].lower()


@WINDOWS_ONLY
@pytest.mark.skipif(os.environ.get("FISHBOT_VERIFY_DESKTOP") != "1",
                    reason="主动启停实际 V2 的集成测试，需显式启用")
def test_windows_lifecycle_real_wsl_and_unrelated_port():
    wsl = launcher.wsl_executable(Path(os.environ["SystemRoot"]) / "System32/wsl.exe")

    def ps(name, *options):
        environment = dict(os.environ, FISHBOT_USE_WSL="1")
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
                               "-File", str(PROJECT_ROOT / name), "-Quiet", *options],
                              capture_output=True, timeout=100, env=environment)

    def checked(name, *options):
        result = ps(name, *options)
        assert result.returncode == 0, result.stderr.decode("utf8", errors="replace")

    def pid():
        state = launcher.control(wsl, "check")
        assert state["state"] == "running"
        return state["pid"]

    try:
        checked("stop.ps1")
        assert launcher.control(wsl, "check")["state"] == "stopped"
        with socket.socket() as unrelated:
            unrelated.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            unrelated.bind((launcher.HOST, launcher.PORT))
            unrelated.listen()
            assert ps("start.ps1", "-NoBrowser").returncode == 1
            assert ps("stop.ps1").returncode == 0
            assert launcher.port_open()
            assert launcher.control(wsl, "check")["state"] == "stopped"
        checked("start.ps1", "-NoBrowser")
        first = pid()
        assert launcher.healthy()
        with urlopen(launcher.PANEL, timeout=5) as response:
            assert "大肥鱼 V2".encode("utf8") in response.read()
        checked("start.ps1", "-NoBrowser")
        assert pid() == first
        checked("stop.ps1")
        assert launcher.control(wsl, "check")["state"] == "stopped"
        assert not launcher.healthy()
        checked("start.ps1", "-NoBrowser")
        assert pid() != first
        assert launcher.healthy()
    finally:
        if not launcher.healthy() and not launcher.port_open():
            checked("start.ps1", "-NoBrowser")
