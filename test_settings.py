from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from settings import PROJECT_ROOT, load_settings


def copy_config(destination: Path) -> Path:
    for filename in ("platforms.toml", "persona.toml"):
        shutil.copyfile(PROJECT_ROOT / filename, destination / filename)
    return destination / "platforms.toml"


def test_default_configuration_is_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 阶段 1 只加载 TOML。即使工作目录有私有凭据文件，也无需访问。
    settings = load_settings()
    assert settings.server.port == 8000
    assert settings.deepseek.name == "deepseek-flash"
    assert settings.deepseek.thinking == "disabled"
    assert settings.persona.name == "鲸小鱼"
    assert settings.persona.aliases == ["大肥鱼", "小鱼"]
    assert settings.persona.max_chars == 0
    assert set(settings.platforms) == {"qq", "bilibili", "douyin"}
    for platform in settings.platforms.values():
        assert (platform.enabled, platform.receive, platform.send, platform.allowlist) == (
            False, False, False, []
        )
    assert settings.database_path.name == "fishbot.db"
    assert settings.database_path.parent != PROJECT_ROOT


def test_can_configure_model_and_local_port(tmp_path):
    config = copy_config(tmp_path)
    contents = config.read_text(encoding="utf8")
    config.write_text(contents.replace("8000", "8765").replace("deepseek-flash", "deepseek-v4-pro"),
                      encoding="utf8")
    settings = load_settings(tmp_path, database_path=tmp_path / "fishbot.db")
    assert settings.server.port == 8765
    assert settings.deepseek.name == "deepseek-v4-pro"
    assert settings.database_path.parent == tmp_path


def test_rejects_invalid_port(tmp_path):
    config = copy_config(tmp_path)
    config.write_text(config.read_text(encoding="utf8").replace("8000", "65536"), encoding="utf8")
    with pytest.raises(ValidationError, match="port"):
        load_settings(tmp_path)


def test_rejects_missing_platform(tmp_path):
    config = copy_config(tmp_path)
    contents = config.read_text(encoding="utf8")
    config.write_text(contents.split("[platforms.douyin]")[0], encoding="utf8")
    with pytest.raises(ValueError, match="平台配置"):
        load_settings(tmp_path)
