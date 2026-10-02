"""Deploy this project over SSH with a pinned server key (run locally)."""

import argparse
import base64
import getpass
import hashlib
import os
from datetime import datetime, timezone
import re
import shlex
from pathlib import Path

import paramiko


HOST = os.getenv("DEPLOY_HOST", "").strip()
HOST_FINGERPRINT = os.getenv("DEPLOY_HOST_FINGERPRINT", "").strip()
REMOTE = os.getenv("DEPLOY_REMOTE", "/opt/cheatcheck").strip()
DEPLOY_USER = os.getenv("DEPLOY_USER", "deploy").strip()
ROOT = Path(__file__).resolve().parent
FILES = ["bot.py", "account_pool.py", "runtime_config.py", "join_policy.py", "group_imports.py", "join_control.py", "group_whitelist.py", "requirements.txt", "Dockerfile", "compose.yaml", ".env"]
SUPPORT_FILES = ["sync_secondary_groups.py"]
def configured_account_files():
    paths = []
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        key, separator, value = line.partition('=')
        if separator and re.fullmatch(r'ACCOUNT_(?:JSON|SESSION)(?:_\d+)?', key.strip()):
            value = value.strip().strip('"').strip("'")
            if not value:
                continue
            path = Path(value)
            paths.append(path.name + ('.session' if 'SESSION' in key else ''))
    return list(dict.fromkeys(paths))

def fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        actual = fingerprint(key)
        if actual != HOST_FINGERPRINT:
            raise paramiko.SSHException(f"Server fingerprint mismatch: {actual}")
        client.get_host_keys().add(hostname, key.get_name(), key)


def command(client, cmd, *, check=True, timeout=180):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    status = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    print(f"$ {cmd}\nexit={status}\n{out}\n{err}")
    if check and status:
        raise RuntimeError(f"Remote command failed: {cmd}")
    return status


def upload(sftp, local, remote):
    sftp.put(str(local), remote)
    print(f"Uploaded {local.name} -> {remote}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["probe", "deploy", "deploy_bot", "rollback_bot", "status", "api", "rollback", "diagnose", "protection_status", "member_status", "bulk_status", "bulk_diagnose", "bulk_probe", "queue_bulk", "investigate", "sync_secondary", "secondary_report", "health", "common_status", "probe_username", "submission_status", "language_status"])
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--username")
    parser.add_argument("--actor-id", type=int)
    parser.add_argument("--bridge", action="store_true")
    parser.add_argument("--backup-name", default="backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--expected-hash")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]+", args.backup_name):
        parser.error("Invalid backup name")
    if args.expected_hash and not re.fullmatch(r"[a-fA-F0-9]{64}", args.expected_hash):
        parser.error("Invalid expected SHA256")
    if args.action == "investigate" and args.chat_id is None:
        parser.error("investigate requires --chat-id")
    if not HOST or not DEPLOY_USER:
        parser.error("Set DEPLOY_HOST and DEPLOY_USER in the local environment")
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", HOST_FINGERPRINT):
        parser.error("Set DEPLOY_HOST_FINGERPRINT to the independently verified SHA256 host key fingerprint")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", REMOTE) or ".." in Path(REMOTE).parts:
        parser.error("DEPLOY_REMOTE must be an absolute path without spaces or shell metacharacters")
    account_files = configured_account_files() if args.action == "deploy" else []
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey())
    client.connect(HOST, username=DEPLOY_USER, password=getpass.getpass("SSH password: "),
                   look_for_keys=False, allow_agent=False, timeout=15)
    try:
        if args.action == "probe":
            command(client, "id && docker --version && docker compose version")
            command(client, f"if test -d {REMOTE}; then ls -la {REMOTE}; else echo 'Deployment directory absent'; fi")
        elif args.action == "deploy":
            status = command(client, f"test -f {REMOTE}/bot.py", check=False)
            if status == 0:
                command(client, f"cd {REMOTE} && docker compose stop")
                command(client, f"mkdir -p {REMOTE}/previous/data")
                for filename in FILES:
                    command(client, f"cp {REMOTE}/{filename} {REMOTE}/previous/{filename}")
                command(client, f"cp -a {REMOTE}/data/. {REMOTE}/previous/data/")
            command(client, f"mkdir -p {REMOTE}/account {REMOTE}/data && chmod 700 {REMOTE} {REMOTE}/account {REMOTE}/data")
            sftp = client.open_sftp()
            try:
                for filename in FILES:
                    upload(sftp, ROOT / filename, f"{REMOTE}/{filename}")
                for filename in SUPPORT_FILES:
                    upload(sftp, ROOT / filename, f"{REMOTE}/{filename}")
                for filename in account_files:
                    destination = f"{REMOTE}/account/{filename}"
                    try:
                        sftp.stat(destination)
                    except FileNotFoundError:
                        upload(sftp, ROOT / "account" / filename, destination)
                    else:
                        print(f"Preserved existing {destination}")
                sftp.chmod(f"{REMOTE}/.env", 0o600)
                for filename in account_files:
                    sftp.chmod(f"{REMOTE}/account/{filename}", 0o600)
            finally:
                sftp.close()
            command(client, f"cd {REMOTE} && docker compose up -d --build")
            command(client, f"cd {REMOTE} && docker compose ps")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --tail=30")
        elif args.action == "deploy_bot":
            backup = f"{REMOTE}/backups/{args.backup_name}"
            command(client, f"cd {REMOTE} && sha256sum bot.py")
            if args.expected_hash:
                command(client, f"cd {REMOTE} && test \"$(sha256sum bot.py | cut -d ' ' -f1)\" = {args.expected_hash.lower()}")
            command(client, f"mkdir -p {backup} && if test ! -e {backup}/bot.py; then cp {REMOTE}/bot.py {backup}/bot.py; fi")
            sftp = client.open_sftp()
            try:
                for module in ('bot.py','account_pool.py','runtime_config.py','join_policy.py','group_imports.py','join_control.py','group_whitelist.py','Dockerfile'):
                    upload(sftp, ROOT / module, f"{REMOTE}/{module}")
                upload(sftp, ROOT / "test_bulk_lookup.py", f"{REMOTE}/test_bulk_lookup.py")
                if (ROOT / "test_submissions.py").exists():
                    upload(sftp, ROOT / "test_submissions.py", f"{REMOTE}/test_submissions.py")
                if (ROOT / "test_language.py").exists():
                    upload(sftp, ROOT / "test_language.py", f"{REMOTE}/test_language.py")
                if (ROOT / "test_bulk_parallel.py").exists():
                    upload(sftp, ROOT / "test_bulk_parallel.py", f"{REMOTE}/test_bulk_parallel.py")
                if (ROOT / "test_onboarding.py").exists():
                    upload(sftp, ROOT / "test_onboarding.py", f"{REMOTE}/test_onboarding.py")
            finally:
                sftp.close()
            try:
                command(client, f"cd {REMOTE} && docker compose build", timeout=240)
                command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/test_bulk_lookup.py:/app/test_bulk_lookup.py cheatcheck python -m unittest -v test_bulk_lookup")
                if (ROOT / "test_bulk_parallel.py").exists():
                    command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/test_bulk_parallel.py:/app/test_bulk_parallel.py cheatcheck python -m unittest -v test_bulk_parallel")
                if (ROOT / "test_submissions.py").exists():
                    command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/test_submissions.py:/app/test_submissions.py cheatcheck python -m unittest -v test_submissions")
                if (ROOT / "test_language.py").exists():
                    command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/test_submissions.py:/app/test_submissions.py -v {REMOTE}/test_language.py:/app/test_language.py cheatcheck python -m unittest -v test_language")
                if (ROOT / "test_onboarding.py").exists():
                    command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/test_submissions.py:/app/test_submissions.py -v {REMOTE}/test_onboarding.py:/app/test_onboarding.py cheatcheck python -m unittest -v test_onboarding")
                command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T cheatcheck python -m py_compile bot.py")
            except Exception:
                command(client, f"cp {backup}/bot.py {REMOTE}/bot.py")
                command(client, f"cd {REMOTE} && docker compose build", timeout=240)
                raise
            if args.chat_id is not None:
                script = f'''import datetime, os, sqlite3
db=sqlite3.connect(os.getenv('DATABASE','data/bot.sqlite3'))
actor=int(db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()[0])
requested_actor={args.actor_id!r}
actor=actor if requested_actor is None else requested_actor
owner=db.execute("SELECT owner_id FROM groups WHERE chat_id=?",({args.chat_id},)).fetchone()
superadmin=int(db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()[0])
if actor != superadmin and (not db.execute('SELECT 1 FROM admins WHERE user_id=?',(actor,)).fetchone() or not owner or owner[0] != actor):
    raise RuntimeError('Requested actor has no permission to check this group')
now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
db.execute("INSERT INTO bulk_jobs(chat_id,actor_id,phase,started_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO NOTHING",({args.chat_id},actor,'等待验证',now,now))
db.commit()
print('queued verification:', {args.chat_id})
'''
                command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose up -d", timeout=120)
            command(client, f"cd {REMOTE} && sha256sum bot.py && docker compose ps")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --tail=20")
        elif args.action == "rollback_bot":
            backup = f"{REMOTE}/backups/{args.backup_name}/bot.py"
            command(client, f"test -f {backup} && cp {backup} {REMOTE}/bot.py")
            command(client, f"cd {REMOTE} && docker compose up -d --build", timeout=240)
            command(client, f"cd {REMOTE} && sha256sum bot.py && docker compose ps")
        elif args.action == "language_status":
            script = '''import json,os,sqlite3,bot
db=sqlite3.connect(os.getenv('DATABASE','data/bot.sqlite3'))
print('user_languages:',db.execute('SELECT language,COUNT(*) FROM user_languages GROUP BY language').fetchall())
print('group_languages:',db.execute('SELECT language,COUNT(*) FROM groups GROUP BY language').fetchall())
print('groups_schema:',[r[1] for r in db.execute('PRAGMA table_info(groups)')])
for label,scope,code in [('private_en',{'type':'default'},'en'),('groups_en',{'type':'all_group_chats'},'en')]:
    try:
        result=bot.bot_api_sync(os.environ['BOT_TOKEN'],'getMyCommands',{'scope':scope,'language_code':code})
        print(label+':',json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print(label+'_error:',type(exc).__name__)
'''
            command(client, f"cd {REMOTE} && sha256sum bot.py && docker compose ps")
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=5m --tail=45")
        elif args.action == "submission_status":
            script = '''import json, os, sqlite3, bot
db=sqlite3.connect(os.getenv('DATABASE','data/bot.sqlite3'))
for table in ('source_submissions','submission_messages','source_join_jobs','cheat_sources'):
    print(table, db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])
print('requests:',json.dumps(db.execute('SELECT id,username,status,reviewer_id FROM source_submissions ORDER BY id DESC LIMIT 10').fetchall(),ensure_ascii=False))
print('join_jobs:',json.dumps(db.execute('SELECT request_id,account_index,state,detail,retry_at FROM source_join_jobs ORDER BY request_id DESC LIMIT 20').fetchall(),ensure_ascii=False))
print('bulk_jobs:',db.execute('SELECT chat_id,actor_id,phase FROM bulk_jobs').fetchall())
scopes=[('default',{'type':'default'}),('groups',{'type':'all_group_chats'})]
owner=db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()
admin=db.execute('SELECT user_id FROM admins LIMIT 1').fetchone()
if owner: scopes.append(('superadmin',{'type':'chat','chat_id':int(owner[0])}))
if admin: scopes.append(('admin',{'type':'chat','chat_id':admin[0]}))
for label,scope in scopes:
    try:
        commands=bot.bot_api_sync(os.environ['BOT_TOKEN'],'getMyCommands',{'scope':scope})
        print(label+'_commands:',[c['command'] for c in commands])
    except Exception as exc:
        print(label+'_commands_error:',type(exc).__name__)
'''
            command(client, f"cd {REMOTE} && sha256sum bot.py && docker compose ps")
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=5m --tail=40")
        elif args.action == "diagnose":
            command(client, f"cd {REMOTE} && docker compose stop")
            try:
                sftp = client.open_sftp()
                try:
                    upload(sftp, ROOT / "diagnose_lookup.py", f"{REMOTE}/diagnose_lookup.py")
                finally:
                    sftp.close()
                command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/diagnose_lookup.py:/app/diagnose_lookup.py cheatcheck python diagnose_lookup.py", timeout=900)
            finally:
                command(client, f"cd {REMOTE} && docker compose up -d")
        elif args.action == "sync_secondary":
            command(client, f"cd {REMOTE} && docker compose stop")
            try:
                command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T -v {REMOTE}/sync_secondary_groups.py:/app/sync_secondary_groups.py cheatcheck python sync_secondary_groups.py", timeout=1200)
            finally:
                command(client, f"cd {REMOTE} && docker compose up -d")
        elif args.action == "secondary_report":
            script = """import collections, json
d=json.load(open('data/secondary_sync.json', encoding='utf-8'))
print({k:len(v) for k,v in d.items() if isinstance(v,list)})
print('totals:', {k:d.get(k) for k in ('primary_total','secondary_joined','updated_at')})
print('failure_reasons:', dict(collections.Counter(item.get('reason','unknown') for item in d.get('failed',[]))))
print('pending_private:', d.get('pending_private',[]))
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
        elif args.action == "health":
            script = """import json, os, sqlite3
db=sqlite3.connect(os.getenv('DATABASE','data/bot.sqlite3'))
tables={name: db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('groups','admins','queries','whitelist','bans','expiring_messages')}
recent=db.execute("SELECT method,status,COUNT(*) FROM queries WHERE created_at >= datetime('now','-24 hours') GROUP BY method,status ORDER BY method,status").fetchall()
print('tables:', json.dumps(tables, ensure_ascii=False))
print('recent_queries_24h:', json.dumps(recent, ensure_ascii=False))
print('settings:', json.dumps(db.execute("SELECT key,value FROM settings WHERE key='superadmin'").fetchall(), ensure_ascii=False))
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=30m | grep -E 'ERROR|Traceback|Bot started|Loaded [0-9]+ source groups|Secondary account joined|Secondary group sync waiting' | tail -100", check=False)
        elif args.action == "common_status":
            script = """import json, os, sqlite3
db=sqlite3.connect(os.getenv('DATABASE','data/bot.sqlite3'))
rows=db.execute("SELECT created_at,chat_id,target_id,method,status,matches,details_json FROM queries WHERE status='完成' AND method IN ('群组check指令','bot私聊','群组auto消息','群组auto表情','群组auto入群') ORDER BY id DESC LIMIT 12").fetchall()
summary=db.execute("SELECT method,status,COUNT(*),SUM(CASE WHEN matches IS NOT NULL THEN 1 ELSE 0 END) FROM queries WHERE created_at >= datetime('now','-24 hours') AND method IN ('群组check指令','bot私聊','群组auto消息','群组auto表情','群组auto入群') GROUP BY method,status ORDER BY method,status").fetchall()
print('summary:', json.dumps(summary, ensure_ascii=False))
for created,chat_id,target_id,method,status,matches,details in rows:
    try: detail_count=len(json.loads(details or '[]'))
    except Exception: detail_count=-1
    print('completed:', json.dumps([created,chat_id,target_id,method,matches,detail_count], ensure_ascii=False))
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=30m | grep -E 'Common group lookup failed|FloodWait|Flood wait|ERROR|Traceback' | tail -80", check=False)
        elif args.action == "probe_username":
            if not args.username:
                parser.error("probe_username requires --username")
            username = args.username.lstrip("@")
            script = f"""import asyncio, json
import bot
from telethon import utils, types
from telethon.tl.functions.messages import GetCommonChatsRequest

async def main():
    result = {{'username': {username!r}, 'accounts': []}}
    for index, client in enumerate(bot.user_clients_from_config(bot.proxy_config()), 1):
      label = "account" + str(index)
      await client.connect()
      account = {{'label': label}}
      try:
        try:
            entity = await asyncio.wait_for(client.get_entity({username!r}), timeout=30)
            account['resolved'] = {{'id': entity.id, 'username': getattr(entity, 'username', None), 'type': type(entity).__name__}}
            if not isinstance(entity, types.User):
                account['error'] = 'Resolved entity is not a user'
            else:
                common = await asyncio.wait_for(client(GetCommonChatsRequest(
                    user_id=utils.get_input_user(entity), max_id=0, limit=100)), timeout=30)
                sources = set()
                async for dialog in client.iter_dialogs():
                    if isinstance(dialog.entity, (types.Channel, types.Chat)) and (not isinstance(dialog.entity, types.Channel) or dialog.entity.megagroup):
                        sources.add(bot.telegram_id(dialog.entity))
                matches = [{{'id': bot.telegram_id(chat), 'title': bot.group_title(chat)}} for chat in common.chats if bot.telegram_id(chat) in sources]
                account['common_chats_returned'] = len(common.chats)
                account['source_matches'] = matches
        except Exception as exc:
            account['error'] = type(exc).__name__ + ': ' + str(exc)
      finally:
        result['accounts'].append(account)
        await client.disconnect()
    print(json.dumps(result, ensure_ascii=False))

asyncio.run(main())
"""
            command(client, f"cd {REMOTE} && docker compose stop")
            try:
                command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T cheatcheck python -c {shlex.quote(script)}", timeout=120)
            finally:
                command(client, f"cd {REMOTE} && docker compose up -d")
        elif args.action == "status":
            command(client, f"cd {REMOTE} && docker compose ps")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --tail=50")
        elif args.action == "investigate":
            script = "chat_id = " + str(args.chat_id) + "\n" + """import json, os, sqlite3
db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
print('group_config:', json.dumps(db.execute('SELECT chat_id,title,owner_id,enabled FROM groups WHERE chat_id=?', (chat_id,)).fetchone(), ensure_ascii=False))
print('recent_queries:', json.dumps(db.execute('SELECT created_at,requester_id,target_id,method,status,matches FROM queries WHERE chat_id=? ORDER BY id DESC LIMIT 20', (chat_id,)).fetchall(), ensure_ascii=False))
print('recent_expiring:', json.dumps(db.execute('SELECT message_id,expires_at FROM expiring_messages WHERE chat_id=? ORDER BY expires_at DESC LIMIT 20', (chat_id,)).fetchall(), ensure_ascii=False))
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=1h | tail -100", check=False)
        elif args.action == "api":
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c \"import os,bot; print(bot.bot_api_sync(os.environ['BOT_TOKEN'],'getMe',{{}})['username'])\"")
        elif args.action == "protection_status":
            if args.chat_id is None:
                parser.error("protection_status requires --chat-id")
            script = f"""import json, os, sqlite3, time, bot
chat_id = {args.chat_id}
db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
superadmin = db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()
api = lambda method, payload: bot.bot_api_sync(os.environ['BOT_TOKEN'], method, payload)
bot_id = api('getMe', {{}})['id']
result = {{'chat_id': chat_id, 'registered': db.execute('SELECT enabled FROM groups WHERE chat_id=?', (chat_id,)).fetchone()}}
for label, method, payload in (
    ('chat', 'getChat', {{'chat_id': chat_id}}),
    ('bot_member', 'getChatMember', {{'chat_id': chat_id, 'user_id': bot_id}}),
    ('superadmin_member', 'getChatMember', {{'chat_id': chat_id, 'user_id': int(superadmin[0])}}),
    ('webhook', 'getWebhookInfo', {{}}),
):
    for attempt in range(3 if label == 'superadmin_member' else 1):
        try:
            data = api(method, payload)
            result[label] = {{key: data.get(key) for key in ('id', 'title', 'status', 'is_member', 'permissions', 'can_restrict_members', 'can_send_messages', 'can_send_other_messages', 'until_date', 'url', 'allowed_updates') if key in data}}
            break
        except Exception as exc:
            result[label] = {{'error': str(exc)}}
            time.sleep(1)
print(json.dumps(result, ensure_ascii=False))
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
        elif args.action == "member_status":
            if args.chat_id is None:
                parser.error("member_status requires --chat-id")
            script = f"""import json, os, sqlite3, time, bot
chat_id = {args.chat_id}
db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
superadmin = int(db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()[0])
for attempt in range(4):
    try:
        member = bot.bot_api_sync(os.environ['BOT_TOKEN'], 'getChatMember',
            {{'chat_id': chat_id, 'user_id': superadmin}})
        print(json.dumps({{'status': member['status'], 'can_send_messages': member.get('can_send_messages')}}))
        break
    except RuntimeError as exc:
        if attempt == 3:
            raise
        time.sleep(2)
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
        elif args.action == "bulk_probe":
            if not args.bridge:
                command(client, f"cd {REMOTE} && docker compose logs --no-color --since=96h | grep -E 'Direct bulk|Source .*delayed|Could not enumerate source|Bulk scan failed|Could not read target|Resuming bulk' | tail -100", check=False)
            sftp=client.open_sftp()
            try:
                upload(sftp, ROOT / 'bulk_probe.py', f'{REMOTE}/bulk_probe.py')
            finally:
                sftp.close()
            command(client, f"cd {REMOTE} && docker compose stop")
            try:
                env_option = '-e BULK_BRIDGE_PROBE=1 ' if args.bridge else ''
                command(client, f"cd {REMOTE} && docker compose run --rm --no-deps -T {env_option}-v {REMOTE}/bulk_probe.py:/app/bulk_probe.py cheatcheck python bulk_probe.py", timeout=240)
            finally:
                command(client, f"cd {REMOTE} && docker compose up -d")
        elif args.action == "bulk_diagnose":
            if not args.username:
                parser.error("bulk_diagnose requires --username")
            script = f'''import json, os, sqlite3, bot
db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
chat_id = None
for key in ['BOT_SESSION'] + [keys[1] for keys in bot.account_config_keys()]:
    path=os.getenv(key)
    if not path: continue
    session=sqlite3.connect('file:'+path+'.session?mode=ro',uri=True)
    row=session.execute('SELECT id,name FROM entities WHERE username=?',({args.username.lstrip('@')!r},)).fetchone()
    if row:
        chat_id=row[0]
        print('chat_cache:', key, json.dumps(row,ensure_ascii=False))
    session.close()
if chat_id is None: raise RuntimeError('Target group is not cached')
print('jobs:', json.dumps(db.execute('SELECT chat_id,actor_id,phase,done,total,started_at,updated_at FROM bulk_jobs').fetchall(),ensure_ascii=False))
print('runs:', json.dumps(db.execute("SELECT created_at,COUNT(*),SUM(status='失败'),SUM(matches>0),SUM(status='完成') FROM queries WHERE chat_id=? AND method='群组check all' GROUP BY created_at ORDER BY created_at DESC LIMIT 8",(chat_id,)).fetchall(),ensure_ascii=False))
stamp = db.execute("SELECT MAX(created_at) FROM queries WHERE chat_id=? AND method='群组check all'",(chat_id,)).fetchone()[0]
failed = [r[0] for r in db.execute("SELECT target_id FROM queries WHERE chat_id=? AND method='群组check all' AND created_at=? AND status='失败'",(chat_id,stamp))]
print('failed_sample:', failed[:12])
for index,keys in enumerate(bot.account_config_keys(),1):
    label,key='account'+str(index),keys[1]
    if not os.getenv(key): continue
    session=sqlite3.connect('file:'+os.environ[key]+'.session?mode=ro',uri=True)
    cached={{r[0]:r[1] for r in session.execute('SELECT id,username FROM entities WHERE id>0')}}
    print(label+'_failed_cached:', sum(uid in cached for uid in failed))
    print(label+'_failed_cached_with_username:',sum(bool(cached.get(uid)) for uid in failed))
    print(label+'_target_group_cached:', bool(session.execute('SELECT 1 FROM entities WHERE id=?', (chat_id,)).fetchone()))
print('account_sync:',json.dumps(json.load(open('data/account_sync.json',encoding='utf-8')),ensure_ascii=False))
'''
            command(client, f"cd {REMOTE} && sha256sum bot.py")
            command(client, f"if test -f {REMOTE}/backups/{args.backup_name}/bot.py; then sha256sum {REMOTE}/backups/{args.backup_name}/bot.py; fi")
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=96h | grep -E 'Direct bulk|Source .*delayed|Could not enumerate source|Bulk scan failed|Could not read target|Resuming bulk' | tail -120", check=False)
        elif args.action == "bulk_status":
            script = """import json, os, sqlite3
db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
print('jobs:', json.dumps(db.execute("SELECT chat_id,actor_id,phase,done,total,eta_seconds,started_at,updated_at FROM bulk_jobs").fetchall(), ensure_ascii=False))
print(json.dumps(db.execute("SELECT chat_id, MIN(created_at), MAX(created_at), COUNT(*), SUM(status='完成'), SUM(status='失败') FROM queries WHERE method='群组check all' AND created_at >= datetime('now','-2 hours') GROUP BY chat_id").fetchall(), ensure_ascii=False))
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
            command(client, f"cd {REMOTE} && docker compose logs --no-color --since=2h | grep -E 'Indexed|Source|Flood|flood|Bulk' | tail -30", check=False)
        elif args.action == "queue_bulk":
            if args.chat_id is None:
                parser.error("queue_bulk requires --chat-id")
            script = f"""import os, sqlite3, datetime
db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
db.execute('CREATE TABLE IF NOT EXISTS bulk_jobs (chat_id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, phase TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, eta_seconds INTEGER, started_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
actor_id = int(db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()[0])
requested_actor={args.actor_id!r}
actor_id=actor_id if requested_actor is None else requested_actor
owner=db.execute("SELECT owner_id FROM groups WHERE chat_id=?",({args.chat_id},)).fetchone()
superadmin=int(db.execute("SELECT value FROM settings WHERE key='superadmin'").fetchone()[0])
if actor_id != superadmin and (not db.execute('SELECT 1 FROM admins WHERE user_id=?',(actor_id,)).fetchone() or not owner or owner[0] != actor_id):
    raise RuntimeError('Requested actor has no permission to check this group')
now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
db.execute('INSERT OR REPLACE INTO bulk_jobs(chat_id,actor_id,phase,started_at,updated_at) VALUES(?,?,?,?,?)', ({args.chat_id}, actor_id, '待重启', now, now))
db.commit()
print('queued', {args.chat_id})
"""
            command(client, f"cd {REMOTE} && docker compose exec -T cheatcheck python -c {shlex.quote(script)}")
        else:
            command(client, f"cd {REMOTE} && docker compose down")
            command(client, f"test -f {REMOTE}/previous/bot.py")
            for filename in FILES:
                command(client, f"cp {REMOTE}/previous/{filename} {REMOTE}/{filename}")
            command(client, f"cp -a {REMOTE}/previous/data/. {REMOTE}/data/")
            command(client, f"cd {REMOTE} && docker compose up -d --build")
    finally:
        client.close()


if __name__ == "__main__":
    main()
