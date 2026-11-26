import os
from pathlib import Path
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from adapter_base import PLATFORMS, Platform

PROJECT_ROOT = Path(__file__).resolve().parent
NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]
Positive = Annotated[int, Field(gt=0)]
NonNegative = Annotated[int, Field(ge=0)]
Emotion = Literal["joy", "teasing", "sleepy", "flustered", "sad", "smug", "neutral", "annoyed"]
EMOTIONS = ("joy", "teasing", "sleepy", "flustered", "sad", "smug", "neutral", "annoyed")


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ServerSettings(ConfigModel):
    port: int = Field(default=8000, ge=1, le=65535)


class ModelSettings(ConfigModel):
    name: NonEmpty
    thinking: Literal["disabled"] = "disabled"


class PlatformSettings(ConfigModel):
    enabled: bool = False
    receive: bool = False
    send: bool = False
    allowlist: list[NonEmpty] = Field(default_factory=list)
    record_memory: bool = True


class PersonaSettings(ConfigModel):
    name: NonEmpty
    aliases: list[NonEmpty]
    system_prompt: NonEmpty
    max_chars: int = Field(ge=0, le=1000)
    temperature: float = Field(ge=0, le=2)
    style: str = Field(default="", max_length=1000)
    emotions: list[Emotion] = Field(default_factory=lambda: list(EMOTIONS), min_length=1, max_length=8)


class ChatSettings(ConfigModel):
    participation_every: Positive = 4
    max_event_age: Positive = 300
    send_timeout: Positive = 10
    model_timeout: Positive = 60


class MemorySettings(ConfigModel):
    recent_messages: Positive = 32
    summary_messages: Positive = 20
    summary_characters: Positive = 6000
    summary_seconds: Positive = 600
    summary_limit: Positive = 1800
    retry_seconds: Positive = 60


class CognitionSettings(ConfigModel):
    extraction: bool = False
    recall: bool = False
    reflection: bool = False
    max_results: int = Field(default=6, ge=1, le=20)
    token_budget: int = Field(default=600, ge=100, le=1600)
    min_confidence: float = Field(default=0.65, ge=0, le=1)
    include_reflections: bool = False
    include_autobiography: bool = False
    reflection_interval: int = Field(default=3600, ge=60)


class Limits(ConfigModel):
    per_channel_daily: NonNegative = 200
    global_daily: NonNegative = 400
    minimum_interval: NonNegative = 20


class StickerSettings(ConfigModel):
    enabled: bool = False
    min_interval: NonNegative = 60
    repeat_interval: NonNegative = 28800


class Settings(ConfigModel):
    server: ServerSettings
    deepseek: ModelSettings
    platforms: dict[Platform, PlatformSettings]
    persona: PersonaSettings
    database_path: Path
    qq_owner_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{4,11}$")
    qq_owner_dm: bool = False
    qq_owner_checkins: bool = False
    qq_owner_record_memory: bool = True
    chat: ChatSettings = Field(default_factory=ChatSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    cognition: CognitionSettings = Field(default_factory=CognitionSettings)
    limits: Limits = Field(default_factory=Limits)
    stickers: StickerSettings = Field(default_factory=StickerSettings)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


def default_database_path() -> Path:
    from local_runtime import data_directory
    return data_directory() / "fishbot.db"


def read_toml(path: Path) -> dict:
    with path.open("rb") as source:
        return tomllib.load(source)


def load_settings(config_dir: Path | None = None, *, database_path: Path | None = None) -> Settings:
    from role_profiles import config_directory
    config_dir = config_dir or config_directory()
    config = read_toml(config_dir / "platforms.toml")
    settings = Settings.model_validate({
        **config,
        "persona": read_toml(config_dir / "persona.toml"),
        "database_path": database_path or default_database_path(),
    })
    if settings.platforms.keys() != set(PLATFORMS):
        raise ValueError("平台配置必须包含 qq、bilibili、douyin")
    port = os.getenv("FISHBOT_PANEL_PORT")
    if port:
        settings = settings.model_copy(update={"server": ServerSettings(port=int(port))})
    return settings
