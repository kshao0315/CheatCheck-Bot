"""Validated CSV input and a durable import queue owned by the running bot."""
import asyncio
import csv
import hashlib
import io
import time
from types import SimpleNamespace
from telethon import errors, types, utils
from join_control import automatic_join_enabled


def csv_usernames(data):
    import re
    result=[];seen=set()
    for line,row in enumerate(csv.reader(io.StringIO(data.decode('utf-8-sig'))),1):
        if not row or not any(part.strip() for part in row):continue
        if len(row)!=1:raise ValueError(f'CSV line {line} must contain one group username')
        username=row[0].strip().lstrip('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}',username):
            raise ValueError(f'Invalid group username on CSV line {line}')
        if username.casefold() not in seen:
            seen.add(username.casefold());result.append(username)
    return result


class GroupImportQueue:
    def __init__(self,db):
        self.db=db
        db.executescript('''CREATE TABLE IF NOT EXISTS source_group_imports(
            batch_id TEXT NOT NULL,username TEXT NOT NULL,actor_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',group_id INTEGER,request_id INTEGER,
            retry_at INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(batch_id,username));
            CREATE TABLE IF NOT EXISTS source_import_settings(key TEXT PRIMARY KEY,value INTEGER NOT NULL);''')
        db.commit()

    def enqueue(self,data,actor_id):
        usernames=csv_usernames(data);batch=hashlib.sha256(data).hexdigest()
        active=automatic_join_enabled(self.db)
        with self.db:self.db.executemany('INSERT OR IGNORE INTO source_group_imports(batch_id,username,actor_id,state,detail) VALUES(?,?,?,?,?)',
                                       [(batch,username.casefold(),actor_id,'queued' if active else 'closed',
                                         '' if active else '用户已停止自动入群') for username in usernames])
        return batch,len(usernames)

    async def cycle(self,submissions,*,limit=8):
        if not automatic_join_enabled(self.db):
            return 0
        cooldown=self.db.execute("SELECT value FROM source_import_settings WHERE key='resolve_until'").fetchone()
        if cooldown and cooldown[0]>time.time():return 0
        rows=self.db.execute("SELECT batch_id,username,actor_id FROM source_group_imports WHERE state IN ('queued','retry','waiting_limit') AND retry_at<=? LIMIT ?",
                             (int(time.time()),limit)).fetchall()
        processed=0
        for batch,username,actor_id in rows:
            if not automatic_join_enabled(self.db):
                break
            group_id=request_id=None;retry_at=0
            try:
                entity=await asyncio.wait_for(submissions.bot.get_entity(username),15)
                if not isinstance(entity,types.Channel) or not entity.megagroup:
                    state,detail='not_group','Username is not a supergroup'
                else:
                    group_id=utils.get_peer_id(entity)
                    old=self.db.execute("SELECT id,status FROM source_submissions WHERE group_id=? AND status IN ('pending','approved') ORDER BY id DESC LIMIT 1",(group_id,)).fetchone()
                    actor=SimpleNamespace(id=actor_id,first_name='CSV import',last_name=None,username=None)
                    if old:
                        request_id=old[0]
                        if old[1]=='pending':submissions.decide(request_id,actor,'approved')
                    else:request_id=submissions.create(entity,username,actor,approved=True,allow_registered=True)
                    needs_join=self.db.execute("SELECT 1 FROM source_join_jobs WHERE request_id=? AND state NOT IN ('joined','closed','manual_required') LIMIT 1",(request_id,)).fetchone()
                    if needs_join:
                        await submissions.policy.allow(0,entity,submissions.rpc,account_entity=False)
                    state,detail='registered','Both account jobs registered; pre-join admin check required'
            except errors.FloodWaitError as exc:
                state,detail,retry_at='waiting_limit',type(exc).__name__,int(time.time())+exc.seconds+2
                with self.db:self.db.execute("INSERT OR REPLACE INTO source_import_settings VALUES('resolve_until',?)",(retry_at,))
            except (errors.UsernameInvalidError,errors.UsernameNotOccupiedError):
                state,detail='invalid','Group username unavailable'
            except PermissionError:
                state,detail='permission_error','Import actor is no longer an administrator'
            except ValueError as exc:
                state,detail='invalid',str(exc)[:200]
            except (TypeError,errors.RPCError,OSError,asyncio.TimeoutError) as exc:
                state,detail,retry_at='retry',type(exc).__name__,int(time.time())+600
            with self.db:self.db.execute('UPDATE source_group_imports SET state=?,group_id=?,request_id=?,retry_at=?,detail=? WHERE batch_id=? AND username=?',
                                         (state,group_id,request_id,retry_at,detail,batch,username))
            processed+=1
            if state=='waiting_limit':break
        return processed
