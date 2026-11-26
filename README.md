# 大肥鱼 V2

大肥鱼的角色形象是 Q 版蓝发女仆鲸鱼娘，兼容旧称「鲸小鱼」。项目提供可独立运行的 QQ 模拟聊天引擎、本机健康检查及 SQLite 存储。正式入口的 QQ、B 站、抖音收发默认关闭。

多人维护、首次发布和协作流程见 [开发与协作说明](CONTRIBUTING.md)。

## 快速体验

### Windows 双击启动和 QQ 登录

**带 NapCat 整包：**解压 `Fatfish-Windows-with-NapCat.zip` 后双击 `start.bat`。NapCat、Node 和所需运行库位于项目的 `runtime/napcat`，启动窗口会自动使用该目录，不必查找麦麦或其他安装路径。首次使用生成独立的本机 Token，默认 WebUI/OneBot 端口为 16099/13001；请重新扫码登录，旧登录态不会打包。

Git 仓库只管理源码及打包工具；约 250 MB 的第三方运行文件及生成的数据不入 Git。另一位开发者需获取本地生成的整包，而不仅是 git clone。整包仍需 Python 3.12+（含 Tcl/Tk），Python 依赖由首次启动自动安装。已安装包更新源码后，可运行 `.venv\Scripts\python.exe package_runtime.py --bundle` 重新生成 `dist/Fatfish-Windows-with-NapCat.zip`，旧整包存在时不会覆盖。不要手工压缩运行过的整个 runtime 目录，因为它可能包含登录凭据和个人数据。

运行包包含第三方 NapCat、Node 和 QQ 运行文件，附带许可文件保持原样；这些文件不适用本项目的开源许可证。当前只生成本地运行包，未向 GitHub 上传第三方二进制。

无需 Codex，也不必先安装 WSL：双击项目目录的 `start.bat`。首次运行会在项目 `.venv` 中安装 `requirements.txt` 的依赖；需要已安装 Python 3.12 或更新版本（包含 Tcl/Tk 并加入 PATH）。安装失败可重试 `setup.bat`，已有环境和本地数据不会被删除。

1. 在启动窗口点击“检测已有 NapCat”，或手动选择已有 NapCat 启动文件及 `webui.json`。支持检测麦麦一键包目录。选择 `webui.json` 后点击“读取已有 NapCat 配置”，可读取实际端口、唯一账号和正向 WS Token；检测不会改写 NapCat 文件，多个账号或 WS 服务需手动选择填写。
2. 填写机器人 QQ 号、授权群号、OneBot 正向 WebSocket 地址与 Token、NapCat WebUI 地址。普通 Windows 部署常用端口为 3001/6099；旧 Docker 部署可能为 13001/16099，请以自己的配置为准。
3. 点击“保存本机配置”，再点击“打开 QQ 扫码登录”。已有服务会被复用；尚未运行时启动所选 NapCat 文件。在 WebUI 登录管理后台后，进入 QQ 登录并扫码、手机确认。需要时点击“复制 WebUI 密码”；密码有变化时以 NapCat 最新配置/提示为准。
4. 在 NapCat 网络配置启用“WebSocket 服务端（正向 WS）”，选择 `array` 消息格式；地址与 Token 应和启动窗口一致。此操作需在 NapCat 中确认，启动器不会覆盖现有网络设置。
5. 点击“启动服务”，再“打开控制面板”。首次登录点击“复制面板访问令牌”，粘贴到面板即可，不必运行终端命令。
6. 要启用真实回复，填写自己的 DeepSeek API Key 并保存/重启服务，然后点击“验证模型并启用回复”。该操作会核对 QQ 账号和群，并调用一次模型产生少量用量；通过后启用回复。扫码本身不会授予新的发送权限。以前已启用的回复权限会保留，可在面板暂停。

关闭启动窗口会停止它启动的大肥鱼服务，不退出 QQ、不停止 NapCat。端口被其他进程占用时会报告，不强制结束进程。服务端口读取 `platforms.toml` 的 `server.port`。

本机配置（包括密钥）保存在 `%LOCALAPPDATA%\FishbotV2Public\desktop.local.json`；数据库和面板令牌也在该用户目录，**不要上传或分享此目录**。`FISHBOT_DATA_DIR` 可为测试指定独立数据目录。显式环境变量优先于桌面配置，修改配置后重启服务生效。

如果本机没有 NapCat，可从启动窗口打开[官方安装说明](https://napneko.icu/guide/boot/Shell)；QQ 扫码和必要的账号验证需本人操作。WebUI/OneBot 配置参考[官方说明](https://napneko.icu/config/basic.html)。程序不会自动安装 QQ、NapCat 或系统服务。

原有 WSL 路线仍保留：设置 `FISHBOT_USE_WSL=1` 后运行 `start.ps1`/`stop.ps1`，沿用下面的 WSL 部署步骤。Windows 原生模式与 WSL 模式的数据目录各自独立。

需要 Python 3.12。进入仓库目录后执行：

    python -m venv .venv
    # Linux / macOS
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python main.py --simulate
    .venv/bin/python main.py

Windows PowerShell 使用 .venv\Scripts\python.exe 替代 .venv/bin/python。
访问 http://127.0.0.1:8000/ ，用同一个虚拟环境运行 main.py --panel-token 取得本地登录令牌。
运行 python -m pytest -q 可执行本地测试。

公开版默认使用本机模拟。连接自己的 NapCat 前，请在运行环境中设置 FISHBOT_QQ_BOT_ID
和 FISHBOT_QQ_GROUP_ID，准备本机 OneBot v11 配置和自己的 DeepSeek API Key。
Windows 桌面步骤假定两端均使用独立虚拟环境，WSL 路径可由 FISHBOT_WSL_DISTRO、
FISHBOT_WSL_PROJECT 和 FISHBOT_WSL_PYTHON 指定。

## 在 WSL 启动

打开 Ubuntu-24.04 终端并进入本仓库的挂载目录。

```bash
cd "/path/to/fishbot-v2-public"
~/.local/share/fishbot-v2-public/venv/bin/python main.py
```

在 Windows 浏览器打开 <http://127.0.0.1:8000/health>。健康时返回 `{"service":"fishbot-v2-public","status":"ok","database":"ok"}`。SQLite 不可用时返回 HTTP 503。可在 `platforms.toml` 修改端口，服务固定绑定本机回环地址。按 Ctrl+C 停止前台服务。

需要重新建立 WSL 环境时，在 Ubuntu 中执行：

```bash
umask 077
mkdir -p ~/.local/share/fishbot-v2-public
python3 -m venv --without-pip ~/.local/share/fishbot-v2-public/venv
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o ~/.local/share/fishbot-v2-public/get-pip.py
~/.local/share/fishbot-v2-public/venv/bin/python ~/.local/share/fishbot-v2-public/get-pip.py
~/.local/share/fishbot-v2-public/venv/bin/python -m pip install -r requirements.txt
```

数据库默认保存到当前用户的应用数据目录。测试使用临时库。

## 在 PyCharm Community 2023.2.5 调试

1. 选择 **File → Open**，打开 `<项目目录>`。
2. 在 **Settings → Project → Python Interpreter** 添加本地已有虚拟环境，选择 `%LOCALAPPDATA%\FishbotV2Public\venv\Scripts\python.exe`。设置对话框里请把 `%LOCALAPPDATA%` 展开为实际路径。
3. 在项目终端运行下方 PowerShell 命令，也可以新建 pytest 运行配置，将目标和工作目录设为项目根目录。该 Community 版本通过本机解释器调试，正式服务通过 WSL 启动。

```powershell
& "$env:LOCALAPPDATA\FishbotV2Public\venv\Scripts\python.exe" -m pytest -q
```

若需重建 Windows 测试环境，请在项目根目录运行：

```powershell
py -3.12 -m venv "$env:LOCALAPPDATA\FishbotV2Public\venv"
& "$env:LOCALAPPDATA\FishbotV2Public\venv\Scripts\python.exe" -m pip install -r requirements.txt
```

用 Windows 解释器运行 `main.py` 会使用本机独立数据库 `%LOCALAPPDATA%\FishbotV2Public\fishbot.db`。如果 WSL 服务正在运行，请先停止 V2 或为调试选择可用端口。

## 配置与模块

- `platforms.toml` 定义端口、DeepSeek 模型和三个平台的默认设置。正式入口只启动 FastAPI。预设模型为 `deepseek-flash`，非思考模式。
- `persona.toml` 保存共享名称、别名、提示词、温度和回复字数。字数上限设为 0 时，不按字符数拦截回复，仍保留模型输出预算和内容审核。
- `events.py` 与 `adapter_base.py` 定义事件、回复意图、能力和发送结果接口。各平台实现在后续阶段加入，QQ 当前经由注入的模拟 OneBot RPC 运行。
- `store.py` 管理 SQLite 结构版本、事件读写及持久化去重。去重范围包含平台、会话类型、会话和消息编号。
- `main.py` 加载配置，通过 `panel.py` 建立 FastAPI 服务，启动时初始化 SQLite。
- `.env.example` 是后续联调的变量模板。阶段 2 的运行入口不读取 `.env`，演示使用本地模拟模型。

## 故障检查

如果端口占用，查看监听进程并给 V2 配置空闲端口。若健康检查返回 503，检查 V2 数据目录下的数据库文件和权限。若 Ubuntu 中的健康检查成功而 Windows 浏览器无法访问，检查 WSL 的本机端口转发及 Windows 端口占用。

## 阶段 2 独立模拟

在项目根目录执行。模拟模式使用独立临时 SQLite，结束后自动清理。输出中的 `mode=simulation` 与 `state=sent` 表示模拟 OneBot 已确认；标准启动和 `/health` 继续使用正式 V2 数据库。

```powershell
& "$env:LOCALAPPDATA\FishbotV2Public\venv\Scripts\python.exe" -X utf8 main.py --simulate
& "$env:LOCALAPPDATA\FishbotV2Public\venv\Scripts\pytest.exe" -q
```

```bash
cd "/path/to/fishbot-v2-public"
~/.local/share/fishbot-v2-public/venv/bin/python main.py --simulate
~/.local/share/fishbot-v2-public/venv/bin/pytest -q
```

### QQ 事件和记忆

- `adapter_qq.py` 把消息段数组或 CQ 字符串转换为统一事件。通过 `get_msg` 核对引用作者、会话和消息 ID。引用无法核查时不作直接触发，显式提及机器人仍有效。
- `dispatcher.py` 先过滤机器人自身、白名单外和过期事件，再把事件、触发计数和处理任务一起入库。普通群文字每 4 条触发一次，直接提及和引用机器人均不改变计数。只提及他人时跳过。群会话白名单可用群号或 `group:<编号>`，私聊需要明确采用 `dm:<编号>`。
- `persona.toml` 定义共享人设。仅将明确配置的账号标记为 `OWNER`，群友标记为 `PEER`。未归属的自身消息识别为 `SELF_UNKNOWN` 并过滤，已确认的自动回复写入记忆，标记为 `BOT`。
- `memory.py` 按平台、会话类型与会话编号隔离。短期注入最近 32 条。长期摘要在新增 20 条、累计 6000 字，或最早一条未摘要内容达到 600 秒时生成。通过 `Dispatcher.tick()` 检查无新事件到达的会话；成功后前移摘要进度，失败按配置退避，无新增内容时不重复生成。摘要默认最多注入 1800 字。
- `llm.py` 实现 DeepSeek 官方 Chat Completions 请求、非思考模式、JSON 校验、超时、有限重试和用量统计。回复生成后经过本地规则和独立模型审核。演示和测试中的请求与审核均为本地模拟。

### 持久化发送

- `store.py` 将通过审核的意图置为 `pending`，平台开关、白名单、内容和限流通过后，原子地置为 `sending`，再调用 OneBot。每会话每日 200 条、全局每日 400 条，均按北京时间切日。默认发送尝试至少相隔 20 秒。发送中及结果不确定的条目仍占用当天的额度。
- 最终结果写为 `sent`、`skipped`、`failed` 或 `uncertain`。超时、OneBot 1200 和中断的发送尝试进入 `uncertain`。恢复时只继续尚未开始发送的任务。结果不确定时可查询历史；只有相同会话、发送者、引用、时间范围与正文完整匹配且结果唯一时，才确认文本已发出。查不到、匹配不唯一、私聊或带图的结果保留为 `uncertain`。
- `stickers.py` 接收显式注入的表情目录，依据发送能力、关键词、语境、情绪和模型选出的 ID 判定候选，发送事务再次检查最小配图间隔与同图防重复时间。当前只使用虚拟元数据。
- 标准启动将阶段 1 的 V2 SQLite 结构升级到版本 2，保留原事件记录。V1 数据位于独立目录。

真实联调需要单独批准后接入 OneBot WebSocket、验证真实回包及账号授权。QQ 登录仍由授权账号持有人在 NapCat WebUI 使用手机扫码。

## 阶段 3 中文控制面板

启动 WSL 服务后，从 Windows 浏览器打开 <http://127.0.0.1:8000/>。在项目目录的 WSL 终端查看本机面板访问令牌：

```bash
cd "/path/to/fishbot-v2-public"
~/.local/share/fishbot-v2-public/venv/bin/python main.py --panel-token
```

把令牌粘贴到登录表单。令牌由 V2 生成并保存在独立数据目录，面板仅把它保存于当前浏览器标签页的会话存储。系统设置页显示模型名称和参数。DeepSeek 密钥由服务进程的环境变量 `DEEPSEEK_API_KEY` 管理，本阶段只检查是否存在，不发起验证请求，面板没有修改密钥的入口。

- **总览** 查看本机服务、各连接和模型配置状态、当日统计、配置版本和最近记录。可以手动运行、暂停或恢复本地模拟。模拟仅作用于 V2 的专用模拟会话，手动运行会在 V2 SQLite 留下 `simulation` 记录。暂停阻止新任务，已经开始的模拟会正常收尾。阶段 2 的命令行 `--simulate` 仍使用自动清理的临时库。
- **平台管理** 查看并保存平台预设、接收偏好和会话白名单。实际连接和授权状态单独显示，真实发送始终关闭。
- **AI 人设** 编辑 `persona.toml` 中的人设、风格、温度、字数和情绪，也可保存模型和接话间隔。面板保存前检查配置版本，每次原子替换一个 TOML 文件。恢复按钮撤销面板最近一次有效保存。
- **记忆管理** 查看会话、历史、摘要状态与时间。清理需确认所选会话编号，仅移除该会话的短期记忆和长期摘要。已记录的事件、消息日志、发送任务和幂等键继续保留。
- **表情包** 导入静态 PNG、JPEG 或 WebP。上传上限 4 MB，图片需通过解码与像素检查。存储时生成最长边 640 像素的 PNG，并移除原图片元数据。可编辑单图状态、情绪、关键词、语境及全局配图间隔和同图防重复时间。
- **消息日志** 按平台、状态或时间筛选 SQLite 记录。模拟发送单独标记，审核拦截的正文隐藏并显示原因。
- **系统设置** 保存额度、发送间隔、接话频率、模型及日志等级。从本页可进入平台开关和白名单配置。

### 管理接口

管理接口要求 `Authorization: Bearer <本机访问令牌>`。写操作还需准确的本机 `Origin`（例如 `http://127.0.0.1:8000`）。

| 方法 | 路径 |
|---|---|
| GET | `/`、`/health` |
| GET | `/api/status`、`/api/platforms`、`/api/persona`、`/api/settings` |
| GET | `/api/memory`、`/api/stickers`、`/api/messages`、`/api/logs` |
| GET | `/api/stickers/{id}/image` |
| PUT | `/api/platforms/{platform}`、`/api/persona`、`/api/settings`、`/api/stickers/{id}` |
| POST | `/api/runtime`、`/api/settings/restore`、`/api/memory/clear`、`/api/stickers` |

阶段 3 启动时把 V2 SQLite 结构从 2 版升级到 3 版。表情包图片位于 WSL 的 `~/.local/share/fishbot-v2-public/stickers`，与旧版图库独立。记录按北京时间切日，消息明细使用浏览器本地时区显示。

## 阶段 4 桌面一键启动

双击 Windows 桌面「大肥鱼 V2 开源版」。启动器检查 `/health` 返回的服务名、状态和数据库，确认后用默认浏览器打开 <http://127.0.0.1:8000/>。服务已就绪时直接复用。冷启动使用独立的隐藏 WSL 进程托管 V2，退出启动窗口或关掉浏览器后服务继续运行。桌面入口使用固定的 8000 端口。

面板沿用阶段 3 的标签页登录机制。当前标签页已登录时仍可继续使用；如果默认浏览器打开新标签页，新标签页会显示原有登录表单。获取令牌使用 `main.py --panel-token`，快捷方式和网址都不含令牌。

重新创建快捷方式或停止 V2 时，可在 Windows PowerShell 运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\create_shortcut.ps1
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\stop.ps1
```

只需启动服务时，可运行 `start.ps1 -NoBrowser`。正常停止使用 WSL 的进程句柄发送 SIGTERM，等待退出。若不能验证端口进程属于本项目，脚本报告原因并保留该进程。V2 启动保持 simulation 模式。

### 启动故障

- **8000 已占用**。先在 Windows 检查 `Get-NetTCPConnection -LocalPort 8000 -State Listen`，在 Ubuntu 检查 `ss -ltnp 'sport = :8000'`，再由占用程序的拥有者处理。启动器不会抢占端口。
- **找不到 Ubuntu 或解释器**。运行 `wsl --list --verbose`，确认 `Ubuntu-24.04`、`<项目目录>` 和 V2 的虚拟环境。Windows 调用使用阶段 1 的 V2 专用虚拟环境，缺失时按本 README 重建。
- **启动异常或健康检查超时**。Windows 记录位于 `%LOCALAPPDATA%\FishbotV2Public\launcher\launcher.log` 和 `startup-worker.log`，WSL 服务日志位于 `~/.local/share/fishbot-v2-public/launcher/server.log`。检查项目文件、SQLite 权限和 Windows 到 WSL 的本机端口转发。

重新生成公开版桌面图标时，可用 V2 虚拟环境执行 `draw_fish.py`。形象来源及署名记录见 `ASSET_CREDITS.md`。日常 pytest 不主动启停真实服务。在本机 Windows 验收启停时，可设置 `FISHBOT_VERIFY_DESKTOP=1` 后运行 pytest。此测试会停止并重新启动 V2，临时占用 8000 的测试监听随测试退出。

## 抖音账号与内容工作台

- **内容工作台**在账号授权前即可使用。「保存模拟示例」生成带模拟标记的本地示例，也可新建、编辑、导出或删除草稿。「用 DeepSeek 生成并保存」会按需调用现有的模型配置，生成选题、脚本、标题、简介、话题、置顶评论草稿和发布计划。该页单独记录抖音草稿请求和 Token 用量。
- **抖音账号**需要开放平台上审核通过的网站应用。按[抖音官方入驻常见问题](https://developer.open-douyin.com/docs/resource/zh-CN/developer/join/issues)，网站应用面向企业、个体工商户等合规主体，个人开发者认证可创建小程序、小游戏和直播小玩法。符合条件的主体登录[开放平台](https://open.douyin.com/)后，在管理中心查看应用审核、回调地址及接口权限。将 Client Key、Client Secret 和登记的回调 URL 填入本机面板，随后在同一浏览器打开官方扫码页并确认 `user_info`。应用配置、账号实际授权和最近一次接口核验分别显示。
- 回调处理地址为 `http://127.0.0.1:8000/douyin/callback`。如果控制台接受并登记此 URL，可直接使用；如果要求已验证的 HTTPS 域名，需要在你控制的域名下提供独立回调入口，把浏览器重定向到该本机地址并转交 `state`、`code` 或 `error`。回调入口应使用固定目标、关闭授权参数记录和缓存，避免把面板或数据库暴露到公网。当前没有部署公网回调。
- 「刷新授权并核验账号」使用官方 refresh token 流程。刷新令牌到期后由用户重新授权。「断开本机连接」清除本机授权记录。若要在抖音端撤销应用授权，可到抖音 App 的「设置 → 账号与安全 → 授权管理」操作。
- 当前模式是草稿。发布、评论发送、私信及直播接口尚未接入。作品发布须核验 `video.create`，普通账号的作品评论须核验 `item.comment`。

抖音侧库存放于 WSL 的 `~/.local/share/fishbot-v2-public/douyin/operations.db`。其中 `oauth_state` 保存授权状态和服务器端凭据，`drafts` 保存内容草稿，`model_usage` 保存模型用量。QQ 库沿用原有结构。抖音库的备份应使用 SQLite backup API，并与原库保持同等的访问权限。恢复时先停止 V2，核验备份的 `PRAGMA quick_check` 和 `user_version`，再替换抖音侧库。

## QQ 可靠接收与恢复（M1）

「平台管理 → QQ」独立显示连接、接收、群记忆、自动回复及待核查任务。暂停自动回复后，授权群消息仍可落库。关闭群记忆会停止记忆写入和调用记忆上下文，授权消息仍留一份事件记录供审计和去重。已有记忆可通过原记忆管理页管理。

每条事件及其 Outbox 记录在同一 SQLite 事务提交。回复窗口在重启、重连和人工恢复时重新划定，旧待评估和待发送任务保留为跳过。曾开始发送的任务仅进入历史核查，确认不了就保留 `uncertain`。恢复回复仅作用于新消息，同一授权群的旧待核查记录不会自动补发。

M1 在 SQLite schema 4 上为事件记录 `memory_allowed`。旧事件缺少该字段时仍可读取；M2 只接收明确授权且在启用后新提交的事件。部署前使用 SQLite backup API 制作一致性备份。

## M2 认知记忆

schema 5 原地增表。原始消息、短期窗口、长摘要和 Outbox 沿用 M1 表结构。新增 `memory_nodes`、`memory_edges`、`memory_sources`、`memory_revisions`、`memory_jobs`、`memory_goals`、`memory_self_state`、`memory_suppression`。反思使用类型为 `reflection` 的节点及 `supports` 证据边，关系边的两个端点通过复合外键限定于同一作用域。

正式部署的提取、回忆和反思开关默认关闭。后续获得授权后，可在 `platforms.toml` 的 `[cognition]` 中设置 `extraction`、`recall`、`reflection`，重启 V2 应用。M2 目前使用本地规则提取与推测性反思，均无外部模型调用。用原有 M1 权限判定是否可以记忆，群聊中的人物通过已核验的平台 ID 区分。开启与关闭只影响新功能，不扩大白名单，也不改变发送开关。

检索默认最多六条，最低置信度 0.65，并用 UTF-8 字节数给 600 Token 配额留出保守边界。候选先按作用域和人物限定，支持中文子串、别名及当前话题，按匹配次数、重要度、置信度和时间衰减排序，再做一层关系扩展。反思和自传分别由 `include_reflections`、`include_autobiography` 控制是否注入。系统自传只采集 M2 启动后观察到的配置及发送确认，无法从当前昵称反推出此前改名日期。

遗忘使用哈希抑制标记，删除该逻辑记忆的各版本和派生反思，停止来源提取任务，清理同会话的重复短期条目及摘要。原始事件和经授权读取的审计历史继续保留。暂停提取后可清除整个会话记忆；已删除的同类记忆仍受抑制。

M3 管理 API 继续复用本机令牌和 Origin 校验。读取接口为 `GET /api/cognition`、`GET /api/cognition/self`、`GET /api/cognition/nodes`、`GET /api/cognition/nodes/{id}`。详情包括证据、关系和修订。按 `platform/kind/channel/mode` 传入作用域。修改用 `PUT /api/cognition/nodes/{id}/state`、`PUT /api/cognition/nodes/{id}/goal`、`POST /api/cognition/nodes/{id}/forget`。写操作校验版本，遗忘还须提交对应节点编号。开启反思后可用 `POST /api/cognition/reflections` 请求按当前证据重新评估。

回滚只涉及代码时，先备份当前 schema 5 库。停止 V2 后，在一份校验过的副本上按依赖逆序移除八个 M2 表，设置 `user_version=4` 并验证 `integrity_check` 和 `foreign_key_check`。旧程序不能直接打开 schema 5。若恢复部署前快照，快照之后的新事件与发送记录会丢失，应先保留现库并核对这些记录。

## M3 中文记忆工作台

访问本机面板的「记忆管理」。总览、关系图、人物档案、时间线、搜索和自我模型共用顶部的记忆范围。旧版短期消息与摘要在「短期会话」中。切换「演示数据」会读取独立的临时 SQLite，随服务停止清理。部署和演示均不改动 M2 的三个实时开关。

- 图谱只画数据库中的真实关系。角色中心供导航辨识，图谱按页加载，可缩放、拖动、用键盘选择以及展开一层关联。
- 中文搜索支持关键词、别名、置信度与分页。召回预览从当前经授权范围的真实证据核验人物，再调用 M2 的同一检索函数，使用保守估算，未记录实际调用量和消耗时显示暂无数据。
- schema 5 的消息来源时点和节点时点可能一致且无独立记录。界面将事件发生日期标记为未独立确认，系统首次观测也不能用来推定历史改名日期。
- 管理员修订生成独立版本并保留原始来源，修订内容会明确标记为管理员的版本。遗忘需要核对当前版本并输入节点编号，保留原始聊天审计及历史备份。
- 新增接口为 `GET /api/cognition/graph`、`/people`、`/timeline`、`/nodes/{id}/sources/{job_id}`、`/nodes/{id}/forget-impact`，以及 `POST /api/cognition/preview`、`PUT /api/cognition/nodes/{id}/content`。原节点列表追加置信度、排序和分页信息。每个请求均限制作用域；演示需明确指定 `dataset=demo`。
- M3 沿用 schema 5。回滚到 M2 源码时，保留当前数据库及其已提交的记忆修订，停止 V2 后替换相应文件，再启动并检查 `/health`。

### 主人 QQ 私聊与问候

`platforms.toml` 的 `qq_owner_dm` 开启后，仅明确配置的 `qq_owner_id` 可直接私聊，不需要 @。私聊使用独立会话记忆，`qq_owner_record_memory` 控制记忆权限。启用 `qq_owner_checkins` 时，V2 仅向已核实为 QQ 好友的主人发起问候。问候安排在北京时间 10:00–21:00，之后随机间隔 1–3 天；最近 8 小时有主人私聊则延后。每个自然日最多产生一条问候任务。发送与回复共同受平台暂停、每日限额和 Outbox 核查保护。

## 许可证

项目代码采用 MIT 许可证。公开版图标说明见 ASSET_CREDITS.md。
