"""Both accounts progress together; stalls and omitted references are bounded."""
import asyncio
import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import bot
from telethon import functions, types
from bot import BulkRPC, bulk_phase, seed_bulk_users, seed_bulk_group_users, bulk_result_state, bulk_eta


class Session:
    def __init__(self, uid):
        self.entities = {99: types.InputPeerUser(99, 100 + uid),
                         -1000000000777: types.InputPeerChannel(777, 1000 + uid)}
    def get_input_entity(self, uid):
        if uid not in self.entities:
            raise ValueError()
        return self.entities[uid]
    def save(self):
        pass


class ConcurrentPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_accounts_read_same_stable_group_batch_concurrently(self):
        entered = set()
        together = asyncio.Event()
        class Account:
            def __init__(self, uid):
                self.uid, self.session = uid, Session(uid)
            async def __call__(self, request):
                entered.add(self.uid)
                if len(entered) == 2:
                    together.set()
                await asyncio.wait_for(together.wait(), .5)
                result = []
                for ref in request.id:
                    self_hash = 10000 * self.uid + ref.user_id
                    self.session.entities[ref.user_id] = types.InputPeerUser(ref.user_id, self_hash)
                    result.append(types.User(ref.user_id, access_hash=self_hash))
                return result
        bot_client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(username='fixture')))
        async def edit(*args, **kwargs):
            return SimpleNamespace(entities=[types.MessageEntityMentionName(e.offset, e.length, e.user_id.user_id)
                                             for e in kwargs['formatting_entities']])
        bot_client.edit_message = AsyncMock(side_effect=edit)
        context = SimpleNamespace(chat_id=-1000000000777, id=123, edit=AsyncMock())
        clients = [Account(1), Account(2)]
        result = await seed_bulk_group_users(bot_client, clients, [types.User(7, access_hash=77)],
                                             BulkRPC(), context)
        self.assertEqual(list(result.values()), [1, 1])
        self.assertEqual(entered, {1, 2})
        self.assertEqual(bot_client.edit_message.await_count, 1)
        self.assertNotEqual(clients[0].session.entities[7].access_hash, clients[1].session.entities[7].access_hash)

    async def test_private_omissions_stop_without_individual_retry_explosion(self):
        sent, calls = {}, []
        entered = set()
        both = asyncio.Event()
        async def send(destination, text, **kwargs):
            sent[destination.user_id] = text
            calls.append((destination.user_id, len(kwargs['formatting_entities'])))
            return SimpleNamespace(id=500 + destination.user_id)
        bot_client = SimpleNamespace(get_input_entity=AsyncMock(side_effect=lambda uid: types.InputPeerUser(uid, uid)),
                                     send_message=AsyncMock(side_effect=send), delete_messages=AsyncMock())
        class Account:
            def __init__(self, uid):
                self.uid, self.session = uid, Session(uid)
            async def get_me(self):
                return SimpleNamespace(id=self.uid)
            async def __call__(self, request):
                entered.add(self.uid)
                if len(entered) == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), .5)
                return SimpleNamespace(messages=[SimpleNamespace(id=self.uid, message=sent[self.uid], entities=[])])
        clients = [Account(1), Account(2)]
        members = [types.User(uid, access_hash=uid) for uid in range(100, 300)]
        counts = await seed_bulk_users(bot_client, clients, SimpleNamespace(id=99), members, BulkRPC())
        self.assertEqual(entered, {1, 2})
        self.assertEqual(list(counts.values()), [0, 0])
        self.assertEqual(sorted(calls), [(1, 20), (1, 20), (2, 20), (2, 20)])
        self.assertEqual(bot_client.delete_messages.await_count, 4)

    async def test_stalled_account_is_cancelled_without_blocking_healthy_account(self):
        sent, cancelled = {}, asyncio.Event()
        async def send(destination, text, **kwargs):
            sent[destination.user_id] = text
            return SimpleNamespace(id=destination.user_id)
        bot_client = SimpleNamespace(get_input_entity=AsyncMock(side_effect=lambda uid: types.InputPeerUser(uid, uid)),
                                     send_message=AsyncMock(side_effect=send), delete_messages=AsyncMock())
        class Account:
            def __init__(self, uid):
                self.uid, self.session = uid, Session(uid)
            async def get_me(self):
                return SimpleNamespace(id=self.uid)
            async def __call__(self, request):
                if self.uid == 1:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                if isinstance(request, functions.messages.GetHistoryRequest):
                    return SimpleNamespace(messages=[SimpleNamespace(id=7002, message=sent[2],
                        entities=[types.MessageEntityMentionName(0, 1, 7)])])
                entity = types.User(7, access_hash=207)
                self.session.entities[7] = types.InputPeerUser(7, 207)
                return [entity]
        clients = [Account(1), Account(2)]
        result = await seed_bulk_users(bot_client, clients, SimpleNamespace(id=99),
                                       [types.User(7, access_hash=77)], BulkRPC(), account_timeout=.05)
        self.assertEqual(list(result.values()), [0, 1])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(bot_client.delete_messages.await_count, 2)


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_eta_does_not_exceed_phase_or_job_deadline(self):
        self.assertEqual(bulk_eta(4689, 180, 500), 180)
        self.assertEqual(bulk_eta(4689, 180, 90), 90)
        self.assertEqual(bulk_eta(20, 180, 90), 20)
        self.assertEqual(bulk_eta(20, -1, 90), 0)
        self.assertIsNone(bulk_eta(None, 180, 90))

    async def test_rpc_timeout_includes_waiting_for_semaphore(self):
        rpc = BulkRPC()
        async def client(request):
            await asyncio.Event().wait()
        request = functions.users.GetUsersRequest([types.InputUser(7, 77)])
        rpc.limits[(id(client), type(request))] = asyncio.Semaphore(0)
        with self.assertRaises(asyncio.TimeoutError):
            await rpc.call(client, request, total_timeout=.02)

    async def test_phase_timeout_cancels_workers_and_preserves_collected_hits(self):
        evidence, cancelled = {1}, asyncio.Event()
        async def worker():
            evidence.add(2)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.assertFalse(await bulk_phase(worker(), .02, 'fixture'))
        self.assertEqual(evidence, {1, 2})
        self.assertTrue(cancelled.is_set())
        self.assertEqual(bulk_result_state({1, 2, 3}, {1}, set(), evidence), (2, {2, 3}))
        self.assertEqual(bulk_result_state({1, 2, 3}, {1}, set(), set()), (None, {2, 3}))

    async def test_parent_cancellation_is_not_swallowed(self):
        worker = asyncio.create_task(bulk_phase(asyncio.Event().wait(), 100, 'fixture'))
        await asyncio.sleep(0)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker

    async def test_progress_persists_before_any_message_edit(self):
        tree = ast.parse(Path('bot.py').read_text(encoding='utf-8'))
        progress = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'progress_loop')
        updates = [n.lineno for n in ast.walk(progress) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and 'UPDATE bulk_jobs SET phase=' in n.value]
        edits = [n.lineno for n in ast.walk(progress) if isinstance(n, ast.Attribute) and n.attr == 'edit']
        self.assertLess(min(updates), min(edits))


class SourceScanTests(unittest.IsolatedAsyncioTestCase):
    def scanner(self, rpc, timeout=.03, sources=(1,)):
        account = SimpleNamespace(session=SimpleNamespace(get_input_entity=lambda gid: types.InputPeerChannel(gid, 123)))
        scope = dict(vars(bot))
        scope.update(account_rpc=BulkRPC(), users=[account], account_scopes={id(account): set(sources)}, member_ids={7},
                     bulk_pool=bot.AccountPool([account], BulkRPC()),
                     needed_sources=set(sources), rpc=rpc, source_hits={}, hit_ids=set(), complete_sources=set(),
                     source_errors={}, BULK_SOURCE_SINGLE_TIMEOUT=timeout,
                     progress={'source_expected': 0, 'source_known': 0, 'source_seen': 0, 'source_done': 0})
        tree = ast.parse(Path('bot.py').read_text(encoding='utf-8'))
        node = copy.deepcopy(next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'scan_sources'))
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), scope)
        return scope['scan_sources'], scope

    async def test_repeated_source_page_stops_without_looping_forever(self):
        calls = 0
        async def call(client, request, **kwargs):
            nonlocal calls
            if isinstance(request, functions.channels.GetFullChannelRequest):
                return SimpleNamespace(full_chat=SimpleNamespace(participants_count=1000, participants_hidden=False))
            calls += 1
            return SimpleNamespace(count=1000, users=[types.User(7, access_hash=77)], participants=[SimpleNamespace()])
        scan, scope = self.scanner(SimpleNamespace(call=call))
        await scan()
        self.assertEqual(calls, 2)
        self.assertEqual(scope['source_hits'], {7: {1}})
        self.assertEqual(scope['complete_sources'], set())
        self.assertEqual(scope['progress']['source_done'], 1)

    async def test_source_timeout_keeps_hits_already_found_on_first_page(self):
        calls = 0
        async def call(client, request, **kwargs):
            nonlocal calls
            if isinstance(request, functions.channels.GetFullChannelRequest):
                return SimpleNamespace(full_chat=SimpleNamespace(participants_count=1000, participants_hidden=False))
            calls += 1
            if calls > 1:
                await asyncio.Event().wait()
            return SimpleNamespace(count=1000, users=[types.User(7, access_hash=77)], participants=[SimpleNamespace()])
        scan, scope = self.scanner(SimpleNamespace(call=call))
        await scan()
        self.assertEqual(scope['source_hits'], {7: {1}})
        self.assertIn('source_scan_timeout', scope['source_errors'][1])
        self.assertEqual(scope['progress']['source_done'], 1)

    async def test_global_cancellation_keeps_partial_source_evidence(self):
        ready = asyncio.Event()
        calls = 0
        async def call(client, request, **kwargs):
            nonlocal calls
            if isinstance(request, functions.channels.GetFullChannelRequest):
                return SimpleNamespace(full_chat=SimpleNamespace(participants_count=1000, participants_hidden=False))
            calls += 1
            if calls > 1:
                ready.set()
                await asyncio.Event().wait()
            return SimpleNamespace(count=1000, users=[types.User(7, access_hash=77)], participants=[SimpleNamespace()])
        scan, scope = self.scanner(SimpleNamespace(call=call), timeout=100)
        task = asyncio.create_task(scan())
        await asyncio.wait_for(ready.wait(), .5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(scope['source_hits'], {7: {1}})
        self.assertEqual(scope['complete_sources'], set())


if __name__ == '__main__':
    unittest.main()
