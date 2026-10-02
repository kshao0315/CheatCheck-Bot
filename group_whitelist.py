"""Persistent source-group exemptions, scoped management and read-only resolution."""
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from telethon import errors, functions, types, utils


def exempt_source_ids(db):
    if db is None:return set()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_check_whitelist'").fetchone():
        return set()
    return {row[0] for row in db.execute('SELECT group_id FROM group_check_whitelist')}


def filter_check_groups(db, groups):
    exempt=exempt_source_ids(db)
    return [group for group in groups if group['id'] not in exempt]


def filter_bulk_hits(outcome, source_hits, exempt):
    outcome['groups']={gid:group for gid,group in outcome['groups'].items() if gid not in exempt}
    return (set(outcome['groups']) | set(source_hits))-exempt


class GroupWhitelist:
    def __init__(self, db):
        self.db=db
        db.execute('''CREATE TABLE IF NOT EXISTS group_check_whitelist(
            group_id INTEGER PRIMARY KEY,title TEXT NOT NULL,username TEXT NOT NULL DEFAULT '',
            added_by INTEGER NOT NULL,created_at TEXT NOT NULL)''')
        db.commit()

    def add(self, entity, actor_id):
        gid=utils.get_peer_id(entity)
        with self.db:
            cursor=self.db.execute('INSERT OR IGNORE INTO group_check_whitelist VALUES(?,?,?,?,?)',
                (gid,entity.title,getattr(entity,'username',None) or '',actor_id,
                 datetime.now(timezone.utc).isoformat(timespec='seconds')))
        return bool(cursor.rowcount)

    def remove(self, gid, actor_id, superadmin=False):
        row=self.db.execute('SELECT added_by FROM group_check_whitelist WHERE group_id=?',(gid,)).fetchone()
        if row is None:return False
        if not superadmin and row[0]!=actor_id:raise PermissionError('group whitelist ownership')
        with self.db:self.db.execute('DELETE FROM group_check_whitelist WHERE group_id=?',(gid,))
        return True

    def rows(self, actor_id, superadmin=False, page=0):
        if superadmin:
            return self.db.execute('SELECT group_id,title,username FROM group_check_whitelist ORDER BY created_at DESC,group_id LIMIT 21 OFFSET ?',
                                   (max(0,page)*20,)).fetchall()
        return self.db.execute('SELECT group_id,title,username FROM group_check_whitelist WHERE added_by=? ORDER BY created_at DESC,group_id LIMIT 21 OFFSET ?',
                               (actor_id,max(0,page)*20)).fetchall()


def parse_group_reference(raw):
    value=raw.strip()
    if re.fullmatch(r'-\d+',value):return 'id',int(value)
    if value.startswith(('t.me/','telegram.me/')):value='https://'+value
    if '://' in value:
        url=urlsplit(value)
        if url.scheme not in ('https','http') or url.netloc.lower() not in ('t.me','telegram.me'):
            raise ValueError('invalid Telegram group link')
        parts=url.path.strip('/').split('/')
        if parts[0]=='c' and len(parts)>1 and parts[1].isdigit():
            return 'id',int('-100'+parts[1])
        if parts[0]=='joinchat' and len(parts)==2:value='+'+parts[1]
        elif parts[0]=='s' and len(parts)>1:value=parts[1]
        else:value=parts[0]
    if value.startswith('+') and re.fullmatch(r'[A-Za-z0-9_-]+',value[1:]):
        return 'invite',value[1:]
    value=value.lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}',value):
        raise ValueError('invalid Telegram group reference')
    return 'username',value.lower()


async def resolve_whitelist_group(raw, clients):
    kind,value=parse_group_reference(raw)
    async def read(client):
        try:
            if kind=='invite':
                result=await asyncio.wait_for(client(functions.messages.CheckChatInviteRequest(value)),8)
                entity=result.chat if isinstance(result,types.ChatInviteAlready) else None
            else:entity=await asyncio.wait_for(client.get_entity(value),8)
            if isinstance(entity,types.Chat) or (isinstance(entity,types.Channel) and entity.megagroup):
                return entity
        except (ValueError,TypeError,errors.RPCError,OSError,asyncio.TimeoutError):pass
        return None
    results=await asyncio.gather(*(read(client) for client in clients))
    entity=next((result for result in results if result is not None),None)
    if entity is None:raise ValueError('group not resolved without joining')
    return entity
