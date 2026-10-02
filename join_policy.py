"""Inspect admins before joining; persist groups that require a human."""
import asyncio
import logging
import os
import time
from telethon import errors, functions, types, utils


class PreJoinPolicy:
    WATCHED_USERNAME=os.getenv("WATCHED_ADMIN_USERNAME", "").strip().lstrip("@").casefold()

    def __init__(self,db,clients,wake):
        self.db,self.clients,self.wake=db,list(clients),wake
        self.locks={}
        db.execute('''CREATE TABLE IF NOT EXISTS source_join_policy_holds(
            group_id INTEGER PRIMARY KEY,title TEXT NOT NULL,username TEXT NOT NULL,
            reason TEXT NOT NULL,detail TEXT NOT NULL,notified INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL)''')
        db.commit()
        self.release_unverified_holds()

    def release_unverified_holds(self):
        """Requeue only old admin-read holds; never requeue a kicked account."""
        with self.db:
            self.db.execute('''UPDATE source_join_jobs SET state='queued',retry_at=0,detail=''
                WHERE state='manual_required' AND detail LIKE '入群前管理员检查：%'
                AND request_id IN (SELECT r.id FROM source_submissions r JOIN source_join_policy_holds h
                    ON h.group_id=r.group_id WHERE h.reason='unverified' AND r.status!='rejected')
                AND NOT EXISTS (SELECT 1 FROM source_membership_alerts a JOIN source_submissions r ON r.group_id=a.group_id
                    WHERE r.id=source_join_jobs.request_id AND a.account_index=source_join_jobs.account_index)''')
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_account_sync'").fetchone():
                self.db.execute('''UPDATE source_account_sync SET state='queued',retry_at=0,detail=''
                    WHERE state='manual_required' AND detail='入群前管理员检查要求手动处理'
                    AND group_id IN (SELECT group_id FROM source_join_policy_holds WHERE reason='unverified')
                    AND NOT EXISTS (SELECT 1 FROM source_membership_alerts a WHERE a.group_id=source_account_sync.group_id
                        AND a.account_index=source_account_sync.account_index)''')
            self.db.execute("DELETE FROM source_join_policy_holds WHERE reason='unverified'")

    def blocked(self,group_id):
        return bool(self.db.execute('SELECT 1 FROM source_join_policy_holds WHERE group_id=?',(group_id,)).fetchone())

    def hold(self,entity,reason,detail):
        group_id=utils.get_peer_id(entity)
        with self.db:
            self.db.execute('''INSERT INTO source_join_policy_holds(group_id,title,username,reason,detail,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(group_id) DO NOTHING''',
                (group_id,entity.title,getattr(entity,'username',None) or '',reason,detail,int(time.time())))
            self.db.execute('''UPDATE source_join_jobs SET state='manual_required',retry_at=0,detail=?
                WHERE state NOT IN ('joined','closed','manual_required') AND request_id IN
                (SELECT id FROM source_submissions WHERE group_id=? AND status!='rejected')''',
                ('入群前管理员检查：'+detail,group_id))
        self.wake.set()

    @staticmethod
    def matches(user):
        usernames=[getattr(user,'username',None)]
        usernames.extend(getattr(item,'username',None) for item in getattr(user,'usernames',None) or ()
                         if getattr(item,'active',False))
        return any(name and name.casefold()==PreJoinPolicy.WATCHED_USERNAME for name in usernames)

    async def inspect(self,client,entity,rpc):
        offset,seen=0,set()
        while True:
            request=functions.channels.GetParticipantsRequest(entity,types.ChannelParticipantsAdmins(),offset,200,0)
            response=(await rpc.call(client,request,max_wait=0,total_timeout=8) if rpc
                      else await asyncio.wait_for(client(request),8))
            if not isinstance(response,types.channels.ChannelParticipants):
                raise ValueError('Admin list response incomplete')
            admin_ids={p.user_id for p in response.participants}
            if any(user.id in admin_ids and self.matches(user) for user in response.users):
                return False
            fresh={p.user_id for p in response.participants}-seen
            seen.update(fresh)
            if response.count>0 and len(seen)>=response.count:
                return True
            if not fresh or not response.participants:
                raise ValueError('Admin list hidden or truncated')
            offset+=len(response.participants)

    async def allow(self,index,entity,rpc=None,*,account_entity=True):
        group_id=utils.get_peer_id(entity)
        async with self.locks.setdefault(group_id,asyncio.Lock()):
            if self.blocked(group_id):
                reason,detail=self.db.execute('SELECT reason,detail FROM source_join_policy_holds WHERE group_id=?',(group_id,)).fetchone()
                self.hold(entity,reason,detail)
                return False
            failures=[]
            for candidate in [index]+[n for n in range(len(self.clients)) if n!=index]:
                client=self.clients[candidate]
                try:
                    scoped=entity if candidate==index and account_entity else await asyncio.wait_for(
                        client.get_entity(getattr(entity,'username',None) or group_id),6)
                    if utils.get_peer_id(scoped)!=group_id:
                        raise ValueError('Group username changed')
                    allowed=await asyncio.wait_for(self.inspect(client,scoped,rpc),12)
                    if not allowed:
                        self.hold(entity,'blocked','管理员名单包含 @'+self.WATCHED_USERNAME)
                    return allowed
                except (ValueError,TypeError,errors.RPCError,OSError,asyncio.TimeoutError) as exc:
                    failures.append('account%s:%s'%(candidate+1,type(exc).__name__))
            logging.getLogger('cheatcheck').warning('Admin list incomplete for %s; no watched bot found; joining allowed (%s)',group_id,', '.join(failures))
            return True

    def pending_notices(self):
        return self.db.execute('SELECT group_id,title,username,reason,detail FROM source_join_policy_holds WHERE notified=0').fetchall()

    def mark_notified(self,group_id):
        with self.db:self.db.execute('UPDATE source_join_policy_holds SET notified=1 WHERE group_id=?',(group_id,))
