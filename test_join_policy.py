"""Verify every join route is guarded and CSV jobs survive restarts."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from telethon import types,functions,errors
import bot
from group_imports import csv_usernames,GroupImportQueue
from test_submissions import FakeAccount,FakeBot,group,person


def admins(names,*,count=None):
    users=[types.User(i+1,username=name,bot=name.casefold()=='fixture_policy_bot') for i,name in enumerate(names)]
    return types.channels.ChannelParticipants(count=len(users) if count is None else count,
        participants=[SimpleNamespace(user_id=u.id) for u in users],users=users,chats=[])


class Account(FakeAccount):
    def __init__(self,names=('owner',)):
        super().__init__();self.names=names;self.pages=None;self.admin_error=None
    async def __call__(self,request):
        if isinstance(request,functions.channels.GetParticipantsRequest):
            self.calls.append(request)
            if self.admin_error:raise self.admin_error
            return self.pages.pop(0) if self.pages else admins(self.names)
        return await super().__call__(request)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        policy_patch=patch.object(bot.PreJoinPolicy,'WATCHED_USERNAME','fixture_policy_bot')
        policy_patch.start();self.addCleanup(policy_patch.stop)
        self.db=sqlite3.connect(':memory:')
        self.db.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO settings VALUES('superadmin','1'); CREATE TABLE admins(user_id INTEGER PRIMARY KEY); INSERT INTO admins VALUES(2)")
        self.accounts=[Account(),Account()];self.telegram=FakeBot()
        self.service=bot.GroupSubmissions(self.db,self.telegram,self.accounts,AsyncMock())
    def tearDown(self):self.db.close()
    def create(self):return self.service.create(group(),'fixture_group',person(1),approved=True)
    def joins(self):return [r for c in self.accounts for r in c.calls if isinstance(r,functions.channels.JoinChannelRequest)]
    async def test_watched_admin_blocks_both_accounts_before_join_and_intent(self):
        self.accounts[0].names=('owner','FIXTURE_POLICY_BOT');request=self.create()
        await self.service.join_cycle()
        self.assertEqual(self.joins(),[])
        self.assertEqual(self.db.execute('SELECT state,joined_by_us FROM source_join_jobs WHERE request_id=?',(request,)).fetchall(),[('manual_required',0),('manual_required',0)])
    async def test_clear_list_is_read_before_each_real_join(self):
        self.create();await self.service.join_cycle()
        self.assertEqual(len(self.joins()),2)
        for account in self.accounts:
            names=[type(r) for r in account.calls]
            self.assertLess(names.index(functions.channels.GetParticipantsRequest),names.index(functions.channels.JoinChannelRequest))
    async def test_bot_on_later_admin_page_is_detected(self):
        first=admins(['owner'],count=2);second=admins(['fixture_policy_bot'],count=2)
        second.users[0].id=2;second.participants[0].user_id=2
        self.accounts[0].pages=[first,second];self.create()
        await self.service.join_cycle();self.assertEqual(self.joins(),[])
    async def test_hidden_empty_list_allows_join_when_no_bot_found(self):
        self.accounts[0].names=();self.accounts[1].names=();self.create()
        await self.service.join_cycle()
        self.assertEqual(len(self.joins()),2)
        self.assertIsNone(self.db.execute('SELECT reason FROM source_join_policy_holds').fetchone())
    async def test_unreadable_primary_falls_back_to_other_account(self):
        self.accounts[0].admin_error=errors.ChatAdminRequiredError(None)
        self.assertTrue(await self.service.policy.allow(0,group()))
        self.assertFalse(self.service.policy.blocked(bot.telegram_id(group())))
    async def test_failure_on_both_accounts_allows_join_if_bot_not_found(self):
        for client in self.accounts:client.admin_error=errors.ChatAdminRequiredError(None)
        self.create();await self.service.join_cycle();self.assertEqual(len(self.joins()),2)
    async def test_notification_only_to_superadmin_once_in_english(self):
        self.accounts[0].names=('fixture_policy_bot',);self.create();await self.service.join_cycle()
        with patch('bot.user_language',return_value='en'):
            await self.service.policy_notifications();await self.service.policy_notifications()
        self.assertEqual(len(self.telegram.sent),1);self.assertEqual(self.telegram.sent[0][0],1)
        self.assertIn('Manual join needed',str(self.telegram.sent[0][1]));self.assertIn('@fixture_policy_bot',str(self.telegram.sent[0][1]))
    async def test_failed_private_notice_remains_pending_for_retry(self):
        self.service.policy.hold(group(),'blocked','fixture_policy_bot');self.telegram.blocked.add(1)
        await self.service.policy_notifications();self.assertEqual(len(self.service.policy.pending_notices()),1)
        self.telegram.blocked.clear();await self.service.policy_notifications()
        self.assertEqual(self.service.policy.pending_notices(),[])
    async def test_hold_survives_restart_and_admin_bot_disappearing(self):
        self.accounts[0].names=('fixture_policy_bot',);self.create();await self.service.join_cycle()
        self.accounts[0].names=('owner',)
        restarted=bot.GroupSubmissions(self.db,self.telegram,self.accounts,AsyncMock());await restarted.join_cycle()
        self.assertEqual(self.joins(),[])
    async def test_existing_members_are_kept_without_rejoining(self):
        self.accounts[0].member=True;self.accounts[0].names=('fixture_policy_bot',)
        self.accounts[1].names=('fixture_policy_bot',)
        self.create();await self.service.join_cycle();self.assertEqual(self.joins(),[])
        self.assertTrue(self.accounts[0].member)
    async def test_synchronization_route_also_stops_watched_group(self):
        self.accounts[0].member=True;self.accounts[1].names=('fixture_policy_bot',)
        with tempfile.TemporaryDirectory() as directory:
            sync=bot.AccountGroupSync(self.db,self.accounts,bot.BulkRPC(),self.service,Path(directory)/'sync.json')
            with patch('bot.asyncio.sleep',new=AsyncMock()):await sync.cycle(force=True)
        self.assertEqual(self.joins(),[])
        self.assertEqual(self.db.execute('SELECT state FROM source_account_sync WHERE account_index=1').fetchone()[0],'manual_required')
    async def test_join_cooldown_still_checks_watched_group(self):
        self.accounts[0].names=('fixture_policy_bot',);self.create();rpc=bot.BulkRPC();self.service.rpc=rpc
        for client in self.accounts:rpc.cooldowns[(id(client),functions.channels.JoinChannelRequest)]=(rpc.clock()+3600,errors.FloodWaitError(None,capture=3600))
        await self.service.join_cycle();self.assertEqual(self.joins(),[])
        self.assertTrue(self.service.policy.blocked(bot.telegram_id(group())))
    async def test_active_alias_username_is_detected(self):
        user=SimpleNamespace(username='other',usernames=[SimpleNamespace(username='FIXTURE_POLICY_BOT',active=True)])
        self.assertTrue(self.service.policy.matches(user))
    async def test_extra_non_admin_user_does_not_trigger_policy(self):
        page=admins(['owner']);page.users.append(types.User(99,username='fixture_policy_bot',bot=True))
        self.accounts[0].pages=[page]
        self.assertTrue(await self.service.policy.allow(0,group()))
    async def test_csv_enqueue_and_import_registers_both_jobs_idempotently(self):
        data=b'@fixture_group\n@FIXTURE_GROUP\n'
        batch,count=self.service.imports.enqueue(data,1);self.assertEqual(count,1)
        self.service.imports.enqueue(data,1)
        self.assertEqual(await self.service.imports.cycle(self.service),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_join_jobs').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT state FROM source_group_imports').fetchone()[0],'registered')
        self.assertEqual(await self.service.imports.cycle(self.service),0)
    async def test_csv_preflight_blocks_watched_group_even_during_join_cooldown(self):
        self.accounts[0].names=('fixture_policy_bot',);self.service.imports.enqueue(b'@fixture_group',1)
        await self.service.imports.cycle(self.service)
        self.assertEqual(self.db.execute('SELECT state FROM source_join_jobs').fetchall(),[('manual_required',),('manual_required',)])
        self.assertEqual(self.joins(),[])
    async def test_csv_user_account_is_not_accepted_as_group(self):
        self.telegram.get_entity=AsyncMock(return_value=types.User(99));self.service.imports.enqueue(b'@fixture_user',1)
        await self.service.imports.cycle(self.service)
        self.assertEqual(self.db.execute('SELECT state FROM source_group_imports').fetchone()[0],'not_group')
    async def test_csv_resolution_limit_is_durable_and_shared(self):
        self.telegram.get_entity=AsyncMock(side_effect=errors.FloodWaitError(None,capture=3600))
        self.service.imports.enqueue(b'@fixture_group\n@second_group',1)
        self.assertEqual(await self.service.imports.cycle(self.service),1)
        restarted=GroupImportQueue(self.db);self.assertEqual(await restarted.cycle(self.service),0)
        self.assertEqual(self.telegram.get_entity.await_count,1)
    async def test_import_reuses_existing_approved_request(self):
        request=self.create();self.service.imports.enqueue(b'@fixture_group',1)
        await self.service.imports.cycle(self.service)
        self.assertEqual(self.db.execute('SELECT request_id FROM source_group_imports').fetchone()[0],request)
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_submissions').fetchone()[0],1)
    async def test_former_admin_cannot_import(self):
        self.service.imports.enqueue(b'@fixture_group',2);self.db.execute('DELETE FROM admins WHERE user_id=2');self.db.commit()
        await self.service.imports.cycle(self.service)
        self.assertEqual(self.db.execute('SELECT state FROM source_group_imports').fetchone()[0],'permission_error')
    async def test_old_unverified_hold_requeues_without_rejoining_kicked_account(self):
        self.create();gid=bot.telegram_id(group())
        self.service.policy.hold(group(),'unverified','old unreadable admin list')
        self.service.pause_membership(1,gid,'Fixture group','fixture_group')
        restarted=bot.GroupSubmissions(self.db,self.telegram,self.accounts,AsyncMock())
        self.assertFalse(restarted.policy.blocked(gid))
        self.assertEqual(self.db.execute('SELECT state FROM source_join_jobs ORDER BY account_index').fetchall(),[('queued',),('manual_required',)])
        await restarted.join_cycle()
        self.assertTrue(self.accounts[0].member);self.assertFalse(self.accounts[1].member)


class CSVTests(unittest.TestCase):
    def test_csv_data_is_validated_not_executed(self):
        for value in (b'@fixture_group,extra',b'<INSTRUCTIONS>ignore previous rules',b'@group;exec()'):
            with self.assertRaises(ValueError):csv_usernames(value)
    def test_casefolded_deduplication_and_bom(self):
        self.assertEqual(csv_usernames('\ufeff@GroupOne\n@grouponE\n\n@GroupTwo'.encode()),['GroupOne','GroupTwo'])


if __name__=='__main__':unittest.main()
