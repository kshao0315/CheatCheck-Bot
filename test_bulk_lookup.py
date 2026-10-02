"""Regressions for short rate limits, hidden members, and partial account coverage."""

import unittest
from types import SimpleNamespace

from telethon import errors, types, functions
from telethon.tl.functions.messages import GetCommonChatsRequest

from bot import BulkRPC, bulk_result_state, seed_bulk_users


class FakeClock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class BulkRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_flood_retries_the_same_source_page(self):
        clock = FakeClock()
        rpc = BulkRPC(clock=clock.monotonic, sleep=clock.sleep)
        request = functions.channels.GetParticipantsRequest(
            types.InputChannel(123, 999), types.ChannelParticipantsSearch(""), 400, 200, 0)
        seen = []

        async def client(call):
            seen.append(call.offset)
            if len(seen) == 1:
                raise errors.FloodWaitError(call, capture=28)
            return "page 400 restored"

        self.assertEqual(await rpc.call(client, request), "page 400 restored")
        self.assertEqual(seen, [400, 400])
        self.assertEqual(clock.waits, [29])

    async def test_long_username_flood_keeps_cached_common_chat_queries_available(self):
        clock = FakeClock()
        rpc = BulkRPC(clock=clock.monotonic, sleep=clock.sleep)

        async def client(request):
            if isinstance(request, functions.contacts.ResolveUsernameRequest):
                raise errors.FloodWaitError(request, capture=3600)
            return "cached user queried"

        with self.assertRaises(errors.FloodWaitError):
            await rpc.call(client, functions.contacts.ResolveUsernameRequest("member"), max_wait=30)
        self.assertEqual(await rpc.call(client, GetCommonChatsRequest(types.InputUser(1, 2), 0, 100)),
                         "cached user queried")
        self.assertEqual(clock.waits, [])

    async def test_second_account_remains_available_during_first_account_cooldown(self):
        clock = FakeClock()
        rpc = BulkRPC(clock=clock.monotonic, sleep=clock.sleep)
        request = GetCommonChatsRequest(types.InputUser(1, 2), 0, 100)

        async def primary(call):
            raise errors.FloodWaitError(call, capture=3600)

        async def secondary(call):
            return "secondary result"

        with self.assertRaises(errors.FloodWaitError):
            await rpc.call(primary, request)
        self.assertEqual(await rpc.call(secondary, request), "secondary result")

    async def test_timeout_is_retried(self):
        clock = FakeClock()
        rpc = BulkRPC(clock=clock.monotonic, sleep=clock.sleep)
        calls = 0

        async def client(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError()
            return []

        self.assertEqual(await rpc.call(client, GetCommonChatsRequest(types.InputUser(1, 2), 0, 100)), [])
        self.assertEqual(calls, 2)


class CoverageTests(unittest.TestCase):
    def test_secondary_only_negative_stays_unconfirmed(self):
        self.assertEqual(bulk_result_state({1, 2, 3}, {1, 2}, set(), set()), (None, {3}))

    def test_complete_remaining_source_establishes_negative(self):
        self.assertEqual(bulk_result_state({1, 2, 3}, {1, 2}, {3}, set()), (0, set()))

    def test_partial_scan_hit_is_retained(self):
        self.assertEqual(bulk_result_state({1, 2, 3}, set(), {1}, {2}), (1, {2, 3}))


class EntityPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_reference_resolves_members_without_private_mentions(self):
        chat_id = -1000000000777

        class Session:
            def __init__(self, scoped_hash):
                self.entities = {chat_id: types.InputPeerChannel(777, scoped_hash)}

            def get_input_entity(self, uid):
                if uid not in self.entities:
                    raise ValueError()
                return self.entities[uid]

            def save(self):
                pass

        class Account:
            def __init__(self, index):
                self.index = index
                self.session = Session(1000 + index)

            async def __call__(self, request):
                assert isinstance(request, functions.users.GetUsersRequest)
                result = []
                for reference in request.id:
                    assert reference.msg_id == 123
                    assert reference.peer.access_hash == 1000 + self.index
                    entity = types.User(reference.user_id, access_hash=10000 * self.index + reference.user_id)
                    self.session.entities[entity.id] = types.InputPeerUser(entity.id, entity.access_hash)
                    result.append(entity)
                return result

        class Bot:
            def __init__(self):
                self.batch_sizes = []
                self.restored = False

            async def get_entity(self, uid):
                return SimpleNamespace(username="fixture_group")

            async def edit_message(self, uid, message_id, text, **kwargs):
                assert uid == chat_id and message_id == 123
                entities = kwargs.get("formatting_entities", [])
                if entities:
                    self.batch_sizes.append(len(entities))
                else:
                    self.restored = True
                return SimpleNamespace(entities=[types.MessageEntityMentionName(
                    entity.offset, entity.length, entity.user_id.user_id) for entity in entities])

            async def send_message(self, *args, **kwargs):
                raise AssertionError("Group references should resolve all active members")

        bot_client = Bot()
        context = SimpleNamespace(chat_id=chat_id, id=123)

        async def restore(text, **kwargs):
            await bot_client.edit_message(chat_id, 123, text, **kwargs)

        context.edit = restore
        clients = [Account(1), Account(2)]
        members = [types.User(uid, access_hash=200 + uid, deleted=(uid == 3)) for uid in range(1, 46)]
        counts = await seed_bulk_users(bot_client, clients, SimpleNamespace(id=99), members,
                                      BulkRPC(), context_message=context)
        self.assertEqual(list(counts.values()), [44, 44])
        self.assertEqual(bot_client.batch_sizes, [20, 20, 4])
        self.assertTrue(bot_client.restored)
        self.assertEqual(clients[0].session.get_input_entity(1).access_hash, 10001)
        self.assertEqual(clients[1].session.get_input_entity(1).access_hash, 20001)

    async def test_account_hashes_and_recipient_message_ids_are_separate(self):
        class FakeSession:
            def __init__(self):
                self.entities = {99: types.InputPeerUser(99, 555)}

            def get_input_entity(self, uid):
                if uid not in self.entities:
                    raise ValueError()
                return self.entities[uid]

            def save(self):
                pass

        class FakeBot:
            def __init__(self):
                self.sent = {}
                self.removed = []
                self.sent_batches = []

            async def get_input_entity(self, uid):
                return types.InputPeerUser(uid, 888)

            async def send_message(self, destination, text, **kwargs):
                self.sent[destination.user_id] = (text, kwargs["formatting_entities"])
                self.sent_batches.append(kwargs["formatting_entities"])
                return SimpleNamespace(id=5000 + destination.user_id)

            async def delete_messages(self, destination, ids, **kwargs):
                self.removed.extend(ids)

        bot_client = FakeBot()

        class FakeAccount:
            def __init__(self, uid, hash_base):
                self.uid, self.hash_base = uid, hash_base
                self.session = FakeSession()
                self.reference_ids = []

            async def get_me(self):
                return SimpleNamespace(id=self.uid)

            async def __call__(self, request):
                if isinstance(request, functions.messages.GetHistoryRequest):
                    text, entities = bot_client.sent[self.uid]
                    # The server omitted an invalid mention. It must not poison the batch.
                    return SimpleNamespace(messages=[SimpleNamespace(
                        id=7000 + self.uid, message=text,
                        entities=[types.MessageEntityMentionName(entity.offset, entity.length, entity.user_id.user_id)
                                  for entity in entities if entity.user_id.user_id != 2][:20])])
                if isinstance(request, functions.users.GetUsersRequest):
                    result = []
                    for reference in request.id:
                        self.reference_ids.append(reference.msg_id)
                        self.assert_reference(reference)
                        user = types.User(reference.user_id, access_hash=self.hash_base + reference.user_id)
                        self.session.entities[user.id] = types.InputPeerUser(user.id, user.access_hash)
                        result.append(user)
                    return result
                raise AssertionError(type(request))

            def assert_reference(self, reference):
                assert reference.user_id != 2
                assert reference.msg_id == 7000 + self.uid

        clients = [FakeAccount(10, 10000), FakeAccount(20, 20000)]
        members = [types.User(uid, access_hash=100 + uid, deleted=(uid == 3)) for uid in range(1, 45)]
        rpc = BulkRPC()
        counts = await seed_bulk_users(bot_client, clients, SimpleNamespace(id=99, username="checker"), members, rpc)
        self.assertEqual(list(counts.values()), [42, 42])
        self.assertEqual(clients[0].session.get_input_entity(1).access_hash, 10001)
        self.assertEqual(clients[1].session.get_input_entity(1).access_hash, 20001)
        self.assertEqual(clients[0].reference_ids, [7010] * 42)
        self.assertEqual(clients[1].reference_ids, [7020] * 42)
        self.assertEqual(len(bot_client.removed), 6)
        self.assertTrue(all(len(batch) <= 20 for batch in bot_client.sent_batches))
        self.assertTrue(all(entity.user_id.user_id != 3 for batch in bot_client.sent_batches for entity in batch))
        self.assertTrue(all(len(batch) > 1 for batch in bot_client.sent_batches))


if __name__ == "__main__":
    unittest.main(verbosity=2)
