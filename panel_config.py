from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import tomllib

import tomli_w

from adapter_base import PLATFORMS
from settings import Settings
from store import Store


class ConfigConflict(Exception):
    pass


class NoBackup(Exception):
    pass


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fishbot-", delete=False) as pending:
        temporary = Path(pending.name)
        try:
            with pending:
                pending.write(content)
                pending.flush()
                os.fsync(pending.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Snapshot:
    revision: str
    settings: Settings
    can_restore: bool


class PanelConfig:
    def __init__(self, store: Store, config_dir: Path, data_dir: Path):
        self.store = store
        self.config_dir = config_dir
        self.data_dir = data_dir

    def _read(self) -> dict[str, bytes]:
        return {name: (self.config_dir / name).read_bytes()
                for name in ("persona.toml", "platforms.toml")}

    def _snapshot(self, files: dict[str, bytes], generation: int) -> Snapshot:
        persona = tomllib.loads(files["persona.toml"].decode("utf-8"))
        platforms = tomllib.loads(files["platforms.toml"].decode("utf-8"))
        settings = Settings.model_validate({
            **platforms, "persona": persona, "database_path": self.store.path,
        })
        if settings.platforms.keys() != set(PLATFORMS):
            raise ValueError("平台配置不完整")
        revision = sha256(b"\0".join([str(generation).encode(), *files.values()])).hexdigest()
        return Snapshot(revision, settings, self._backup.exists())

    @property
    def _backup(self) -> Path:
        return self.data_dir / "panel-backup.toml"

    @staticmethod
    def _generation(db) -> int:
        row = db.execute("SELECT value FROM panel_state WHERE key='config_generation'").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _bump(db, generation: int) -> int:
        generation += 1
        db.execute("INSERT INTO panel_state VALUES ('config_generation',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(generation),))
        return generation

    def read(self) -> Snapshot:
        with self.store.transaction() as db:
            return self._snapshot(self._read(), self._generation(db))

    def update(self, expected: str, filename: str, transform, action: str) -> Snapshot:
        # SQLite 写锁覆盖整个版本检查和文件替换，跨进程也只允许一个保存者。
        with self.store.transaction() as db:
            files = self._read()
            generation = self._generation(db)
            before = self._snapshot(files, generation)
            if before.revision != expected:
                raise ConfigConflict
            original = files[filename]
            values = tomllib.loads(original.decode("utf-8"))
            files[filename] = tomli_w.dumps(transform(values)).encode("utf-8")
            self._snapshot(files, generation)
            if files[filename] != original:
                backup = tomli_w.dumps({"file": filename, "previous": original.decode("utf-8")})
                atomic_write(self._backup, backup.encode("utf-8"))
                atomic_write(self.config_dir / filename, files[filename])
                self.store.record_panel_action(db, action)
                generation = self._bump(db, generation)
            return self._snapshot(files, generation)

    def restore(self, expected: str) -> Snapshot:
        with self.store.transaction() as db:
            files = self._read()
            generation = self._generation(db)
            if self._snapshot(files, generation).revision != expected:
                raise ConfigConflict
            if not self._backup.exists():
                raise NoBackup
            backup = tomllib.loads(self._backup.read_text(encoding="utf-8"))
            filename = backup["file"]
            if filename not in files:
                raise ValueError("配置备份无效")
            files[filename] = backup["previous"].encode("utf-8")
            self._snapshot(files, generation)
            atomic_write(self.config_dir / filename, files[filename])
            self._backup.unlink()
            self.store.record_panel_action(db, "restore_config")
            return self._snapshot(files, self._bump(db, generation))
