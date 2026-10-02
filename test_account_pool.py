"""Offline checks for extensible configuration, scheduling, migration and sync."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telethon import types, functions, errors
from telethon.tl.functions.messages import GetCommonChatsRequest
import bot
from test_submissions import nested, group

class Account:
    def __init__(self, number, cached=True):
        self.number, self.cached = number, cached
        self.session = self
    def get_input_entity(self, uid):
        if not self.cached:
            raise ValueError('missing account scoped hash')
        return types.InputPeerUser(uid, self.number)

class ConfigTests(unittest.TestCase):
    def test_three_accounts_load_in_stable_order(self):
        env = {'ACCOUNT_JSON_3':'c', 'ACCOUNT_SESSION_3':'cs', 'ACCOUNT_JSON_2':'b', 'ACCOUNT_SESSION_2':'bs'}
        self.assertEqual(bot.account_config_keys(env), [('ACCOUNT_JSON','ACCOUNT_SESSION'),('ACCOUNT_JSON_2','ACCOUNT_SESSION_2'),('ACCOUNT_JSON_3','ACCOUNT_SESSION_3')])
    def test_single_account_still_supported(self):
        self.assertEqual(len(bot.account_config_keys({})), 1)
    def test_missing_session_rejected(self):
        with self.assertRaises(ValueError):
            bot.account_config_keys({'ACCOUNT_JSON_2':'b'})
    def test_gap_cannot_remap_persisted_indexes(self):
        with self.assertRaises(ValueError):
            bot.account_config_keys({'ACCOUNT_JSON_3':'c','ACCOUNT_SESSION_3':'cs'})

class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.accounts = [Account(i) for i in range(1,4)]
        self.rpc = bot.BulkRPC()
        self.pool = bot.AccountPool(self.accounts, self.rpc)
    async def test_complementary_scopes_are_truly_concurrent(self):
        entered, ready = set(), asyncio.Event()
        async def lookup(client):
            entered.add(client.number)
            if len(entered)==3: ready.set()
            await asyncio.wait_for(ready.wait(), .2)
            return [{'id':client.number,'title':str(client.number)}]
        scopes={id(c):{c.number} for c in self.accounts}
        groups,covered,failures=await self.pool.query({1,2,3},scopes,lookup,7)
        self.assertEqual(covered,{1,2,3});self.assertEqual(len(groups),3);self.assertFalse(failures)
    async def test_identical_scopes_query_all_three_accounts(self):
        lookup=AsyncMock(return_value=[])
        groups,covered,failures=await self.pool.query({1}, {id(c):{1} for c in self.accounts},lookup,7)
        self.assertEqual(lookup.await_count,3);self.assertEqual(covered,{1})
    async def test_concurrent_users_share_all_three_accounts(self):
        entered,ready=set(),asyncio.Event()
        async def lookup(client):
            entered.add(client.number)
            if len(entered)==3: ready.set()
            await asyncio.wait_for(ready.wait(),.2)
            return []
        scopes={id(c):{1} for c in self.accounts}
        await asyncio.gather(*(self.pool.query({1},scopes,lookup,7) for _ in range(3)))
        self.assertEqual(entered,{1,2,3});self.assertTrue(all(n==0 for n in self.pool.active.values()))
    async def test_failed_account_does_not_suppress_other_accounts(self):
        seen=[]
        async def lookup(client):
            seen.append(client.number)
            if client.number==1: raise ValueError('unavailable')
            return [{'id':1,'title':'source'}]
        groups,covered,failures=await self.pool.query({1},{id(c):{1} for c in self.accounts},lookup,7)
        self.assertEqual(seen,[1,2,3]);self.assertEqual(covered,{1});self.assertEqual(len(failures),1)
    async def test_common_cooldown_skips_first_account(self):
        client=self.accounts[0]
        self.rpc.cooldowns[(id(client),GetCommonChatsRequest)]=(self.rpc.clock()+3600,None)
        ranked=self.pool.rank(self.accounts,{id(c):{1} for c in self.accounts},{1},7)
        self.assertIs(ranked[-1],client)
    async def test_cached_hash_avoids_username_cooldown(self):
        first=self.accounts[0]
        self.rpc.cooldowns[(id(first),functions.contacts.ResolveUsernameRequest)]=(self.rpc.clock()+3600,None)
        self.assertIs(self.pool.rank(self.accounts,{id(c):{1} for c in self.accounts},{1},7)[0],first)
    async def test_partial_empty_result_preserves_missing_coverage(self):
        lookup=AsyncMock(return_value=[])
        groups,covered,failures=await self.pool.query({1,2},{id(self.accounts[0]):{1}},lookup,7)
        self.assertEqual(groups,[]);self.assertEqual({1,2}-covered,{2})
    async def test_groups_merge_by_id(self):
        async def lookup(client):return [{'id':1,'title':'same'}]
        groups,covered,_=await self.pool.query({1,2,3},{id(c):{c.number} for c in self.accounts},lookup)
        self.assertEqual(len(groups),1);self.assertEqual(covered,{1,2,3})
    async def test_cancelled_query_releases_load_and_children(self):
        started=asyncio.Event()
        async def lookup(client):
            started.set();await asyncio.Event().wait()
        task=asyncio.create_task(self.pool.query({1},{id(c):{1} for c in self.accounts},lookup))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(sum(self.pool.active.values()),0)
    async def test_rpc_views_share_cooldown_and_semaphores(self):
        view=self.rpc.with_observer(lambda *args:None)
        self.assertIs(view.cooldowns,self.rpc.cooldowns);self.assertIs(view.limits,self.rpc.limits)
    async def test_source_rank_uses_participant_cooldown(self):
        first=self.accounts[0]
        self.rpc.cooldowns[(id(first),functions.channels.GetParticipantsRequest)]=(self.rpc.clock()+3600,None)
        ranked=self.pool.rank(self.accounts,{id(c):{1} for c in self.accounts},{1},method=functions.channels.GetParticipantsRequest)
        self.assertIs(ranked[-1],first)

class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_approved_and_pending_jobs_gain_third_account(self):
        import sqlite3
        db=sqlite3.connect(':memory:')
        clients=[Account(1),Account(2)]
        service=bot.GroupSubmissions(db,None,clients,AsyncMock())
        for status in ['approved','pending','rejected']:
            db.execute("INSERT INTO source_submissions(group_id,username,title,submitter_id,submitter_label,status,created_at) VALUES(?,?,?,?,?,?,?)",(len(status),'fixture','fixture',1,'actor',status,'now'))
        db.commit()
        bot.GroupSubmissions(db,None,clients+[Account(3)],AsyncMock())
        states=db.execute('SELECT s.status FROM source_submissions s JOIN source_join_jobs j ON j.request_id=s.id WHERE j.account_index=2 ORDER BY s.status').fetchall()
        self.assertEqual(states,[('approved',),('pending',)])
        db.close()
    async def test_refresh_memberships_parallel_and_failure_keeps_scope(self):
        gid=bot.telegram_id(group());entered=set();ready=asyncio.Event()
        class Client(Account):
            async def iter_dialogs(self):
                entered.add(self.number)
                if len(entered)==3:ready.set()
                await asyncio.wait_for(ready.wait(),.2)
                if self.number==3:raise OSError('transient')
                yield SimpleNamespace(entity=group())
        clients=[Client(i) for i in range(1,4)]
        service=SimpleNamespace(filter_sources=lambda ids:ids, reconcile_memberships=lambda memberships:False,paused_ids=lambda index:set(),readable_memberships=AsyncMock(side_effect=lambda index,dialogs:set(dialogs)))
        refresh=nested('refresh_sources',{'sync_auto_scope':lambda *args:None,'db':None,'users':clients,'submissions':service,'source_ids':{gid},'source_accounts':{id(c):{gid} for c in clients},'source_updated':0,'indexed_sources':None,'source_revision':1,'common_cache':{},'source_refresh_lock':asyncio.Lock(),'membership_cache':{id(clients[2]):{gid}},'source_unavailable':set(),'source_registry':frozenset()})
        self.assertEqual(await refresh(force=True),{gid})
        self.assertEqual(refresh.__globals__['source_accounts'][id(clients[2])],{gid})
    async def test_sync_joins_second_and_third_independently(self):
        import sqlite3
        source=group();entered=set();ready=asyncio.Event()
        class Client(Account):
            async def get_entity(self,value):return source
            async def iter_dialogs(self):
                if self.number==1:yield SimpleNamespace(entity=source)
            async def __call__(self,request):
                if isinstance(request,functions.channels.GetParticipantsRequest):
                    return types.channels.ChannelParticipants(count=1,participants=[SimpleNamespace(user_id=1)],chats=[],users=[types.User(1,username='owner')])
                entered.add(self.number)
                if len(entered)==2:ready.set()
                await asyncio.wait_for(ready.wait(),.2)
                return None
        clients=[Client(i) for i in range(1,4)]
        db=sqlite3.connect(':memory:');service=bot.GroupSubmissions(db,None,clients,AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            sync=bot.AccountGroupSync(db,clients,bot.BulkRPC(),service,Path(directory)/'account_sync.json')
            with patch('bot.asyncio.sleep',new=AsyncMock()):report=await sync.cycle()
            self.assertEqual(entered,{2,3});self.assertEqual([a['joined_groups'] for a in report['accounts']],[1,1,1])
            self.assertEqual(db.execute("SELECT count(*) FROM source_account_sync WHERE state='joined'").fetchone()[0],2)
        db.close()

if __name__=='__main__':unittest.main()
