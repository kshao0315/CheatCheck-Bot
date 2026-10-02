"""Isolated new-group registration, notification and actual group-role checks."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import errors
import bot
from test_submissions import nested


GROUP = -1000000000777


class Telegram:
    def __init__(self):
        self.messages = []
        self.fail_group = self.fail_dm = False
        self.get_permissions = AsyncMock(return_value=SimpleNamespace(is_admin=False, is_creator=False))
    async def send_message(self, chat_id, text, **kwargs):
        if (chat_id == GROUP and self.fail_group) or (chat_id == 1 and self.fail_dm):
            raise errors.ChatWriteForbiddenError(None)
        self.messages.append((chat_id, str(text), kwargs))
        return SimpleNamespace(id=len(self.messages), chat_id=chat_id)


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript("""CREATE TABLE groups(chat_id INTEGER PRIMARY KEY,title TEXT,enabled INTEGER DEFAULT 0,owner_id INTEGER,language TEXT DEFAULT 'zh');
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO settings VALUES('superadmin','1');
            CREATE TABLE admins(user_id INTEGER PRIMARY KEY); INSERT INTO admins VALUES(2);
            CREATE TABLE user_languages(user_id INTEGER PRIMARY KEY,language TEXT); INSERT INTO user_languages VALUES(1,'en');""")
        self.telegram = Telegram()
        self.details = {'title': 'Fixture <group>', 'username': 'fixture_group'}
        self.roles = {1: 'member', 2: 'member', 3: 'administrator', 4: 'creator'}
        async def api(method, payload):
            if method == 'getChat':
                return self.details
            return {'status': self.roles.get(payload['user_id'], 'member')}
        self.api = AsyncMock(side_effect=api)
        self.service = bot.GroupOnboarding(self.db, self.telegram, self.api, 99)
    def tearDown(self):
        self.db.close()
    def joined(self, stamp=100):
        self.service.joined(GROUP, 'Fixture <group>', 'fixture_group',
                            {'id': 2, 'first_name': 'Manager', 'username': 'manager'}, stamp)
    def event(self, uid=3, chat_id=GROUP, generation=1, message_id=1, private=False):
        return SimpleNamespace(data=f'join_language:{GROUP}:{generation}:en'.encode(), sender_id=uid,
            chat_id=chat_id, is_private=private, query=SimpleNamespace(msg_id=message_id),
            answer=AsyncMock(), edit=AsyncMock(), get_sender=AsyncMock(return_value=SimpleNamespace(id=uid)))

    async def test_join_enables_queries_sends_one_prompt_and_superadmin_details(self):
        self.joined()
        self.assertEqual(self.db.execute('SELECT enabled,owner_id FROM groups').fetchone(), (1, 2))
        await self.service.deliver(GROUP)
        self.joined(101)  # Duplicate update from the second delivery mechanism.
        await self.service.deliver(GROUP)
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertIn('choose the language', self.telegram.messages[0][1])
        recipient, text, kwargs = self.telegram.messages[1]
        self.assertEqual(recipient, 1)
        self.assertIn('The bot joined a new group', text)
        self.assertIn('&lt;group&gt;', text)
        self.assertIn('https://t.me/fixture_group', text)
        self.assertIn('@manager', text)
        self.assertIn(str(GROUP), text)
        self.assertTrue(self.service.state(GROUP)['notified'])

    async def test_new_groups_default_on_but_existing_disabled_groups_stay_off(self):
        remember = nested('remember_group', {'db': self.db})
        remember(GROUP, 'Fixture')
        self.assertEqual(self.db.execute('SELECT enabled FROM groups').fetchone()[0], 1)
        self.db.execute('UPDATE groups SET enabled=0')
        remember(GROUP, 'Renamed')
        self.assertEqual(self.db.execute('SELECT enabled FROM groups').fetchone()[0], 0)

    async def test_duplicate_join_does_not_override_manual_disable(self):
        self.joined()
        self.db.execute('UPDATE groups SET enabled=0')
        self.joined(101)
        self.assertEqual(self.db.execute('SELECT enabled FROM groups').fetchone()[0], 0)
        self.assertEqual(self.service.state(GROUP)['generation'], 1)

    async def test_rejoin_reenables_queries_and_notifies_each_generation(self):
        self.joined()
        await self.service.deliver(GROUP)
        self.service.left(GROUP, event_at=101)
        self.db.execute('UPDATE groups SET enabled=0')
        self.joined(102)
        await self.service.deliver(GROUP)
        self.assertEqual(self.service.state(GROUP)['generation'], 2)
        self.assertEqual(self.db.execute('SELECT enabled FROM groups').fetchone()[0], 1)
        self.assertEqual([recipient for recipient, _, _ in self.telegram.messages], [GROUP, 1, GROUP, 1])

    async def test_quick_removal_does_not_lose_superadmin_notification(self):
        self.joined()
        self.service.left(GROUP, event_at=101)
        await self.service.deliver(GROUP, 1)
        self.assertEqual([recipient for recipient, _, _ in self.telegram.messages], [1])
        self.assertEqual(self.db.execute('SELECT notified FROM group_additions').fetchone()[0], 1)

    async def test_out_of_order_old_join_cannot_reactivate_removed_bot(self):
        self.joined()
        self.service.left(GROUP, event_at=102)
        self.joined(101)
        self.assertFalse(self.service.state(GROUP)['active'])

    async def test_ordinary_group_admin_can_select_english_without_bot_admin_role(self):
        self.joined()
        await self.service.deliver(GROUP)
        event = self.event()
        self.service.language_changed = AsyncMock()
        await self.service.choose_language(event)
        self.assertEqual(bot.group_language(self.db, GROUP), 'en')
        self.assertTrue(event.edit.await_args.args[0].startswith('✅ Checks are enabled'))
        self.service.language_changed.assert_awaited_once()

    async def test_bot_admin_and_superadmin_do_not_bypass_group_admin_requirement(self):
        self.joined()
        await self.service.deliver(GROUP)
        for uid in (1, 2, 5):
            event = self.event(uid=uid)
            await self.service.choose_language(event)
            self.assertEqual(bot.group_language(self.db, GROUP), 'zh')
            event.edit.assert_not_awaited()
            self.assertTrue(event.answer.await_args.kwargs['alert'])

    async def test_callback_rejects_wrong_group_message_private_and_old_generation(self):
        self.joined()
        await self.service.deliver(GROUP)
        self.api.reset_mock()
        for event in (self.event(chat_id=-42), self.event(message_id=77), self.event(private=True), self.event(generation=8)):
            await self.service.choose_language(event)
            event.edit.assert_not_awaited()
        self.api.assert_not_awaited()
        self.assertEqual(bot.group_language(self.db, GROUP), 'zh')

    async def test_language_selection_keeps_manually_disabled_group_disabled(self):
        self.joined()
        await self.service.deliver(GROUP)
        self.db.execute('UPDATE groups SET enabled=0')
        event = self.event(uid=4)
        await self.service.choose_language(event)
        self.assertEqual(self.db.execute('SELECT enabled,language FROM groups').fetchone(), (0, 'en'))
        self.assertIn('turned off', event.edit.await_args.args[0])

    async def test_group_prompt_failure_does_not_block_superadmin_notification(self):
        self.joined()
        self.telegram.fail_group = True
        await self.service.deliver(GROUP)
        self.assertEqual([recipient for recipient, _, _ in self.telegram.messages], [1])
        self.telegram.fail_group = False
        self.db.execute('UPDATE group_additions SET retry_at=0')
        await self.service.deliver(GROUP)
        self.assertEqual([recipient for recipient, _, _ in self.telegram.messages], [1, GROUP])

    async def test_dm_failure_retries_after_restart_without_duplicate_group_prompt(self):
        self.joined()
        self.telegram.fail_dm = True
        await self.service.deliver(GROUP)
        self.assertFalse(self.service.state(GROUP)['notified'])
        self.telegram.fail_dm = False
        self.db.execute('UPDATE group_additions SET retry_at=0')
        restarted = bot.GroupOnboarding(self.db, self.telegram, self.api, 99)
        await restarted.deliver(GROUP)
        self.assertEqual([recipient for recipient, _, _ in self.telegram.messages], [GROUP, 1])

    async def test_private_group_internal_link_is_labelled_and_no_invite_created(self):
        self.joined()
        self.details = {'title': 'Private group'}
        self.db.execute('UPDATE group_additions SET username=NULL')
        await self.service.deliver(GROUP)
        self.assertIn('you need to be a member', self.telegram.messages[1][1])
        self.assertIn('https://t.me/c/777/1', self.telegram.messages[1][1])
        self.assertFalse(any(call.args[0].startswith('create') for call in self.api.await_args_list))

    async def test_membership_promotion_is_not_a_new_join_and_channels_are_ignored(self):
        change = {'chat': {'id': GROUP, 'title': 'Fixture', 'type': 'supergroup'},
                  'from': {'id': 2}, 'date': 100,
                  'old_chat_member': {'status': 'left'},
                  'new_chat_member': {'status': 'member', 'user': {'id': 99}}}
        self.service.membership_update(change)
        change['date'] = 101
        change['old_chat_member'] = change['new_chat_member']
        change['new_chat_member'] = {'status': 'administrator', 'user': {'id': 99}}
        self.service.membership_update(change)
        self.assertEqual(self.service.state(GROUP)['generation'], 1)
        change['chat'] = {'id': -42, 'title': 'Channel', 'type': 'channel'}
        change['old_chat_member'] = {'status': 'left'}
        self.service.membership_update(change)
        self.assertIsNone(self.service.state(-42))


if __name__ == '__main__':
    unittest.main()
