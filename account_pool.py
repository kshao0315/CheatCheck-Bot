"""Account-count-independent scheduling and query result metadata."""
import asyncio
import time
from telethon import functions
from telethon.tl.functions.messages import GetCommonChatsRequest

class AccountPool:
    """Query every account's readable scope concurrently and merge evidence."""
    def __init__(self, clients, rpc, *, clock=time.monotonic):
        self.clients, self.rpc, self.clock = list(clients), rpc, clock
        self.active = {id(client): 0 for client in clients}
        self.latency = {id(client): 0.0 for client in clients}
        self.last_used = {id(client): 0.0 for client in clients}

    def rank(self, candidates, scopes, needed, target_id=None, method=GetCommonChatsRequest):
        def score(client):
            cached = False
            if target_id is not None:
                try:
                    client.session.get_input_entity(target_id)
                    cached = True
                except (ValueError, AttributeError):
                    pass
            wait = self.rpc.remaining(client, method)
            if target_id is not None and not cached:
                wait = max(wait, self.rpc.remaining(client, functions.contacts.ResolveUsernameRequest))
            scope = set(scopes.get(id(client), ())) & needed
            return (bool(wait), wait, -len(scope), self.active[id(client)],
                    not cached, self.latency[id(client)], self.last_used[id(client)])
        return sorted(candidates, key=score)

    async def query(self, sources, scopes, lookup, target_id=None, *, scoped_lookup=False):
        """Run each readable account once, including overlapping source scopes.

        A successful account never suppresses another account's lookup. Failures
        stay isolated so healthy accounts still contribute their positive hits.
        """
        sources, covered, groups, failures = set(sources), set(), {}, []
        lane_scopes = {id(client): set(scopes.get(id(client), ())) & sources
                       for client in self.clients}
        lanes = [client for client in self.clients if lane_scopes[id(client)]]

        async def run(client):
            started = self.clock()
            self.active[id(client)] += 1
            self.last_used[id(client)] = started
            try:
                return await (lookup(client, lane_scopes[id(client)])
                              if scoped_lookup else lookup(client))
            finally:
                elapsed = self.clock() - started
                self.latency[id(client)] = .8 * self.latency[id(client)] + .2 * elapsed
                self.active[id(client)] -= 1

        tasks = [asyncio.create_task(run(client)) for client in lanes]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for client, result in zip(lanes, results):
            if isinstance(result, BaseException):
                failures.append(result)
                continue
            covered.update(lane_scopes[id(client)])
            groups.update({group["id"]: group for group in result if group["id"] in sources})
        return [groups[key] for key in sorted(groups)], covered, failures

class QueryGroups(list):
    """Keep incomplete scope explicit without changing group-list callers."""
    def __init__(self, groups=(), missing_sources=()):
        super().__init__(groups)
        self.missing_sources = frozenset(missing_sources)
