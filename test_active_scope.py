"""Current readable scope, scalable account slots, and shared concurrency."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import types
import bot
from runtime_config import RuntimeSettings, account_config_keys
from test_query_reliability import query_db
from test_submissions import nested, person


class SettingsTests(unittest.TestCase):
    def test_blank_future_slots_do_not_activate(self):
        env={'ACCOUNT_JSON_2':'two.json','ACCOUNT_SESSION_2':'two',
             'ACCOUNT_JSON_3':'','ACCOUNT_SESSION_3':''}
        self.assertEqual(len(account_config_keys(env)),2)
    def test_eight_accounts_need_no_code_change(self):
        env={f'ACCOUNT_{kind}_{i}':f'{i}' for i in range(2,9) for kind in ('JSON','SESSION')}
        self.assertEqual(len(account_config_keys(env)),8)
        self.assertEqual(account_config_keys(env)[-1],('ACCOUNT_JSON_8','ACCOUNT_SESSION_8'))
    def test_incomplete_and_gapped_slots_fail(self):
        for env in ({'ACCOUNT_JSON_2':'two'}, {'ACCOUNT_JSON_3':'three','ACCOUNT_SESSION_3':'three'}):
            with self.assertRaises(ValueError):account_config_keys(env)
    def test_workers_scale_and_are_capped(self):
        settings=RuntimeSettings.from_env({})
        self.assertEqual([settings.bulk_workers(n) for n in (2,3,8,100)],[8,12,32,32])
        self.assertEqual(settings.source_workers(100),32)
        custom=RuntimeSettings.from_env({'BULK_WORKER_LIMIT':'10','ACCOUNT_RPC_CONCURRENCY':'6'})
        self.assertEqual(custom.bulk_workers(8),10)
        self.assertEqual(custom.rpc_concurrency,6)
    def test_invalid_performance_settings_fail(self):
        for value in ('0','65','oops'):
            with self.assertRaises(ValueError):RuntimeSettings.from_env({'ACCOUNT_RPC_CONCURRENCY':value})
    def test_bulk_successful_partial_scope_is_a_normal_result(self):
        self.assertEqual(bot.bulk_result_state({1,2},{1},set(),set(),available_only=True),(0,{2}))
        self.assertEqual(bot.bulk_result_state({1,2},set(),{1},set(),available_only=True),(0,{2}))
        self.assertEqual(bot.bulk_result_state({1,2},set(),set(),set(),available_only=True),(None,{1,2}))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.db=query_db()
        self.db.execute('CREATE TABLE whitelist(user_id INTEGER PRIMARY KEY)')
        self.db.execute('INSERT INTO whitelist VALUES(7)')
    def tearDown(self):self.db.close()
    def recent(self):
        self.db.execute('INSERT OR REPLACE INTO auto_recent VALUES(-42,7,9999999999)');self.db.commit()
    def count(self):return self.db.execute('SELECT count(*) FROM auto_recent').fetchone()[0]
    def test_expansion_clears_only_temporary_negatives(self):
        bot.sync_auto_scope(self.db,{1});self.recent()
        self.assertTrue(bot.sync_auto_scope(self.db,{1,2}))
        self.assertEqual(self.count(),0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM whitelist').fetchone()[0],1)
    def test_same_scope_and_shrink_preserve_negatives(self):
        bot.sync_auto_scope(self.db,{1,2});self.recent()
        self.assertFalse(bot.sync_auto_scope(self.db,{1,2}))
        self.assertFalse(bot.sync_auto_scope(self.db,{1}))
        self.assertEqual(self.count(),1)
        self.assertTrue(bot.sync_auto_scope(self.db,{1,2}));self.assertEqual(self.count(),0)
    def test_first_upgrade_invalidates_old_scope_unknown_cache(self):
        self.recent();self.assertTrue(bot.sync_auto_scope(self.db,{1}));self.assertEqual(self.count(),0)


class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_fresh_query_rechecks_all_accounts(self):
        clients=[SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,9))) for _ in range(2)]
        rpc=bot.BulkRPC();lookup=AsyncMock()
        async def run(client,*args):
            if client is clients[1]:raise TimeoutError()
            return []
        lookup.side_effect=run
        common=nested('common_groups',{'users':clients,'account_rpc':rpc,'account_pool':bot.AccountPool(clients,rpc),
            'source_revision':1,'source_accounts':{id(clients[0]):{1},id(clients[1]):{2}},'source_unavailable':{3},
            'indexed_sources':None,'source_index_errors':False,'source_index_built':0,'common_cache':{},'common_inflight':{},
            'refresh_sources':AsyncMock(return_value={1,2}),'fetch_common_groups':lookup})
        result=await common(person(7));calls=lookup.await_count
        self.assertEqual(result.missing_sources,{2})
        repeated=await common(person(7))
        self.assertIsNot(repeated,result);self.assertEqual(lookup.await_count,2*calls)
        self.assertFalse(common.__globals__['common_cache'])
        await common(person(7));self.assertEqual(lookup.await_count,3*calls)
    async def test_auto_negative_uses_24_hour_current_scope_cache(self):
        db=query_db();publish=AsyncMock(return_value=False)
        check=nested('auto_check',{'db':db,'bot_me':SimpleNamespace(id=999),'whitelisted':lambda uid:False,
            'auto_inflight':set(),'auto_retry_after':{},'publish_check':publish})
        await check(-42,person(7),123,'群组auto消息');await check(-42,person(7),124,'群组auto消息')
        self.assertEqual(publish.await_count,1)
        expires=db.execute('SELECT expires_at FROM auto_recent').fetchone()[0]
        self.assertAlmostEqual(expires-time.time(),86400,delta=2);db.close()
    async def test_eight_complementary_accounts_run_concurrently(self):
        clients=[SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda uid:types.InputPeerUser(uid,9))) for _ in range(8)]
        ready=asyncio.Event();entered=set()
        async def lookup(client,needed):
            entered.add(id(client))
            if len(entered)==8:ready.set()
            await asyncio.wait_for(ready.wait(),.5)
            return [{'id':next(iter(needed))}]
        pool=bot.AccountPool(clients,bot.BulkRPC())
        groups,covered,errors=await pool.query(set(range(8)),{id(c):{i} for i,c in enumerate(clients)},lookup,7,scoped_lookup=True)
        self.assertEqual(len(groups),8);self.assertEqual(covered,set(range(8)));self.assertEqual(errors,[])
        self.assertTrue(all(v==0 for v in pool.active.values()))
    async def test_shared_rpc_observer_preserves_configured_limits(self):
        settings=RuntimeSettings.from_env({'ACCOUNT_RPC_CONCURRENCY':'6','ACCOUNT_MEMBER_CONCURRENCY':'3'})
        rpc=bot.BulkRPC(settings=settings);observed=rpc.with_observer(lambda *args:None)
        self.assertIs(observed.settings,settings);self.assertIs(observed.limits,rpc.limits)
        self.assertIs(observed.cooldowns,rpc.cooldowns)
        client=AsyncMock(return_value=None)
        await rpc.call(client,bot.GetCommonChatsRequest(types.InputUserSelf(),0,100))
        await observed.call(client,bot.functions.channels.GetParticipantsRequest(types.InputChannel(1,1),types.ChannelParticipantsSearch(''),0,200,0))
        self.assertEqual(rpc.limits[(id(client),bot.GetCommonChatsRequest)]._value,6)
        self.assertEqual(rpc.limits[(id(client),bot.functions.channels.GetParticipantsRequest)]._value,3)


if __name__=='__main__':unittest.main()
