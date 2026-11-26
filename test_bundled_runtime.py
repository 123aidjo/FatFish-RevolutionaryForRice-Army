import json
from pathlib import Path
import subprocess
import zipfile

import pytest

import bundled_runtime
import package_runtime


def source_at(root):
    for name in package_runtime.ROOT_FILES | {"napcat/napcat.mjs", "napcat/static/index.html"}:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    for name in ("napcat/config/webui.json", "napcat/cache/qr.png", "napcat/logs/private.log", "guild1.db"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-test-value")


def test_install_excludes_private_data_and_never_overwrites(tmp_path):
    source = tmp_path / "original"
    project = tmp_path / "project"
    source_at(source)
    target = package_runtime.install(source, project)
    assert not (target / "napcat/config").exists()
    assert not (target / "guild1.db").exists()
    assert all(b"private-test-value" not in p.read_bytes() for p in target.rglob("*") if p.is_file())
    assert (source / "napcat/config/webui.json").read_text() == "private-test-value"
    with pytest.raises(ValueError):
        package_runtime.install(source, project)


def test_bundle_has_programs_but_not_generated_tokens(tmp_path, monkeypatch):
    source_at(tmp_path / "original")
    project = tmp_path / "project"
    package_runtime.install(tmp_path / "original", project)
    initial = bundled_runtime.settings(project)
    assert initial == bundled_runtime.settings(project)
    (project / "start.bat").write_text("test")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: b"start.bat\0")
    archive = package_runtime.bundle(project)
    with zipfile.ZipFile(archive) as contents:
        assert "Fatfish/start.bat" in contents.namelist()
        assert "Fatfish/runtime/napcat/node.exe" in contents.namelist()
        assert not any("/config/" in name for name in contents.namelist())
        assert all(initial["onebot_token"].encode() not in contents.read(name) for name in contents.namelist())
    with pytest.raises(ValueError):
        package_runtime.bundle(project)
