"""Checks for source exemptions, role boundaries, commands and read-only links."""
import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import types, utils
import bot
from group_whitelist import GroupWhitelist, exempt_source_ids, filter_check_groups, filter_bulk_hits, parse_group_reference, resolve_whitelist_group
from test_submissions import group, person, nested
from test_query_reliability import query_db


class ExemptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=query_db();self.whites=GroupWhitelist(self.db);self.entity=group();self.gid=utils.get_peer_id(self.entity)
    def tearDown(self):self.db.close()
    async def test_default_and_approved_sources_excluded_for_all_accounts(self):
        service=bot.GroupSubmissions(self.db,None,[SimpleNamespace() for _ in range(3)],AsyncMock())
        self.whites.add(self.entity,1)
        self.assertEqual(service.filter_sources({self.gid,-1000000000043}),{-1000000000043})
        service.create(self.entity,'fixture_group',person(1),approved=True)
        self.assertEqual(service.filter_sources({self.gid}),set())
        self.assertEqual(service.filter_sources({self.gid},include_exempt=True),{self.gid})
    async def test_removal_restores_source(self):
        service=bot.GroupSubmissions(self.db,None,[SimpleNamespace()],AsyncMock())
        self.whites.add(self.entity,2)
        self.assertTrue(self.whites.remove(self.gid,2));self.assertEqual(service.filter_sources({self.gid}),{self.gid})
    async def test_ownership_and_superadmin(self):
        self.whites.add(self.entity,2)
        self.assertEqual(self.whites.rows(3),[])
        self.assertEqual(len(self.whites.rows(1,True)),1)
        self.assertFalse(self.whites.add(self.entity,3))
        with self.assertRaises(PermissionError):self.whites.remove(self.gid,3)
        self.assertTrue(self.whites.remove(self.gid,1,True))
    async def test_renamed_group_stays_exempt_and_reinitialization_keeps_data(self):
        self.whites.add(self.entity,2);self.entity.title='renamed';self.entity.username='renamed_group'
        GroupWhitelist(self.db)
        self.assertEqual(exempt_source_ids(self.db),{self.gid})
    async def test_filtering_does_not_exempt_member_from_other_sources(self):
        self.whites.add(self.entity,2)
        groups=[{'id':self.gid,'title':'excluded'},{'id':-1000000000043,'title':'cheating'}]
        self.assertEqual(filter_check_groups(self.db,groups),[groups[1]])
    async def test_result_filter_rechecks_whitelist_after_inflight_lookup(self):
        self.whites.add(self.entity,2)
        telegram=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1,chat_id=7)))
        publish=nested('publish_check',{'db':self.db,'bot':telegram,'whitelisted':lambda uid:False,
            'common_groups':AsyncMock(return_value=bot.QueryGroups([{'id':self.gid,'title':'excluded'}])),
            'expire':lambda *args:None})
        self.assertFalse(await publish(7,1,person(9),'bot私聊',positive_only=True))
        telegram.send_message.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT matches FROM queries').fetchone()[0],0)
    async def test_add_command_and_literal_plus(self):
        for args in ('group https://t.me/fixture_group','group+https://t.me/fixture_group'):
            change=nested('change_group_whitelist',{'db':self.db,'bot':SimpleNamespace(),'users':[],
                'submissions':SimpleNamespace(group_whitelist=self.whites),'apply_group_whitelist':lambda:None,
                'resolve_whitelist_group':AsyncMock(return_value=self.entity)})
            event=SimpleNamespace(sender_id=1,chat_id=1,is_private=True,is_group=False,respond=AsyncMock(),reply=AsyncMock())
            await change(event,person(1),args)
            self.assertIn(self.gid,exempt_source_ids(self.db));event.respond.assert_awaited_once()
    async def test_nonadmin_rejected_before_resolution(self):
        resolver=AsyncMock()
        change=nested('change_group_whitelist',{'db':self.db,'resolve_whitelist_group':resolver})
        event=SimpleNamespace(reply=AsyncMock())
        await change(event,person(3),'group https://t.me/fixture_group');resolver.assert_not_awaited()
    async def test_menu_input_edits_existing_message(self):
        edit=AsyncMock()
        change=nested('change_group_whitelist',{'db':self.db,'bot':SimpleNamespace(edit_message=edit),'users':[],
            'submissions':SimpleNamespace(group_whitelist=self.whites),'apply_group_whitelist':lambda:None,
            'resolve_whitelist_group':AsyncMock(return_value=self.entity)})
        event=SimpleNamespace(sender_id=1,chat_id=1,is_private=True,is_group=False,respond=AsyncMock())
        await change(event,person(1),'group @fixture_group',menu_message_id=99)
        edit.assert_awaited_once();event.respond.assert_not_awaited()
    async def test_forged_remove_callback_cannot_delete_other_admin_entry(self):
        self.db.execute('INSERT INTO admins VALUES(3)');self.db.commit()
        self.whites.add(self.entity,2)
        callback=nested('on_group_whitelist',{'db':self.db,'submissions':SimpleNamespace(group_whitelist=self.whites)})
        event=SimpleNamespace(sender_id=3,is_private=True,chat_id=3,data=f'groupwhite:remove:{self.gid}:0'.encode(),answer=AsyncMock())
        await callback(event)
        self.assertIn(self.gid,exempt_source_ids(self.db));self.assertTrue(event.answer.call_args.kwargs['alert'])
    async def test_bulk_filter_removes_direct_and_scanned_hits(self):
        outcome={'groups':{self.gid:{'id':self.gid,'title':'exempt'},-1000000000043:{'id':-1000000000043,'title':'cheating'}}}
        matched=filter_bulk_hits(outcome,{self.gid},{self.gid})
        self.assertEqual(matched,{-1000000000043});self.assertEqual(set(outcome['groups']),matched)
    async def test_all_sources_exempt_returns_clean_without_rpc(self):
        lookup=AsyncMock()
        common=nested('common_groups',{'refresh_sources':AsyncMock(return_value=set()),
            'all_sources_exempt':lambda:True,'fetch_common_groups':lookup})
        self.assertEqual(await common(person(7)),[]);lookup.assert_not_awaited()
    async def test_english_group_whitelist_messages(self):
        with bot.language_context('en'):
            self.assertEqual(str(bot.tr('🛡️ 群组白名单')),'🛡️ Group whitelist')
            self.assertIn('excluded from cheating checks',str(bot.tr('群组白名单（这些群组不计入作弊查询）：\n')))


class LinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_message_private_and_invite_links(self):
        self.assertEqual(parse_group_reference('https://t.me/Fixture_group/88?x=1'),('username','fixture_group'))
        self.assertEqual(parse_group_reference('https://t.me/c/123456/7/8'),('id',-100123456))
        self.assertEqual(parse_group_reference('https://t.me/+abcdef_12'),('invite','abcdef_12'))
        self.assertEqual(parse_group_reference('https://t.me/joinchat/abcdef_12'),('invite','abcdef_12'))
        self.assertEqual(parse_group_reference('@fixture_group'),('username','fixture_group'))
        with self.assertRaises(ValueError):parse_group_reference('https://example.com/group')
    async def test_private_invite_uses_check_only_and_never_joins(self):
        entity=group();client=AsyncMock(return_value=types.ChatInviteAlready(entity))
        self.assertIs(await resolve_whitelist_group('https://t.me/+abcdef_12',[client]),entity)
        request=client.call_args.args[0]
        self.assertIsInstance(request,bot.functions.messages.CheckChatInviteRequest)
        self.assertEqual(client.await_count,1)
    async def test_broadcast_channels_are_rejected(self):
        channel=group();channel.megagroup=False
        client=SimpleNamespace(get_entity=AsyncMock(return_value=channel))
        with self.assertRaises(ValueError):await resolve_whitelist_group('@fixture_group',[client])
    async def test_unknown_invite_does_not_join(self):
        client=AsyncMock(return_value=SimpleNamespace(title='private'))
        with self.assertRaises(ValueError):await resolve_whitelist_group('https://t.me/+abcdef_12',[client])
        self.assertIsInstance(client.call_args.args[0],bot.functions.messages.CheckChatInviteRequest)


if __name__=='__main__':unittest.main()
