"""Review lifecycle and command routing with isolated databases and Telegram fakes."""

import ast
import asyncio
import copy
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors, functions, types
import bot


def person(uid):
    return SimpleNamespace(id=uid, first_name=f"User {uid}", last_name=None, username=f"u{uid}")


def group(gid=42):
    return types.Channel(id=gid, title="Fixture group", photo=types.ChatPhotoEmpty(),
                         date=None, megagroup=True, access_hash=321, username="fixture_group")


class FakeBot:
    def __init__(self):
        self.sent, self.edited, self.blocked = [], [], set()

    async def send_message(self, recipient, text, **options):
        if recipient in self.blocked:
            raise errors.UserIsBlockedError(None)
        self.sent.append((recipient, text, options))
        return SimpleNamespace(id=len(self.sent))

    async def edit_message(self, recipient, message_id, text, **options):
        self.edited.append((recipient, message_id, text, options))

    async def get_entity(self, username):
        return group()


class FakeAccount:
    def __init__(self):
        self.member, self.calls, self.entity, self.join_error = False, [], group(), None

    async def get_input_entity(self, value):
        raise ValueError("uncached")

    async def get_entity(self, value):
        return self.entity

    async def iter_dialogs(self):
        if self.member:
            yield SimpleNamespace(entity=self.entity)

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.channels.GetParticipantRequest):
            if not self.member:
                raise errors.UserNotParticipantError(request)
            return SimpleNamespace(participant=types.ChannelParticipantSelf(1, None, 1))
        if isinstance(request, functions.channels.GetParticipantsRequest):
            return types.channels.ChannelParticipants(count=1,participants=[SimpleNamespace(user_id=1)],
                chats=[],users=[types.User(1,username='owner')])
        if isinstance(request, functions.channels.JoinChannelRequest):
            if self.join_error:
                exc, self.join_error = self.join_error, None
                raise exc
            self.member = True
        return SimpleNamespace()


def nested(name, namespace):
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "main")
    node = copy.deepcopy(next(node for node in main.body if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name))
    node.decorator_list = []
    node.body = [ast.copy_location(ast.Global(names=statement.names), statement)
                 if isinstance(statement, ast.Nonlocal) else statement for statement in node.body]
    scope = dict(vars(bot))
    scope.update(namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(Path(bot.__file__)), "exec"), scope)
    return scope[name]


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO settings VALUES('superadmin','1');
            CREATE TABLE admins(user_id INTEGER PRIMARY KEY); INSERT INTO admins VALUES(2);""")
        self.telegram = FakeBot()
        self.accounts = [FakeAccount(), FakeAccount()]
        self.refresh = AsyncMock(return_value=set())
        self.service = bot.GroupSubmissions(self.db, self.telegram, self.accounts, self.refresh)

    def tearDown(self):
        self.db.close()

    def create(self, approved=False, sender=100):
        return self.service.create(group(), "fixture_group", person(sender), approved)

    async def test_pending_not_a_source_and_does_not_join(self):
        request_id = self.create()
        await self.service.cycle()
        self.assertEqual(self.service.request(request_id)["status"], "pending")
        self.assertNotIn(bot.telegram_id(group()), self.service.filter_sources({bot.telegram_id(group())}))
        self.assertTrue(all(not account.calls for account in self.accounts))
        self.assertEqual({m[0] for m in self.telegram.sent}, {1, 2})
        await self.service.notifications()
        self.assertEqual(len(self.telegram.sent), 2)

    async def test_approval_updates_all_reviews_and_feedback_then_joins_both(self):
        request_id = self.create()
        await self.service.notifications()
        self.assertTrue(self.service.decide(request_id, person(2), "approved"))
        self.assertFalse(self.service.decide(request_id, person(1), "rejected"))
        await self.service.cycle()
        self.assertTrue(all(account.member for account in self.accounts))
        self.assertIn(bot.telegram_id(group()), self.service.filter_sources(set()))
        self.assertEqual(len(self.telegram.edited), 2)
        self.assertTrue(all(m[3]["buttons"] is None and "已通过" in m[2] for m in self.telegram.edited))
        feedback = [m for m in self.telegram.sent if m[0] == 100]
        self.assertEqual(len(feedback), 1)
        self.assertIn("处理人：@u2", feedback[0][1])
        await self.service.cycle()
        self.assertEqual(len([m for m in self.telegram.sent if m[0] == 100]), 1)
        self.assertEqual(sum(isinstance(call, functions.channels.JoinChannelRequest) for a in self.accounts for call in a.calls), 2)

    async def test_rejection_does_not_join_or_enable_source(self):
        request_id = self.create()
        await self.service.notifications()
        self.service.decide(request_id, person(1), "rejected")
        await self.service.cycle()
        self.assertTrue(all(not a.calls for a in self.accounts))
        self.assertEqual(self.service.filter_sources({bot.telegram_id(group())}), set())
        self.assertIn("未通过", self.telegram.sent[-1][1])
        self.assertIn("处理人：@u1", self.telegram.sent[-1][1])

    async def test_direct_add_requires_live_admin_permission(self):
        with self.assertRaises(PermissionError):
            self.create(approved=True)
        request_id = self.create(approved=True, sender=2)
        await self.service.cycle()
        self.assertEqual(self.service.request(request_id)["reviewer_id"], 2)
        self.assertTrue(all(a.member for a in self.accounts))

    async def test_duplicate_pending_and_already_approved(self):
        request_id = self.create()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.create(approved=True, sender=1), request_id)
        self.assertEqual(self.service.request(request_id)["status"], "approved")
        with self.assertRaises(ValueError):
            self.create()

    async def test_revoked_role_and_wrong_message_cannot_review(self):
        request_id = self.create()
        self.service.bind_message(request_id, 2, 88)
        self.assertTrue(self.service.can_review_message(request_id, 2, 88))
        self.assertFalse(self.service.can_review_message(request_id, 1, 88))
        self.assertFalse(self.service.can_review_message(request_id, 2, 89))
        self.db.execute("DELETE FROM admins WHERE user_id=2")
        self.db.commit()
        self.assertFalse(self.service.can_review_message(request_id, 2, 88))
        with self.assertRaises(PermissionError):
            self.service.decide(request_id, person(2), "approved")
        self.assertEqual(self.service.request(request_id)["status"], "pending")

    async def test_short_flood_is_persisted_and_does_not_stop_second_account(self):
        request_id = self.create(approved=True, sender=2)
        self.accounts[0].join_error = errors.FloodWaitError(None, capture=28)
        await self.service.cycle()
        rows = self.db.execute("SELECT state,retry_at FROM source_join_jobs WHERE request_id=? ORDER BY account_index", (request_id,)).fetchall()
        self.assertEqual(rows[0][0], "waiting_limit")
        self.assertGreater(rows[0][1], 0)
        self.assertEqual(rows[1][0], "joined")
        restarted = bot.GroupSubmissions(self.db, self.telegram, self.accounts, self.refresh)
        self.db.execute("UPDATE source_join_jobs SET retry_at=0")
        self.db.commit()
        await restarted.cycle()
        self.assertTrue(all(a.member for a in self.accounts))

    async def test_invite_approval_is_pending_not_claimed_joined(self):
        request_id = self.create(approved=True, sender=2)
        self.accounts[0].join_error = errors.InviteRequestSentError(None)
        await self.service.cycle()
        state = self.db.execute("SELECT state FROM source_join_jobs WHERE request_id=? AND account_index=0", (request_id,)).fetchone()[0]
        self.assertEqual(state, "waiting_approval")
        self.assertFalse(self.accounts[0].member)

    async def test_changed_username_cannot_join_a_different_group(self):
        self.create(approved=True, sender=2)
        self.accounts[0].entity = group(999)
        await self.service.cycle()
        self.assertFalse(self.accounts[0].member)
        self.assertTrue(self.accounts[1].member)

    async def test_existing_member_does_not_join_again(self):
        self.accounts[0].member = True
        self.create(approved=True, sender=2)
        await self.service.cycle()
        self.assertFalse(any(isinstance(c, functions.channels.JoinChannelRequest) for c in self.accounts[0].calls))

    async def test_new_membership_clears_old_query_cache_even_if_source_id_is_unchanged(self):
        self.create(approved=True, sender=2)
        gid = bot.telegram_id(group())
        refresh = nested("refresh_sources", {"db": self.db, "users": self.accounts, "submissions": self.service,
            "source_ids": {gid}, "source_accounts": {id(a): set() for a in self.accounts},
            "source_updated": 0, "indexed_sources": {"old": "cache"}, "source_revision": 1,
            "common_cache": {"old": "query"}, "source_refresh_lock": asyncio.Lock(), "membership_cache": {},
            "source_unavailable": set(), "source_registry": frozenset()})
        self.accounts[0].member = True
        await refresh(force=True)
        self.assertEqual(refresh.__globals__["common_cache"], {})
        self.assertIsNone(refresh.__globals__["indexed_sources"])
        self.assertEqual(refresh.__globals__["source_revision"], 2)

    async def test_regular_user_submit_and_button_text_route(self):
        handler = AsyncMock()
        pending = {100: ("submit", bot.time.monotonic() + 120, 73)}
        message = nested("on_message", {"bot_me": person(999), "db": self.db, "pending": pending, "submit_group": handler})
        for text in ("fixture_group", "/submit @fixture_group"):
            event = SimpleNamespace(raw_text=text, is_private=True, get_sender=AsyncMock(return_value=person(100)))
            await message(event)
        self.assertEqual(handler.await_count, 2)
        self.assertEqual(handler.await_args_list[0].kwargs["menu_message_id"], 73)

    async def test_group_submission_does_not_create_request(self):
        handler, claim_owner = AsyncMock(), AsyncMock()
        message = nested("on_message", {"bot_me": person(999), "db": self.db, "pending": {}, "submit_group": handler,
            "remember_user_message": lambda *args: None, "remember_group": lambda *args: None,
            "enabled": lambda chat_id: False, "sync_group_menu_background": AsyncMock(),
            "claim_group_owner": claim_owner})
        event = SimpleNamespace(raw_text="/submit fixture_group", is_private=False, is_group=True,
            chat_id=-123, id=7, get_sender=AsyncMock(return_value=person(100)), get_chat=AsyncMock(return_value=group()))
        await message(event)
        handler.assert_not_awaited()
        claim_owner.assert_awaited_once_with(-123, 100)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM source_submissions").fetchone()[0], 0)

    async def test_private_direct_add_denies_regular_user_before_resolution(self):
        self.telegram.get_entity = AsyncMock()
        submit = nested("submit_group", {"db": self.db, "bot": self.telegram})
        event = SimpleNamespace(is_private=True, respond=AsyncMock())
        await submit(event, person(100), "fixture_group", direct=True)
        self.telegram.get_entity.assert_not_awaited()
        self.assertIn("管理员权限", event.respond.await_args.args[0])

    async def test_pending_direct_add_route_preserves_admin_authorization_command(self):
        handler = AsyncMock()
        message = nested("on_message", {"bot_me": person(999), "db": self.db, "pending": {}, "submit_group": handler})
        event = SimpleNamespace(raw_text="/add group fixture_group", is_private=True, get_sender=AsyncMock(return_value=person(2)))
        await message(event)
        self.assertTrue(handler.await_args.kwargs["direct"])
        self.assertEqual(handler.await_args.args[2], "fixture_group")


class BulkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_owner_and_actor_as_code_one_id_per_line(self):
        telegram = FakeBot()
        self.assertEqual(await bot.deliver_bulk_result(telegram, "1", 2, "结果\n", [22, 33]), [])
        self.assertEqual([m[0] for m in telegram.sent], [1, 2])
        self.assertIn("<pre>22\n33</pre>", telegram.sent[0][1])

    async def test_superadmin_actor_gets_exactly_one_copy(self):
        telegram = FakeBot()
        await bot.deliver_bulk_result(telegram, "1", 1, "结果\n", [22])
        self.assertEqual(len(telegram.sent), 1)

    async def test_blocked_owner_does_not_discard_actor_result(self):
        telegram = FakeBot()
        telegram.blocked.add(1)
        with self.assertLogs("cheatcheck", level="ERROR"):
            self.assertEqual(await bot.deliver_bulk_result(telegram, "1", 2, "结果\n", []), [1])
        self.assertEqual(telegram.sent[0][0], 2)

    async def test_multiple_chunks_for_both_recipients(self):
        telegram = FakeBot()
        await bot.deliver_bulk_result(telegram, "1", 2, "结果\n", list(range(501)))
        self.assertEqual([m[0] for m in telegram.sent], [1, 1, 1, 2, 2, 2])


class UsernameTests(unittest.TestCase):
    def test_supported_formats_and_invalid_invites(self):
        for value in ("@Fixture_Group", "Fixture_Group", "https://t.me/Fixture_Group/"):
            self.assertEqual(bot.group_username(value), "fixture_group")
        for value in ("12345", "https://t.me/+secret", "name extra", "https://t.me/name/12", ""):
            with self.assertRaises(ValueError):
                bot.group_username(value)


if __name__ == "__main__":
    unittest.main()
