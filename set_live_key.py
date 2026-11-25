import getpass
import os
from pathlib import Path
from settings import default_database_path
from tempfile import NamedTemporaryFile

folder = default_database_path().parent
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
value = getpass.getpass("输入大肥鱼 V2 的 DeepSeek API Key（输入不会显示）：").strip()
if not value or any(char.isspace() for char in value):
    raise SystemExit("密钥为空或包含空白，现有配置未修改。")
with NamedTemporaryFile(dir=folder, delete=False, mode="w", encoding="utf8") as file:
    file.write("DEEPSEEK_API_KEY=" + value + "\n")
    path = Path(file.name)
os.replace(path, folder / ".env")
print("已保存 V2 的 DeepSeek 密钥，可以关闭此窗口。")
