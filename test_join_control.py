"""Persistent join stop leaves all query accounts usable."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
from group_imports import GroupImportQueue
from join_control import automatic_join_enabled
from test_submissions import group, person


class StopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute("INSERT INTO settings VALUES('automatic_join_enabled','0')")
        self.db.execute("INSERT INTO settings VALUES('superadmin','1')")
        self.db.commit()
        self.clients=[SimpleNamespace(get_entity=AsyncMock()) for _ in range(3)]
        self.refresh=AsyncMock()
        self.service=bot.GroupSubmissions(self.db,None,self.clients,self.refresh)
    def tearDown(self):self.db.close()
    async def test_paused_join_account_and_cycle_do_not_call_clients(self):
        self.assertFalse(await self.service.join_account(999,0))
        await self.service.join_cycle()
        for client in self.clients:client.get_entity.assert_not_awaited()
        self.refresh.assert_not_awaited()
    async def test_paused_sync_does_not_read_or_join(self):
        with tempfile.TemporaryDirectory() as directory:
            sync=bot.AccountGroupSync(self.db,self.clients,bot.BulkRPC(),self.service,Path(directory)/'sync.json')
            self.assertIsNone(await sync.cycle(force=True))
        for client in self.clients:client.get_entity.assert_not_awaited()
    async def test_paused_csv_does_not_create_join_tasks(self):
        queue=GroupImportQueue(self.db)
        queue.enqueue(b'fixture_group\n',1)
        self.assertEqual(self.db.execute('SELECT state FROM source_group_imports').fetchone()[0],'closed')
        self.assertEqual(await queue.cycle(self.service),0)
    async def test_new_approved_request_gets_closed_jobs(self):
        rid=self.service.create(group(),'fixture_group',person(1),approved=True)
        self.assertEqual(self.db.execute('SELECT state,count(*) FROM source_join_jobs WHERE request_id=? GROUP BY state',(rid,)).fetchall(),[('closed',3)])
    async def test_restart_and_new_account_do_not_restore_jobs(self):
        self.service.create(group(),'fixture_group',person(1),approved=True)
        bot.GroupSubmissions(self.db,None,self.clients+[SimpleNamespace()],self.refresh)
        self.assertEqual(self.db.execute('SELECT state,count(*) FROM source_join_jobs GROUP BY state').fetchall(),[('closed',4)])
    async def test_missing_stop_flag_keeps_existing_installations_enabled(self):
        self.db.execute("DELETE FROM settings WHERE key='automatic_join_enabled'");self.db.commit()
        self.assertTrue(automatic_join_enabled(self.db))
    async def test_same_database_stop_is_read_without_restart(self):
        self.assertFalse(automatic_join_enabled(self.db))
        self.db.execute("UPDATE settings SET value='1' WHERE key='automatic_join_enabled'");self.db.commit()
        self.assertTrue(automatic_join_enabled(self.db))


if __name__=='__main__':unittest.main()
