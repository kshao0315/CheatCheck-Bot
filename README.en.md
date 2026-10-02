# CheatCheck Bot

[中文](README.md) · [English](README.en.md)

A Telegram group membership checking bot. It combines a bot session with one or more user account sessions to find groups a user shares with your configured sources. It provides individual lookups, bulk checks, whitelists, and group management.

**Your source configuration defines the scope.** Sources default to accessible regular groups and supergroups joined by your user accounts. Set `CHEAT_GROUP_IDS` to restrict them. A match indicates shared membership in a configured source; the operator selects sources and interprets results.

## Features

- Look up usernames and user IDs privately or in groups; reply to messages to check users without usernames.
- Run `/check all` with progress updates, private result delivery, and job resumption after restart.
- Query multiple accounts concurrently, deduplicate group IDs, and share rate limits and concurrency controls.
- Maintain user and source group whitelists; automatically check new members, message authors, and reaction users.
- Manage bot admins, group switches, bans, and source submissions and reviews.
- Chinese and English interfaces, with persistent SQLite storage and Telegram sessions.

## Prerequisites

Docker Engine / Docker Desktop with Docker Compose is recommended. The image uses Python 3.12; Python 3.12 is also recommended for direct execution.

1. Get a bot token from [@BotFather](https://t.me/BotFather).
2. Obtain an `api_id` and `api_hash` from [Telegram API development tools](https://my.telegram.org/apps). See the [official application guide](https://core.telegram.org/api/obtaining_api_id).
3. Have at least one registered Telegram user account that can receive login codes, plus its two-step verification password if enabled. User sessions and the bot token are configured separately.
4. Ensure Telegram connectivity; use `TELEGRAM_PROXY` when a proxy is needed.

## Quick start with Docker

### 1. Clone and prepare directories

Linux / macOS, using Bash:

```bash
git clone https://github.com/kshao0315/CheatCheck-Bot.git
cd CheatCheck-Bot
mkdir -p account data
cp .env.example .env
```

Windows PowerShell:

```powershell
git clone https://github.com/kshao0315/CheatCheck-Bot.git
Set-Location CheatCheck-Bot
New-Item -ItemType Directory -Force account, data | Out-Null
Copy-Item .env.example .env
```

### 2. Configure the bot and account

Edit `.env` and replace these placeholders:

```dotenv
BOT_TOKEN=replace_with_bot_token
BOOTSTRAP_CODE=replace_with_random_code
ACCOUNT_JSON=account/primary.json
ACCOUNT_SESSION=account/primary
```

`BOOTSTRAP_CODE` must contain at least 12 characters and is used to claim the first superadmin. Generate a random value and copy it into your local `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

Create `account/primary.json`, replacing the sample API configuration:

```json
{
  "api_id": 12345,
  "api_hash": "replace_with_your_api_hash"
}
```

`api_id` is a number. The main application also supports `app_id` / `app_hash`. `ACCOUNT_SESSION` omits the `.session` suffix. The sample enables only the first account; leave both second-account settings empty when unused.

### 3. Build and sign in to the user account

```bash
docker compose build
docker compose run --rm cheatcheck python -c "from telethon.sync import TelegramClient; import bot; c=bot.user_client_from_config(bot.proxy_config()); c.start(); c.disconnect()"
```

Enter the phone number, Telegram login code, and two-step verification password when prompted. This creates `account/primary.session` locally. The account should already belong to your intended source groups. An existing valid Telethon session at that path lets you skip sign-in. See [Telethon's sign-in documentation](https://docs.telethon.dev/en/stable/basic/signing-in.html).

### 4. Start the service

```bash
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

`Bot started as ...` confirms startup. `account/` stores user sessions; `data/` stores the bot session, SQLite database, and job state. Both host-mounted directories survive container recreation.

### 5. Initialize and run a check

1. Privately send `/start YOUR_BOOTSTRAP_CODE` to the bot. The first successful claimant becomes the superadmin; select a language when prompted.
2. Use `/add USER_ID` privately to authorize bot admins.
3. Add the bot to a target group. Bulk checks, automatic checks, and bans require the relevant admin permissions. Deleting query messages requires permission to delete messages.
4. Open `/group` privately to inspect and configure the group's query switch.
5. Send `/check @username` in the group, or reply to a message with `/check`.

If commands do not reach the bot, make it a group admin or adjust privacy mode through BotFather and re-add it when Telegram requests this. See [Telegram's privacy mode documentation](https://core.telegram.org/bots/features#privacy-mode). Admins receiving private results must first open a private chat with the bot.

## Configuration and multiple accounts

| Variable | Purpose |
| --- | --- |
| `BOT_TOKEN` | Bot token |
| `BOOTSTRAP_CODE` | Initial superadmin claim code, at least 12 characters |
| `ACCOUNT_JSON` / `ACCOUNT_SESSION` | First account's API configuration and session path |
| `ACCOUNT_JSON_2` / `ACCOUNT_SESSION_2` | Second account; leave both empty when unused |
| `BOT_SESSION` | Bot session path; default `data/bot` |
| `DATABASE` | SQLite path; default `data/bot.sqlite3` |
| `TELEGRAM_PROXY` | Optional SOCKS5 URL; empty means a direct connection |
| `CHEAT_GROUP_IDS` | Comma-separated numeric group IDs; empty uses accessible sources joined by accounts |
| `WATCHED_ADMIN_USERNAME` | Admin username that triggers manual joining; empty leaves this policy unconfigured |

The proxy URL uses `socks5://host:port` and may include a username and password. Inside a container, `localhost` refers to that container. For a Docker Desktop host proxy, use an address reachable from the container, such as `host.docker.internal`.

The sample policy account is a placeholder. To preserve an existing join policy, set its actual `WATCHED_ADMIN_USERNAME` locally. When that account appears in a source's admin list, accounts that have not joined pause automatic joining and notify the superadmin. Database setting `automatic_join_enabled=0` pauses automatic joining tasks and persists across restarts.

### Add a second account

First run `docker compose stop`. Create `account/secondary.json`, then set `ACCOUNT_JSON_2=account/secondary.json` and `ACCOUNT_SESSION_2=account/secondary` in `.env`. Sign in and restart:

```bash
docker compose run --rm -e ACCOUNT_JSON=account/secondary.json -e ACCOUNT_SESSION=account/secondary cheatcheck python -c "from telethon.sync import TelegramClient; import bot; c=bot.user_client_from_config(bot.proxy_config()); c.start(); c.disconnect()"
docker compose up -d
```

Use consecutive `_3`, `_4`, and later suffixes, configuring both JSON and SESSION for every slot. Keep existing slots stable. New accounts contribute coverage only after joining sources they can actually read.

### Performance settings

| Variable | Default |
| --- | ---: |
| `ACCOUNT_RPC_CONCURRENCY` | 4 |
| `ACCOUNT_MEMBER_CONCURRENCY` | 2 |
| `BULK_WORKERS_PER_ACCOUNT` | 4 |
| `BULK_WORKER_LIMIT` | 32 |
| `SOURCE_WORKER_LIMIT` | 32 |
| `QUERY_PARTIAL_CACHE_SECONDS` | 20 |
| `QUERY_CACHE_ENTRIES` | 4096 |

FloodWait cooldowns are shared per account and API operation. Increasing concurrency does not remove platform limits. New checks request all readable accounts' coverage again and merge successful results by group ID.

## Common commands

| Command | Purpose |
| --- | --- |
| `/start` | Guide, language selection, and role-specific menus |
| `/check @username`, `/check USER_ID` | User lookup; also supports replying with `/check` or `!check` |
| `/check all` | Admin-initiated bulk check; the bot must be a group admin |
| `/group` | Manage group query switches privately |
| `/auto check on` / `/auto check off` | Group-admin automatic check controls |
| `/whitelist add USER` / `/whitelist remove USER` | User whitelist |
| `/whitelist group groupname` | Source group exemption |
| `/whitelist group remove groupname` | Remove a source exemption |
| `/ban USER` / `/unban USER` | Ban or unban when both operator and bot have the required permissions |
| `/submit groupname` | Submit a source for admin review |
| `/add USER_ID` / `/revoke USER_ID` | Superadmin bot-admin authorization or revocation |
| `/admins`, `/logs`, `/logs clear` | Superadmin admin-list and query-log management |

Commands and buttons check permissions. Bulk results are sent privately to the superadmin and initiating admin, deduplicating recipients. Coverage depends on actual account access; inspect progress and failure records when groups cannot be fully enumerated or lookups fail.

## Run directly with Python

Prepare `.env`, `account/primary.json`, and the directories first. The application **does not automatically load `.env`**; import its values into the current terminal.

Linux / macOS:

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

Windows PowerShell, without requiring environment activation:

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

Choose either Docker or direct execution. Do not share one session file between running processes.

## Updates, backups, and server deployment

Update a container deployment:

```bash
git pull
docker compose up -d --build
docker compose logs --tail=100
```

For a consistent backup, run `docker compose stop`, copy `.env`, `account/`, and `data/` together into a protected backup directory, then run `docker compose up -d`. To restore, stop the service, restore all three parts from the same snapshot, and restart.

Servers can clone the repository and follow the Docker steps. The optional SSH tool `remote_deploy.py` requires local `paramiko` and process environment variables `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_HOST_FINGERPRINT`, and optionally `DEPLOY_REMOTE`. The server needs Docker / Compose; the SSH user needs deployment-directory and Docker permissions. Independently verify the SHA256 host key fingerprint. The password is entered interactively.

```bash
python -m pip install paramiko
python remote_deploy.py probe
python remote_deploy.py deploy
python remote_deploy.py status
```

The tool reads local `.env` to identify account files, while SSH settings come from the process environment. `deploy` uploads application files, `.env`, and configured user sessions to your specified server, preserving existing server-side account files. Review configuration and backups first. `rollback` restores the previous application, configuration, and data saved by the tool. Use the same backup name for bot-only deployment and rollback:

```bash
python remote_deploy.py deploy_bot --backup-name release-example
python remote_deploy.py rollback_bot --backup-name release-example
```

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `Mounted user session ... is not authorized` | Complete account sign-in, verify its session path, and restart |
| `BOOTSTRAP_CODE must be at least 12 characters` | Set a random code with at least 12 characters |
| Missing JSON or invalid account settings | Check mounts, filenames, consecutive slots, and paired values |
| No response to group commands | Check the query switch, permissions, privacy mode, and commands addressed to the bot username |
| SQLite `database is locked` | Stop other processes sharing the session or database; avoid concurrent sign-in and execution |
| Connection or proxy failure | Check Telegram connectivity, SOCKS5 settings, and container reachability |
| An expected source is missing | Check actual membership, access, `CHEAT_GROUP_IDS`, and whitelists |

Advanced diagnostics have no preset real targets. `bulk_probe.py` uses `BULK_PROBE_CHAT_ID` and `BULK_PROBE_USERNAME`; `diagnose_lookup.py` uses `DIAGNOSTIC_GROUP`, `DIAGNOSTIC_NAME_FRAGMENT`, and `DIAGNOSTIC_TARGET_NAME`. They require the complete source directory, exported environment variables, and valid accounts. Stop the main service before session diagnostics. SSH `investigate` requires `--chat-id`.

## Development and files

```bash
python -m pip install -r requirements.txt
python -B -m unittest discover -v
```

Existing tests use fake Telegram clients and isolated databases; 178 tests passed during publication checks. Main modules: `bot.py` (entry point and commands), `account_pool.py` (account scheduling), `runtime_config.py` (configuration), `join_policy.py` (join policy), `group_imports.py` (imports), and `group_whitelist.py` (group exemptions).

Real `.env` files, account JSON, Telegram sessions, login state, SQLite databases, logs, private keys, and backups stay local and are excluded by Git and Docker rules. Repository configuration contains placeholders only.
