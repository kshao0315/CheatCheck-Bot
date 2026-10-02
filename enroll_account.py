"""Enroll a user session without placing login codes or passwords in arguments."""
import asyncio
import json
import os
import sys
from pathlib import Path
if Path(__file__).resolve().parent.name == "account":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from telethon import TelegramClient, errors
import bot

async def main():
    config_path = Path(sys.argv[2])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    session_path = config_path.with_suffix("")
    state_path = config_path.with_suffix(".login.json")
    client = TelegramClient(str(session_path), int(config["api_id"]), config["api_hash"],
                            proxy=bot.proxy_config(), flood_sleep_threshold=0)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            state_path.unlink(missing_ok=True)
            print(json.dumps({"status": "authorized", "user_id": me.id}))
            return
        if sys.argv[1] == "send":
            sent = await client.send_code_request(config["phone"])
            state_path.write_text(json.dumps({"phone_code_hash": sent.phone_code_hash}), encoding="utf-8")
            os.chmod(state_path, 0o600)
            print(json.dumps({"status": "code_required", "delivery": type(sent.type).__name__}))
            return
        secret = json.load(sys.stdin)["secret"]
        if sys.argv[1] == "code":
            state = json.loads(state_path.read_text(encoding="utf-8"))
            await client.sign_in(phone=config["phone"], code=secret, phone_code_hash=state["phone_code_hash"])
        elif sys.argv[1] == "password":
            await client.sign_in(password=secret)
        me = await client.get_me()
        state_path.unlink(missing_ok=True)
        print(json.dumps({"status": "authorized", "user_id": me.id}))
    except errors.SessionPasswordNeededError:
        print(json.dumps({"status": "password_required"}))
    except (errors.RPCError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__}))
        sys.exit(1)
    finally:
        await client.disconnect()
        if session_path.with_suffix(".session").exists():
            os.chmod(session_path.with_suffix(".session"), 0o600)

if __name__ == "__main__":
    asyncio.run(main())
