# CheatCheck Bot

[中文](README.md) · [English](README_en.md)

Telegram 群组成员检查 Bot。通过一个 Bot 和一个或多个用户账号会话，查询用户与已配置来源群的共同群组，提供单人查询、全员检查、白名单和群管理功能。

**检测范围由你配置的来源群决定。** 默认采用用户账号已加入且可读取的普通群、超级群；可用 `CHEAT_GROUP_IDS` 指定范围。命中表示与这些来源群存在共同成员关系，来源群的选择与结果解读由部署者决定。

## 功能

- 私聊、群聊查询用户名、用户 ID，或引用消息查询无用户名用户。
- `/check all` 检查群成员，显示进度并私发命中用户 ID，重启后接续未完成任务。
- 多账号并行查询，按群组 ID 合并去重，共享限速和并发额度。
- 用户与来源群白名单；自动检查新成员、发言者和表情回应者。
- 管理员授权、群组开关、封禁与解封、来源群提交和审核。
- 中文、英文交互界面；SQLite 与 Telegram 会话通过本地目录持久化。

## 准备工作

推荐 Docker Engine / Docker Desktop 和 Docker Compose。容器使用 Python 3.12；直接运行时也建议 Python 3.12。

1. 向 [@BotFather](https://t.me/BotFather) 创建 Bot，取得 Bot token。
2. 在 [Telegram API development tools](https://my.telegram.org/apps) 取得 `api_id`、`api_hash`，参见 [官方申请说明](https://core.telegram.org/api/obtaining_api_id)。
3. 准备至少一个已注册的 Telegram 用户账号，能够接收验证码；如启用两步验证，还需密码。用户账号会话与 Bot token 分别配置。
4. 主机需能够访问 Telegram；使用代理时配置 `TELEGRAM_PROXY`。

## 快速开始：Docker

### 1. 获取代码与创建目录

Linux / macOS 的 Bash：

```bash
git clone https://github.com/kshao0315/CheatCheck-Bot.git
cd CheatCheck-Bot
mkdir -p account data
cp .env.example .env
```

Windows PowerShell：

```powershell
git clone https://github.com/kshao0315/CheatCheck-Bot.git
Set-Location CheatCheck-Bot
New-Item -ItemType Directory -Force account, data | Out-Null
Copy-Item .env.example .env
```

### 2. 填写本地配置

编辑 `.env`，替换以下占位值：

```dotenv
BOT_TOKEN=replace_with_bot_token
BOOTSTRAP_CODE=replace_with_random_code
ACCOUNT_JSON=account/primary.json
ACCOUNT_SESSION=account/primary
```

`BOOTSTRAP_CODE` 至少 12 个字符，用于首次领取超级管理员身份。可生成随机值并复制到本地 `.env`：

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

创建 `account/primary.json`，替换示例 API 配置：

```json
{
  "api_id": 12345,
  "api_hash": "replace_with_your_api_hash"
}
```

`api_id` 是数值。主程序也支持 `app_id` / `app_hash` 格式。`ACCOUNT_SESSION` 不带 `.session` 后缀。示例默认只启用第一个账号，第二个账号两项配置均留空即可。

### 3. 构建镜像并登录用户账号

```bash
docker compose build
docker compose run --rm cheatcheck python -c "from telethon.sync import TelegramClient; import bot; c=bot.user_client_from_config(bot.proxy_config()); c.start(); c.disconnect()"
```

按终端提示输入用户账号手机号、Telegram 验证码及可能需要的两步验证密码。完成后生成本地 `account/primary.session`。该账号应已加入你要检查的来源群；已有有效 Telethon 会话时，把文件放到此路径即可跳过登录。参见 [Telethon 登录说明](https://docs.telethon.dev/en/stable/basic/signing-in.html)。

### 4. 启动服务

```bash
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

日志出现 `Bot started as ...` 表示启动成功。`account/` 保存用户会话，`data/` 保存 Bot 会话、SQLite 数据库和任务状态；容器重建后两个挂载目录保留。

### 5. 初始化与首次查询

1. 用自己的 Telegram 账号私信 Bot：`/start 你的BOOTSTRAP_CODE`。首次成功领取者成为超级管理员，按提示选择语言。
2. 私信使用 `/add 用户ID` 授权需要管理群组的 Bot 管理员。
3. 把 Bot 加入目标群；全员检查、自动检查和封禁需相应管理员权限，删除查询消息需“删除消息”权限。
4. 私信 `/group`，检查并设置目标群查询开关。
5. 在群里发送 `/check @用户名`，或引用目标消息发送 `/check`。

如果群内指令没有到达 Bot，可将 Bot 设为群管理员，或通过 BotFather 调整隐私模式后按 Telegram 提示重新加入群。参见 [隐私模式说明](https://core.telegram.org/bots/features#privacy-mode)。接收 Bot 私信结果的管理员需先主动打开 Bot 私聊。

## 配置与多账号

| 变量 | 用途 |
| --- | --- |
| `BOT_TOKEN` | Bot token |
| `BOOTSTRAP_CODE` | 首次管理员校验码，至少 12 字符 |
| `ACCOUNT_JSON` / `ACCOUNT_SESSION` | 首个用户账号 API 配置与会话路径 |
| `ACCOUNT_JSON_2` / `ACCOUNT_SESSION_2` | 第二个账号；不使用时两项都留空 |
| `BOT_SESSION` | Bot 会话路径，默认 `data/bot` |
| `DATABASE` | SQLite 路径，默认 `data/bot.sqlite3` |
| `TELEGRAM_PROXY` | 可选 SOCKS5 URL；留空直连 |
| `CHEAT_GROUP_IDS` | 逗号分隔的数值群组 ID；留空采用账号已加入且可读取的来源群 |
| `WATCHED_ADMIN_USERNAME` | 入群前需转为手动处理的管理员账号；留空表示未配置此策略 |

代理格式为 `socks5://host:port`，也可包含用户名和密码。容器内的 `localhost` 指向容器自身；Docker Desktop 宿主机代理可使用从容器可访问的地址，例如 `host.docker.internal`。

示例中的策略账号只是占位值。要保留已有部署的入群策略，应在本地填写实际 `WATCHED_ADMIN_USERNAME`。管理员名单包含该账号时，尚未加入的账号停止自动入群，通知超级管理员手动处理。数据库设置 `automatic_join_enabled=0` 可暂停自动入群任务；该状态持久化保存。

### 增加第二个账号

先执行 `docker compose stop`。创建 `account/secondary.json`，在 `.env` 填入 `ACCOUNT_JSON_2=account/secondary.json` 和 `ACCOUNT_SESSION_2=account/secondary`，再登录并启动：

```bash
docker compose run --rm -e ACCOUNT_JSON=account/secondary.json -e ACCOUNT_SESSION=account/secondary cheatcheck python -c "from telethon.sync import TelegramClient; import bot; c=bot.user_client_from_config(bot.proxy_config()); c.start(); c.disconnect()"
docker compose up -d
```

后续使用连续的 `_3`、`_4` 等后缀，每个槽位同时填写 JSON 和 SESSION，保持已有槽位顺序稳定。新增账号实际加入且可读取来源群后，才提供对应范围的查询能力。

### 性能设置

| 变量 | 默认值 |
| --- | ---: |
| `ACCOUNT_RPC_CONCURRENCY` | 4 |
| `ACCOUNT_MEMBER_CONCURRENCY` | 2 |
| `BULK_WORKERS_PER_ACCOUNT` | 4 |
| `BULK_WORKER_LIMIT` | 32 |
| `SOURCE_WORKER_LIMIT` | 32 |
| `QUERY_PARTIAL_CACHE_SECONDS` | 20 |
| `QUERY_CACHE_ENTRIES` | 4096 |

FloodWait 冷却按账号和接口共享；提高并发不消除平台限速。新的用户查询重新请求全部可读取账号的范围，合并成功结果并按群组 ID 去重。

## 常用指令

| 指令 | 用途 |
| --- | --- |
| `/start` | 向导、语言与角色对应菜单 |
| `/check @username`、`/check 用户ID` | 指定用户查询；也支持引用消息后 `/check` 或 `!check` |
| `/check all` | 管理员发起全员检查；Bot 需群管理员权限 |
| `/group` | 私信管理群组查询开关 |
| `/auto check on` / `/auto check off` | 群管理员开启或关闭自动检查 |
| `/whitelist add 用户` / `/whitelist remove 用户` | 用户白名单 |
| `/whitelist group groupname` | 来源群白名单 |
| `/whitelist group remove groupname` | 移除来源群白名单 |
| `/ban 用户` / `/unban 用户` | 操作人与 Bot 均有相应权限时封禁或解封 |
| `/submit groupname` | 提交来源群待管理员审核 |
| `/add 用户ID` / `/revoke 用户ID` | 超级管理员授权或撤销 Bot 管理员 |
| `/admins`、`/logs`、`/logs clear` | 超级管理员查看管理员与查询日志、清理日志 |

命令与按钮均检查权限。全员结果私发超级管理员及发起者，相同接收者只发送一次。查询覆盖由账号实际可访问的来源决定；群成员无法完整枚举或查询失败时，应结合进度与失败记录判断结果。

## 直接用 Python 运行

先准备 `.env`、`account/primary.json` 和目录。主程序**不会自动加载 `.env`**，需先导入当前终端。

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
set -a
source .env
set +a
python -c "from telethon.sync import TelegramClient; import bot; c=bot.user_client_from_config(bot.proxy_config()); c.start(); c.disconnect()"
python bot.py
```

Windows PowerShell（无需激活虚拟环境）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim().Trim('"').Trim("'"), 'Process')
    }
}
.\.venv\Scripts\python.exe -c "from telethon.sync import TelegramClient; import bot; c=bot.user_client_from_config(bot.proxy_config()); c.start(); c.disconnect()"
.\.venv\Scripts\python.exe bot.py
```

Docker 与直接运行二选一，同一个会话文件不要同时交给多个进程。

## 更新、备份与服务器部署

更新容器：

```bash
git pull
docker compose up -d --build
docker compose logs --tail=100
```

备份时先执行 `docker compose stop`，将 `.env`、`account/`、`data/` 一起复制到受保护的备份目录，再执行 `docker compose up -d`。恢复时停止服务，从同一快照恢复三个部分后启动。

服务器可直接克隆仓库并执行 Docker 流程。另有可选 SSH 工具 `remote_deploy.py`：本地安装 `paramiko`，在当前终端设置 `DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_HOST_FINGERPRINT` 和可选 `DEPLOY_REMOTE`。服务器需 Docker / Compose，SSH 用户需目标目录写入与 Docker 执行权限。通过独立渠道核对 SHA256 主机密钥指纹；SSH 密码交互输入。

```bash
python -m pip install paramiko
python remote_deploy.py probe
python remote_deploy.py deploy
python remote_deploy.py status
```

工具从本地 `.env` 查找需上传的账号文件，SSH 连接变量从进程环境读取。`deploy` 上传程序、`.env` 和已配置用户会话到指定服务器，保留已有服务器账号文件；先核对配置与备份。`rollback` 恢复工具保留的上一版程序、配置和数据。单文件部署与回退使用相同备份名：

```bash
python remote_deploy.py deploy_bot --backup-name release-example
python remote_deploy.py rollback_bot --backup-name release-example
```

## 常见问题

| 情况 | 检查方法 |
| --- | --- |
| `Mounted user session ... is not authorized` | 完成对应账号登录，核对会话路径并重启 |
| `BOOTSTRAP_CODE must be at least 12 characters` | 使用至少 12 字符的随机校验码 |
| JSON 找不到或账号配置错误 | 检查挂载、文件名、连续槽位与配对配置 |
| 群内命令无反应 | 检查查询开关、Bot 权限、隐私模式；试用带 Bot 用户名的命令 |
| SQLite `database is locked` | 停止共享会话或数据库的其他进程，避免同时登录和运行 |
| 连接失败或代理不可达 | 检查 Telegram 连通性、SOCKS5 地址及容器访问路径 |
| 来源群没有出现在结果里 | 检查实际成员身份、访问权限、`CHEAT_GROUP_IDS` 与白名单 |

高级诊断没有预设真实目标：`bulk_probe.py` 使用 `BULK_PROBE_CHAT_ID`、`BULK_PROBE_USERNAME`；`diagnose_lookup.py` 使用 `DIAGNOSTIC_GROUP`、`DIAGNOSTIC_NAME_FRAGMENT`、`DIAGNOSTIC_TARGET_NAME`。需完整源码、已导入的环境变量和有效账号；先停止主服务再运行会话诊断。SSH 工具 `investigate` 必须传入 `--chat-id`。

## 开发与文件说明

```bash
python -m pip install -r requirements.txt
python -B -m unittest discover -v
```

现有测试使用模拟 Telegram 客户端和隔离数据库，发布检查中 178 项测试通过。核心文件：`bot.py`（入口与命令）、`account_pool.py`（账号调度）、`runtime_config.py`（配置）、`join_policy.py`（入群策略）、`group_imports.py`（导入）、`group_whitelist.py`（群白名单）。

真实 `.env`、账号 JSON、Telegram 会话、登录状态、SQLite 数据库、日志、私钥及备份保留在本地，已加入 Git / Docker 排除规则。仓库配置只含占位示例。
