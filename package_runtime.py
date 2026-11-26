"""Build a clean local runtime and shareable zip without login or user data."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

PROJECT = Path(__file__).resolve().parent
ROOT_FILES = {"node.exe", "wrapper.node", "index.js", "package.json", "config.json"}
CODE_DIRS = {"native", "node_modules", "static", "worker"}


def program_files(source: Path):
    for path in source.rglob("*"):
        if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            continue
        relative = path.relative_to(source)
        parts = relative.parts
        if len(parts) == 1 and (path.name in ROOT_FILES or path.suffix.lower() == ".dll"
                               or path.name.lower().startswith(("license", "notice", "copying"))):
            yield path, relative
        elif parts[0] == "win64" and path.suffix.lower() == ".dll":
            yield path, relative
        elif parts[0] == "napcat":
            if len(parts) == 2 and (path.suffix in {".js", ".mjs", ".dll", ".exe"}
                                   or path.name in {"package.json", "qqnt.json"}
                                   or path.name.lower().startswith(("license", "notice", "copying"))):
                yield path, relative
            elif len(parts) > 2 and parts[1] in CODE_DIRS:
                yield path, relative
            elif (len(parts) > 3 and parts[1:3] == ("plugins", "napcat-plugin-builtin")
                  and not set(parts[3:-1]) & {"config", "data", "cache", "logs"}
                  and path.suffix.lower() not in {".db", ".log", ".sqlite", ".sqlite3"}):
                yield path, relative


def install(source: Path, project: Path = PROJECT):
    destination = project / "runtime" / "napcat"
    if destination.exists():
        raise ValueError("runtime/napcat 已存在，未覆盖；更新时请先自行备份并处理已有目录。")
    files = list(program_files(source))
    required = {Path(name) for name in ROOT_FILES} | {Path("napcat/napcat.mjs")}
    if not required <= {relative for _, relative in files}:
        raise ValueError("所选目录不是完整的 Windows Node NapCat 运行包。")
    for path, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (destination / "napcat.bat").write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\n"%~dp0node.exe" "%~dp0index.js"\r\n')
    (destination / "BUNDLE-NOTICE.txt").write_text(
        "Local unmodified NapCat/Node/QQ runtime. Original third-party licenses remain applicable.\n"
        "Source: user-provided MaiBot Windows Node runtime; no account configuration or session copied.\n"
        "NapCat: https://github.com/NapNeko/NapCatQQ\nNode: https://nodejs.org/\nQQ: https://im.qq.com/\n",
        encoding="utf8")
    return destination


def bundle(project: Path = PROJECT):
    runtime = project / "runtime" / "napcat"
    if not runtime.is_dir():
        raise ValueError("请先准备 runtime/napcat。")
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=project).decode().split("\0")
    output = project / "dist" / "Fatfish-Windows-with-NapCat.zip"
    output.parent.mkdir(exist_ok=True)
    if output.exists():
        raise ValueError("整包已存在，未覆盖。请先移动或重命名旧压缩包。")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in names:
            if name and (project / name).is_file():
                archive.write(project / name, "Fatfish/" + name)
        for path, relative in program_files(runtime):
            archive.write(path, "Fatfish/runtime/napcat/" + relative.as_posix())
        for name in ("napcat.bat", "BUNDLE-NOTICE.txt"):
            archive.write(runtime / name, "Fatfish/runtime/napcat/" + name)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--bundle", action="store_true")
    args = parser.parse_args()
    if args.source:
        install(args.source)
        print("Clean runtime installed in runtime/napcat.")
    if args.bundle:
        print(bundle())
