"""Checks for concurrent full account scopes and a fresh merged result."""
import asyncio
import ast
import copy
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
from test_account_pool import Account
from test_submissions import nested, person


class FanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_scopes_all_start_before_any_finish_and_merge(self):
        clients=[Account(i) for i in range(1,4)]
        entered=set();ready=asyncio.Event();seen={}
        scopes={id(clients[0]):{1,2},id(clients[1]):{2,3},id(clients[2]):{1,3}}
        async def lookup(client,scope):
            entered.add(client.number);seen[client.number]=set(scope)
            if len(entered)==3:ready.set()
            await asyncio.wait_for(ready.wait(),.2)
            # Each account contributes a distinct hit even in overlapping scopes.
            return [{'id':client.number,'title':str(client.number)}, {'id':1,'title':'shared'}]
        groups,covered,failures=await bot.AccountPool(clients,bot.BulkRPC()).query(
            {1,2,3},scopes,lookup,7,scoped_lookup=True)
        self.assertEqual(seen,{1:{1,2},2:{2,3},3:{1,3}})
        self.assertEqual([g['id'] for g in groups],[1,2,3])
        self.assertEqual(covered,{1,2,3});self.assertFalse(failures)

    async def test_empty_first_account_does_not_hide_other_positive_results(self):
        clients=[Account(i) for i in range(1,4)]
        calls=[]
        async def lookup(client):
            calls.append(client.number)
            return [] if client.number==1 else [{'id':client.number,'title':'hit'}]
        groups,covered,failures=await bot.AccountPool(clients,bot.BulkRPC()).query(
            {1,2,3},{id(c):{1,2,3} for c in clients},lookup,7)
        self.assertEqual(calls,[1,2,3]);self.assertEqual([g['id'] for g in groups],[2,3])
        self.assertFalse(failures);self.assertEqual(covered,{1,2,3})

    async def test_timeout_keeps_two_healthy_accounts_results(self):
        clients=[Account(i) for i in range(1,4)]
        async def lookup(client):
            if client.number==1:
                await asyncio.wait_for(asyncio.Event().wait(),.02)
            return [{'id':client.number,'title':'hit'}]
        pool=bot.AccountPool(clients,bot.BulkRPC())
        groups,covered,failures=await pool.query({1,2,3},{id(c):{c.number} for c in clients},lookup,7)
        self.assertEqual([g['id'] for g in groups],[2,3]);self.assertEqual(covered,{2,3})
        self.assertEqual(len(failures),1);self.assertEqual(sum(pool.active.values()),0)

    async def test_completed_cache_and_index_do_not_bypass_any_account(self):
        clients=[Account(i) for i in range(1,4)];rpc=bot.BulkRPC()
        calls=[]
        async def lookup(client,target,scope,*args):
            calls.append(client.number)
            return [{'id':client.number,'title':'live'}]
        stale=bot.QueryGroups([{'id':99,'title':'stale'}])
        common=nested('common_groups',{
            'users':clients,'account_rpc':rpc,'account_pool':bot.AccountPool(clients,rpc),
            'source_revision':1,'source_accounts':{id(c):{1,2,3} for c in clients},
            'indexed_sources':{7:{99:'stale'}},'source_index_errors':False,
            'source_index_built':time.monotonic(),'common_cache':{(1,7):(time.monotonic(),stale)},
            'common_inflight':{},'refresh_sources':AsyncMock(return_value={1,2,3}),
            'fetch_common_groups':lookup})
        for _ in range(2):
            result=await common(person(7),prefer_index=True)
            self.assertEqual([g['id'] for g in result],[1,2,3])
        self.assertEqual(calls,[1,2,3,1,2,3])

    async def test_bulk_member_also_queries_all_three_accounts(self):
        clients=[Account(i) for i in range(1,4)];rpc=bot.BulkRPC();calls=[]
        results={7:{'groups':{},'covered':set(),'errors':[]}}
        async def lookup(client,resolved,sources,**kwargs):
            calls.append(client.number)
            return [{'id':client.number,'title':'hit'}]
        scope=dict(vars(bot))
        scope.update({
            'direct_results':results,'cached':{},'rpc':rpc,'sources':{1,2,3},
            'account_scopes':{id(c):{1,2,3} for c in clients},'hit_ids':set(),
            'bulk_pool':bot.AccountPool(clients,rpc),'fetch_common_groups_resolved':lookup,
            'progress':{}})
        tree=ast.parse(Path('bot.py').read_text(encoding='utf-8'))
        node=copy.deepcopy(next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='check_member'))
        exec(compile(ast.Module(body=[node],type_ignores=[]),'bot.py','exec'),scope)
        await scope['check_member'](person(7))
        self.assertEqual(calls,[1,2,3]);self.assertEqual(set(results[7]['groups']),{1,2,3})


if __name__=='__main__':unittest.main()
