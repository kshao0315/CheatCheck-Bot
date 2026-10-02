"""Reproduce stale registration failures and preserve honest partial results."""
import asyncio
import sqlite3
import time
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telethon import types
import bot
from test_submissions import nested, FakeAccount, person, group

def query_db():
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE queries(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,requester_id INTEGER,target_id INTEGER,method TEXT,chat_id INTEGER,matches INTEGER,status TEXT,details_json TEXT,result_text TEXT);
        CREATE TABLE auto_recent(chat_id INTEGER,user_id INTEGER,expires_at INTEGER,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO settings VALUES('superadmin','1');
        CREATE TABLE admins(user_id INTEGER PRIMARY KEY);''')
    return db

class MembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_absence_clears_previous_unknown_member_state(self):
        db=query_db();client=AsyncMock(side_effect=TimeoutError())
        service=bot.GroupSubmissions(db,None,[client],AsyncMock())
        service.create(group(),'fixture_group',person(1),approved=True)
        db.execute("UPDATE source_join_jobs SET state='joined'")
        await service.readable_memberships(0,{bot.telegram_id(group()):group()})
        ids=await service.readable_memberships(0,{})
        service.reconcile_memberships({id(client):ids})
        self.assertEqual(db.execute('SELECT state FROM source_join_jobs').fetchone()[0],'manual_required')
        db.close()
    async def test_member_verification_timeout_is_not_treated_as_a_kick(self):
        db=query_db();client=AsyncMock(side_effect=TimeoutError())
        service=bot.GroupSubmissions(db,None,[client],AsyncMock())
        service.create(group(),'fixture_group',person(1),approved=True)
        db.execute("UPDATE source_join_jobs SET state='joined'")
        ids=await service.readable_memberships(0,{bot.telegram_id(group()):group()})
        self.assertEqual(ids,set())
        service.reconcile_memberships({id(client):ids})
        self.assertEqual(db.execute('SELECT state FROM source_join_jobs').fetchone()[0],'joined')
        self.assertEqual(service.paused_ids(0),set())
        db.close()
    async def test_ghost_dialog_is_removed_when_account_is_not_a_member(self):
        db=query_db();client=FakeAccount();service=bot.GroupSubmissions(db,None,[client],AsyncMock())
        service.create(group(),'fixture_group',person(1),approved=True)
        self.assertEqual(await service.readable_memberships(0,{bot.telegram_id(group()):group()}),set())
        self.assertTrue(any(isinstance(call,bot.functions.channels.GetParticipantRequest) for call in client.calls))
        db.close()
    async def test_banned_dialog_does_not_restore_manual_membership(self):
        db=query_db();client=AsyncMock()
        rights=types.ChatBannedRights(until_date=None,view_messages=True)
        client.return_value=SimpleNamespace(participant=types.ChannelParticipantBanned(1,types.PeerUser(1),None,rights,left=True))
        service=bot.GroupSubmissions(db,None,[client],AsyncMock())
        service.pause_membership(0,bot.telegram_id(group()),'Fixture group','fixture_group')
        ids=await service.readable_memberships(0,{bot.telegram_id(group()):group()})
        self.assertEqual(ids,set())
        service.reconcile_memberships({id(client):ids})
        self.assertEqual(service.paused_ids(0),{bot.telegram_id(group())})
        db.close()
    async def test_previous_private_error_retry_is_migrated_to_manual_hold(self):
        db=query_db();client=FakeAccount()
        service=bot.GroupSubmissions(db,None,[client],AsyncMock())
        service.create(group(),'fixture_group',person(1),approved=True)
        db.execute("UPDATE source_join_jobs SET state='retry',detail='ChannelPrivateError',retry_at=9999999999")
        service.reconcile_memberships({id(client):set()})
        self.assertEqual(db.execute('SELECT state FROM source_join_jobs').fetchone()[0],'manual_required')
        await service.join_cycle()
        self.assertEqual(client.calls,[])
        db.close()
    async def test_join_success_is_rechecked_after_verification_window(self):
        db=query_db();refresh=AsyncMock()
        service=bot.GroupSubmissions(db,None,[FakeAccount()],refresh)
        service.create(group(),'fixture_group',person(1),approved=True)
        await service.join_cycle()
        self.assertGreater(service.membership_recheck_at,time.monotonic())
        service.membership_recheck_at=time.monotonic()-1
        await service.join_cycle()
        self.assertEqual(refresh.await_count,2)
        self.assertEqual(service.membership_recheck_at,0)
        await service.join_cycle()
        self.assertEqual(refresh.await_count,2)
        db.close()

    async def test_stale_joined_jobs_pause_without_removing_registration(self):
        db=query_db();clients=[FakeAccount(),FakeAccount()]
        service=bot.GroupSubmissions(db,None,clients,AsyncMock())
        request=service.create(group(),'fixture_group',person(1),approved=True)
        await service.join_cycle()
        self.assertEqual(db.execute("SELECT count(*) FROM source_join_jobs WHERE state='joined'").fetchone()[0],2)
        self.assertTrue(service.reconcile_memberships({id(c):set() for c in clients}))
        self.assertEqual(db.execute("SELECT count(*) FROM source_join_jobs WHERE state='manual_required'").fetchone()[0],2)
        self.assertEqual(db.execute('SELECT count(*) FROM cheat_sources').fetchone()[0],1)
        calls=[len(c.calls) for c in clients]
        await service.join_cycle()
        self.assertEqual([len(c.calls) for c in clients],calls)
        service.reconcile_memberships({id(c):{bot.telegram_id(group())} for c in clients})
        self.assertEqual(db.execute("SELECT count(*) FROM source_join_jobs WHERE state='joined'").fetchone()[0],2)
        self.assertEqual(db.execute('SELECT count(*) FROM source_membership_alerts').fetchone()[0],0)
        db.close()
    async def test_manual_notice_is_durable_and_sent_once_per_account(self):
        db=query_db();telegram=SimpleNamespace(send_message=AsyncMock())
        service=bot.GroupSubmissions(db,telegram,[FakeAccount()],AsyncMock())
        service.pause_membership(0,bot.telegram_id(group()),'Fixture group','fixture_group')
        with patch('bot.user_language',return_value='zh'):
            await service.membership_notifications();await service.membership_notifications()
        self.assertEqual(telegram.send_message.await_count,1)
        text=str(telegram.send_message.call_args.args[1])
        self.assertIn('Fixture group',text);self.assertIn('https://t.me/fixture_group',text)
        self.assertIn('第 1 个',text);self.assertIn('自动重新入群已停止',text)
        db.close()
    async def test_sync_cannot_rejoin_a_paused_account(self):
        import tempfile
        from pathlib import Path
        db=query_db();clients=[FakeAccount(),FakeAccount()];clients[0].member=True
        service=bot.GroupSubmissions(db,None,clients,AsyncMock())
        service.pause_membership(1,bot.telegram_id(group()),'Fixture group','fixture_group')
        with tempfile.TemporaryDirectory() as directory:
            sync=bot.AccountGroupSync(db,clients,bot.BulkRPC(),service,Path(directory)/'sync.json')
            await sync.cycle(force=True)
        self.assertEqual(len(clients[1].calls),0)
        db.close()
    async def test_blocked_join_jobs_do_not_force_a_membership_refresh(self):
        db=query_db();clients=[FakeAccount(),FakeAccount()];refresh=AsyncMock()
        service=bot.GroupSubmissions(db,None,clients,refresh)
        service.create(group(),'fixture_group',person(1),approved=True)
        rpc=bot.BulkRPC();service.rpc=rpc
        for client in clients:rpc.cooldowns[(id(client),bot.functions.channels.JoinChannelRequest)]=(time.monotonic()+3600,None)
        await service.join_cycle();await service.join_cycle()
        self.assertEqual(refresh.await_count,0)
        db.close()
    async def test_unreadable_registry_is_separate_from_active_memberships(self):
        available=bot.telegram_id(group());unavailable=-1000000000043
        client=FakeAccount();client.member=True
        service=SimpleNamespace(filter_sources=lambda ids:set(ids)|{unavailable},reconcile_memberships=lambda memberships:False,paused_ids=lambda index:set(),readable_memberships=AsyncMock(side_effect=lambda index,dialogs:set(dialogs)))
        refresh=nested('refresh_sources',{'sync_auto_scope':lambda *args:None,'db':None,'users':[client],'submissions':service,'source_ids':set(),
            'source_accounts':{},'source_updated':0,'indexed_sources':None,'source_revision':1,'common_cache':{},
            'source_unavailable':set(),'source_registry':frozenset(),'source_refresh_lock':asyncio.Lock(),'membership_cache':{}})
        self.assertEqual(await refresh(force=True),{available})
        self.assertEqual(refresh.__globals__['source_unavailable'],{unavailable})
        self.assertEqual(refresh.__globals__['source_accounts'],{id(client):{available}})

class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_account_receives_its_full_readable_scope(self):
        clients=[SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,1))) for _ in range(2)]
        scopes={id(clients[0]):{1,2},id(clients[1]):{2,3}};calls=[]
        async def lookup(client,needed):calls.append((clients.index(client),set(needed)));return []
        groups,covered,errors=await bot.AccountPool(clients,bot.BulkRPC()).query({1,2,3},scopes,lookup,7,scoped_lookup=True)
        self.assertEqual([needed for _,needed in calls],[{1,2},{2,3}]);self.assertEqual(covered,{1,2,3})
        self.assertEqual(errors,[])
    async def test_failed_account_does_not_require_sequential_scope_retry(self):
        first=SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,1)))
        second=SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,1)))
        scopes={id(first):{1,2},id(second):{2,3}};calls=[]
        async def lookup(client,needed):
            calls.append((id(client),set(needed)))
            if client is first:raise TimeoutError()
            return []
        _,covered,errors=await bot.AccountPool([first,second],bot.BulkRPC()).query({1,2,3},scopes,lookup,7,scoped_lookup=True)
        self.assertEqual([needed for cid,needed in calls if cid==id(second)],[{2,3}])
        self.assertEqual(covered,{2,3});self.assertEqual(len(errors),1)
    async def test_successful_empty_scope_survives_a_complementary_failure(self):
        clients=[SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,9))) for _ in range(2)]
        rpc=bot.BulkRPC()
        async def lookup(client,*args):
            if client is clients[1]:raise TimeoutError()
            return []
        common=nested('common_groups',{'users':clients,'account_rpc':rpc,'account_pool':bot.AccountPool(clients,rpc),
            'source_revision':1,'source_accounts':{id(clients[0]):{1,2},id(clients[1]):{2,3}},'source_unavailable':set(),
            'indexed_sources':None,'source_index_errors':False,'source_index_built':0,'common_cache':{},'common_inflight':{},
            'refresh_sources':AsyncMock(return_value={1,2,3}),'fetch_common_groups':lookup})
        result=await common(person(7));self.assertEqual(result,[]);self.assertEqual(result.missing_sources,{3})
        self.assertFalse(common.__globals__['common_cache'])
        repeated=await common(person(7))
        self.assertIsNot(repeated,result);self.assertEqual(repeated.missing_sources,{3})
    async def test_source_search_only_reads_assigned_scope(self):
        client=SimpleNamespace()
        visited=[]
        async def pages(account,gid,rpc):
            visited.append(gid);yield [],True
        find=nested('find_source_user',{'source_accounts':{id(client):{1,2,3}},'refresh_sources':AsyncMock(),
            'indexed_sources':None,'source_index_errors':False,'source_index_built':0,'account_rpc':bot.BulkRPC(),
            'source_member_pages':pages,'users':[client]})
        self.assertIsNone(await find(client,7,{3}));self.assertEqual(visited,[3])
    async def test_empty_api_success_no_longer_fails_due_to_unreadable_registration(self):
        class Account:
            session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,9))
        client=Account();rpc=bot.BulkRPC();lookup=AsyncMock(return_value=[])
        scope={'users':[client],'account_rpc':rpc,'account_pool':bot.AccountPool([client],rpc),
            'source_revision':1,'source_accounts':{id(client):{42}},'source_unavailable':{43},
            'indexed_sources':None,'source_index_errors':False,'source_index_built':0,
            'common_cache':{},'common_inflight':{},'refresh_sources':AsyncMock(return_value={42}),
            'fetch_common_groups':lookup}
        common=nested('common_groups',scope)
        target=person(7)
        result=await common(target);cached=await common(target)
        self.assertEqual(result,[]);self.assertEqual(result.missing_sources,set())
        self.assertEqual(cached.missing_sources,set());self.assertEqual(lookup.await_count,2)
    async def test_active_rpc_failure_still_fails_and_is_not_cached_clean(self):
        client=SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,9)))
        common=nested('common_groups',{'users':[client],'account_rpc':bot.BulkRPC(),
            'account_pool':bot.AccountPool([client],bot.BulkRPC()),'source_revision':1,
            'source_accounts':{id(client):{42}},'source_unavailable':set(),'indexed_sources':None,
            'source_index_errors':False,'source_index_built':0,'common_cache':{},'common_inflight':{},
            'refresh_sources':AsyncMock(return_value={42}),'fetch_common_groups':AsyncMock(side_effect=TimeoutError())})
        with self.assertRaises(TimeoutError):await common(person(7))
        self.assertEqual(common.__globals__['common_cache'],{})
    async def publish(self, groups, positive_only=False):
        db=query_db();message=SimpleNamespace(id=100,chat_id=7)
        message.edit=AsyncMock(return_value=message)
        telegram=SimpleNamespace(send_message=AsyncMock(return_value=message))
        function=nested('publish_check',{'db':db,'bot':telegram,'whitelisted':lambda uid:False,
            'common_groups':AsyncMock(return_value=groups),'expire':lambda *args:None})
        value=await function(7,8,person(9),'bot私聊',positive_only=positive_only)
        return value,db,message,telegram
    async def test_scoped_negative_has_standard_output_and_completed_audit_status(self):
        value,db,message,_=await self.publish(bot.QueryGroups([],{43}))
        output=str(message.edit.call_args.args[0])
        self.assertIn('✅ 用户检查完成',output);self.assertNotIn('来源群',output)
        self.assertIn('未发现该用户在作弊群组中',output)
        self.assertEqual(db.execute('SELECT status,matches FROM queries').fetchone(),('完成',0));db.close()
    async def test_full_negative_keeps_original_output(self):
        value,db,message,_=await self.publish(bot.QueryGroups())
        self.assertIn('✅ 用户检查完成',str(message.edit.call_args.args[0]))
        self.assertEqual(db.execute('SELECT status FROM queries').fetchone()[0],'完成');db.close()
    async def test_auto_scoped_negative_is_cached_for_available_scope(self):
        value,db,message,telegram=await self.publish(bot.QueryGroups([],{43}),positive_only=True)
        self.assertIs(value,False);self.assertEqual(telegram.send_message.await_count,0);db.close()
    async def test_scoped_positive_retains_details_button_without_warning(self):
        value,db,message,_=await self.publish(bot.QueryGroups([{'id':42,'title':'source'}],{43}))
        self.assertTrue(value);self.assertIn('作弊群组数量: 1',str(message.edit.call_args.args[0]))
        self.assertNotIn('来源群',str(message.edit.call_args.args[0]))
        self.assertTrue(message.edit.call_args.kwargs['buttons']);db.close()
    async def test_english_negative_is_translated_without_scope_note(self):
        with bot.language_context('en'):
            _,db,message,_=await self.publish(bot.QueryGroups([],{43}))
            output=str(message.edit.call_args.args[0])
            self.assertIn('✅ Check complete',output)
            self.assertNotIn('source groups',output);db.close()

class PreparationAndRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_reference_uses_account_hash_instead_of_bot_hash(self):
        cached={};client=SimpleNamespace(session=SimpleNamespace())
        def lookup(uid):
            if uid not in cached:raise ValueError()
            return cached[uid]
        client.session.get_input_entity=lookup
        async def seed(*args,**kwargs):cached[7]=types.InputPeerUser(7,123)
        helper=nested('input_user_from_private_reference',{'user_preparation_locks':{id(client):asyncio.Lock()},
            'bot':None,'bot_me':None,'account_rpc':bot.BulkRPC(),'seed_bulk_users':AsyncMock(side_effect=seed)})
        result=await helper(client,types.User(7,access_hash=999))
        self.assertEqual(result.access_hash,123)
    async def test_retry_does_not_whitelist_unknown_or_spam_queries(self):
        db=query_db();publish=AsyncMock(return_value=None)
        check=nested('auto_check',{'db':db,'bot_me':SimpleNamespace(id=999),'whitelisted':lambda uid:False,
            'auto_inflight':set(),'auto_retry_after':OrderedDict(),'publish_check':publish})
        await check(-42,person(7),123,'群组auto消息');await check(-42,person(7),124,'群组auto消息')
        self.assertEqual(publish.await_count,1)
        self.assertEqual(db.execute('SELECT count(*) FROM auto_recent').fetchone()[0],0);db.close()

if __name__=='__main__':unittest.main()
