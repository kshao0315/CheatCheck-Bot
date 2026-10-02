"""Synchronize public source memberships across all configured user accounts.

Run only while the main bot is stopped; session files must have a single owner.
The background bot worker performs the same synchronization during normal use.
"""
import asyncio
import json
import bot

async def main():
    clients = bot.user_clients_from_config(bot.proxy_config())
    db = bot.db_connect()
    try:
        await asyncio.gather(*(client.connect() for client in clients))
        for index, client in enumerate(clients, 1):
            if not await client.is_user_authorized():
                raise RuntimeError(f"Account {index} is not authorized")
        async def refresh(**kwargs):
            pass
        submissions = bot.GroupSubmissions(db, None, clients, refresh)
        rpc = bot.BulkRPC()
        submissions.rpc = rpc
        sync = bot.AccountGroupSync(db, clients, rpc, submissions, bot.DB_PATH.parent / "account_sync.json")
        print(json.dumps(await sync.cycle(force=True), ensure_ascii=False))
    finally:
        await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
