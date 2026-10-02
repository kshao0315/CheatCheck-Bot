"""Read-only account-scoped bulk lookup diagnostics; run with the service paused."""

import asyncio
import json
import os
import sqlite3
import time

from telethon import errors, types, functions, utils, TelegramClient
from telethon.tl.functions.messages import GetCommonChatsRequest

import bot


CHAT_ID = None
USERNAME = ""


async def main():
    global CHAT_ID, USERNAME
    chat_id = os.getenv("BULK_PROBE_CHAT_ID", "").strip()
    USERNAME = os.getenv("BULK_PROBE_USERNAME", "").strip().lstrip("@")
    if not chat_id or not USERNAME:
        raise SystemExit("Set BULK_PROBE_CHAT_ID and BULK_PROBE_USERNAME before running diagnostics")
    try:
        CHAT_ID = int(chat_id)
    except ValueError:
        raise SystemExit("BULK_PROBE_CHAT_ID must be an integer") from None
    db = sqlite3.connect(os.getenv('DATABASE', 'data/bot.sqlite3'))
    stamp = db.execute("SELECT MAX(created_at) FROM queries WHERE chat_id=? AND method='群组check all'", (CHAT_ID,)).fetchone()[0]
    ids = [row[0] for row in db.execute("SELECT target_id FROM queries WHERE chat_id=? AND created_at=? AND status='失败' ORDER BY id LIMIT 12", (CHAT_ID, stamp))]
    known_hit=db.execute("SELECT target_id FROM queries WHERE chat_id=? AND matches>0 ORDER BY id DESC LIMIT 1",(CHAT_ID,)).fetchone()
    bot_session = sqlite3.connect('file:' + os.getenv('BOT_SESSION','data/bot') + '.session?mode=ro', uri=True)
    names = {uid: bot_session.execute('SELECT username,name FROM entities WHERE id=?',(uid,)).fetchone() for uid in ids}
    print('failed_sample:', json.dumps(names, ensure_ascii=False), flush=True)
    if os.getenv('BULK_BRIDGE_PROBE'):
        await bridge_probe(ids[:2] + ([known_hit[0]] if known_hit else []))
        return
    for index, (key, session_key) in enumerate(bot.account_config_keys(), 1):
        label = 'account' + str(index)
        client = bot.user_client_from_config(bot.proxy_config(),key,session_key)
        await client.connect()
        try:
            started = time.monotonic()
            dialogs = [dialog async for dialog in client.iter_dialogs()]
            sources = {bot.telegram_id(d.entity) for d in dialogs if isinstance(d.entity,(types.Channel,types.Chat)) and (not isinstance(d.entity,types.Channel) or d.entity.megagroup)}
            print(label, 'sources:', len(sources), 'target_joined:', CHAT_ID in sources, flush=True)
            try:
                try: peer = client.session.get_input_entity(CHAT_ID)
                except ValueError: peer = await asyncio.wait_for(client.get_input_entity(USERNAME),20)
                full = await asyncio.wait_for(client(functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))),20)
                print(label, 'target_metadata:', json.dumps({'count':getattr(full.full_chat,'participants_count',None),'hidden':getattr(full.full_chat,'participants_hidden',None)},ensure_ascii=False), flush=True)
                participants = await asyncio.wait_for(client.get_participants(peer,limit=1000),45)
                found = set(p.id for p in participants)
                print(label, 'target_participants:', len(found), 'failed_sample_present:', len(found.intersection(ids)), 'seconds:',round(time.monotonic()-started,2), flush=True)
            except Exception as exc:
                print(label,'target_read_error:',type(exc).__name__,str(exc),flush=True)
            for uid in ([known_hit[0]] if known_hit else []) + ids[:2]:
                foreign=bot_session.execute('SELECT hash FROM entities WHERE id=?',(uid,)).fetchone()
                for label_hash, access_hash in [('zero',0)] + ([('bot_hash',foreign[0])] if foreign and foreign[0] else []):
                    try:
                        result=await asyncio.wait_for(client(GetCommonChatsRequest(types.InputUser(uid,access_hash),0,100)),15)
                        print(label,'hash_probe:',uid,label_hash,'chats:',len(result.chats),flush=True)
                    except Exception as exc:
                        print(label,'hash_probe_error:',uid,label_hash,type(exc).__name__,str(exc),flush=True)
            for uid in ids[:4]:
                started=time.monotonic()
                try:
                    try: resolved=client.session.get_input_entity(uid)
                    except ValueError:
                        username = (names[uid] or (None,))[0]
                        if not username: raise ValueError('No account-scoped entity or username')
                        resolved=await asyncio.wait_for(client.get_input_entity(username),20)
                    chats=await asyncio.wait_for(client(GetCommonChatsRequest(resolved,0,100)),20)
                    print(label,'lookup:',uid,'chats:',len(chats.chats),'seconds:',round(time.monotonic()-started,2),flush=True)
                except Exception as exc:
                    print(label,'lookup_error:',uid,type(exc).__name__,str(exc),flush=True)
        finally:
            await client.disconnect()


async def bridge_probe(ids):
    clients=bot.user_clients_from_config(bot.proxy_config())
    primary=clients[0]
    bot_client=TelegramClient(os.getenv('BOT_SESSION','data/bot'),primary.api_id,primary.api_hash,proxy=bot.proxy_config())
    await bot_client.start(bot_token=os.environ['BOT_TOKEN'])
    bot_me=await bot_client.get_me()
    members={p.id:p async for p in bot_client.iter_participants(CHAT_ID)}
    db=sqlite3.connect(os.getenv('DATABASE','data/bot.sqlite3'))
    stamp=db.execute("SELECT MAX(created_at) FROM queries WHERE chat_id=? AND method='群组check all'",(CHAT_ID,)).fetchone()[0]
    all_failed=[r[0] for r in db.execute("SELECT target_id FROM queries WHERE chat_id=? AND created_at=? AND status='失败'",(CHAT_ID,stamp))]
    print('deleted_failed:',sum(bool(getattr(members.get(uid),'deleted',False)) for uid in all_failed),flush=True)
    ids=[]
    for uid in all_failed:
        if uid not in members or getattr(members[uid],'deleted',False): continue
        try: primary.session.get_input_entity(uid)
        except ValueError: ids.append(uid)
        if len(ids)==3: break
    print('uncached_probe_ids:',ids,flush=True)
    refreshed=await bot_client(functions.users.GetUsersRequest([utils.get_input_user(members[uid]) for uid in ids]))
    print('bot_refreshed:',[(u.id,type(u).__name__,getattr(u,'deleted',None),getattr(u,'min',None),getattr(u,'access_hash',None)==getattr(members.get(u.id),'access_hash',None)) for u in refreshed],flush=True)
    for u in refreshed:
        if isinstance(u,types.User): members[u.id]=u
    context_message=await bot_client.send_message(CHAT_ID,'正在准备全员检查…')
    try:
        for index,client in enumerate(clients,1):
            label='account'+str(index)
            await client.connect()
            sent=None
            start_message=None
            peer=None
            try:
                me=await client.get_me()
                peer=await client.get_input_entity(bot_me.id)
                start_message=await client.send_message(peer,'/start')
                await asyncio.sleep(1)
                destination=await bot_client.get_input_entity(me.id)
                text='全员检查诊断批次：'
                entities=[]
                for i,uid in enumerate(ids,1):
                    name=str(i)
                    entities.append(types.InputMessageEntityMentionName(len(text.encode('utf-16-le'))//2,len(name),utils.get_input_user(members[uid])))
                    text+=name+' '
                try:
                    edited=await bot_client.edit_message(CHAT_ID,context_message.id,text,formatting_entities=entities,parse_mode=None)
                except errors.MessageNotModifiedError:
                    edited=await bot_client.get_messages(CHAT_ID,ids=context_message.id)
                print(label,'group_reference_entities:',[getattr(e,'user_id',None) for e in edited.entities or []],flush=True)
                lookup_peer=await client.get_input_entity(CHAT_ID)
                mentioned={e.user_id for e in edited.entities or [] if isinstance(e,types.MessageEntityMentionName)}
                references=[types.InputUserFromMessage(lookup_peer,context_message.id,uid) for uid in ids if uid in mentioned]
                full=await asyncio.wait_for(client(functions.users.GetUsersRequest(references)),20)
                print(label,'bridge_resolved:',[(u.id,getattr(u,'min',None),bool(getattr(u,'access_hash',None))) for u in full],flush=True)
                for target in full:
                    try:
                        result=await asyncio.wait_for(client(GetCommonChatsRequest(utils.get_input_user(target),0,100)),20)
                        print(label,'bridge_common:',target.id,len(result.chats),flush=True)
                    except Exception as exc:
                        print(label,'bridge_common_error:',target.id,type(exc).__name__,str(exc),flush=True)
            except Exception as exc:
                print(label,'bridge_error:',type(exc).__name__,str(exc),flush=True)
            finally:
                if sent is not None:
                    await bot_client.delete_messages(destination,[sent.id],revoke=True)
                if start_message is not None:
                    await client.delete_messages(peer,[start_message.id],revoke=True)
                await client.disconnect()
    finally:
        await bot_client.delete_messages(CHAT_ID,[context_message.id])
        await bot_client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
