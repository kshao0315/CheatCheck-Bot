import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
from test_submissions import FakeBot, FakeAccount, group, person, nested


def button_data(button):
    if hasattr(button, "data"):
        return button.data
    return button.type.data


class LanguageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript("""CREATE TABLE user_languages(user_id INTEGER PRIMARY KEY,language TEXT);
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO settings VALUES('superadmin','1');
            CREATE TABLE admins(user_id INTEGER PRIMARY KEY); INSERT INTO admins VALUES(2);
            CREATE TABLE groups(chat_id INTEGER PRIMARY KEY,owner_id INTEGER,title TEXT,enabled INTEGER DEFAULT 0,language TEXT DEFAULT 'zh');
            INSERT INTO groups(chat_id,owner_id,title,language) VALUES(-42,2,'Fixture group','en');
            INSERT INTO groups(chat_id,owner_id,title,language) VALUES(-43,3,'Other group','zh');
            INSERT INTO user_languages VALUES(1,'zh'); INSERT INTO user_languages VALUES(2,'en');
            INSERT INTO user_languages VALUES(100,'en');""")
        self.telegram = FakeBot()
        self.service = bot.GroupSubmissions(self.db, self.telegram, [FakeAccount(),FakeAccount()], AsyncMock())

    def tearDown(self):
        self.db.close()

    async def test_first_start_shows_picker_and_bootstrap_still_works(self):
        self.db.execute("DELETE FROM settings")
        picker, welcome = AsyncMock(), AsyncMock()
        handler = nested('on_message', {'db':self.db,'bot_me':person(999),'bootstrap':'fixture-code',
            'choose_language':picker,'welcome':welcome})
        event=SimpleNamespace(raw_text='/start fixture-code',is_private=True,get_sender=AsyncMock(return_value=person(1)))
        self.db.execute("DELETE FROM user_languages WHERE user_id=1")
        await handler(event)
        picker.assert_awaited_once()
        welcome.assert_not_awaited()
        self.assertEqual(bot.setting(self.db,'superadmin'),'1')

    async def test_returning_user_goes_straight_to_guide(self):
        picker,welcome=AsyncMock(),AsyncMock()
        handler=nested('on_message',{'db':self.db,'bot_me':person(999),'choose_language':picker,'welcome':welcome})
        await handler(SimpleNamespace(raw_text='/start',is_private=True,get_sender=AsyncMock(return_value=person(100))))
        welcome.assert_awaited_once()
        picker.assert_not_awaited()

    async def test_selection_persists_and_edits_guide_in_selected_language(self):
        welcome=AsyncMock()
        async def capture(*args):
            self.assertEqual(bot.CURRENT_LANGUAGE.get(),'en')
        welcome.side_effect=capture
        handler=nested('on_language',{'db':self.db,'pending':{200:('check',1)},'group_menu_cache':{},
            'welcome':welcome,'sync_group_menu':AsyncMock()})
        event=SimpleNamespace(is_private=True,chat_id=200,sender_id=200,data=b'language:en',query=SimpleNamespace(msg_id=9),
            answer=AsyncMock(),get_sender=AsyncMock(return_value=person(200)))
        await handler(event)
        self.assertEqual(bot.user_language(self.db,200),'en')
        self.assertEqual(bot.CURRENT_LANGUAGE.get(),'zh')
        self.assertEqual(event.answer.await_args.args[0],'Language set to English')

    async def test_group_language_button_cannot_change_preference(self):
        handler=nested('on_language',{'db':self.db})
        event=SimpleNamespace(is_private=False,chat_id=-42,sender_id=2,data=b'language:zh',answer=AsyncMock())
        await handler(event)
        self.assertEqual(bot.user_language(self.db,2),'en')

    async def test_bilingual_picker_has_exactly_two_options(self):
        output=AsyncMock()
        handler=nested('choose_language',{'pending':{},'present':output})
        await handler(SimpleNamespace(sender_id=100))
        buttons=output.await_args.kwargs['buttons'][0]
        self.assertEqual([b.text for b in buttons],['简体中文','English'])
        self.assertEqual([button_data(b) for b in buttons],[b'language:zh',b'language:en'])

    async def test_english_menu_preserves_roles(self):
        menu=nested('menu_buttons',{'db':self.db})
        with bot.language_context('en'):
            regular=[button_data(b) for row in menu(100) for b in row]
            admin=[button_data(b) for row in menu(2) for b in row]
            superadmin=[button_data(b) for row in menu(1) for b in row]
            self.assertIn(b'menu:language',regular)
            self.assertNotIn(b'menu:reviews',regular)
            self.assertNotIn(b'menu:admins',admin)
            self.assertIn(b'menu:admins',superadmin)
            self.assertEqual(menu(100)[0][0].text,'🔎 Check a user')

    async def test_english_guide_ends_with_role(self):
        output=AsyncMock()
        menu=nested('menu_buttons',{'db':self.db})
        welcome=nested('welcome',{'db':self.db,'sync_menu':AsyncMock(),'menu_buttons':menu,'present':output})
        with bot.language_context('en'):
            await welcome(SimpleNamespace(),person(2))
        text=output.await_args.args[1]
        self.assertTrue(text.startswith('Welcome to CheateChecker!'))
        self.assertTrue(text.endswith('Your role: Admin'))
        self.assertNotRegex(text,r'[\u4e00-\u9fff]')

    async def test_review_and_feedback_use_each_recipient_language(self):
        request_id=self.service.create(group(),'fixture_group',person(100))
        await self.service.notifications()
        by_user={m[0]:m for m in self.telegram.sent}
        self.assertIn('待审核',by_user[1][1])
        self.assertIn('Pending review',by_user[2][1])
        self.assertEqual(by_user[2][2]['buttons'][0][0].text,'Approve')
        self.service.decide(request_id,person(1),'approved')
        await self.service.notifications()
        self.assertIn("Here's the outcome",self.telegram.sent[-1][1])
        self.assertIn('Reviewed by:',self.telegram.sent[-1][1])

    async def test_bulk_messages_use_each_recipient_language_and_preserve_group_name(self):
        with bot.language_context('en'):
            header=bot.tr('群聊 {0} 检查完成：{1} 人命中，成功检查 {2} 人，未完成 {3} 人{4}。跳过已注销账号 {5} 人。\n',
                '管理员 {literal}',2,20,0,'',0)
            await bot.deliver_bulk_result(self.telegram,'1',2,header,[99],db=self.db)
        self.assertTrue(self.telegram.sent[0][1].startswith('群聊 管理员 {literal}'))
        self.assertTrue(self.telegram.sent[1][1].startswith('Finished checking 管理员 {literal}'))

    async def test_concurrent_languages_do_not_leak(self):
        @bot.language_handler(self.db)
        async def callback(event):
            await asyncio.sleep(0)
            return bot.tr('返回主页')
        zh,en=await asyncio.gather(callback(SimpleNamespace(sender_id=1)),callback(SimpleNamespace(sender_id=2)))
        self.assertEqual((zh,en),('返回主页','Back to home'))
        self.assertEqual(bot.CURRENT_LANGUAGE.get(),'zh')

    async def test_stored_chinese_results_render_in_english_without_altering_names(self):
        original='⚠️ 检测到作弊用户！\n\n用户: @fixture 管理员 {literal}\nID: <code>99</code>\n作弊群组数量: 2'
        with bot.language_context('en'):
            rendered=bot.saved_result_text(original)
        self.assertIn('User: @fixture 管理员 {literal}',rendered)
        self.assertIn('Cheating groups: 2',rendered)
        self.assertEqual(rendered.render('zh'),original)

    async def test_catalog_placeholders_match(self):
        import string
        for original,english in bot.EN_MESSAGES.items():
            before={field for _,field,_,_ in string.Formatter().parse(original) if field is not None}
            after={field for _,field,_,_ in string.Formatter().parse(english) if field is not None}
            self.assertEqual(before,after,original)

    async def test_group_language_overrides_each_users_private_preference(self):
        @bot.language_handler(self.db)
        async def callback(event):
            return bot.tr('返回结果')
        self.assertEqual(await callback(SimpleNamespace(chat_id=-42,sender_id=1)),'Back to result')
        self.assertEqual(await callback(SimpleNamespace(chat_id=-43,sender_id=2)),'返回结果')
        self.assertEqual(await callback(SimpleNamespace(chat_id=2,sender_id=2)),'Back to result')

    async def test_bulk_uses_group_language_on_resume(self):
        @bot.actor_language(self.db)
        async def bulk(chat_id,actor_id):
            return bot.tr('正在全员检查…')
        self.assertEqual(await bulk(-42,1),'Checking everyone…')
        self.assertEqual(await bulk(-43,2),'正在全员检查…')

    async def test_manager_can_update_only_their_own_groups_language(self):
        manager=nested('may_manage_group',{'db':self.db})
        show,sync=AsyncMock(),AsyncMock()
        handler=nested('on_set_group_language',{'db':self.db,'may_manage_group':manager,'show_groups':show,'sync_group_language':sync})
        event=SimpleNamespace(is_private=True,sender_id=2,data=b'set_group_language:-42:zh:0',
            answer=AsyncMock(),get_sender=AsyncMock(return_value=person(2)))
        await handler(event)
        await asyncio.sleep(0)
        self.assertEqual(bot.group_language(self.db,-42),'zh')
        show.assert_awaited_once()
        sync.assert_awaited_once()
        event.data=b'set_group_language:-43:en:0'
        await handler(event)
        self.assertEqual(bot.group_language(self.db,-43),'zh')
        self.assertEqual(show.await_count,1)

    async def test_group_management_buttons_show_language_without_leaking_groups(self):
        buttons=nested('group_buttons',{'db':self.db})
        own=buttons(2)
        data=[button_data(b) for row in own for b in row]
        self.assertIn(b'group_language:-42:0',data)
        self.assertNotIn(b'group_language:-43:0',data)
        self.assertEqual(own[0][1].text,'🌐 English')

    async def test_existing_database_migrates_without_losing_group_settings(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bot.sqlite3'
            old=sqlite3.connect(path)
            old.executescript("CREATE TABLE groups(chat_id INTEGER PRIMARY KEY,title TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,owner_id INTEGER); INSERT INTO groups VALUES(-42,'Existing group',1,2);")
            old.close()
            with patch.object(bot,'DB_PATH',path):
                db=bot.db_connect()
            self.assertEqual(db.execute('SELECT title,enabled,owner_id,language FROM groups').fetchone(),('Existing group',1,2,'zh'))
            db.execute("INSERT INTO user_languages VALUES(99,'en')")
            db.execute("UPDATE groups SET language='en' WHERE chat_id=-42")
            db.commit()
            db.close()
            reopened=sqlite3.connect(path)
            self.assertEqual(bot.user_language(reopened,99),'en')
            self.assertEqual(bot.group_language(reopened,-42),'en')
            reopened.close()


if __name__=='__main__':
    unittest.main()
