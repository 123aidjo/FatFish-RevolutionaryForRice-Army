# 大肥鱼 V2 开发与协作

## 本地开发

使用 Python 3.12 和独立虚拟环境。Windows PowerShell 示例：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --simulate
.\.venv\Scripts\python.exe -m pytest -q
```

在 Linux 或 WSL 上使用 `python3.12 -m venv .venv`，随后用 `.venv/bin/python` 运行后续命令。
单独启动面板用 `python main.py`，获取本机登录令牌用 `python main.py --panel-token`。
如果原项目正在使用 8000 端口，直接运行公开版时可在本副本的 `platforms.toml` 中为它选一个空闲端口。

## 从哪里改

| 功能 | 入口 |
| --- | --- |
| FastAPI 与本地面板 | `main.py`、`panel.py`、`app.js` |
| QQ 适配、回复策略和发送队列 | `adapter_qq.py`、`dispatcher.py`、`live_qq.py`、`store.py` |
| M2 认知记忆与 M3 工作台 | `cognition.py`、`cognitive_store.py`、`cognitive_panel.py`、`memory-workspace.js` |
| 抖音账号和创作工作台 | `douyin_panel.py`、`douyin_auth.py`、`douyin_content.py` |

## 首次发布

GitHub 上先建空仓库，保留本文件夹里已有的 README、许可证和 .gitignore。使用「Upload files」上传本文件夹的内容，或在本文件夹执行下列命令。把远程地址替换为你实际创建的仓库地址。

```bash
git init -b main
git add .
git status --short
git commit -m "Initial public release"
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

仓库所有者可以从 GitHub 的 **Settings → Collaborators → Add people** 邀请成员。详见 [GitHub 协作者文档](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/inviting-collaborators-to-a-personal-repository)。

## 多人开发

1. 每位成员克隆同一个仓库，在自己的电脑安装依赖。
2. 从最新的 `main` 建分支，例如 `git switch -c feature/memory-search`。
3. 跑本地模拟和测试，提交相关改动并推送分支。
4. 在 GitHub 发起 Pull Request，写明改动和测试结果，请另一位成员审查后合并。合并后各自同步 `main`。

真实平台账户和密钥由每位开发者在本机配置。用模拟数据和临时库复现问题，提交前检查是否夹带了真实聊天、数据库、截图、QQ 账号或令牌。
