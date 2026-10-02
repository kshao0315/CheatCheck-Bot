"""Read-only lookup timing for the named user in the public diagnostic group."""

import asyncio
import os
import time

from telethon import TelegramClient, errors
from telethon.tl.functions.messages import GetCommonChatsRequest
from telethon.tl.types import Channel, Chat, InputUser, InputUserFromMessage

from bot import group_title, proxy_config, telegram_id, user_client_from_config


GROUP = os.getenv("DIAGNOSTIC_GROUP", "").strip()
NAME_FRAGMENT = os.getenv("DIAGNOSTIC_NAME_FRAGMENT", "").strip()
TARGET_NAME = os.getenv("DIAGNOSTIC_TARGET_NAME", "").strip()


async def search_group(client, group, label):
    started = time.monotonic()
    try:
        async for candidate in client.iter_participants(group, search=NAME_FRAGMENT, limit=100):
            name = " ".join(filter(None, (candidate.first_name, candidate.last_name)))
            if name == TARGET_NAME:
                print(f"{label}: found id={candidate.id}, username={bool(candidate.username)}, seconds={time.monotonic()-started:.2f}", flush=True)
                return candidate
        print(f"{label}: participant search found no match in {time.monotonic()-started:.2f}s", flush=True)
    except (ValueError, errors.RPCError) as exc:
        print(f"{label}: participant search {type(exc).__name__} in {time.monotonic()-started:.2f}s", flush=True)
    return None


async def search_messages(client, group, label):
    started = time.monotonic()
    try:
        async for message in client.iter_messages(group, limit=1200):
            sender = await message.get_sender()
            if sender is None:
                continue
            name = " ".join(filter(None, (getattr(sender, "first_name", None), getattr(sender, "last_name", None))))
            if name == TARGET_NAME:
                print(f"{label}: message found id={sender.id}, username={bool(sender.username)}, seconds={time.monotonic()-started:.2f}", flush=True)
                return sender
        print(f"{label}: recent messages found no match in {time.monotonic()-started:.2f}s", flush=True)
    except (ValueError, errors.RPCError) as exc:
        print(f"{label}: message search {type(exc).__name__} in {time.monotonic()-started:.2f}s", flush=True)
    return None


async def main():
    if not all((GROUP, NAME_FRAGMENT, TARGET_NAME)):
        raise SystemExit("Set DIAGNOSTIC_GROUP, DIAGNOSTIC_NAME_FRAGMENT and DIAGNOSTIC_TARGET_NAME before running diagnostics")
    user = user_client_from_config(proxy_config())
    bot = TelegramClient(os.getenv("BOT_SESSION", "data/bot"), user.api_id, user.api_hash, proxy=proxy_config())
    try:
        await user.connect()
        await bot.start(bot_token=os.environ["BOT_TOKEN"])
        bot_group = None
        user_group = None
        for client, label in ((bot, "bot"),):
            try:
                group = await client.get_entity(GROUP)
                print(f"{label}: group id={telegram_id(group)} title={group_title(group)}", flush=True)
                if label == "bot":
                    bot_group = group
                else:
                    user_group = group
            except (ValueError, errors.RPCError) as exc:
                print(f"{label}: group resolution {type(exc).__name__} seconds={getattr(exc, 'seconds', '-')}", flush=True)
        if bot_group is not None:
            try:
                user_group = await user.get_input_entity(telegram_id(bot_group))
                print("user: diagnostic group already cached", flush=True)
            except (ValueError, errors.RPCError):
                print("user: diagnostic group not cached", flush=True)
        target = None
        for client, group, label in ((user, user_group, "user"), (bot, bot_group, "bot")):
            if group is not None and target is None:
                target = await search_group(client, group, label)
        for client, group, label in ((bot, bot_group, "bot"), (user, user_group, "user")):
            if group is not None and target is None:
                target = await search_messages(client, group, label)
        if target is None:
            print("target: not found in searchable participants or recent messages", flush=True)
            return
        started = time.monotonic()
        try:
            resolved = await user.get_input_entity(target.id)
            print(f"user account ID resolution: {time.monotonic()-started:.2f}s", flush=True)
        except (ValueError, errors.RPCError) as exc:
            print(f"user account ID resolution: {type(exc).__name__} in {time.monotonic()-started:.2f}s", flush=True)
            resolved = None
        if resolved is None and user_group is not None:
            started = time.monotonic()
            try:
                async def recent_target_message():
                    async for item in user.iter_messages(user_group, limit=1200):
                        if item.sender_id == target.id:
                            return item
                    return None
                message = await asyncio.wait_for(recent_target_message(), timeout=30)
                print(f"user recent-message search: found={message is not None}; seconds={time.monotonic()-started:.2f}", flush=True)
                if message is not None:
                    started = time.monotonic()
                    from_message = InputUserFromMessage(user_group, message.id, target.id)
                    probe = await asyncio.wait_for(user(GetCommonChatsRequest(
                        user_id=from_message, max_id=0, limit=100)), timeout=20)
                    print(f"message-reference probe: accepted, chats={len(probe.chats)}, seconds={time.monotonic()-started:.2f}", flush=True)
                    resolved = from_message
            except (ValueError, errors.RPCError, asyncio.TimeoutError) as exc:
                print(f"message-reference probe: {type(exc).__name__}, seconds={time.monotonic()-started:.2f}", flush=True)
        if resolved is None and getattr(target, "access_hash", None):
            started = time.monotonic()
            try:
                probe = await asyncio.wait_for(user(GetCommonChatsRequest(
                    user_id=InputUser(target.id, target.access_hash), max_id=0, limit=100)), timeout=20)
                print(f"bot access hash probe: accepted, chats={len(probe.chats)}, seconds={time.monotonic()-started:.2f}", flush=True)
                resolved = InputUser(target.id, target.access_hash)
            except (errors.RPCError, asyncio.TimeoutError) as exc:
                print(f"bot access hash probe: {type(exc).__name__}, seconds={time.monotonic()-started:.2f}", flush=True)
        configured = os.getenv("CHEAT_GROUP_IDS", "").strip()
        if configured:
            sources = {int(part.strip()) for part in configured.split(",") if part.strip()}
        else:
            sources = {
                telegram_id(dialog.entity) async for dialog in user.iter_dialogs()
                if isinstance(dialog.entity, (Channel, Chat))
                and (not isinstance(dialog.entity, Channel) or dialog.entity.megagroup)
            }
        print(f"source groups: {len(sources)}", flush=True)
        me = await user.get_me()
        if sources:
            started = time.monotonic()
            try:
                source = next(iter(sources))
                async def get_numeric_hits():
                    return [p.id async for p in user.iter_participants(source, search=str(me.id), limit=10)]
                numeric_hits = await asyncio.wait_for(get_numeric_hits(), timeout=20)
                print(f"numeric participant search: own ID found={me.id in numeric_hits}; seconds={time.monotonic()-started:.2f}", flush=True)
            except (ValueError, errors.RPCError, asyncio.TimeoutError) as exc:
                print(f"numeric participant search: {type(exc).__name__}; seconds={getattr(exc, 'seconds', '-')}", flush=True)
        if resolved is None:
            print("common groups: account-scoped target access hash still required", flush=True)
            return
        if resolved is None:
            print("common groups: unresolved target, no result", flush=True)
            return
        started = time.monotonic()
        found = {}
        max_id = 0
        calls = 0
        while True:
            result = await user(GetCommonChatsRequest(user_id=resolved, max_id=max_id, limit=100))
            calls += 1
            chats = result.chats
            for chat in chats:
                chat_id = telegram_id(chat)
                if chat_id in sources:
                    found[chat_id] = group_title(chat)
            if len(chats) < 100:
                break
            next_id = min(chat.id for chat in chats)
            if next_id == max_id:
                break
            max_id = next_id
        print(f"common groups: {len(found)} configured matches; calls={calls}; seconds={time.monotonic()-started:.2f}", flush=True)
        for chat_id, title in sorted(found.items()):
            print(f"  {chat_id} {title}", flush=True)
        if user_group is not None:
            try:
                async for message in user.iter_messages(user_group, limit=50):
                    sample = await message.get_sender()
                    if sample is None or not getattr(sample, "username", None) or sample.bot:
                        continue
                    started = time.monotonic()
                    input_user = await user.get_input_entity(sample.username)
                    cache_seconds = time.monotonic() - started
                    started = time.monotonic()
                    await asyncio.wait_for(user.get_entity(input_user), timeout=20)
                    entity_seconds = time.monotonic() - started
                    started = time.monotonic()
                    await asyncio.wait_for(user(GetCommonChatsRequest(
                        user_id=input_user, max_id=0, limit=100)), timeout=20)
                    print(f"username lookup: cached input={cache_seconds:.3f}s, user entity={entity_seconds:.2f}s, common API={time.monotonic()-started:.2f}s", flush=True)
                    break
            except (ValueError, errors.RPCError, asyncio.TimeoutError) as exc:
                print(f"username lookup: {type(exc).__name__}", flush=True)
    finally:
        await bot.disconnect()
        await user.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
