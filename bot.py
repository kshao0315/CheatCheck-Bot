"""Telegram group membership checker using a bot and an extensible user account pool."""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from account_pool import AccountPool, QueryGroups
from runtime_config import RuntimeSettings, account_config_keys
from join_policy import PreJoinPolicy
from group_imports import GroupImportQueue
from join_control import automatic_join_enabled
from group_whitelist import GroupWhitelist, exempt_source_ids, filter_check_groups, filter_bulk_hits, resolve_whitelist_group

from telethon import Button, TelegramClient, events, errors, utils
from telethon.tl import functions, types
from telethon.tl.functions.messages import GetCommonChatsRequest
from telethon.tl.types import Channel, Chat, InputUserFromMessage


EN_MESSAGES = {'请填写公开群组的username，例如 /submit groupname。': "Enter a public group's username, like /submit groupname.",
 '⚠️ 来源群需手动入群\n\n群组：{0}\n链接：{1}\nID：{2}\n协议号：第 {3} 个\n\n该账号已失去成员身份或被禁止访问。自动重新入群已停止。请使用该协议号手动加入并完成验证，加入后检测范围会自动恢复。': '⚠️ Manual action needed for a source group\n\nGroup: {0}\nLink: {1}\nID: {2}\nAccount: #{3}\n\nThis account lost membership or access to the group. Automatic rejoining has stopped. Please join with this account and complete verification yourself. The group will be included in checks again once membership is detected.',
 '🆕 Bot 已加入新群聊\n\n群组：<b>{0}</b>\nID：<code>{1}</code>\n链接：{2}\n添加者：{3}\n\n查询已自动启用。': '🆕 The bot joined a new group\n\nGroup: <b>{0}</b>\nID: <code>{1}</code>\nLink: {2}\nAdded by: {3}\n\nChecks are enabled automatically.',
 '打开群聊': 'Open group',
 '群内消息链接（需已加入该群）': 'Group message link (you need to be a member)',
 '暂无可用链接（私有群组）': 'No link available (private group)',
 '未知添加者': 'Unknown inviter',
 '仅本群群主或管理员可以选择语言。': 'Only this group’s owner or admins can choose the language.',
 '该语言选择消息已失效。': 'This language picker is no longer active.',
 '✅ 本群查询已启用。\n当前语言：{0}\n\n群管理员可通过下方按钮修改语言。': '✅ Checks are enabled for this group.\nLanguage: {0}\n\nGroup admins can change it using the buttons below.',
 '本群查询当前已关闭。\n当前语言：{0}\n\n群管理员可通过下方按钮修改语言。': 'Checks are currently turned off for this group.\nLanguage: {0}\n\nGroup admins can change it using the buttons below.',
 '来源群扫描': 'Scanning source groups',
 '补充查询': 'Retrying unresolved checks',
 '当前阶段：{0}': 'Current stage: {0}',
 '全员检查已超时停止，尚未完成的用户不视为检查通过，请稍后重试。': "The full group check timed out. Unchecked users haven't been cleared. Please try again later.",
 '需要管理员权限。': 'You need admin access for this.',
 '该群组已在检测范围内。': 'This group is already on the list.',
 '该群已有待审核申请（#{0}），请等待审核。': 'This group already has a pending request (#{0}). Please wait for a review.',
 '无效审核选项。': "That isn't a valid review option.",
 '⏳ 待审核': '⏳ Pending review',
 '✅ 已通过': '✅ Approved',
 '❌ 未通过': '❌ Declined',
 '作弊群组申请 #{0} · {1}\n\n群组：{2}\n@{3}\nID：{4}\n提交者：{5}': 'Group submission #{0} · {1}\n'
                                                       '\n'
                                                       'Group: {2}\n'
                                                       '@{3}\n'
                                                       'ID: {4}\n'
                                                       'Submitted by: {5}',
 '\n处理人：{0}': '\nReviewed by: {0}',
 '\n所有协议号入群任务已安排；需群主批准或账号限速时会自动等待重试。': '\n'
                                    'All connected accounts are queued to join. If approval is needed or Telegram '
                                    "asks us to wait, we'll retry automatically.",
 '通过': 'Approve',
 '不通过': 'Decline',
 '您的申请审核结果：\n\n': "Here's the outcome of your submission:\n\n",
 'Bot 管理员或超级管理员受到封禁保护。': 'Bot admins and the super admin are protected from bans.',
 '暂时无法核实目标的群组身份，请稍后重试。': "We couldn't check this user's group role just now. Please try again in a bit.",
 '群组管理员受到封禁保护。': 'Group admins are protected from bans.',
 '🔎 查询用户': '🔎 Check a user',
 '📨 提交作弊群组': '📨 Submit a cheating group',
 '🗳️ 待审核申请': '🗳️ Pending submissions',
 '➕ 添加作弊群组': '➕ Add a cheating group',
 '📁 群聊管理': '📁 Manage groups',
 '📁 我的群聊': '📁 My groups',
 '📋 全员检查': '📋 Check everyone',
 '🚫 已封禁用户': '🚫 Banned users',
 '🛡️ 已豁免用户': '🛡️ Whitelisted users',
 '👥 管理管理员': '👥 Manage admins',
 '📝 查询日志': '📝 Check history',
 '查看向导和身份': 'Open the guide and see your role',
 '查询用户：/check 用户名或ID': 'Check a user: /check username or ID',
 '提交作弊群组：/submit 群组username': 'Submit a group: /submit group_username',
 '管理自己添加的群聊': "Manage the groups you've added",
 '添加或移除白名单用户': 'Add or remove whitelisted users',
 '在群内封禁用户': 'Ban a user in a group',
 '在群内解除封禁': 'Unban a user in a group',
 '在群内开启或关闭自动查询': 'Turn automatic checks on or off',
 '添加作弊群组：/add group 群组username': 'Add a cheating group: /add group username',
 '添加群组：/add group；授权管理员：/add 用户ID': 'Add a group: /add group; grant admin access: /add user_ID',
 '撤销管理员：/revoke 用户ID': 'Remove an admin: /revoke user_ID',
 '管理管理员': 'Manage admins',
 '查看查询日志': 'View check history',
 '查询用户': 'Check a user',
 '封禁用户': 'Ban a user',
 '自动查询开关': 'Turn automatic checks on or off',
 '查询用户；all 检查全员': 'Check a user; use all to check everyone',
 '欢迎使用 CheateChecker！\n\n本项目旨在反对作弊，维护公平的群聊环境。\n请引用群内用户的消息发送 /check，或发送 /check 用户名/用户ID。\n查询结果中的按钮可查看命中的共同群组。\n发现新的作弊群组，可在私聊点击提交按钮或发送 /submit 群组username，等待管理员审核。\n': 'Welcome '
                                                                                                                                                                     'to '
                                                                                                                                                                     'CheateChecker!\n'
                                                                                                                                                                     '\n'
                                                                                                                                                                     "We're "
                                                                                                                                                                     'here '
                                                                                                                                                                     'to '
                                                                                                                                                                     'help '
                                                                                                                                                                     'fight '
                                                                                                                                                                     'cheating '
                                                                                                                                                                     'and '
                                                                                                                                                                     'keep '
                                                                                                                                                                     'your '
                                                                                                                                                                     'groups '
                                                                                                                                                                     'fair.\n'
                                                                                                                                                                     'Reply '
                                                                                                                                                                     'to '
                                                                                                                                                                     "someone's "
                                                                                                                                                                     'message '
                                                                                                                                                                     'with '
                                                                                                                                                                     '/check, '
                                                                                                                                                                     'or '
                                                                                                                                                                     'send '
                                                                                                                                                                     '/check '
                                                                                                                                                                     'followed '
                                                                                                                                                                     'by '
                                                                                                                                                                     'their '
                                                                                                                                                                     'username '
                                                                                                                                                                     'or '
                                                                                                                                                                     'user '
                                                                                                                                                                     'ID.\n'
                                                                                                                                                                     'If '
                                                                                                                                                                     'we '
                                                                                                                                                                     'find '
                                                                                                                                                                     'a '
                                                                                                                                                                     'match, '
                                                                                                                                                                     'tap '
                                                                                                                                                                     'the '
                                                                                                                                                                     'button '
                                                                                                                                                                     'below '
                                                                                                                                                                     'the '
                                                                                                                                                                     'result '
                                                                                                                                                                     'to '
                                                                                                                                                                     'see '
                                                                                                                                                                     'the '
                                                                                                                                                                     'groups.\n'
                                                                                                                                                                     'Found '
                                                                                                                                                                     'a '
                                                                                                                                                                     'cheating '
                                                                                                                                                                     'group '
                                                                                                                                                                     "we're "
                                                                                                                                                                     'missing? '
                                                                                                                                                                     'Submit '
                                                                                                                                                                     'it '
                                                                                                                                                                     'here '
                                                                                                                                                                     'with '
                                                                                                                                                                     'the '
                                                                                                                                                                     'button '
                                                                                                                                                                     'or '
                                                                                                                                                                     '/submit '
                                                                                                                                                                     'group_username, '
                                                                                                                                                                     'and '
                                                                                                                                                                     'an '
                                                                                                                                                                     'admin '
                                                                                                                                                                     'will '
                                                                                                                                                                     'review '
                                                                                                                                                                     'it.\n',
 '\n管理员可管理群聊、白名单和封禁记录；在群内发送 /check all 检查全部成员。\n': '\n'
                                                   'As an admin, you can manage your groups, whitelist and '
                                                   'ban records. Send /check all in a group to check '
                                                   'everyone.\n',
 '可审核新群组申请，或发送 /add group 群组username 直接添加。\n': 'You can review submissions or add a group directly with /add '
                                               'group group_username.\n',
 '超级管理员还可管理授权并查看全部查询日志。\n': 'As the super admin, you can also manage admin access and view all check '
                            'history.\n',
 '\n您当前身份为:{0}': '\nYour role: {0}',
 '返回主页': 'Back to home',
 '请提交公开群聊的username；用户账号和广播频道不支持提交。': "Enter a public group's username. Personal accounts and broadcast "
                                     "channels aren't accepted.",
 '暂未读取到该群组，请确认username后稍后重试。': "We couldn't find that group just now. Double-check the username and try "
                               'again in a bit.',
 '✅ 已添加群组 @{0}（申请 #{1}）。\n所有协议号将自动尝试入群；遇到群主审批或账号限速会等待重试。': '✅ Added @{0} (request #{1}).\n'
                                                           'All connected accounts will try to join. If approval is '
                                                           "needed or Telegram asks us to wait, we'll retry "
                                                           'automatically.',
 '📨 群组 @{0} 已提交（申请 #{1}）。\n管理员和超级管理员会收到审核申请；审核通过后所有协议号自动入群。': '📨 Submitted @{0} (request #{1}).\n'
                                                              'The admins and super admin will get your '
                                                              "request. Once it's approved, all connected accounts "
                                                              'will automatically try to join.',
 '上一页': 'Previous',
 '下一页': 'Next',
 '待审核作弊群组申请：': 'Groups waiting for review:',
 '暂无待审核申请。': "You're all caught up — no submissions to review.",
 '需要管理员权限': 'You need admin access for this',
 '申请不存在': "This submission doesn't exist",
 '返回申请列表': 'Back to submissions',
 '需要管理员权限，且只能操作自己的审核消息': 'You need admin access and must use your own review message',
 '您的管理员权限已撤销': 'Your admin access has been removed',
 '审核完成': 'Review saved',
 '该申请已由其他管理员处理': 'Another admin has already reviewed this submission',
 '尚无您可管理的群聊。请将 Bot 添加到您的群，并发送 /check 让 Bot 记录该群。': "You don't have any groups to manage yet. Add the bot to "
                                                   'your group and send /check so it can register the group.',
 '选择允许查询的群聊：': 'Choose which groups can use checks:',
 '撤销 {0}': 'Remove {0}',
 '➕ 添加管理员': '➕ Add an admin',
 '超级管理员：': 'Super admin: ',
 '\n管理员：': '\nAdmins: ',
 '查询日志（最新在前）：\n<pre>': 'Check history (newest first):\n<pre>',
 '暂无记录': 'Nothing here yet',
 '已封禁用户：\n<pre>': 'Banned users:\n<pre>',
 '已豁免用户：\n<pre>': 'Whitelisted users:\n<pre>',
 '用户: {0}\nID: <code>{1}</code>\n\n该用户为白名单用户，豁免查询': 'User: {0}\n'
                                                    'ID: <code>{1}</code>\n'
                                                    '\n'
                                                    'This user is whitelisted and exempt from checks.',
 '正在查询，请稍候…': 'Checking — hang tight…',
 '查询限速，请在 {0} 秒后重试。': 'Telegram has asked us to slow down. Try again in {0} seconds.',
 '查询失败，请稍后重试。': "That check didn't go through. Please try again in a bit.",
 '⚠️ 检测到作弊用户！\n\n用户: {0}\nID: <code>{1}</code>\n作弊群组数量: {2}': '⚠️ User found in cheating groups!\n'
                                                              '\n'
                                                              'User: {0}\n'
                                                              'ID: <code>{1}</code>\n'
                                                              'Cheating groups: {2}',
 '✅ 用户检查完成\n\n用户: {0}\nID: <code>{1}</code>\n\n未发现该用户在作弊群组中': '✅ Check complete\n'
                                                              '\n'
                                                              'User: {0}\n'
                                                              'ID: <code>{1}</code>\n'
                                                              '\n'
                                                              "We didn't find this user in any cheating "
                                                              'groups.',
 '查看详情群组': 'View matching groups',
 '封禁用户（仅管理员）': 'Ban user (admins only)',
 '格式：/whitelist add|remove 用户名或用户ID，也可引用用户消息。': 'Use /whitelist add|remove username or user_ID, or reply to '
                                                "the user's message.",
 '请引用用户消息，或填写用户名/用户ID。': "Reply to the user's message, or enter their username or user ID.",
 '已{0}白名单用户 {1}。': 'Whitelist updated: {0} user {1}.',
 '封禁操作需要您和 Bot 都拥有本群管理员权限。': 'You and the bot both need admin rights in this group to manage bans.',
 '操作失败：{0}': "That didn't go through: {0}",
 '已{0}用户 <code>{1}</code>。': '{0} user <code>{1}</code>.',
 '已有全员检查正在运行。': 'A full group check is already running.',
 '全员检查需要 Bot 在本群拥有管理员权限。': 'The bot needs admin rights in this group to check everyone.',
 '正在读取群成员，预计用时：计算中…': 'Loading group members. Estimated time: working it out…',
 '计算中': 'Working it out…',
 '约 {0} 秒': 'about {0} seconds',
 '约 {0} 分钟': 'about {0} minutes',
 '约 {0} 小时 {1} 分钟': 'about {0} hours and {1} minutes',
 '准备用户查询': 'Preparing user checks',
 '正在{0}…': '{0}…',
 '已读取：{0} / {1}': 'Loaded: {0} / {1}',
 '未知': 'unknown',
 '正在全员检查…': 'Checking everyone…',
 '已读取群成员：{0}': 'Group members loaded: {0}',
 '直接查询：{0} / {1}': 'User checks: {0} / {1}',
 '已自动处理接口等待，最长 {0}': "We're handling Telegram's wait requests automatically (up to {0})",
 '直接查询预计剩余：{0}': 'User checks — time left: {0}',
 '来源群交集扫描：{0} / {1}': 'Cheating groups scanned: {0} / {1}',
 '来源群成员已读取：{0}': 'Members read from cheating groups: {0}',
 '来源群预计剩余：{0}': 'Group scans — time left: {0}',
 '当前命中：{0}': 'Matches so far: {0}',
 '预计剩余：{0}': 'Estimated time left: {0}',
 '并行查询': 'Running user checks',
 '来源群覆盖不足:%s': 'Missing source group coverage: %s',
 '，仅枚举 {0}/{1} 位成员': '; only {0}/{1} members were visible',
 '群聊 {0} 检查完成：{1} 人命中，成功检查 {2} 人，未完成 {3} 人{4}。跳过已注销账号 {5} 人。\n': 'Finished checking {0}: {1} matches, {2} '
                                                                 'checked, {3} unfinished{4}. Skipped {5} '
                                                                 'deleted accounts.\n',
 '仅枚举 {0}/{1} 位成员。': 'Only {0}/{1} members were visible.',
 '全员检查完成，用时 {0}。命中 {1} 人，结果已私发超级管理员及发起者。查询未完成 {2} 人。跳过已注销账号 {3} 人。{4}': 'Full check finished in {0}. Found {1} matches; results were sent privately to the super admin and the person who started the check. {2} user checks are unfinished. Skipped {3} deleted accounts. {4}',
 '部分收件人私信发送失败，请先私聊 Bot 发送 /start。': "Some private messages didn't go through. Please open the bot's private "
                                    'chat and send /start.',
 '全员检查失败，请稍后重试。': "The full check didn't finish. Please try again in a bit.",
 '请在您管理的群聊中发送 /check all。': 'Send /check all in a group you manage.',
 '请输入要查询的username或用户ID，也可引用用户消息。': "Enter a username or user ID, or reply to the user's message.",
 '查询日志已清空。': 'Check history cleared.',
 '格式：/add 用户ID 或 /revoke 用户ID': 'Use /add user_ID or /revoke user_ID.',
 '已{0}管理员 {1}。': '{0} {1}.',
 '未能解析目标用户。请引用目标消息，或发送 /check 用户名/用户ID。': "We couldn't find that user. Reply to their message, or send "
                                          '/check username or user_ID.',
 '请引用目标消息，或发送 /{0} 用户名/用户ID。': 'Reply to their message, or send /{0} username or user_ID.',
 '格式：/auto check on 或 /auto check off': 'Use /auto check on or /auto check off.',
 '自动查询需要 Bot 在本群拥有管理员权限。': 'The bot needs admin rights in this group to run automatic checks.',
 '已经开启自动查询。': 'Automatic checks are on.',
 '已经关闭自动查询。': 'Automatic checks are off.',
 '无管理权限': "You don't have admin access",
 '无权管理该群聊': "You don't have access to manage this group",
 '设置已更新': 'Settings saved',
 '已到末页': "You're on the last page",
 '该结果不可查看': "This result isn't available here",
 '返回结果': 'Back to result',
 '共同群组列表\n': 'Matching groups\n',
 '请在 Bot 私信中操作': "Please do this in the bot's private chat",
 '请输入要查询的username：\n也可直接发送 /check 用户ID。': "Enter the username you'd like to check:\n"
                                          'You can also send /check followed by a user ID.',
 '请输入作弊群组的username：\n': "Enter the cheating group's username:\n",
 '群组将直接添加，并安排所有协议号入群。': 'The group will be added directly, and all connected accounts will try to join.',
 '提交后等待管理员审核，通过后所有协议号自动入群。': 'An admin will review your submission. Once approved, all connected accounts will try to '
                          'join.',
 '请在您管理且已启用的群聊中发送 /check all；Bot 需要是该群管理员。': 'Send /check all in a group you manage with checks enabled. The '
                                             'bot needs admin rights there.',
 '请输入要添加的用户名或用户ID：': 'Enter the username or user ID to add:',
 '请输入要移除的用户名或用户ID：': 'Enter the username or user ID to remove:',
 '请输入要授权的用户 ID：': 'Enter the user ID to give admin access to:',
 '已撤销': 'Admin access removed',
 '该结果不可操作': "This result can't be used for that action",
 '需要您和 Bot 都拥有群管理员权限': 'You and the bot both need group admin rights',
 '已封禁': 'User banned',
 '已封禁用户 <code>{0}</code>。': 'Banned user <code>{0}</code>.',
 '用户': 'User',
 '管理员': 'Admin',
 '超级管理员': 'Super admin',
 '无': 'None',
 '添加': 'added',
 '移除': 'removed',
 '解除封禁': 'Unbanned',
 '封禁': 'Banned',
 '授权': 'Granted admin access to',
 '撤销': 'Removed admin access for',
 '完成': 'Complete',
 '失败': 'Failed',
 '限速': 'Rate limited',
 '白名单': 'Whitelisted',
 'bot私聊': 'Private check',
 '群组check指令': 'Group check',
 '群组check all': 'Full group check',
 '群组auto消息': 'Automatic message check',
 '群组auto表情': 'Automatic reaction check',
 '群组auto入群': 'Automatic join check',
 '读取群成员': 'Loading group members',
 '扫描来源群': 'Scanning cheating groups',
 '由查询结果封禁': 'Banned from a check result'}

LOG = logging.getLogger("cheatcheck")
EN_MESSAGES.update({
    '🛡️ 群组白名单': '🛡️ Group whitelist',
    '管理用户或群组白名单': 'Manage user and group whitelists',
    '群组白名单（这些群组不计入作弊查询）：\n': 'Group whitelist (excluded from cheating checks):\n',
    '移除：{0}': 'Remove: {0}',
    '返回群组白名单': 'Back to group whitelist',
    '请输入要添加到白名单的群组username：': 'Send the username of the group you want to whitelist:',
    '请输入要移出白名单的群组username：': 'Send the username of the group you want to remove from the whitelist:',
    '格式：/whitelist group 群组username；移除：/whitelist group remove 群组username。': 'Use /whitelist group username to add a group, or /whitelist group remove username to remove it.',
    '已添加群组白名单：{0}（{1}）。\n该群组不计入所有账号的作弊查询。': 'Whitelisted group: {0} ({1}).\nThis group is excluded from cheating checks across all accounts.',
    '已移除群组白名单：{0}（{1}）。\n该群组恢复参与当前可查询范围。': 'Removed group from the whitelist: {0} ({1}).\nChecks will include it again when an account can read it.',
    '该群组已在白名单中。': 'This group is already whitelisted.',
    '该群组不在白名单中。': 'This group is not whitelisted.',
    '仅可移除自己添加的群组白名单；超级管理员可管理全部记录。': 'You can only remove groups you whitelisted. The super admin can manage all entries.',
    '请填写有效群组username，例如 groupname 或 @groupname。': 'Send a valid group username, such as groupname or @groupname.',
    '管理员名单包含 @{0}': 'The admin list includes @{0}',
    '管理员名单读取未完成：{0}': "We couldn't read the full admin list: {0}",
    '⚠️ 来源群已转为手动入群\n\n群组：{0}\n链接：{1}\nID：{2}\n原因：{3}\n账号：所有尚未加入的协议号\n\n自动入群已停止。请超级管理员使用对应协议号手动加入并完成群组验证；已加入的账号无需重复加入。': '⚠️ Manual join needed\n\nGroup: {0}\nLink: {1}\nID: {2}\nReason: {3}\nAccounts: any account that has not joined yet\n\nAutomatic joining has stopped. Please join with those accounts and complete the group verification yourself. Accounts already in the group can stay as they are.',
    "范围受限": "Limited coverage",
    "用户信息准备完成，正在查询…": "User details loaded — starting the checks…",
    "解除封禁用户": "Unban a user",
    "添加": "Add", "移除": "Remove",
    "已添加白名单用户 {0}。": "Added user {0} to the whitelist.",
    "已移除白名单用户 {0}。": "Removed user {0} from the whitelist.",
    "查询用户名或用户ID": "Check a username or user ID",
    "查询群成员或用户ID": "Check a group member or user ID",
    "正在准备全员检查…\n当前批次 {0}：": "Getting ready to check everyone…\nBatch {0}: ",
    "群组：{0}\n当前语言：{1}\n\n请选择群内消息使用的语言：": "Group: {0}\nCurrent language: {1}\n\nChoose the language for all messages in this group:",
    "群组语言已更新": "Group language saved",
    "返回群组管理": "Back to groups",
})
COMMAND = re.compile(r"^(?:/|!)(check|ban|unban|whitelist|auto|submit)(?:@\w+)?(?:\s+(.*))?$", re.I | re.S)
DB_PATH = Path(os.getenv("DATABASE", "data/bot.sqlite3"))
UNRESTRICT_PERMISSIONS = {field: True for field in (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_react_to_messages",
    "can_edit_tag", "can_change_info", "can_invite_users", "can_pin_messages",
    "can_manage_topics",
)}


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS user_languages (user_id INTEGER PRIMARY KEY, language TEXT NOT NULL CHECK(language IN ('zh','en')))")
    db.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)")
    columns = {row[1] for row in db.execute("PRAGMA table_info(groups)")}
    if "owner_id" not in columns:
        db.execute("ALTER TABLE groups ADD COLUMN owner_id INTEGER")
    if "language" not in columns:
        db.execute("ALTER TABLE groups ADD COLUMN language TEXT NOT NULL DEFAULT 'zh'")
    db.execute("""CREATE TABLE IF NOT EXISTS queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        requester_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        method TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        matches INTEGER,
        status TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '[]',
        result_text TEXT NOT NULL DEFAULT ''
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY, label TEXT NOT NULL, added_by INTEGER NOT NULL, created_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS bans (
        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, label TEXT NOT NULL,
        banned_by INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(chat_id,user_id)
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS auto_groups (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS auto_recent (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id))")
    db.execute("CREATE TABLE IF NOT EXISTS expiring_messages (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))")
    db.execute("""CREATE TABLE IF NOT EXISTS bulk_jobs (
        chat_id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, phase TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
        eta_seconds INTEGER, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        request_message_id INTEGER
    )""")
    if "request_message_id" not in {column[1] for column in db.execute("PRAGMA table_info(bulk_jobs)")}:
        db.execute("ALTER TABLE bulk_jobs ADD COLUMN request_message_id INTEGER")
    db.commit()
    return db


def setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def sync_auto_scope(db, current_sources):
    """Invalidate negative auto results when the readable scope expands."""
    signature = json.dumps(sorted(current_sources), separators=(',', ':'))
    previous = setting(db, 'auto_source_ids')
    if previous == signature:
        return False
    try:
        old_sources = set(json.loads(previous)) if previous else set()
    except (TypeError, ValueError):
        old_sources = set()
    expanded = bool(set(current_sources) - old_sources)
    if expanded and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_recent'").fetchone():
        db.execute("DELETE FROM auto_recent")
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_source_ids',?)", (signature,))
    db.commit()
    return expanded


CURRENT_LANGUAGE = ContextVar("cheatcheck_language", default="zh")


def user_language(db, user_id):
    try:
        row = db.execute("SELECT language FROM user_languages WHERE user_id=?", (user_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] in ("zh", "en") else None


def group_language(db, chat_id):
    try:
        row = db.execute("SELECT language FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
    except sqlite3.OperationalError:
        return "zh"
    return row[0] if row and row[0] in ("zh", "en") else "zh"


@contextmanager
def language_context(language):
    token = CURRENT_LANGUAGE.set(language or "zh")
    try:
        yield
    finally:
        CURRENT_LANGUAGE.reset(token)


class LocalizedText(str):
    """Keep templates separate from user-provided names, even when text is joined."""
    def __new__(cls, template=None, args=(), parts=None, language=None):
        language = language or CURRENT_LANGUAGE.get()
        def render(value):
            return value.render(language) if isinstance(value, LocalizedText) else value
        text = ("".join(str(render(part)) for part in parts) if parts is not None else
                (EN_MESSAGES.get(template, template) if language == "en" else template).format(*[render(arg) for arg in args]))
        result = super().__new__(cls, text)
        result.template, result.args, result.parts = template, args, parts
        return result

    def render(self, language):
        return str(LocalizedText(self.template, self.args, self.parts, language))

    def __add__(self, other):
        return LocalizedText(parts=(self, other))

    def __radd__(self, other):
        return LocalizedText(parts=(other, self))


def tr(template, *args):
    return LocalizedText(template, args)


def language_handler(db):
    def decorate(handler):
        @wraps(handler)
        async def wrapped(event):
            language = (group_language(db, event.chat_id) if getattr(event, "chat_id", 0) and event.chat_id < 0
                        else user_language(db, getattr(event, "sender_id", None)))
            with language_context(language):
                return await handler(event)
        return wrapped
    return decorate


def actor_language(db):
    def decorate(handler):
        @wraps(handler)
        async def wrapped(chat_id, actor_id, *args, **kwargs):
            with language_context(group_language(db, chat_id) if chat_id < 0 else user_language(db, actor_id)):
                return await handler(chat_id, actor_id, *args, **kwargs)
        return wrapped
    return decorate


def object_language(db, argument=0):
    def decorate(handler):
        @wraps(handler)
        async def wrapped(*args, **kwargs):
            language = group_language(db, args[0]) if argument == 1 else user_language(db, args[argument].id)
            with language_context(language):
                return await handler(*args, **kwargs)
        return wrapped
    return decorate


def saved_result_text(text):
    match = re.fullmatch(r"(⚠️ 检测到作弊用户！|✅ 用户检查完成)\n\n用户: ([\s\S]*?)\nID: <code>(\d+)</code>\n(?:作弊群组数量: (\d+)|\n未发现该用户在作弊群组中)", text)
    if not match:
        return text
    if match[4] is not None:
        return tr("⚠️ 检测到作弊用户！\n\n用户: {0}\nID: <code>{1}</code>\n作弊群组数量: {2}", match[2], match[3], match[4])
    return tr("✅ 用户检查完成\n\n用户: {0}\nID: <code>{1}</code>\n\n未发现该用户在作弊群组中", match[2], match[3])


def is_admin(db, user_id):
    if str(user_id) == setting(db, "superadmin"):
        return True
    return db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def role(db, user_id):
    if str(user_id) == setting(db, "superadmin"):
        return tr('超级管理员')
    return tr('管理员') if is_admin(db, user_id) else tr('用户')


def log_query(db, requester_id, target_id, method, chat_id, matches, status, details=None, result_text=""):
    cursor = db.execute(
        "INSERT INTO queries(created_at,requester_id,target_id,method,chat_id,matches,status,details_json,result_text) VALUES(?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), requester_id,
         target_id, method, chat_id, matches, status, json.dumps(details or [], ensure_ascii=False), result_text),
    )
    db.commit()
    return cursor.lastrowid


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def group_username(value):
    value = value.strip()
    if value.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        value = value.split("t.me/", 1)[1].rstrip("/")
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError(tr('请填写公开群组的username，例如 /submit groupname。'))
    return value.lower()


def person_label(person):
    name = " ".join(filter(None, (getattr(person, "first_name", ""), getattr(person, "last_name", ""))))
    username = getattr(person, "username", None)
    return (f"@{username} " if username else "") + (name or str(person.id)) + f"（{person.id}）"


def result_recipients(owner, actor_id):
    return list(dict.fromkeys(([int(owner)] if owner else []) + [int(actor_id)]))


async def deliver_bulk_result(bot, owner, actor_id, header, hits, db=None):
    failed = []
    lines = [str(user_id) for user_id in hits]
    for recipient in result_recipients(owner, actor_id):
        try:
            for start in range(0, max(1, len(lines)), 250):
                chunk = lines[start:start + 250]
                language = user_language(db, recipient) if db is not None else CURRENT_LANGUAGE.get()
                localized_header = header.render(language or "zh") if isinstance(header, LocalizedText) else header
                continuation = "Continued:\n" if language == "en" else "续：\n"
                await bot.send_message(recipient, (localized_header if start == 0 else continuation) +
                                       "<pre>" + "\n".join(chunk or ["无"]) + "</pre>", parse_mode="html")
        except Exception:
            LOG.exception("Could not deliver bulk result to %s", recipient)
            failed.append(recipient)
    return failed


class GroupSubmissions:
    """Durable review decisions, notifications and account membership jobs."""

    FIELDS = "id,group_id,username,title,submitter_id,submitter_label,status,reviewer_id,reviewer_label,feedback_sent"

    def __init__(self, db, bot, users, refresh, join_pending=False):
        self.db, self.bot, self.users, self.refresh = db, bot, users, refresh
        self.join_pending = join_pending
        self.group_whitelist = GroupWhitelist(db)
        self.wake = asyncio.Event()
        self.account_locks = [asyncio.Lock() for _ in users]
        self.membership_recheck_at = 0.0
        self.unverified_memberships = set()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS source_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
                username TEXT NOT NULL, title TEXT NOT NULL,
                submitter_id INTEGER NOT NULL, submitter_label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', reviewer_id INTEGER,
                reviewer_label TEXT, created_at TEXT NOT NULL, reviewed_at TEXT,
                feedback_sent INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS pending_source_group
                ON source_submissions(group_id) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS cheat_sources (
                group_id INTEGER PRIMARY KEY, username TEXT NOT NULL, title TEXT NOT NULL,
                added_by INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS submission_messages (
                recipient_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                request_id INTEGER NOT NULL, finalized INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(recipient_id,message_id));
            CREATE TABLE IF NOT EXISTS source_join_jobs (
                request_id INTEGER NOT NULL, account_index INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', joined_by_us INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(request_id,account_index));
            CREATE TABLE IF NOT EXISTS source_membership_alerts (
                account_index INTEGER NOT NULL,group_id INTEGER NOT NULL,title TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',notified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_index,group_id));
        """)
        db.commit()
        with db:
            for index in range(len(users)):
                active=automatic_join_enabled(db)
                db.execute("""INSERT OR IGNORE INTO source_join_jobs(request_id,account_index,state,detail)
                    SELECT id,?,?,? FROM source_submissions WHERE status IN ('pending','approved')""",
                    (index,'queued' if active else 'closed','' if active else '用户已停止自动入群'))
        self.rpc = None
        self.policy = PreJoinPolicy(db, users, self.wake)
        self.imports = GroupImportQueue(db)

    def request(self, request_id):
        row = self.db.execute(f"SELECT {self.FIELDS} FROM source_submissions WHERE id=?", (request_id,)).fetchone()
        return dict(zip(self.FIELDS.split(","), row)) if row else None

    def managed_ids(self):
        return {row[0] for row in self.db.execute("SELECT DISTINCT group_id FROM source_submissions")}

    def paused_ids(self, index):
        return {row[0] for row in self.db.execute('SELECT group_id FROM source_membership_alerts WHERE account_index=?',(index,))}

    async def readable_memberships(self, index, dialogs):
        dialogs={gid:entity for gid,entity in dialogs.items() if not getattr(entity,'left',False)}
        verify_ids=self.filter_sources(set()) | self.paused_ids(index)
        self.unverified_memberships.difference_update((index,gid) for gid in verify_ids-dialogs.keys())
        client=self.users[index]
        for gid in verify_ids & dialogs.keys():
            if not isinstance(dialogs[gid],Channel):
                continue
            self.unverified_memberships.discard((index,gid))
            try:
                request=functions.channels.GetParticipantRequest(dialogs[gid],types.InputPeerSelf())
                member=(await self.rpc.call(client,request,max_wait=0,total_timeout=6)
                        if self.rpc else await asyncio.wait_for(client(request),6))
            except (errors.UserNotParticipantError,errors.ChannelPrivateError):
                dialogs.pop(gid,None)
                continue
            except (errors.RPCError,OSError,asyncio.TimeoutError):
                # A timeout/limit is unknown, not evidence of a kick. Keep the
                # membership job unchanged while excluding this scope from checks.
                self.unverified_memberships.add((index,gid))
                dialogs.pop(gid,None)
                LOG.warning('Account %s membership verification unavailable for %s',index+1,gid)
                continue
            participant=member.participant
            if isinstance(participant,types.ChannelParticipantLeft) or (
                    isinstance(participant,types.ChannelParticipantBanned) and (
                        participant.left or participant.banned_rights.view_messages)):
                dialogs.pop(gid,None)
        return set(dialogs)

    def filter_sources(self, ids, include_exempt=False):
        approved = {row[0] for row in self.db.execute("SELECT group_id FROM cheat_sources")}
        sources=(set(ids) - self.managed_ids()) | approved
        return sources if include_exempt else sources - exempt_source_ids(self.db)

    def reconcile_memberships(self, memberships):
        """Requeue stale completed joins after an account leaves or is removed."""
        changed = False
        rows = self.db.execute("""SELECT j.request_id,j.account_index,r.group_id,j.state,r.title,r.username,j.detail
            FROM source_join_jobs j JOIN source_submissions r ON r.id=j.request_id
            WHERE r.status='approved' AND j.account_index<?""", (len(self.users),)).fetchall()
        with self.db:
            for request_id, index, group_id, state, title, username, detail in rows:
                client_key = id(self.users[index])
                if client_key not in memberships:
                    continue
                if (index,group_id) in self.unverified_memberships:
                    continue
                joined = group_id in memberships[client_key]
                if not joined and (state == "joined" or (state == 'retry' and detail in ('ChannelPrivateError','UserBannedInChannelError'))):
                    self.pause_membership(index, group_id, title, username)
                    changed = True
                elif joined and state != "joined":
                    self.db.execute("""UPDATE source_join_jobs SET state='joined',retry_at=0,detail=''
                        WHERE request_id=? AND account_index=?""", (request_id, index))
                    changed = True
            for index, group_id in self.db.execute('SELECT account_index,group_id FROM source_membership_alerts').fetchall():
                if index < len(self.users) and group_id in memberships.get(id(self.users[index]), ()):
                    self.db.execute('DELETE FROM source_membership_alerts WHERE account_index=? AND group_id=?', (index, group_id))
                    changed = True
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_account_sync'").fetchone():
                sync_rows=self.db.execute("""SELECT s.account_index,s.group_id,s.state,COALESCE(g.title,CAST(s.group_id AS TEXT)),COALESCE(g.username,'')
                    FROM source_account_sync s LEFT JOIN source_sync_groups g ON g.group_id=s.group_id
                    WHERE s.account_index<? AND s.state IN ('joined','manual_required')""",(len(self.users),)).fetchall()
                for index,gid,state,title,username in sync_rows:
                    if id(self.users[index]) not in memberships:
                        continue
                    if (index,gid) in self.unverified_memberships:
                        continue
                    joined=gid in memberships[id(self.users[index])]
                    if state=='joined' and not joined:
                        self.pause_membership(index,gid,title,username)
                        self.db.execute("UPDATE source_account_sync SET state='manual_required',retry_at=0 WHERE account_index=? AND group_id=?",(index,gid))
                        changed=True
                    elif state=='manual_required' and joined:
                        self.db.execute("UPDATE source_account_sync SET state='joined',retry_at=0,detail='' WHERE account_index=? AND group_id=?",(index,gid))
                        changed=True
        if changed:
            self.wake.set()
        return changed

    def pause_membership(self, index, group_id, title, username=''):
        if title == str(group_id) or not username:
            for client in self.users:
                filename=getattr(client.session,'filename',None) if hasattr(client,'session') else None
                if not filename or not Path(filename).is_file():
                    continue
                try:
                    cached=sqlite3.connect('file:'+str(Path(filename).resolve()).replace('\\','/')+'?mode=ro',uri=True)
                    try:row=cached.execute('SELECT username,name FROM entities WHERE id=?',(group_id,)).fetchone()
                    finally:cached.close()
                    if row:
                        username=username or row[0] or ''
                        if title==str(group_id):title=row[1] or title
                except sqlite3.Error:
                    continue
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO source_membership_alerts(account_index,group_id,title,username)
                VALUES(?,?,?,?)""", (index,group_id,title,username or ''))
            self.db.execute("""UPDATE source_join_jobs SET state='manual_required',retry_at=0,
                detail='成员身份失效，等待超级管理员手动入群并完成验证'
                WHERE account_index=? AND request_id IN (SELECT id FROM source_submissions WHERE group_id=? AND status!='rejected')""", (index,group_id))
        LOG.warning('Account %s lost source membership %s; automatic rejoin paused',index+1,group_id)
        self.wake.set()

    async def membership_notifications(self):
        owner=setting(self.db,'superadmin')
        if not owner:
            return
        for index,gid,title,username in self.db.execute('SELECT account_index,group_id,title,username FROM source_membership_alerts WHERE notified=0').fetchall():
            if index >= len(self.users):
                continue
            try:
                with language_context(user_language(self.db,int(owner))):
                    text=tr('⚠️ 来源群需手动入群\n\n群组：{0}\n链接：{1}\nID：{2}\n协议号：第 {3} 个\n\n该账号已失去成员身份或被禁止访问。自动重新入群已停止。请使用该协议号手动加入并完成验证，加入后检测范围会自动恢复。',title,'https://t.me/'+username if username else tr('暂无可用链接（私有群组）'),gid,index+1)
                    await asyncio.wait_for(self.bot.send_message(int(owner),text,parse_mode=None),15)
            except (ValueError,errors.RPCError,OSError,asyncio.TimeoutError):
                LOG.warning('Manual membership notice deferred: account %s group %s',index+1,gid)
                continue
            with self.db:
                self.db.execute('UPDATE source_membership_alerts SET notified=1 WHERE account_index=? AND group_id=?',(index,gid))

    async def policy_notifications(self):
        owner=setting(self.db,'superadmin')
        if not owner or not self.bot:
            return
        for gid,title,username,reason,detail in self.policy.pending_notices():
            try:
                with language_context(user_language(self.db,int(owner))):
                    explanation=(tr('管理员名单包含 @{0}',self.policy.WATCHED_USERNAME) if reason=='blocked'
                                 else tr('管理员名单读取未完成：{0}',detail))
                    text=tr('⚠️ 来源群已转为手动入群\n\n群组：{0}\n链接：{1}\nID：{2}\n原因：{3}\n账号：所有尚未加入的协议号\n\n自动入群已停止。请超级管理员使用对应协议号手动加入并完成群组验证；已加入的账号无需重复加入。',
                            title,'https://t.me/'+username if username else tr('暂无可用链接（私有群组）'),gid,explanation)
                    await asyncio.wait_for(self.bot.send_message(int(owner),text,parse_mode=None),15)
            except (ValueError,errors.RPCError,OSError,asyncio.TimeoutError):
                LOG.warning('Pre-join policy notice deferred for group %s',gid)
                continue
            self.policy.mark_notified(gid)

    def create(self, group, username, sender, approved=False, *, allow_registered=False):
        if approved and not is_admin(self.db, sender.id):
            raise PermissionError(tr('需要管理员权限。'))
        group_id = telegram_id(group)
        if self.db.execute("SELECT 1 FROM cheat_sources WHERE group_id=?", (group_id,)).fetchone() and not (approved and allow_registered):
            raise ValueError(tr('该群组已在检测范围内。'))
        old = self.db.execute("SELECT id FROM source_submissions WHERE group_id=? AND status='pending'", (group_id,)).fetchone()
        if old:
            if approved:
                self.decide(old[0], sender, "approved")
                return old[0]
            raise ValueError(tr('该群已有待审核申请（#{0}），请等待审核。' ,old[0]))
        with self.db:
            cursor = self.db.execute("""INSERT INTO source_submissions
                (group_id,username,title,submitter_id,submitter_label,status,reviewer_id,reviewer_label,created_at,reviewed_at,feedback_sent)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (group_id, username, group_title(group), sender.id, person_label(sender),
                 "approved" if approved else "pending", sender.id if approved else None,
                 person_label(sender) if approved else None, utc_now(), utc_now() if approved else None, int(approved)))
            request_id = cursor.lastrowid
            active=automatic_join_enabled(self.db)
            self.db.executemany("INSERT INTO source_join_jobs(request_id,account_index,state,detail) VALUES(?,?,?,?)",
                                [(request_id,index,'queued' if active else 'closed','' if active else '用户已停止自动入群')
                                 for index in range(len(self.users))])
            if approved:
                self._activate(self.request(request_id), sender.id)
        self.wake.set()
        return request_id

    def _activate(self, request, actor_id):
        self.db.execute("""INSERT INTO cheat_sources(group_id,username,title,added_by,created_at)
            VALUES(?,?,?,?,?) ON CONFLICT(group_id) DO NOTHING""",
            (request["group_id"], request["username"], request["title"], actor_id, utc_now()))

    def decide(self, request_id, reviewer, status):
        if not is_admin(self.db, reviewer.id):
            raise PermissionError(tr('需要管理员权限。'))
        if status not in ("approved", "rejected"):
            raise ValueError(tr('无效审核选项。'))
        with self.db:
            changed = self.db.execute("""UPDATE source_submissions SET status=?,reviewer_id=?,reviewer_label=?,reviewed_at=?
                WHERE id=? AND status='pending'""", (status, reviewer.id, person_label(reviewer), utc_now(), request_id)).rowcount
            if changed and status == "approved":
                self._activate(self.request(request_id), reviewer.id)
        self.wake.set()
        return bool(changed)

    def review_text(self, request):
        state = {"pending": tr('⏳ 待审核'), "approved": tr('✅ 已通过'), "rejected": tr('❌ 未通过')}[request["status"]]
        text = (tr('作弊群组申请 #{0} · {1}\n\n群组：{2}\n@{3}\nID：{4}\n提交者：{5}' ,request['id'], state, request['title'], request['username'], request['group_id'], request['submitter_label']))
        if request["status"] != "pending":
            text += tr('\n处理人：{0}' ,request['reviewer_label'])
            if request["status"] == "approved":
                text += tr('\n所有协议号入群任务已安排；需群主批准或账号限速时会自动等待重试。')
        return text

    def review_buttons(self, request):
        if request["status"] != "pending":
            return None
        return [[Button.inline(tr('通过'), f"review:approved:{request['id']}".encode()),
                 Button.inline(tr('不通过'), f"review:rejected:{request['id']}".encode())]]

    def bind_message(self, request_id, recipient, message_id):
        with self.db:
            self.db.execute("""INSERT INTO submission_messages(recipient_id,message_id,request_id) VALUES(?,?,?)
                ON CONFLICT(recipient_id,message_id) DO UPDATE SET request_id=excluded.request_id,finalized=0""",
                (recipient, message_id, request_id))

    def can_review_message(self, request_id, actor_id, message_id):
        return is_admin(self.db, actor_id) and bool(self.db.execute(
            "SELECT 1 FROM submission_messages WHERE recipient_id=? AND message_id=? AND request_id=?",
            (actor_id, message_id, request_id)).fetchone())

    async def notifications(self):
        await self.membership_notifications()
        await self.policy_notifications()
        owner = setting(self.db, "superadmin")
        admins = {row[0] for row in self.db.execute("SELECT user_id FROM admins")}
        if owner:
            admins.add(int(owner))
        for (request_id,) in self.db.execute("SELECT id FROM source_submissions WHERE status='pending'").fetchall():
            for admin_id in admins:
                if self.db.execute("SELECT 1 FROM submission_messages WHERE request_id=? AND recipient_id=?", (request_id, admin_id)).fetchone():
                    continue
                try:
                    request = self.request(request_id)
                    if request["status"] != "pending" or not is_admin(self.db, admin_id):
                        continue
                    with language_context(user_language(self.db, admin_id)):
                        message = await self.bot.send_message(admin_id, self.review_text(request),
                            buttons=self.review_buttons(request), parse_mode=None)
                    self.bind_message(request_id, admin_id, message.id)
                except (ValueError, errors.RPCError):
                    LOG.warning("Review notification deferred for request %s to %s", request_id, admin_id)
        rows = self.db.execute("""SELECT m.request_id,m.recipient_id,m.message_id FROM submission_messages m
            JOIN source_submissions r ON r.id=m.request_id WHERE r.status!='pending' AND m.finalized=0""").fetchall()
        for request_id, recipient, message_id in rows:
            try:
                with language_context(user_language(self.db, recipient)):
                    await self.bot.edit_message(recipient, message_id, self.review_text(self.request(request_id)), buttons=None, parse_mode=None)
            except errors.MessageNotModifiedError:
                pass
            except (ValueError, errors.RPCError):
                continue
            with self.db:
                self.db.execute("UPDATE submission_messages SET finalized=1 WHERE recipient_id=? AND message_id=? AND request_id=?",
                                (recipient, message_id, request_id))
        for (request_id,) in self.db.execute("SELECT id FROM source_submissions WHERE status!='pending' AND feedback_sent=0").fetchall():
            request = self.request(request_id)
            try:
                with language_context(user_language(self.db, request["submitter_id"])):
                    await self.bot.send_message(request["submitter_id"], tr('您的申请审核结果：\n\n') + self.review_text(request), parse_mode=None)
            except (ValueError, errors.RPCError):
                continue
            with self.db:
                self.db.execute("UPDATE source_submissions SET feedback_sent=1 WHERE id=?", (request_id,))

    async def join_account(self, request_id, index):
        if not automatic_join_enabled(self.db):
            return False
        async with self.account_locks[index]:
            if not automatic_join_enabled(self.db):
                return False
            request = self.request(request_id)
            state, joined_by_us = self.db.execute("SELECT state,joined_by_us FROM source_join_jobs WHERE request_id=? AND account_index=?", (request_id, index)).fetchone()
            if state == 'closed' or (state == 'manual_required' and request['status'] != 'rejected'):
                return False
            client = self.users[index]
            try:
                if request["status"] == "pending" and not self.join_pending:
                    return False
                if request["status"] == "rejected":
                    if joined_by_us:
                        entity = await client.get_input_entity(request["group_id"])
                        await client(functions.channels.LeaveChannelRequest(entity))
                    next_state, detail, retry = "closed", "", 0
                else:
                    try:
                        peer = await client.get_input_entity(request["group_id"])
                    except ValueError:
                        peer = request["username"]
                    entity = await asyncio.wait_for(client.get_entity(peer), timeout=30)
                    if not isinstance(entity, Channel) or not entity.megagroup or telegram_id(entity) != request["group_id"]:
                        raise ValueError("群组username已改变或不再指向原群组。")
                    # Persist intent before joining, so a restart/rejection can clean up an in-flight join.
                    if not joined_by_us:
                        try:
                            member = await asyncio.wait_for(client(functions.channels.GetParticipantRequest(entity, types.InputPeerSelf())), timeout=30)
                            is_member = not isinstance(member.participant, types.ChannelParticipantLeft) and not (
                                isinstance(member.participant, types.ChannelParticipantBanned) and member.participant.left)
                        except errors.UserNotParticipantError:
                            is_member = False
                        if is_member:
                            next_state, detail, retry = "joined", "原已加入", 0
                            with self.db:
                                self.db.execute("UPDATE source_join_jobs SET state=?,retry_at=0,detail=? WHERE request_id=? AND account_index=?", (next_state, detail, request_id, index))
                            return True
                    if not await self.policy.allow(index,entity,self.rpc):
                        return False
                    if self.rpc and self.rpc.remaining(client, functions.channels.JoinChannelRequest):
                        until=int(time.time()+self.rpc.remaining(client,functions.channels.JoinChannelRequest))+2
                        with self.db:self.db.execute("UPDATE source_join_jobs SET state='waiting_limit',retry_at=?,detail='等待入群接口冷却' WHERE request_id=? AND account_index=?",(until,request_id,index))
                        return False
                    if self.request(request_id)["status"] == "rejected":
                        self.wake.set()
                        return False
                    if not automatic_join_enabled(self.db):
                        return False
                    if not joined_by_us:
                        with self.db:
                            self.db.execute("UPDATE source_join_jobs SET joined_by_us=1 WHERE request_id=? AND account_index=?", (request_id,index))
                    if self.rpc:
                        await self.rpc.call(client, functions.channels.JoinChannelRequest(entity), max_wait=0, total_timeout=30)
                    else:
                        await asyncio.wait_for(client(functions.channels.JoinChannelRequest(entity)), timeout=30)
                    next_state, detail, retry = "joined", "", 0
            except errors.UserAlreadyParticipantError:
                next_state, detail, retry = "joined", "", 0
            except errors.UserNotParticipantError:
                next_state, detail, retry = ("closed", "", 0) if request["status"] == "rejected" else (
                    "retry", "UserNotParticipantError", int(time.time()) + 600)
            except errors.InviteRequestSentError:
                next_state, detail, retry = "waiting_approval", "等待群主批准", int(time.time()) + 600
            except (errors.ChannelPrivateError,errors.UserBannedInChannelError):
                self.pause_membership(index,request['group_id'],request['title'],request['username'])
                return False
            except errors.FloodWaitError as exc:
                next_state, detail, retry = "waiting_limit", f"等待{exc.seconds}秒", int(time.time()) + exc.seconds + 2
            except (ValueError, errors.RPCError, asyncio.TimeoutError) as exc:
                next_state, detail, retry = "retry", type(exc).__name__, int(time.time()) + 600
                LOG.warning("Source join request %s account %s: %s", request_id, index + 1, detail)
            with self.db:
                self.db.execute("UPDATE source_join_jobs SET state=?,retry_at=?,detail=? WHERE request_id=? AND account_index=?",
                                (next_state, retry, detail, request_id, index))
            return next_state in ("joined", "closed")

    async def cycle(self):
        await self.notifications()
        await self.join_cycle()

    async def join_cycle(self):
        if not automatic_join_enabled(self.db):
            return
        # Source bots may eject a newly joined account after the join RPC succeeds.
        # Check once after their verification window, rather than trusting that response.
        if self.membership_recheck_at and time.monotonic() >= self.membership_recheck_at:
            self.membership_recheck_at = 0.0
            await self.refresh(force=True)
        rows = self.db.execute("""SELECT j.request_id,j.account_index FROM source_join_jobs j
            JOIN source_submissions r ON r.id=j.request_id WHERE j.retry_at<=? AND
            j.account_index<? AND ((r.status='approved' AND j.state NOT IN ('joined','closed','manual_required')) OR
             (r.status='pending' AND ? AND j.state NOT IN ('joined','closed','manual_required')) OR
             (r.status='rejected' AND j.state!='closed'))""", (int(time.time()), len(self.users), int(self.join_pending))).fetchall()
        if rows:
            results = await asyncio.gather(*(self.join_account(request_id, index) for request_id, index in rows))
            # A queued job skipped during cooldown must not invalidate every query cache.
            if any(results):
                await self.refresh(force=True)
                self.membership_recheck_at = time.monotonic() + 30

    async def run(self):
        async def import_loop():
            while True:
                try:
                    await self.imports.cycle(self)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception('Source CSV import worker failed')
                await asyncio.sleep(3)

        async def membership_loop():
            while True:
                try:
                    await self.join_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Source membership worker failed")
                await asyncio.sleep(15)

        membership_task = asyncio.create_task(membership_loop())
        import_task = asyncio.create_task(import_loop())
        try:
            while True:
                self.wake.clear()
                try:
                    await self.notifications()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Source submissions notification worker failed")
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
        finally:
            membership_task.cancel()
            import_task.cancel()
            await asyncio.gather(membership_task,import_task, return_exceptions=True)


def bot_api_sync(token, method, payload):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:
            data = json.load(response)
    except HTTPError as exc:
        try:
            description = json.load(exc).get("description", "Telegram API request failed")
        except (ValueError, OSError):
            description = "Telegram API request failed"
        raise RuntimeError(description) from None
    except URLError:
        raise RuntimeError("Telegram API connection failed") from None
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API request failed"))
    return data.get("result")


def proxy_config():
    value = os.getenv("TELEGRAM_PROXY", "").strip()
    if not value:
        return None
    url = urlparse(value)
    if url.scheme.lower() != "socks5" or not url.hostname or not url.port:
        raise ValueError("TELEGRAM_PROXY must be socks5://host:port")
    return ("socks5", url.hostname, url.port, bool(url.username), url.username, url.password)


def user_client_from_config(proxy, json_key="ACCOUNT_JSON", session_key="ACCOUNT_SESSION"):
    with open(os.environ[json_key], encoding="utf-8") as file:
        data = json.load(file)
    return TelegramClient(
        os.environ[session_key], int(data.get("app_id") or data["api_id"]),
        str(data.get("app_hash") or data["api_hash"]),
        device_model=data.get("device_model") or data.get("device") or "Desktop",
        system_version=data.get("system_version") or data.get("sdk") or "Linux",
        app_version=data.get("app_version") or "1.0",
        lang_code=data.get("lang_code") or "en",
        system_lang_code=data.get("system_lang_code") or "en",
        proxy=proxy,
        flood_sleep_threshold=0,
    )




def user_clients_from_config(proxy):
    return [user_client_from_config(proxy, *keys) for keys in account_config_keys()]


class AccountGroupSync:
    """Persist group join progress separately for every configured account."""
    def __init__(self, db, clients, rpc, submissions, report_path):
        self.db, self.clients, self.rpc = db, list(clients), rpc
        self.submissions, self.report_path = submissions, Path(report_path)
        self.last_read, self.dialogs = 0.0, {}
        db.execute("""CREATE TABLE IF NOT EXISTS source_account_sync (
            account_index INTEGER NOT NULL, group_id INTEGER NOT NULL, state TEXT NOT NULL,
            retry_at INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(account_index,group_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS source_sync_groups (
            group_id INTEGER PRIMARY KEY,title TEXT NOT NULL,username TEXT NOT NULL DEFAULT '')""")
        db.commit()

    async def cycle(self, force=False):
        if not automatic_join_enabled(self.db):
            return
        if len(self.clients) < 2:
            return
        refreshed = force or time.monotonic() - self.last_read >= 900 or not self.dialogs
        changed = False
        if refreshed:
            fresh_memberships = {}
            async def read(client):
                try:
                    async def collect():
                        return [d.entity async for d in client.iter_dialogs()
                                if isinstance(d.entity, (Channel, Chat))
                                and (not isinstance(d.entity, Channel) or d.entity.megagroup)]
                    async def collect_readable():
                        entities=await collect()
                        readable=await self.submissions.readable_memberships(self.clients.index(client),
                                      {telegram_id(entity):entity for entity in entities})
                        return [entity for entity in entities if telegram_id(entity) in readable]
                    self.dialogs[id(client)] = await asyncio.wait_for(collect_readable(), 45)
                    fresh_memberships[id(client)]={telegram_id(entity) for entity in self.dialogs[id(client)]}
                except (errors.RPCError, OSError, asyncio.TimeoutError):
                    LOG.warning("Account %s group sync refresh failed", self.clients.index(client) + 1)
            await asyncio.gather(*(read(client) for client in self.clients))
            self.last_read = time.monotonic()
        targets = {telegram_id(entity): entity for entities in self.dialogs.values() for entity in entities}
        with self.db:
            self.db.executemany("""INSERT INTO source_sync_groups VALUES(?,?,?) ON CONFLICT(group_id)
                DO UPDATE SET title=excluded.title,username=excluded.username""",
                [(gid,group_title(entity),getattr(entity,'username',None) or '') for gid,entity in targets.items()])
        if refreshed:
            self.submissions.reconcile_memberships(fresh_memberships)
        managed = self.submissions.managed_ids()

        async def sync(index, client):
            nonlocal changed
            existing = {telegram_id(entity) for entity in self.dialogs.get(id(client), ())}
            attempted = 0
            for group_id, entity in targets.items():
                if not automatic_join_enabled(self.db):
                    break
                if group_id in existing or group_id in managed:
                    continue
                row = self.db.execute("SELECT state,retry_at FROM source_account_sync WHERE account_index=? AND group_id=?",
                                      (index, group_id)).fetchone()
                if (row and row[0] in ('manual_required','closed')) or self.db.execute(
                        'SELECT 1 FROM source_membership_alerts WHERE account_index=? AND group_id=?',(index,group_id)).fetchone():
                    continue
                if row and row[1] > time.time():
                    continue
                username = getattr(entity, "username", None)
                state, detail, retry_at = "pending_private", "", int(time.time()) + 86400
                if username:
                    if self.rpc.remaining(client, functions.channels.JoinChannelRequest):
                        break
                    if attempted >= 10:
                        break
                    attempted += 1
                    try:
                        async with self.submissions.account_locks[index]:
                            allowed=await self.submissions.policy.allow(index,entity,self.rpc,account_entity=False)
                            if allowed and automatic_join_enabled(self.db):
                                await self.rpc.call(client, functions.channels.JoinChannelRequest(username),
                                                    max_wait=0, total_timeout=20)
                            elif allowed:
                                break
                        if allowed:
                            state, retry_at = "joined", 0
                            self.dialogs.setdefault(id(client), []).append(entity)
                            existing.add(group_id)
                            changed = True
                            LOG.info("Account %s joined source group %s", index + 1, group_id)
                        else:
                            state,detail,retry_at='manual_required','入群前管理员检查要求手动处理',0
                    except errors.UserAlreadyParticipantError:
                        state, retry_at = "joined", 0
                        self.dialogs.setdefault(id(client), []).append(entity)
                        existing.add(group_id)
                        changed = True
                    except errors.InviteRequestSentError:
                        state, retry_at = "waiting_approval", int(time.time()) + 3600
                    except (errors.ChannelPrivateError,errors.UserBannedInChannelError):
                        self.submissions.pause_membership(index,group_id,group_title(entity),username)
                        state,detail,retry_at='manual_required','等待超级管理员手动入群并完成验证',0
                    except errors.FloodWaitError as exc:
                        state, detail, retry_at = "waiting_limit", f"FloodWait {exc.seconds}s", int(time.time()) + exc.seconds + 2
                    except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                        state, detail, retry_at = "retry", type(exc).__name__, int(time.time()) + 3600
                with self.db:
                    self.db.execute("""INSERT INTO source_account_sync(account_index,group_id,state,retry_at,detail)
                        VALUES(?,?,?,?,?) ON CONFLICT(account_index,group_id)
                        DO UPDATE SET state=excluded.state,retry_at=excluded.retry_at,detail=excluded.detail""",
                                    (index, group_id, state, retry_at, detail))
                if state == "waiting_limit":
                    break
                if username:
                    await asyncio.sleep(2)
            counts = {state: count for state, count in self.db.execute(
                "SELECT state,count(*) FROM source_account_sync WHERE account_index=? GROUP BY state", (index,))}
            return {"account": index + 1, "joined_groups": len(existing), "tasks": counts}

        accounts = await asyncio.gather(*(sync(index, client) for index, client in enumerate(self.clients)))
        if changed:
            self.submissions.membership_recheck_at=time.monotonic()+30
        report = {"updated_at": int(time.time()), "source_total": len(targets), "accounts": accounts,
                  "membership_changed": changed, "memberships_refreshed": refreshed}
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Keep older status tools usable while the full report covers every account.
        secondary = accounts[1]
        legacy = {"updated_at": report["updated_at"], "primary_total": len(targets),
                  "secondary_joined": secondary["joined_groups"]}
        for field, state in [("joined_public", "joined"), ("pending_private", "pending_private"),
                             ("pending_approval_or_verification", "waiting_approval"), ("failed", "retry")]:
            legacy[field] = [{"id": gid, "reason": detail} for gid, detail in self.db.execute(
                "SELECT group_id,detail FROM source_account_sync WHERE account_index=1 AND state=?", (state,))]
        self.report_path.with_name("secondary_sync.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
        return report






async def source_member_pages(client, source_id, rpc):
    """Read account-scoped pages with shared throttling and repeat-page guards."""
    peer = await client.get_input_entity(source_id)
    if isinstance(peer, types.InputPeerChat):
        full = await rpc.call(client, functions.messages.GetFullChatRequest(peer.chat_id), max_wait=0)
        participants = full.full_chat.participants
        complete = isinstance(participants, types.ChatParticipants)
        member_ids = {p.user_id for p in participants.participants} if complete else set()
        yield [user for user in full.users if user.id in member_ids], complete
        return
    channel = utils.get_input_channel(peer)
    full = await rpc.call(client, functions.channels.GetFullChannelRequest(channel), max_wait=0)
    expected = getattr(full.full_chat, "participants_count", None)
    hidden = bool(getattr(full.full_chat, "participants_hidden", False))
    offset, seen = 0, set()
    while True:
        page = await rpc.call(client, functions.channels.GetParticipantsRequest(
            channel, types.ChannelParticipantsSearch(""), offset, 200, 0), max_wait=0)
        fresh = {user.id for user in page.users} - seen
        seen.update(user.id for user in page.users)
        complete = len(seen) >= max(page.count, expected or 0) and (not hidden or expected is not None)
        yield page.users, complete
        if complete or not fresh or not page.participants:
            return
        offset += len(page.participants)


def display_user(user):
    username = f"@{html.escape(user.username)} " if user.username else ""
    name = " ".join(filter(None, [user.first_name, user.last_name])) or "未知昵称"
    return username + html.escape(name)


def group_title(entity):
    return getattr(entity, "title", None) or str(entity.id)


def telegram_id(entity):
    return -1000000000000 - entity.id if isinstance(entity, Channel) else -entity.id


class GroupOnboarding:
    """Durable new-group notifications and an admin-only group language picker."""

    def __init__(self, db, bot_client, api, bot_id):
        self.db, self.bot, self.api, self.bot_id = db, bot_client, api, bot_id
        self.locks = {}
        self.wake = asyncio.Event()
        self.language_changed = None
        db.execute("""CREATE TABLE IF NOT EXISTS group_onboarding (
            chat_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL,
            username TEXT, inviter_json TEXT NOT NULL DEFAULT '{}',
            event_at INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
            prompt_id INTEGER, retry_at INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS group_additions (
            chat_id INTEGER NOT NULL, generation INTEGER NOT NULL, title TEXT NOT NULL,
            username TEXT, inviter_json TEXT NOT NULL DEFAULT '{}', event_at INTEGER NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0, prompt_id INTEGER, retry_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id,generation)
        )""")
        db.commit()

    def state(self, chat_id):
        cursor = self.db.execute("SELECT * FROM group_onboarding WHERE chat_id=?", (chat_id,))
        row = cursor.fetchone()
        return dict(zip((column[0] for column in cursor.description), row)) if row else None

    def joined(self, chat_id, title, username=None, inviter=None, event_at=None):
        if chat_id >= 0:
            return
        event_at = int(event_at or time.time())
        previous = self.state(chat_id)
        if previous and event_at < previous['event_at']:
            return
        inviter = inviter or {}
        owner_id = inviter.get('id') if is_admin(self.db, inviter.get('id', 0)) else None
        with self.db:
            self.db.execute("""INSERT INTO groups(chat_id,title,enabled,owner_id) VALUES(?,?,1,?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                owner_id=COALESCE(groups.owner_id,excluded.owner_id)""", (chat_id, title, owner_id))
            if not previous or not previous['active']:
                generation = (previous['generation'] if previous else 0) + 1
                self.db.execute("UPDATE groups SET enabled=1 WHERE chat_id=?", (chat_id,))
                self.db.execute("""INSERT INTO group_onboarding(chat_id,generation,active,title,username,inviter_json,event_at)
                    VALUES(?,?,1,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                    generation=excluded.generation,active=1,title=excluded.title,username=excluded.username,
                    inviter_json=excluded.inviter_json,event_at=excluded.event_at,notified=0,prompt_id=NULL,retry_at=0""",
                    (chat_id, generation, title, username, json.dumps(inviter, ensure_ascii=False), event_at))
                self.db.execute('INSERT INTO group_additions(chat_id,generation,title,username,inviter_json,event_at) VALUES(?,?,?,?,?,?)',
                                (chat_id, generation, title, username, json.dumps(inviter, ensure_ascii=False), event_at))
                LOG.info('Bot joined group %s; checks enabled; onboarding generation %s', chat_id, generation)
            else:
                # Duplicate MTProto/Bot API events must not re-enable a manually disabled group.
                actor_json = json.dumps(inviter, ensure_ascii=False) if inviter.get('id') else previous['inviter_json']
                self.db.execute("UPDATE group_onboarding SET title=?,username=COALESCE(?,username),inviter_json=?,event_at=? WHERE chat_id=?",
                                (title, username, actor_json, event_at, chat_id))
                self.db.execute('UPDATE group_additions SET title=?,username=COALESCE(?,username),inviter_json=? WHERE chat_id=? AND generation=? AND notified=0',
                                (title, username, actor_json, chat_id, previous['generation']))
        self.wake.set()

    def left(self, chat_id, title='', event_at=None):
        if chat_id >= 0:
            return
        event_at = int(event_at or time.time())
        previous = self.state(chat_id)
        if previous and event_at < previous['event_at']:
            return
        with self.db:
            self.db.execute("""INSERT INTO group_onboarding(chat_id,active,title,event_at) VALUES(?,0,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET active=0,event_at=excluded.event_at""", (chat_id, title, event_at))

    def buttons(self, chat_id, generation):
        return [[Button.inline('简体中文', f'join_language:{chat_id}:{generation}:zh'.encode()),
                 Button.inline('English', f'join_language:{chat_id}:{generation}:en'.encode())]]

    def membership_update(self, change):
        chat = change.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup') or chat.get('id', 0) >= 0:
            return
        old, new = change.get('old_chat_member') or {}, change.get('new_chat_member') or {}
        if new.get('user', {}).get('id') != self.bot_id:
            return
        def present(member):
            return member.get('status') in ('member', 'administrator', 'creator') or (
                member.get('status') == 'restricted' and member.get('is_member', False))
        if not present(new):
            self.left(chat['id'], chat.get('title', ''), change.get('date'))
        elif not present(old):
            self.joined(chat['id'], chat.get('title') or str(chat['id']), chat.get('username'),
                        change.get('from'), change.get('date'))

    async def deliver(self, chat_id, generation=None):
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            current = self.state(chat_id)
            if generation is None:
                generation = current['generation'] if current else 0
            cursor = self.db.execute('SELECT * FROM group_additions WHERE chat_id=? AND generation=?', (chat_id, generation))
            values = cursor.fetchone()
            row = dict(zip((column[0] for column in cursor.description), values)) if values else None
            if not row or row['retry_at'] > int(time.time()):
                return
            active = bool(current and current['active'] and current['generation'] == generation)
            retry_after = 30
            details = {}
            try:
                details = await asyncio.wait_for(self.api('getChat', {'chat_id': chat_id}), 8)
            except (RuntimeError, OSError, asyncio.TimeoutError):
                LOG.warning('Could not fetch new group metadata in %s', chat_id)
            current = self.state(chat_id)
            active = bool(current and current['active'] and current['generation'] == generation)
            if active and row['prompt_id'] is None:
                try:
                    prompt = await asyncio.wait_for(self.bot.send_message(chat_id,
                        '欢迎使用 CheateChecker！已自动开启本群查询。\n请群主或管理员选择本群使用的语言。\n\n'
                        'Welcome to CheateChecker! Checks are enabled for this group.\n'
                        'Group owners and admins: choose the language for this group.',
                        buttons=self.buttons(chat_id, generation), parse_mode=None), 15)
                    with self.db:
                        self.db.execute('UPDATE group_onboarding SET prompt_id=? WHERE chat_id=? AND generation=? AND active=1',
                                        (prompt.id, chat_id, generation))
                        self.db.execute('UPDATE group_additions SET prompt_id=? WHERE chat_id=? AND generation=?',
                                        (prompt.id, chat_id, generation))
                    row['prompt_id'] = prompt.id
                except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                    retry_after = max(retry_after, getattr(exc, 'seconds', 0) + 1)
                    LOG.warning('New group language prompt deferred in %s: %s', chat_id, type(exc).__name__)
            owner = setting(self.db, 'superadmin')
            if not row['notified'] and owner:
                with language_context(user_language(self.db, int(owner)) or 'zh'):
                    username = details.get('username') or row['username']
                    url = f'https://t.me/{username}' if username and re.fullmatch(r'[A-Za-z0-9_]+', username) else details.get('invite_link')
                    if url and (urlparse(url).scheme != 'https' or urlparse(url).hostname not in ('t.me', 'telegram.me')):
                        url = None
                    internal = False
                    if not url and chat_id < -1000000000000 and row['prompt_id']:
                        url = f'https://t.me/c/{-chat_id - 1000000000000}/{row["prompt_id"]}'
                        internal = True
                    link_text = (tr('群内消息链接（需已加入该群）') if internal else url) if url else tr('暂无可用链接（私有群组）')
                    link = f'<a href="{html.escape(url, quote=True)}">{html.escape(str(link_text))}</a>' if url else html.escape(str(link_text))
                    inviter = json.loads(row['inviter_json'])
                    actor = display_user(SimpleNamespace(username=inviter.get('username'),
                        first_name=inviter.get('first_name'), last_name=inviter.get('last_name'))) if inviter.get('id') else tr('未知添加者')
                    if inviter.get('id'):
                        actor += f' (<code>{int(inviter["id"])}</code>)'
                    text = tr('🆕 Bot 已加入新群聊\n\n群组：<b>{0}</b>\nID：<code>{1}</code>\n链接：{2}\n添加者：{3}\n\n查询已自动启用。',
                              html.escape(details.get('title') or row['title']), chat_id, link, actor)
                    try:
                        await asyncio.wait_for(self.bot.send_message(int(owner), text, parse_mode='html',
                            link_preview=False, buttons=[[Button.url(tr('打开群聊'), url)]] if url else None), 15)
                        with self.db:
                            self.db.execute('UPDATE group_onboarding SET notified=1 WHERE chat_id=? AND generation=?', (chat_id, generation))
                            self.db.execute('UPDATE group_additions SET notified=1 WHERE chat_id=? AND generation=?', (chat_id, generation))
                        LOG.info('New group %s notification sent to superadmin', chat_id)
                    except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                        retry_after = max(retry_after, getattr(exc, 'seconds', 0) + 1)
                        LOG.warning('New group notification deferred in %s: %s', chat_id, type(exc).__name__)
            with self.db:
                self.db.execute('UPDATE group_onboarding SET retry_at=? WHERE chat_id=? AND generation=?',
                                (int(time.time()) + retry_after, chat_id, generation))
                self.db.execute('UPDATE group_additions SET retry_at=? WHERE chat_id=? AND generation=?',
                                (int(time.time()) + retry_after, chat_id, generation))

    async def choose_language(self, event):
        _, raw_id, raw_generation, language = event.data.decode().split(':')
        chat_id, generation = int(raw_id), int(raw_generation)
        row = self.state(chat_id)
        if language not in ('zh', 'en') or event.is_private or event.chat_id != chat_id or not row or not row['active'] or row['generation'] != generation or row['prompt_id'] != event.query.msg_id:
            await event.answer(tr('该语言选择消息已失效。'), alert=True)
            return
        try:
            membership = await asyncio.wait_for(self.api('getChatMember', {'chat_id': chat_id, 'user_id': event.sender_id}), 10)
            can_choose = membership.get('status') in ('creator', 'administrator')
        except (RuntimeError, OSError, asyncio.TimeoutError):
            try:
                permissions = await asyncio.wait_for(self.bot.get_permissions(chat_id, event.sender_id), 5)
                can_choose = bool(permissions and (permissions.is_admin or permissions.is_creator))
            except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError):
                can_choose = False
        # Bot roles never bypass the actual group-admin requirement.
        if not can_choose:
            await event.answer(tr('仅本群群主或管理员可以选择语言。'), alert=True)
            return
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            row = self.state(chat_id)
            if not row or not row['active'] or row['generation'] != generation or row['prompt_id'] != event.query.msg_id:
                await event.answer(tr('该语言选择消息已失效。'), alert=True)
                return
            with self.db:
                self.db.execute('UPDATE groups SET language=? WHERE chat_id=?', (language, chat_id))
            with language_context(language):
                await event.answer(tr('群组语言已更新'))
                active = self.db.execute('SELECT enabled FROM groups WHERE chat_id=?', (chat_id,)).fetchone()[0]
                template = ('✅ 本群查询已启用。\n当前语言：{0}\n\n群管理员可通过下方按钮修改语言。' if active
                            else '本群查询当前已关闭。\n当前语言：{0}\n\n群管理员可通过下方按钮修改语言。')
                try:
                    await event.edit(tr(template, 'English' if language == 'en' else '简体中文'),
                                     buttons=self.buttons(chat_id, generation), parse_mode=None)
                except errors.MessageNotModifiedError:
                    pass
            if self.language_changed:
                try:
                    actor = await event.get_sender()
                    await asyncio.wait_for(self.language_changed(chat_id, actor), 15)
                except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError):
                    LOG.warning('New group language menu refresh deferred in %s', chat_id)

    async def run(self):
        limit = asyncio.Semaphore(2)
        async def process(chat_id, generation):
            async with limit:
                try:
                    await self.deliver(chat_id, generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception('New group onboarding failed in %s', chat_id)
        while True:
            self.wake.clear()
            rows = self.db.execute('''SELECT a.chat_id,a.generation FROM group_additions a
                LEFT JOIN group_onboarding s ON s.chat_id=a.chat_id
                WHERE a.retry_at<=? AND (a.notified=0 OR (a.prompt_id IS NULL AND s.active=1 AND s.generation=a.generation))''',
                                   (int(time.time()),)).fetchall()
            await asyncio.gather(*(process(chat_id, generation) for chat_id, generation in rows))
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except asyncio.TimeoutError:
                pass


class BulkRPC:
    """Retry the same page after short waits, with account/method-specific cooldowns."""

    def __init__(self, on_wait=None, *, clock=time.monotonic, sleep=asyncio.sleep, settings=None):
        self.clock = clock
        self.sleep = sleep
        self.on_wait = on_wait
        self.cooldowns = {}
        self.limits = {}
        self.retries = 0
        self.settings = settings or RuntimeSettings.from_env()

    def remaining(self, client, method):
        return max(0, self.cooldowns.get((id(client), method), (0, None))[0] - self.clock())

    def with_observer(self, observer):
        view = BulkRPC(observer, clock=self.clock, sleep=self.sleep, settings=self.settings)
        view.cooldowns, view.limits = self.cooldowns, self.limits
        return view

    async def call(self, client, request, *, max_wait=35, timeout=15, total_timeout=45):
        # Include semaphore queueing, cooldowns and retries in the wall-clock limit.
        return await asyncio.wait_for(self._call(client, request, max_wait=max_wait,
                                                timeout=timeout), total_timeout)

    async def _call(self, client, request, *, max_wait, timeout):
        key = (id(client), type(request))
        concurrency = (self.settings.member_concurrency if isinstance(request, functions.channels.GetParticipantsRequest)
                       else self.settings.rpc_concurrency)
        limit = self.limits.setdefault(key, asyncio.Semaphore(concurrency))
        waited = 0
        async with limit:
            for attempt in range(4):
                until, previous_error = self.cooldowns.get(key, (0, None))
                remaining = max(0, until - self.clock())
                if remaining:
                    if waited + remaining > max_wait:
                        raise previous_error
                    await self.sleep(remaining)
                    waited += remaining
                try:
                    return await asyncio.wait_for(client(request), timeout=timeout)
                except errors.FloodWaitError as exc:
                    self.cooldowns[key] = (self.clock() + exc.seconds + 1, exc)
                    if self.on_wait:
                        self.on_wait(client, type(request).__name__, exc.seconds)
                    if attempt == 3 or waited + exc.seconds + 1 > max_wait:
                        raise
                    self.retries += 1
                except (errors.ServerError, errors.TimedOutError, OSError, asyncio.TimeoutError):
                    if attempt == 3:
                        raise
                    self.retries += 1
                    await self.sleep(min(2 ** attempt, 4))


def bulk_result_state(sources, direct_coverage, scanned_coverage, hits, *, available_only=False):
    """Results refer to checked sources; an entirely unread query remains unresolved."""
    missing = set(sources) - set(direct_coverage) - set(scanned_coverage)
    checked = set(direct_coverage) | set(scanned_coverage)
    return (len(hits) if hits or not missing or (available_only and checked) else None), missing


BULK_PREP_TIMEOUT = 120
BULK_ACCOUNT_PREP_TIMEOUT = 45
BULK_DIRECT_TIMEOUT = 300
BULK_SOURCE_TIMEOUT = 300
BULK_SOURCE_SINGLE_TIMEOUT = 15
BULK_JOB_TIMEOUT = 900


def bulk_eta(estimate, phase_remaining, job_remaining):
    if estimate is None:
        return None
    return max(0, int(min(estimate, phase_remaining, job_remaining)))


async def bulk_phase(awaitable, timeout, label):
    """Cancel a stalled phase; callers retain already collected evidence."""
    try:
        await asyncio.wait_for(awaitable, timeout)
        return True
    except asyncio.TimeoutError:
        LOG.warning("Bulk phase %s timed out after %ss", label, timeout)
        return False


def missing_bulk_members(client, members):
    missing = []
    for member in members:
        try:
            client.session.get_input_entity(member.id)
        except ValueError:
            if not getattr(member, "deleted", False) and getattr(member, "access_hash", None):
                missing.append(member)
    return missing


async def seed_bulk_group_users(bot_client, clients, members, rpc, context_message,
                                *, edit_lock=None, timeout=60):
    """One stable group reference batch is read by all connected accounts concurrently."""
    counts = {id(client): 0 for client in clients}
    lock = edit_lock or asyncio.Lock()

    async def prepare():
        chat = await asyncio.wait_for(bot_client.get_entity(context_message.chat_id), 15)
        peers = {}

        async def resolve_peer(client):
            try:
                try:
                    peer = client.session.get_input_entity(context_message.chat_id)
                except ValueError:
                    if not getattr(chat, "username", None):
                        return
                    response = await rpc.call(client, functions.contacts.ResolveUsernameRequest(chat.username), max_wait=5)
                    scoped_chat = next(entity for entity in response.chats
                                       if utils.get_peer_id(entity) == context_message.chat_id)
                    peer = utils.get_input_peer(scoped_chat)
                peers[id(client)] = peer
            except (ValueError, StopIteration, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                LOG.warning("Bulk account %s group peer unavailable: %s", clients.index(client) + 1, type(exc).__name__)

        await asyncio.gather(*(resolve_peer(client) for client in clients))
        missing_ids = set().union(*(set(m.id for m in missing_bulk_members(client, members))
                                    for client in clients))
        missing = [m for m in members if m.id in missing_ids]
        stalled_batches = 0
        for offset in range(0, len(missing), 20):
            batch = missing[offset:offset + 20]
            text = str(tr("正在准备全员检查…\n当前批次 {0}：", offset // 20 + 1))
            entities = []
            for index, member in enumerate(batch, 1):
                label = str(index)
                entities.append(types.InputMessageEntityMentionName(
                    len(text.encode("utf-16-le")) // 2, len(label), utils.get_input_user(member)))
                text += label + " "
            text = text.rstrip()
            before = sum(counts.values())
            # Hold the edit lock only while the connected accounts use this exact message.
            async with lock:
                edited = await asyncio.wait_for(bot_client.edit_message(
                    context_message.chat_id, context_message.id, text,
                    formatting_entities=entities, parse_mode=None), 15)
                mentioned = {entity.user_id for entity in edited.entities or []
                             if isinstance(entity, types.MessageEntityMentionName)}

                async def resolve_batch(client):
                    peer = peers.get(id(client))
                    if peer is None:
                        return
                    needed = {m.id for m in missing_bulk_members(client, batch)} & mentioned
                    if not needed:
                        return
                    try:
                        message_id = context_message.id
                        if isinstance(peer, types.InputPeerChat):
                            history = await rpc.call(client, functions.messages.GetHistoryRequest(
                                peer, 0, None, 0, 8, 0, 0, 0), max_wait=5)
                            message_id = next(message.id for message in history.messages
                                              if getattr(message, "message", "").strip() == text)
                        references = [InputUserFromMessage(peer, message_id, m.id)
                                      for m in batch if m.id in needed]
                        resolved = await rpc.call(client, functions.users.GetUsersRequest(references), max_wait=5)
                        counts[id(client)] += sum(isinstance(entity, types.User) and not entity.min
                                                  and not entity.deleted and bool(entity.access_hash)
                                                  for entity in resolved)
                        client.session.save()
                    except (ValueError, StopIteration, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                        LOG.warning("Bulk account %s group batch unavailable: %s", clients.index(client) + 1, type(exc).__name__)

                await asyncio.gather(*(resolve_batch(client) for client in clients))
            stalled_batches = stalled_batches + 1 if sum(counts.values()) == before else 0
            if stalled_batches >= 2:
                LOG.warning("Bulk group references made no progress for two batches; using other lookup paths")
                break

    try:
        await bulk_phase(prepare(), timeout, "group entity preparation")
    except (ValueError, StopIteration, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
        LOG.warning("Bulk group entity preparation incomplete: %s", type(exc).__name__)
    finally:
        try:
            async with lock:
                await asyncio.wait_for(context_message.edit(tr("用户信息准备完成，正在查询…"),
                                       parse_mode=None, formatting_entities=[]), 5)
        except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError):
            LOG.warning("Could not restore plain bulk progress message")
    return counts


async def seed_bulk_users(bot_client, clients, bot_me, members, rpc, context_message=None,
                          *, edit_lock=None, account_timeout=BULK_ACCOUNT_PREP_TIMEOUT):
    """Prepare account-scoped hashes concurrently with bounded, batched fallbacks."""
    counts = (await seed_bulk_group_users(bot_client, clients, members, rpc, context_message,
                                         edit_lock=edit_lock)
              if context_message is not None else {id(client): 0 for client in clients})

    async def prepare_account(client):
        missing = missing_bulk_members(client, members)
        if not missing:
            return
        index = clients.index(client) + 1
        LOG.info("Bulk account %s private preparation started: missing=%s", index, len(missing))
        start_message = None
        peer = None
        try:
            me = await asyncio.wait_for(client.get_me(), 10)
            try:
                peer = client.session.get_input_entity(bot_me.id)
            except ValueError:
                response = await rpc.call(client, functions.contacts.ResolveUsernameRequest(bot_me.username), max_wait=5)
                peer = utils.get_input_peer(next(user for user in response.users if user.id == bot_me.id))
            try:
                destination = await asyncio.wait_for(bot_client.get_input_entity(me.id), 10)
            except ValueError:
                start_message = await asyncio.wait_for(client.send_message(peer, "/start"), 10)
                await asyncio.sleep(1)
                destination = await asyncio.wait_for(bot_client.get_input_entity(me.id), 10)

            stalled_batches = 0
            for offset in range(0, len(missing), 20):
                batch = missing[offset:offset + 20]
                text = "全员检查数据 %s：" % time.monotonic_ns()
                entities = []
                for position, member in enumerate(batch, 1):
                    label = str(position)
                    entities.append(types.InputMessageEntityMentionName(
                        len(text.encode("utf-16-le")) // 2, len(label), utils.get_input_user(member)))
                    text += label + " "
                text = text.rstrip()
                sent = None
                try:
                    try:
                        sent = await asyncio.wait_for(bot_client.send_message(
                            destination, text, formatting_entities=entities, parse_mode=None), 10)
                    except errors.ChatWriteForbiddenError:
                        if start_message is not None:
                            raise
                        start_message = await asyncio.wait_for(client.send_message(peer, "/start"), 10)
                        await asyncio.sleep(1)
                        sent = await asyncio.wait_for(bot_client.send_message(
                            destination, text, formatting_entities=entities, parse_mode=None), 10)
                    received = None
                    for attempt in range(2):
                        history = await rpc.call(client, functions.messages.GetHistoryRequest(
                            peer, 0, None, 0, 8, 0, 0, 0), max_wait=5, timeout=10)
                        received = next((message for message in history.messages
                                         if getattr(message, "message", "").strip() == text), None)
                        if received is not None:
                            break
                        await asyncio.sleep(0.5)
                    if received is None:
                        raise ValueError("Private user reference was not received")
                    mentioned = {entity.user_id for entity in received.entities or []
                                 if isinstance(entity, types.MessageEntityMentionName)}
                    references = [InputUserFromMessage(peer, received.id, member.id)
                                  for member in batch if member.id in mentioned]
                    if len(references) != len(batch):
                        LOG.info("Bulk account %s private mentions omitted %s/%s; no individual retries",
                                 index, len(batch) - len(references), len(batch))
                    resolved = (await rpc.call(client, functions.users.GetUsersRequest(references), max_wait=5)
                                if references else [])
                    gained = sum(isinstance(entity, types.User) and not entity.min and not entity.deleted
                                 and bool(entity.access_hash) for entity in resolved)
                    counts[id(client)] += gained
                    client.session.save()
                    stalled_batches = stalled_batches + 1 if not gained else 0
                    if stalled_batches >= 2:
                        LOG.warning("Bulk account %s private preparation made no progress; stopping fallback", index)
                        break
                finally:
                    if sent is not None:
                        try:
                            await asyncio.wait_for(bot_client.delete_messages(destination, [sent.id], revoke=True), 3)
                        except (errors.RPCError, OSError, asyncio.TimeoutError):
                            LOG.warning("Could not remove temporary private bulk reference")
        except (ValueError, StopIteration, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
            LOG.warning("Bulk account %s private preparation incomplete: %s", index, type(exc).__name__)
        finally:
            if start_message is not None and peer is not None:
                try:
                    await asyncio.wait_for(client.delete_messages(peer, [start_message.id], revoke=True), 3)
                except (errors.RPCError, OSError, asyncio.TimeoutError):
                    pass
            LOG.info("Bulk account %s preparation finished: resolved=%s", index, counts[id(client)])

    await asyncio.gather(*(bulk_phase(prepare_account(client), account_timeout,
                                     "account%s private preparation" % (index + 1))
                           for index, client in enumerate(clients)))
    return counts


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ["BOT_TOKEN"]
    bootstrap = os.environ["BOOTSTRAP_CODE"]
    if len(bootstrap) < 12:
        raise ValueError("BOOTSTRAP_CODE must be at least 12 characters")
    proxy = proxy_config()
    db = db_connect()
    users = user_clients_from_config(proxy)
    user = users[0]
    bot = TelegramClient(os.getenv("BOT_SESSION", "data/bot"), user.api_id, user.api_hash,
                         proxy=proxy, flood_sleep_threshold=0)
    for index, client in enumerate(users, 1):
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(f"Mounted user session {index} is not authorized")
    await bot.start(bot_token=token)
    bot_me = await bot.get_me()
    LOG.info("Bot started as @%s; %d user accounts authorized", bot_me.username, len(users))
    account_rpc = BulkRPC()
    account_pool = AccountPool(users, account_rpc)
    source_ids = set()
    source_accounts = {}
    source_updated = 0.0
    source_unavailable = set()
    source_registry = frozenset()
    source_refresh_lock = asyncio.Lock()
    membership_cache = {}
    scan_lock = asyncio.Lock()
    index_lock = asyncio.Lock()
    auto_source_lock = asyncio.Lock()
    join_check_limit = asyncio.Semaphore(3)
    user_preparation_locks = {id(client): asyncio.Lock() for client in users}
    protection_locks = {}
    indexed_sources = None
    source_index_errors = False
    source_index_built = 0.0
    source_revision = 0
    common_cache = {}
    common_inflight = {}
    recent_user_messages = OrderedDict()
    username_cache = OrderedDict()
    pending = {}
    group_menu_cache = {}
    group_menu_inflight = set()

    async def refresh_sources(force=False):
        nonlocal source_ids, source_accounts, source_updated, indexed_sources, source_revision, source_unavailable, source_registry
        registry = (frozenset(submissions.filter_sources(set())), frozenset(exempt_source_ids(db)))
        if not force and registry == source_registry and time.monotonic() - source_updated < 900:
            return source_ids
        async with source_refresh_lock:
            registry = (frozenset(submissions.filter_sources(set())), frozenset(exempt_source_ids(db)))
            if not force and registry == source_registry and time.monotonic() - source_updated < 900:
                return source_ids
            configured = os.getenv("CHEAT_GROUP_IDS", "").strip()
            async def read_memberships(client):
                try:
                    async def read():
                        dialogs = {telegram_id(dialog.entity):dialog.entity async for dialog in client.iter_dialogs()
                                if isinstance(dialog.entity, (Channel, Chat))
                                and (not isinstance(dialog.entity, Channel) or dialog.entity.megagroup)
                                and not getattr(dialog.entity,'left',False)}
                        # A kicked group can linger in Telegram's dialog list. Verify the
                        # registered scopes with the fresh dialog entity's account hash.
                        return await submissions.readable_memberships(users.index(client),dialogs)
                    membership_cache[id(client)] = await asyncio.wait_for(read(), 45)
                except (errors.RPCError, OSError, asyncio.TimeoutError):
                    LOG.warning("Account %s source refresh failed; keeping previous coverage", users.index(client) + 1)
            await asyncio.gather(*(read_memberships(client) for client in users))
            if not membership_cache:
                raise ValueError("Source membership refresh failed for all accounts")
            memberships = {id(client): membership_cache.get(id(client), set()) for client in users}
            new_ids = ({int(part.strip()) for part in configured.split(",") if part.strip()}
                       if configured else set().union(*memberships.values()))
            registered = submissions.filter_sources(new_ids)
            readable = set().union(*memberships.values())
            unavailable = registered - readable
            new_ids = registered & readable
            submissions.reconcile_memberships(membership_cache)
            new_accounts = {key: ids & new_ids for key, ids in memberships.items()}
            sync_auto_scope(db, new_ids)
            if new_ids != source_ids or new_accounts != source_accounts or unavailable != source_unavailable:
                indexed_sources = None
                source_revision += 1
                common_cache.clear()
                if unavailable != source_unavailable:
                    LOG.warning("Registered sources temporarily unavailable: %s; readable source count=%s",
                                sorted(unavailable), len(new_ids))
            source_accounts, source_ids = new_accounts, new_ids
            source_unavailable, source_registry = unavailable, registry
            source_updated = time.monotonic()
            LOG.info("Loaded %d source groups; account coverage: %s", len(source_ids),
                     [len(source_accounts[id(client)]) for client in users])
            return source_ids

    submissions = GroupSubmissions(db, bot, users, refresh_sources)

    def apply_group_whitelist():
        nonlocal source_ids, source_accounts, source_unavailable, source_registry, indexed_sources, source_revision
        readable=set().union(*membership_cache.values()) if membership_cache else set()
        configured=os.getenv('CHEAT_GROUP_IDS','').strip()
        candidates={int(part.strip()) for part in configured.split(',') if part.strip()} if configured else readable
        registered=submissions.filter_sources(candidates)
        source_ids=registered & readable
        source_unavailable=registered-readable
        source_accounts={id(client):membership_cache.get(id(client),set()) & source_ids for client in users}
        source_registry=(frozenset(submissions.filter_sources(set())),frozenset(exempt_source_ids(db)))
        indexed_sources=None
        source_revision+=1
        common_cache.clear()
        sync_auto_scope(db,source_ids)

    def all_sources_exempt():
        readable=set().union(*membership_cache.values()) if membership_cache else set()
        configured=os.getenv('CHEAT_GROUP_IDS','').strip()
        candidates={int(part.strip()) for part in configured.split(',') if part.strip()} if configured else readable
        available=submissions.filter_sources(candidates,include_exempt=True) & readable
        return bool(available) and available <= exempt_source_ids(db)
    submissions.rpc = account_rpc
    source_sync = AccountGroupSync(db, users, account_rpc, submissions,
                                   DB_PATH.parent / "account_sync.json")

    async def secondary_group_sync_loop():
        while True:
            try:
                report = await source_sync.cycle()
                if report and (report["membership_changed"] or report["memberships_refreshed"]):
                    await refresh_sources(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Account group sync failed")
            await asyncio.sleep(60)

    async def build_source_index():
        nonlocal indexed_sources, source_index_errors, source_index_built
        await refresh_sources()
        if indexed_sources is not None and time.monotonic() - source_index_built < 3600:
            return indexed_sources
        async with index_lock:
            if indexed_sources is not None and time.monotonic() - source_index_built < 3600:
                return indexed_sources
            found = {}
            semaphore = asyncio.Semaphore(2)
            failures = 0

            async def scan_source(source_id):
                async with semaphore:
                    members, complete = {}, False
                    candidates = account_pool.rank(
                        [client for client in users if source_id in source_accounts.get(id(client), ())],
                        source_accounts, {source_id}, method=functions.channels.GetParticipantsRequest)
                    for client in candidates:
                        try:
                            entity = await client.get_entity(source_id)
                            title = group_title(entity)
                            async for page, complete in source_member_pages(client, source_id, account_rpc):
                                members.update({candidate.id: title for candidate in page})
                            if complete:
                                break
                        except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError):
                            LOG.warning("Account %s could not index source %s", users.index(client) + 1, source_id)
                    return source_id, members, not complete

            results = await asyncio.gather(*(scan_source(source_id) for source_id in source_ids))
            for source_id, members, failed in results:
                failures += int(failed)
                for user_id, title in members.items():
                    found.setdefault(user_id, {})[source_id] = title
            indexed_sources = found
            source_index_errors = bool(failures)
            source_index_built = time.monotonic()
            LOG.info("Indexed %d source users; %d source groups unavailable", len(found), failures)
            return found

    async def find_source_user(client, target_id, search_sources=None):
        """Find an account-scoped InputUser without indexing every source group."""
        await refresh_sources()
        if indexed_sources is not None and not source_index_errors and time.monotonic() - source_index_built < 3600:
            if target_id not in indexed_sources:
                return None
            try:
                return await client.get_input_entity(target_id)
            except (ValueError, errors.RPCError):
                pass

        found = asyncio.Event()
        semaphore = asyncio.Semaphore(3)

        async def scan_source(source_id):
            if found.is_set():
                return None, False
            async with semaphore:
                if found.is_set():
                    return None, False
                try:
                    complete = False
                    async for page, complete in source_member_pages(client, source_id, account_rpc):
                        if found.is_set():
                            return None, False
                        for candidate in page:
                            if candidate.id == target_id:
                                found.set()
                                return utils.get_input_user(candidate), False
                    return None, not complete
                except errors.FloodWaitError as exc:
                    LOG.warning("Source %s rate limited for %s seconds", source_id, exc.seconds)
                except (ValueError, errors.RPCError):
                    LOG.warning("Could not search source group %s", source_id)
                return None, True

        eligible=set(source_accounts.get(id(client), ()))
        if search_sources is not None:
            eligible.intersection_update(search_sources)
        results = await asyncio.gather(*(scan_source(source_id) for source_id in eligible))
        for resolved, _ in results:
            if resolved is not None:
                return resolved
        if any(failed for _, failed in results):
            raise ValueError("Incomplete source search for user without username")
        return None

    def remember_user_message(chat_id, user_id, message_id):
        key = (chat_id, user_id)
        recent_user_messages.pop(key, None)
        recent_user_messages[key] = message_id
        if len(recent_user_messages) > 20000:
            recent_user_messages.popitem(last=False)

    async def input_user_from_message(client, chat_id, message_id, target_id):
        try:
            peer = await client.get_input_entity(chat_id)
            message = await asyncio.wait_for(client.get_messages(peer, ids=message_id), timeout=5)
            if message is not None and message.sender_id == target_id:
                return InputUserFromMessage(peer, message_id, target_id)
        except (ValueError, errors.RPCError, asyncio.TimeoutError):
            pass
        return None

    async def input_user_from_history(client, chat_id, target_id):
        try:
            peer = await client.get_input_entity(chat_id)

            async def search():
                async for message in client.iter_messages(peer, limit=1200):
                    if message.sender_id == target_id:
                        return InputUserFromMessage(peer, message.id, target_id)
                return None

            return await asyncio.wait_for(search(), timeout=5)
        except (ValueError, errors.RPCError, asyncio.TimeoutError):
            return None

    async def input_user_from_private_reference(client, target):
        """Acquire an account-scoped hash through a private bot mention before scans."""
        if not getattr(target, "access_hash", None):
            return None
        async def prepare():
            async with user_preparation_locks[id(client)]:
                try:
                    return client.session.get_input_entity(target.id)
                except ValueError:
                    pass
                await seed_bulk_users(bot, [client], bot_me, [target], account_rpc, account_timeout=6)
                try:
                    return client.session.get_input_entity(target.id)
                except ValueError:
                    return None
        try:
            return await asyncio.wait_for(prepare(), 8)
        except (ValueError, TypeError, errors.RPCError, OSError, asyncio.TimeoutError):
            return None

    async def resolve_for_user_account(client, target, reference=None, context_chat_id=None,
                                       allow_source_scan=True, search_sources=None):
        try:
            return client.session.get_input_entity(target.id)
        except ValueError:
            pass
        if target.username:
            try:
                response = await account_rpc.call(client, functions.contacts.ResolveUsernameRequest(target.username), max_wait=0)
                entity = next((item for item in response.users if item.id == target.id), None)
                if entity is not None:
                    return utils.get_input_user(entity)
            except (ValueError, errors.RPCError):
                pass
        if reference is not None:
            resolved = await input_user_from_message(client, reference[0], reference[1], target.id)
            if resolved is not None:
                return resolved
        references = [(chat_id, message_id) for (chat_id, user_id), message_id
                      in reversed(recent_user_messages.items())
                      if user_id == target.id and (chat_id, message_id) != reference][:3]
        for chat_id, message_id in references:
            resolved = await input_user_from_message(client, chat_id, message_id, target.id)
            if resolved is not None:
                return resolved
        resolved = await input_user_from_private_reference(client, target)
        if resolved is not None:
            return resolved
        if context_chat_id is not None:
            resolved = await input_user_from_history(client, context_chat_id, target.id)
            if resolved is not None:
                return resolved
        if not allow_source_scan:
            if auto_source_lock.locked():
                raise ValueError("Automatic source lookup is busy")
            async with auto_source_lock:
                return await find_source_user(client, target.id, search_sources)
        return await find_source_user(client, target.id, search_sources)

    async def fetch_common_groups_resolved(client, resolved, sources, rpc=None):
        found = {}
        max_id = 0
        while True:
            request = GetCommonChatsRequest(user_id=resolved, max_id=max_id, limit=100)
            result = await (rpc or account_rpc).call(client, request, max_wait=0)
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
        return [{"id": group_id, "title": title} for group_id, title in sorted(found.items())]

    async def fetch_common_groups(client, target, sources, reference, context_chat_id, allow_source_scan):
        resolved = await resolve_for_user_account(
            client, target, reference, context_chat_id, allow_source_scan, sources)
        if resolved is None:
            # None is only returned after a complete source search found no match.
            return []
        return await fetch_common_groups_resolved(client, resolved, sources)

    async def common_groups(target, prefer_index=False, reference=None, context_chat_id=None,
                            allow_source_scan=True):
        sources = await refresh_sources()
        if not sources:
            if all_sources_exempt():
                return QueryGroups([])
            raise ValueError("No source groups available")
        key = (source_revision, target.id)
        # Each fresh query reaches every readable account. Only requests already
        # running for the same target/context share work; completed results and
        # source indexes must not bypass the account fanout.
        inflight_key = (key, reference, context_chat_id, allow_source_scan)
        task = common_inflight.get(inflight_key)
        if task is None:
            async def query_accounts():
                scopes = {id(client): set(source_accounts.get(id(client), ())) & sources for client in users}
                LOG.info("Common lookup %s fanout: %d/%d readable accounts", target.id,
                         sum(bool(scope) for scope in scopes.values()), len(users))
                async def lookup(client, needed_sources):
                    return await asyncio.wait_for(
                        fetch_common_groups(client, target, needed_sources, reference,
                                            context_chat_id, allow_source_scan),
                        timeout=20 if allow_source_scan else 10)
                groups, covered, failures = await account_pool.query(sources, scopes, lookup, target.id,scoped_lookup=True)
                complete = not (sources - covered)
                if not complete and not groups and not covered:
                    raise (failures[0] if failures else ValueError("Incomplete source coverage"))
                if not complete:
                    LOG.warning("Common lookup %s has partial coverage: %d/%d", target.id, len(covered), len(sources))
                return QueryGroups(groups, sources - covered), complete

            task = asyncio.create_task(query_accounts())
            common_inflight[inflight_key] = task
        try:
            groups, complete = await asyncio.shield(task)
            return groups
        finally:
            if common_inflight.get(inflight_key) is task and task.done():
                common_inflight.pop(inflight_key, None)

    def remember_group(chat_id, title, owner_id=None):
        db.execute("""INSERT INTO groups(chat_id,title,owner_id,enabled) VALUES(?,?,?,1)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
            owner_id=COALESCE(excluded.owner_id,groups.owner_id)""", (chat_id, title, owner_id))
        db.commit()

    def enabled(chat_id):
        row = db.execute("SELECT enabled FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row[0])

    def may_manage_group(actor_id, group_id):
        row = db.execute("SELECT owner_id FROM groups WHERE chat_id=?", (group_id,)).fetchone()
        return row is not None and (str(actor_id) == setting(db, "superadmin") or row[0] == actor_id)

    async def claim_group_owner(chat_id, actor_id):
        """Discover an unassigned group's owner without blocking public checks."""
        if actor_id is None or actor_id <= 0 or not is_admin(db, actor_id):
            return False
        row = db.execute("SELECT owner_id FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None or row[0] is not None:
            return False
        try:
            permissions = await asyncio.wait_for(bot.get_permissions(chat_id, actor_id), timeout=3)
        except (ValueError, errors.RPCError, asyncio.TimeoutError, OSError) as exc:
            LOG.warning("Group owner discovery skipped for %s in %s: %s", actor_id, chat_id, type(exc).__name__)
            return False
        if not permissions or not (permissions.is_admin or permissions.is_creator):
            return False
        cursor = db.execute("UPDATE groups SET owner_id=? WHERE chat_id=? AND owner_id IS NULL", (actor_id, chat_id))
        db.commit()
        return bool(cursor.rowcount)

    async def api(method, payload):
        return await asyncio.to_thread(bot_api_sync, token, method, payload)

    onboarding = GroupOnboarding(db, bot, api, bot_me.id)

    async def present(event, text, *, buttons=None, parse_mode=None):
        if isinstance(event, events.CallbackQuery.Event):
            return await event.edit(text, buttons=buttons, parse_mode=parse_mode)
        else:
            return await event.respond(text, buttons=buttons, parse_mode=parse_mode)

    async def expire_loop():
        while True:
            due = db.execute("SELECT chat_id,message_id FROM expiring_messages WHERE expires_at<=? LIMIT 100", (int(time.time()),)).fetchall()
            for chat_id, message_id in due:
                try:
                    await bot.delete_messages(chat_id, message_id)
                except (ValueError, errors.RPCError):
                    LOG.warning("Could not expire message %s in %s", message_id, chat_id)
                db.execute("DELETE FROM expiring_messages WHERE chat_id=? AND message_id=?", (chat_id, message_id))
            if due:
                db.commit()
            db.execute("DELETE FROM auto_recent WHERE expires_at<=?", (int(time.time()),))
            db.commit()
            await asyncio.sleep(10)

    def expire(message, request_message_id=None):
        expires_at = int(time.time()) + 180
        rows = [(message.chat_id, message.id, expires_at)]
        if request_message_id is not None and message.chat_id < 0 and request_message_id != message.id:
            rows.append((message.chat_id, request_message_id, expires_at))
        db.executemany("INSERT OR REPLACE INTO expiring_messages(chat_id,message_id,expires_at) VALUES(?,?,?)", rows)
        db.commit()

    async def group_reply(event, text, *, expire_request=False, **kwargs):
        message = await event.reply(text, **kwargs)
        expire(message, event.id if expire_request else None)
        return message

    async def group_admin(chat_id, actor_id):
        if str(actor_id) == setting(db, "superadmin") or (is_admin(db, actor_id) and may_manage_group(actor_id, chat_id)):
            return True
        try:
            permissions = await bot.get_permissions(chat_id, actor_id)
            return bool(permissions and (permissions.is_admin or permissions.is_creator))
        except (ValueError, errors.RPCError):
            return False

    async def bot_group_admin(chat_id):
        try:
            permissions = await bot.get_permissions(chat_id, bot_me.id)
            return bool(permissions and (permissions.is_admin or permissions.is_creator))
        except (ValueError, errors.RPCError):
            return False

    async def protect_superadmin(chat_id):
        superadmin = setting(db, "superadmin")
        if not superadmin or chat_id >= 0:
            return
        lock = protection_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            superadmin_id = int(superadmin)
            try:
                member = await api("getChatMember", {"chat_id": chat_id, "user_id": superadmin_id})
            except RuntimeError:
                return
            status = member.get("status")
            try:
                if status == "kicked" or (status == "restricted" and not member.get("is_member", True)):
                    await api("unbanChatMember", {
                        "chat_id": chat_id, "user_id": superadmin_id, "only_if_banned": True,
                    })
                    db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, superadmin_id))
                    db.commit()
                    LOG.warning("Removed ban on superadmin %s in group %s", superadmin_id, chat_id)
                elif status == "restricted":
                    await api("restrictChatMember", {
                        "chat_id": chat_id, "user_id": superadmin_id,
                        "permissions": UNRESTRICT_PERMISSIONS,
                        "use_independent_chat_permissions": True,
                    })
                    LOG.warning("Removed restrictions on superadmin %s in group %s", superadmin_id, chat_id)
            except RuntimeError as exc:
                LOG.error("Could not restore superadmin %s in group %s: %s", superadmin_id, chat_id, exc)

    async def protection_loop():
        while True:
            groups = db.execute("SELECT chat_id FROM groups WHERE chat_id<0").fetchall()
            for (chat_id,) in groups:
                try:
                    await protect_superadmin(chat_id)
                except Exception:
                    LOG.exception("Superadmin protection check failed in group %s", chat_id)
                await asyncio.sleep(0.2)
            await asyncio.sleep(60)

    async def member_update_loop():
        offset = None
        while True:
            payload = {"timeout": 5, "allowed_updates": ["chat_member", "my_chat_member"]}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = await api("getUpdates", payload)
            except RuntimeError as exc:
                LOG.warning("Could not receive chat member updates: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = max(offset or 0, update["update_id"] + 1)
                if update.get('my_chat_member'):
                    onboarding.membership_update(update['my_chat_member'])
                change = update.get("chat_member")
                if not change:
                    continue
                member = change.get("new_chat_member") or {}
                superadmin = setting(db, "superadmin")
                if not superadmin or member.get("user", {}).get("id") != int(superadmin):
                    continue
                chat_id = change.get("chat", {}).get("id")
                if chat_id is None or chat_id >= 0 or member.get("status") not in ("kicked", "restricted"):
                    continue
                LOG.info("Superadmin membership changed to %s in group %s", member["status"], chat_id)
                await protect_superadmin(chat_id)

    async def ban_protection_reason(chat_id, user_id):
        if user_id == bot_me.id or is_admin(db, user_id):
            return tr('Bot 管理员或超级管理员受到封禁保护。')
        try:
            member = await api("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        except RuntimeError:
            return tr('暂时无法核实目标的群组身份，请稍后重试。')
        if member.get("status") in ("administrator", "creator"):
            return tr('群组管理员受到封禁保护。')
        return None

    def whitelisted(user_id):
        return db.execute("SELECT 1 FROM whitelist WHERE user_id=?", (user_id,)).fetchone() is not None

    def auto_enabled(chat_id):
        row = db.execute("SELECT enabled FROM auto_groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row[0])

    async def resolve_target(raw, chat_id=None, use_cache=False):
        value = raw.strip().lstrip("@")
        if not value:
            raise ValueError("empty user")
        if value.isdecimal():
            user_id = int(value)
            if user_id <= 0:
                raise ValueError("invalid user ID")
            for client in users:
                try:
                    input_user = client.session.get_input_entity(user_id)
                    return await client.get_entity(input_user)
                except (ValueError, errors.RPCError):
                    pass
            try:
                return await bot.get_entity(user_id)
            except (ValueError, errors.RPCError):
                pass
            if chat_id is not None:
                try:
                    member = await api("getChatMember", {"chat_id": chat_id, "user_id": user_id})
                    return SimpleNamespace(id=user_id, username=member["user"].get("username"),
                                           first_name=member["user"].get("first_name"),
                                           last_name=member["user"].get("last_name"), bot=member["user"].get("is_bot", False))
                except RuntimeError:
                    pass
            for client in users:
                try:
                    return await client.get_entity(user_id)
                except (ValueError, errors.RPCError):
                    pass
            return SimpleNamespace(id=user_id, username=None, first_name=None, last_name=None, bot=False)
        key = value.casefold()
        cached = username_cache.get(key) if use_cache else None
        if cached is not None and time.monotonic() - cached[0] < 60:
            return cached[1]
        target = None
        for client in users:
            try:
                input_user = client.session.get_input_entity(value)
            except ValueError:
                continue
            else:
                try:
                    target = await client.get_entity(input_user)
                    break
                except (ValueError, errors.RPCError):
                    pass
        if target is None:
            try:
                target = await bot.get_entity(value)
            except (ValueError, errors.RPCError):
                for client in users:
                    try:
                        target = await client.get_entity(value)
                        break
                    except (ValueError, errors.RPCError):
                        pass
        if target is None:
            raise ValueError("target is not resolvable")
        if not isinstance(target, types.User):
            raise ValueError("target is not a user")
        if use_cache:
            username_cache[key] = (time.monotonic(), target)
            username_cache.move_to_end(key)
            if len(username_cache) > 4096:
                username_cache.popitem(last=False)
        return target

    def menu_buttons(actor_id):
        buttons = [[Button.inline(tr('🔎 查询用户'), b"menu:check")]]
        buttons.append([Button.inline(tr('📨 提交作弊群组'), b"menu:submit")])
        if is_admin(db, actor_id):
            buttons.append([Button.inline(tr('🗳️ 待审核申请'), b"menu:reviews"), Button.inline(tr('➕ 添加作弊群组'), b"menu:addgroup")])
            group_label = tr('📁 群聊管理') if str(actor_id) == setting(db, "superadmin") else tr('📁 我的群聊')
            buttons.append([Button.inline(group_label, b"menu:groups"), Button.inline(tr('📋 全员检查'), b"menu:all")])
            buttons.append([Button.inline(tr('🚫 已封禁用户'), b"menu:bans"), Button.inline(tr('🛡️ 已豁免用户'), b"menu:whitelist")])
            buttons.append([Button.inline(tr('🛡️ 群组白名单'), b"menu:groupwhitelist")])
        if str(actor_id) == setting(db, "superadmin"):
            buttons.append([Button.inline(tr('👥 管理管理员'), b"menu:admins"), Button.inline(tr('📝 查询日志'), b"menu:logs")])
        buttons.append([Button.inline("🌐 Language / 切换语言", b"menu:language")])
        return buttons

    @object_language(db)
    async def sync_menu(sender):
        commands = [types.BotCommand("start", tr('查看向导和身份')), types.BotCommand("check", tr('查询用户：/check 用户名或ID')),
                    types.BotCommand("submit", tr('提交作弊群组：/submit 群组username'))]
        if is_admin(db, sender.id):
            commands += [types.BotCommand("group", tr('管理自己添加的群聊')),
                         types.BotCommand("whitelist", tr('管理用户或群组白名单')),
                         types.BotCommand("ban", tr('在群内封禁用户')),
                         types.BotCommand("unban", tr('在群内解除封禁')),
                         types.BotCommand("auto", tr('在群内开启或关闭自动查询')),
                         types.BotCommand("add", tr('添加作弊群组：/add group 群组username'))]
        if str(sender.id) == setting(db, "superadmin"):
            commands[-1] = types.BotCommand("add", tr('添加群组：/add group；授权管理员：/add 用户ID'))
            commands += [types.BotCommand("revoke", tr('撤销管理员：/revoke 用户ID')),
                         types.BotCommand("admins", tr('管理管理员')), types.BotCommand("logs", tr('查看查询日志'))]
        try:
            peer = await bot.get_input_entity(sender)
            await bot(functions.bots.SetBotCommandsRequest(types.BotCommandScopePeer(peer), "", commands))
        except (ValueError, errors.RPCError):
            LOG.exception("Could not update command menu for %s", sender.id)

    @object_language(db, 1)
    async def sync_group_menu(chat_id, member):
        commands = [types.BotCommand("check", tr('查询用户'))]
        privileged = is_admin(db, member.id) and may_manage_group(member.id, chat_id)
        can_moderate = await group_admin(chat_id, member.id)
        key = (chat_id, member.id)
        if group_menu_cache.get(key) == (privileged, can_moderate, CURRENT_LANGUAGE.get()):
            return
        if can_moderate:
            commands += [types.BotCommand("ban", tr('封禁用户')), types.BotCommand("unban", tr("解除封禁用户")),
                         types.BotCommand("auto", tr('自动查询开关'))]
        if privileged:
            commands[0] = types.BotCommand("check", tr('查询用户；all 检查全员'))
            commands.append(types.BotCommand("whitelist", tr('管理用户或群组白名单')))
        try:
            peer = await bot.get_input_entity(chat_id)
            user_peer = utils.get_input_user(member)
            await bot(functions.bots.SetBotCommandsRequest(
                types.BotCommandScopePeerUser(peer, user_peer), "", commands,
            ))
            group_menu_cache[key] = (privileged, can_moderate, CURRENT_LANGUAGE.get())
        except (ValueError, errors.RPCError):
            LOG.exception("Could not update group command menu for %s in %s", member.id, chat_id)

    async def sync_group_menu_background(chat_id, member):
        key = (chat_id, member.id)
        if key in group_menu_inflight:
            return
        group_menu_inflight.add(key)
        try:
            await asyncio.wait_for(sync_group_menu(chat_id, member), timeout=10)
        except asyncio.TimeoutError:
            LOG.warning("Group command menu update timed out for %s in %s", member.id, chat_id)
        except Exception:
            LOG.exception("Group command menu update failed for %s in %s", member.id, chat_id)
        finally:
            group_menu_inflight.discard(key)

    async def welcome(event, sender):
        await sync_menu(sender)
        message = (
            tr('欢迎使用 CheateChecker！\n\n本项目旨在反对作弊，维护公平的群聊环境。\n请引用群内用户的消息发送 /check，或发送 /check 用户名/用户ID。\n查询结果中的按钮可查看命中的共同群组。\n发现新的作弊群组，可在私聊点击提交按钮或发送 /submit 群组username，等待管理员审核。\n')
        )
        if is_admin(db, sender.id):
            message += tr('\n管理员可管理群聊、白名单和封禁记录；在群内发送 /check all 检查全部成员。\n')
            message += tr('可审核新群组申请，或发送 /add group 群组username 直接添加。\n')
        if str(sender.id) == setting(db, "superadmin"):
            message += tr('超级管理员还可管理授权并查看全部查询日志。\n')
        message += tr('\n您当前身份为:{0}' ,role(db, sender.id))
        await present(event, message, buttons=menu_buttons(sender.id))

    async def choose_language(event):
        pending.pop(event.sender_id, None)
        await present(event, "请选择语言 / Choose your language:", buttons=[[
            Button.inline("简体中文", b"language:zh"), Button.inline("English", b"language:en")]])

    @bot.on(events.CallbackQuery(pattern=rb"^language:(?:zh|en)$"))
    @language_handler(db)
    async def on_language(event):
        if not event.is_private or event.chat_id != event.sender_id:
            await event.answer("Please choose your language in the bot's private chat.", alert=True)
            return
        language = event.data.decode().split(":")[1]
        with db:
            db.execute("INSERT INTO user_languages(user_id,language) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
                       (event.sender_id, language))
            db.execute("DELETE FROM submission_messages WHERE recipient_id=? AND message_id=?", (event.sender_id, event.query.msg_id))
        pending.pop(event.sender_id, None)
        group_menu_cache.clear()
        with language_context(language):
            await event.answer("Language set to English" if language == "en" else "已切换为简体中文")
            sender = await event.get_sender()
            await welcome(event, sender)
            # Refresh this user's group command menus as well as their private menu.
            for (group_id,) in db.execute("SELECT chat_id FROM groups WHERE owner_id=?", (event.sender_id,)).fetchall():
                await sync_group_menu(group_id, sender)

    async def submit_group(event, sender, raw, direct=False, menu_message_id=None):
        if not event.is_private:
            return
        if direct and not is_admin(db, sender.id):
            await event.respond(tr('需要管理员权限。'))
            return
        back = [[Button.inline(tr('返回主页'), b"menu:home")]]

        async def respond(text):
            if menu_message_id:
                try:
                    return await bot.edit_message(event.chat_id, menu_message_id, text, buttons=back, parse_mode=None)
                except (ValueError, errors.RPCError):
                    pass
            return await event.respond(text, buttons=back, parse_mode=None)

        try:
            username = group_username(raw)
            group = await asyncio.wait_for(bot.get_entity(username), timeout=15)
            if not isinstance(group, Channel) or not group.megagroup:
                raise ValueError(tr('请提交公开群聊的username；用户账号和广播频道不支持提交。'))
            if telegram_id(group) in await refresh_sources():
                raise ValueError(tr('该群组已在检测范围内。'))
            request_id = submissions.create(group, username, sender, approved=direct)
        except (ValueError, PermissionError) as exc:
            await respond(str(exc))
            return
        except (errors.RPCError, asyncio.TimeoutError):
            await respond(tr('暂未读取到该群组，请确认username后稍后重试。'))
            return
        if direct:
            await respond(tr('✅ 已添加群组 @{0}（申请 #{1}）。\n所有协议号将自动尝试入群；遇到群主审批或账号限速会等待重试。' ,username, request_id))
        else:
            await respond(tr('📨 群组 @{0} 已提交（申请 #{1}）。\n管理员和超级管理员会收到审核申请；审核通过后所有协议号自动入群。' ,username, request_id))

    async def show_reviews(event, page=0):
        rows = db.execute("SELECT id,title FROM source_submissions WHERE status='pending' ORDER BY id LIMIT 11 OFFSET ?", (page * 10,)).fetchall()
        buttons = [[Button.inline(f"#{request_id} {title[:35]}", f"review_item:{request_id}".encode())]
                   for request_id, title in rows[:10]]
        navigation = []
        if page:
            navigation.append(Button.inline(tr('上一页'), f"reviews:{page-1}".encode()))
        if len(rows) > 10:
            navigation.append(Button.inline(tr('下一页'), f"reviews:{page+1}".encode()))
        if navigation:
            buttons.append(navigation)
        buttons.append([Button.inline(tr('返回主页'), b"menu:home")])
        await present(event, tr('待审核作弊群组申请：') if rows else tr('暂无待审核申请。'), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"^reviews:\d+$"))
    @language_handler(db)
    async def on_reviews_page(event):
        if not event.is_private or not is_admin(db, event.sender_id):
            await event.answer(tr('需要管理员权限'), alert=True)
            return
        await event.answer()
        await show_reviews(event, int(event.data.split(b":")[1]))

    @bot.on(events.CallbackQuery(pattern=rb"^review_item:\d+$"))
    @language_handler(db)
    async def on_review_item(event):
        if not event.is_private or not is_admin(db, event.sender_id):
            await event.answer(tr('需要管理员权限'), alert=True)
            return
        request = submissions.request(int(event.data.split(b":")[1]))
        if request is None:
            await event.answer(tr('申请不存在'), alert=True)
            return
        await event.answer()
        buttons = (submissions.review_buttons(request) or []) + [[Button.inline(tr('返回申请列表'), b"menu:reviews")]]
        await event.edit(submissions.review_text(request), buttons=buttons, parse_mode=None)
        submissions.bind_message(request["id"], event.sender_id, event.query.msg_id)

    @bot.on(events.CallbackQuery(pattern=rb"^review:(?:approved|rejected):\d+$"))
    @language_handler(db)
    async def on_review(event):
        _, decision, raw_id = event.data.decode().split(":")
        request_id = int(raw_id)
        if not event.is_private or not submissions.can_review_message(request_id, event.sender_id, event.query.msg_id):
            await event.answer(tr('需要管理员权限，且只能操作自己的审核消息'), alert=True)
            return
        reviewer = await event.get_sender()
        try:
            changed = submissions.decide(request_id, reviewer, decision)
        except PermissionError:
            await event.answer(tr('您的管理员权限已撤销'), alert=True)
            return
        await event.answer(tr('审核完成') if changed else tr('该申请已由其他管理员处理'))
        request = submissions.request(request_id)
        try:
            await event.edit(submissions.review_text(request), buttons=None, parse_mode=None)
        except errors.MessageNotModifiedError:
            pass
        with db:
            db.execute("UPDATE submission_messages SET finalized=1 WHERE recipient_id=? AND message_id=? AND request_id=?",
                       (event.sender_id, event.query.msg_id, request_id))

    @bot.on(events.ChatAction)
    @language_handler(db)
    async def on_chat_action(event):
        if event.is_group and event.chat_id:
            bot_involved = bot_me.id in event.user_ids
            event_at = int(event.action_message.date.timestamp()) if event.action_message and event.action_message.date else None
            if bot_involved and (event.user_kicked or event.user_left):
                onboarding.left(event.chat_id, event_at=event_at)
                return
            if event.user_kicked and setting(db, "superadmin") in {str(user_id) for user_id in event.user_ids}:
                await protect_superadmin(event.chat_id)
            chat = await event.get_chat()
            if chat:
                owner_id = None
                actor = None
                if event.user_added and bot_me.id in event.user_ids:
                    try:
                        actor = await asyncio.wait_for(event.get_added_by(), 10)
                    except (ValueError, errors.RPCError, asyncio.TimeoutError):
                        actor = None
                    if actor and is_admin(db, actor.id):
                        owner_id = actor.id
                remember_group(event.chat_id, group_title(chat), owner_id)
                if bot_involved and (event.user_added or event.user_joined or event.created):
                    inviter = ({'id': actor.id, 'username': getattr(actor, 'username', None),
                                'first_name': getattr(actor, 'first_name', None),
                                'last_name': getattr(actor, 'last_name', None)} if actor else None)
                    onboarding.joined(event.chat_id, group_title(chat), getattr(chat, 'username', None), inviter, event_at)
                if owner_id:
                    await sync_group_menu(event.chat_id, actor)
            if not (event.user_joined or event.user_added) or not enabled(event.chat_id) or not auto_enabled(event.chat_id):
                return
            try:
                known_users = {joined.id: joined for joined in await event.get_users()
                               if isinstance(joined, types.User)}
            except (ValueError, errors.RPCError):
                known_users = {}
            join_message_id = event.action_message.id if event.action_message else None

            async def check_joined(user_id):
                if user_id <= 0 or user_id == bot_me.id:
                    return
                async with join_check_limit:
                    target = known_users.get(user_id)
                    if target is None:
                        try:
                            target = await resolve_target(str(user_id), event.chat_id)
                        except (ValueError, errors.RPCError, RuntimeError):
                            LOG.exception("Could not resolve new member %s in %s", user_id, event.chat_id)
                            return
                    try:
                        reference = ((event.chat_id, join_message_id)
                                     if event.action_message and event.action_message.sender_id == user_id else None)
                        await auto_check(event.chat_id, target, join_message_id, "群组auto入群",
                                         reference=reference)
                    except (ValueError, errors.RPCError, RuntimeError):
                        LOG.exception("Could not auto-check new member %s in %s", user_id, event.chat_id)

            await asyncio.gather(*(check_joined(user_id) for user_id in dict.fromkeys(event.user_ids)))

    def group_buttons(actor_id, page=0):
        page = max(0, page)
        if str(actor_id) == setting(db, "superadmin"):
            rows = db.execute("SELECT chat_id,title,enabled FROM groups ORDER BY title LIMIT 21 OFFSET ?", (page * 20,)).fetchall()
        else:
            rows = db.execute("SELECT chat_id,title,enabled FROM groups WHERE owner_id=? ORDER BY title LIMIT 21 OFFSET ?", (actor_id, page * 20)).fetchall()
        buttons = [[Button.inline(f"{'✅' if on else '⬜'} {title[:30]}", f"toggle:{gid}:{page}".encode()),
                    Button.inline("🌐 English" if group_language(db, gid) == "en" else "🌐 简体中文", f"group_language:{gid}:{page}".encode())]
                   for gid, title, on in rows[:20]]
        pages = []
        if page:
            pages.append(Button.inline(tr('上一页'), f"groups:{page-1}".encode()))
        if len(rows) > 20:
            pages.append(Button.inline(tr('下一页'), f"groups:{page+1}".encode()))
        if pages:
            buttons.append(pages)
        buttons.append([Button.inline(tr('返回主页'), b"menu:home")])
        return buttons

    async def show_groups(event, actor_id, page=0):
        buttons = group_buttons(actor_id, page)
        if len(buttons) == 1:
            await present(event, tr('尚无您可管理的群聊。请将 Bot 添加到您的群，并发送 /check 让 Bot 记录该群。'),
                          buttons=[[Button.inline(tr('返回主页'), b"menu:home")]])
            return
        await present(event, tr('选择允许查询的群聊：'), buttons=buttons)

    async def sync_group_language(chat_id, actor):
        members = {member_id for (group_id, member_id) in group_menu_cache if group_id == chat_id}
        members.add(actor.id)
        owner = setting(db, "superadmin")
        if owner:
            members.add(int(owner))
        try:
            peer = await bot.get_input_entity(chat_id)
            with language_context(group_language(db, chat_id)):
                for code in ("", "en"):
                    await bot(functions.bots.SetBotCommandsRequest(types.BotCommandScopePeer(peer), code,
                        [types.BotCommand("check", tr("查询群成员或用户ID"))]))
            for member_id in members:
                try:
                    member = actor if member_id == actor.id else await bot.get_entity(member_id)
                    await sync_group_menu(chat_id, member)
                except (ValueError, errors.RPCError):
                    continue
        except (ValueError, errors.RPCError):
            LOG.warning("Group language menu update deferred in %s", chat_id)

    onboarding.language_changed = sync_group_language

    @bot.on(events.CallbackQuery(pattern=rb"^join_language:-\d+:\d+:(?:zh|en)$"))
    @language_handler(db)
    async def on_join_group_language(event):
        await onboarding.choose_language(event)

    @bot.on(events.CallbackQuery(pattern=rb"^group_language:-?\d+:\d+$"))
    @language_handler(db)
    async def on_group_language(event):
        _, raw_id, raw_page = event.data.decode().split(":")
        group_id, page = int(raw_id), int(raw_page)
        if not event.is_private or not is_admin(db, event.sender_id) or not may_manage_group(event.sender_id, group_id):
            await event.answer(tr("无权管理该群聊"), alert=True)
            return
        title = db.execute("SELECT title FROM groups WHERE chat_id=?", (group_id,)).fetchone()[0]
        await event.answer()
        await event.edit(tr("群组：{0}\n当前语言：{1}\n\n请选择群内消息使用的语言：", title,
            "English" if group_language(db, group_id) == "en" else "简体中文"), buttons=[[
                Button.inline("简体中文", f"set_group_language:{group_id}:zh:{page}".encode()),
                Button.inline("English", f"set_group_language:{group_id}:en:{page}".encode())],
                [Button.inline(tr("返回群组管理"), f"groups:{page}".encode())]], parse_mode=None)

    @bot.on(events.CallbackQuery(pattern=rb"^set_group_language:-?\d+:(?:zh|en):\d+$"))
    @language_handler(db)
    async def on_set_group_language(event):
        _, raw_id, language, raw_page = event.data.decode().split(":")
        group_id = int(raw_id)
        if not event.is_private or not is_admin(db, event.sender_id) or not may_manage_group(event.sender_id, group_id):
            await event.answer(tr("无权管理该群聊"), alert=True)
            return
        with db:
            db.execute("UPDATE groups SET language=? WHERE chat_id=?", (language, group_id))
        await event.answer(tr("群组语言已更新"))
        await show_groups(event, event.sender_id, int(raw_page))
        actor = await event.get_sender()
        asyncio.create_task(sync_group_language(group_id, actor))

    async def show_admins(event):
        rows = db.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()
        buttons = [[Button.inline(tr('撤销 {0}' ,user_id), f"revoke:{user_id}".encode())] for (user_id,) in rows[:50]]
        buttons.append([Button.inline(tr('➕ 添加管理员'), b"menu:add")])
        buttons.append([Button.inline(tr('返回主页'), b"menu:home")])
        text = tr('超级管理员：') + str(setting(db, "superadmin")) + tr('\n管理员：') + (", ".join(str(row[0]) for row in rows) or tr('无'))
        await present(event, text, buttons=buttons)

    async def show_logs(event, page=0):
        page = max(0, page)
        rows = db.execute("SELECT created_at,requester_id,target_id,method,matches,status FROM queries ORDER BY id DESC LIMIT 15 OFFSET ?", (page * 15,)).fetchall()
        lines = [f"{date} | {requester} → {target} | {tr(method)} | {tr(status)} {matches if matches is not None else '-'}"
                 for date, requester, target, method, matches, status in rows]
        buttons = []
        if page:
            buttons.append(Button.inline(tr('上一页'), f"logs:{page-1}".encode()))
        if len(rows) == 15:
            buttons.append(Button.inline(tr('下一页'), f"logs:{page+1}".encode()))
        await present(event, tr('查询日志（最新在前）：\n<pre>') + html.escape("\n".join(lines) or tr('暂无记录')) + "</pre>",
                      parse_mode="html", buttons=([buttons] if buttons else []) + [[Button.inline(tr('返回主页'), b"menu:home")]])

    async def show_bans(event, actor_id, page=0):
        if str(actor_id) == setting(db, "superadmin"):
            rows = db.execute("SELECT chat_id,user_id,label FROM bans ORDER BY created_at DESC LIMIT 21 OFFSET ?", (page * 20,)).fetchall()
        else:
            rows = db.execute("""SELECT b.chat_id,b.user_id,b.label FROM bans b JOIN groups g ON g.chat_id=b.chat_id
                WHERE g.owner_id=? ORDER BY b.created_at DESC LIMIT 21 OFFSET ?""", (actor_id, page * 20)).fetchall()
        lines = [f"{chat_id} | {user_id} | {tr(label) if label == '由查询结果封禁' else label}" for chat_id, user_id, label in rows[:20]]
        pages = []
        if page:
            pages.append(Button.inline(tr('上一页'), f"bans:{page-1}".encode()))
        if len(rows) > 20:
            pages.append(Button.inline(tr('下一页'), f"bans:{page+1}".encode()))
        await present(event, tr('已封禁用户：\n<pre>') + html.escape("\n".join(lines) or tr('暂无记录')) + "</pre>",
                      parse_mode="html", buttons=([pages] if pages else []) + [[Button.inline(tr('返回主页'), b"menu:home")]])

    async def show_whitelist(event, page=0):
        rows = db.execute("SELECT user_id,label FROM whitelist ORDER BY created_at DESC LIMIT 21 OFFSET ?", (page * 20,)).fetchall()
        lines = [f"{user_id} | {label}" for user_id, label in rows[:20]]
        pages = []
        if page:
            pages.append(Button.inline(tr('上一页'), f"whitelist:{page-1}".encode()))
        if len(rows) > 20:
            pages.append(Button.inline(tr('下一页'), f"whitelist:{page+1}".encode()))
        pages.extend([Button.inline(tr("添加"), b"menu:whiteadd"), Button.inline(tr("移除"), b"menu:whiteremove")])
        await present(event, tr('已豁免用户：\n<pre>') + html.escape("\n".join(lines) or tr('暂无记录')) + "</pre>",
                      parse_mode="html", buttons=[pages, [Button.inline(tr('返回主页'), b"menu:home")]])

    async def show_group_whitelist(event, page=0):
        actor_id=event.sender_id
        rows=submissions.group_whitelist.rows(actor_id,str(actor_id)==setting(db,'superadmin'),page)
        lines=[f"{index}. {html.escape(title[:60])} (<code>{gid}</code>)" +
               (f" @{html.escape(username)}" if username else '')
               for index,(gid,title,username) in enumerate(rows[:20],page*20+1)]
        buttons=[[Button.inline(tr('移除：{0}',title[:25]),f'groupwhite:remove:{gid}:{page}'.encode())]
                 for gid,title,_ in rows[:20]]
        navigation=[]
        if page:navigation.append(Button.inline(tr('上一页'),f'groupwhite:page:{page-1}'.encode()))
        if len(rows)>20:navigation.append(Button.inline(tr('下一页'),f'groupwhite:page:{page+1}'.encode()))
        if navigation:buttons.append(navigation)
        buttons.append([Button.inline(tr('添加'),b'menu:groupwhiteadd'),Button.inline(tr('移除'),b'menu:groupwhiteremove')])
        buttons.append([Button.inline(tr('返回主页'),b'menu:home')])
        await present(event,tr('群组白名单（这些群组不计入作弊查询）：\n')+
                      ('\n'.join(lines) or tr('暂无记录')),parse_mode='html',buttons=buttons)

    async def publish_check(chat_id, requester_id, target, method, reply_to=None, positive_only=False,
                            reference=None, context_chat_id=None, progress_message=None):
        is_group = chat_id < 0
        request_message_id = reply_to if method == "群组check指令" else None
        LOG.info("Check requested in %s by %s for %s (%s)", chat_id, requester_id, target.id, method)
        if whitelisted(target.id):
            result = tr('用户: {0}\nID: <code>{1}</code>\n\n该用户为白名单用户，豁免查询' ,display_user(target), target.id)
            log_query(db, requester_id, target.id, method, chat_id, None, "白名单")
            if positive_only:
                return False
            if progress_message is not None:
                message = await progress_message.edit(result, parse_mode="html")
            else:
                message = await bot.send_message(chat_id, result, parse_mode="html", reply_to=reply_to)
            if is_group:
                expire(message, request_message_id)
            return False
        progress = progress_message
        if not positive_only and progress is None:
            progress = await bot.send_message(chat_id, tr('正在查询，请稍候…'), reply_to=reply_to)
        try:
            groups = await common_groups(target, reference=reference, context_chat_id=context_chat_id,
                                         allow_source_scan=not positive_only)
        except errors.FloodWaitError as exc:
            log_query(db, requester_id, target.id, method, chat_id, None, "限速")
            if not positive_only:
                message = await progress.edit(tr('查询限速，请在 {0} 秒后重试。' ,exc.seconds))
                if is_group:
                    expire(message, request_message_id)
            return None
        except (errors.RPCError, ValueError, OSError, asyncio.TimeoutError) as exc:
            LOG.exception("Common group lookup failed")
            log_query(db, requester_id, target.id, method, chat_id, None, "失败",
                      result_text=f"{type(exc).__name__}: {str(exc)[:200]}")
            if not positive_only:
                message = await progress.edit(tr('查询失败，请稍后重试。'))
                if is_group:
                    expire(message, request_message_id)
            return None
        groups=QueryGroups(filter_check_groups(db,groups),set(getattr(groups,'missing_sources',()))-exempt_source_ids(db))
        if groups:
            if is_group:
                CURRENT_LANGUAGE.set(group_language(db, chat_id))
            result = tr('⚠️ 检测到作弊用户！\n\n用户: {0}\nID: <code>{1}</code>\n作弊群组数量: {2}' ,display_user(target), target.id, len(groups))
        else:
            if is_group:
                CURRENT_LANGUAGE.set(group_language(db, chat_id))
            result = tr('✅ 用户检查完成\n\n用户: {0}\nID: <code>{1}</code>\n\n未发现该用户在作弊群组中' ,display_user(target), target.id)
        query_id = log_query(db, requester_id, target.id, method, chat_id, len(groups),
                             "完成", groups, result.render("zh"))
        if positive_only and not groups:
            return False
        buttons = None
        if groups:
            buttons = [[Button.inline(tr('查看详情群组'), f"detail:{query_id}:0".encode())]]
            if is_group and await bot_group_admin(chat_id):
                buttons.append([Button.inline(tr('封禁用户（仅管理员）'), f"ban:{query_id}".encode())])
        if progress is not None:
            message = await progress.edit(result, parse_mode="html", buttons=buttons)
        else:
            message = await bot.send_message(chat_id, result, parse_mode="html", buttons=buttons, reply_to=reply_to)
        if is_group:
            expire(message, request_message_id)
        return bool(groups)

    async def target_from_event(event, raw, use_cache=False):
        if raw:
            return await resolve_target(raw, event.chat_id if event.is_group else None, use_cache=use_cache)
        reply = await event.get_reply_message()
        return await reply.get_sender() if reply else None

    async def change_group_whitelist(event, actor, args, menu_message_id=None):
        if not is_admin(db,actor.id):
            await event.reply(tr('需要管理员权限。'))
            return
        raw=re.sub(r'^group(?:\s+|\+|$)','',args,flags=re.I).strip()
        action='add'
        pieces=raw.split(maxsplit=1)
        if pieces and pieces[0].lower() in ('add','remove'):
            action=pieces[0].lower();raw=pieces[1] if len(pieces)>1 else ''
        async def respond(text):
            if event.is_group:return await group_reply(event,text)
            if menu_message_id:
                return await bot.edit_message(event.chat_id,menu_message_id,text,parse_mode=None,
                    buttons=[[Button.inline(tr('返回群组白名单'),b'menu:groupwhitelist')]])
            return await event.respond(text,parse_mode=None)
        if not raw:
            if action=='add' and event.is_private:
                await show_group_whitelist(event)
            else:await respond(tr('格式：/whitelist group 群组username；移除：/whitelist group remove 群组username。'))
            return
        try:
            entity=await resolve_whitelist_group(raw,[bot,*users])
            if not is_admin(db,actor.id):
                await respond(tr('需要管理员权限。'))
                return
            gid=telegram_id(entity)
            if action=='add':
                changed=submissions.group_whitelist.add(entity,actor.id)
                message=tr('已添加群组白名单：{0}（{1}）。\n该群组不计入所有账号的作弊查询。',entity.title,gid) if changed else tr('该群组已在白名单中。')
            else:
                changed=submissions.group_whitelist.remove(gid,actor.id,str(actor.id)==setting(db,'superadmin'))
                message=tr('已移除群组白名单：{0}（{1}）。\n该群组恢复参与当前可查询范围。',entity.title,gid) if changed else tr('该群组不在白名单中。')
            if changed:apply_group_whitelist()
        except PermissionError:
            message=tr('仅可移除自己添加的群组白名单；超级管理员可管理全部记录。')
        except (ValueError,errors.RPCError,asyncio.TimeoutError):
            message=tr('请填写有效群组username，例如 groupname 或 @groupname。')
        await respond(message)

    async def change_whitelist(event, actor, args, menu_message_id=None):
        if re.match(r'^group(?:\s|\+|$)',args,re.I):
            await change_group_whitelist(event,actor,args,menu_message_id)
            return
        pieces = args.split(maxsplit=1)
        if not pieces or pieces[0].lower() not in ("add", "remove"):
            await event.reply(tr('格式：/whitelist add|remove 用户名或用户ID，也可引用用户消息。'))
            return
        action = pieces[0].lower()
        raw = pieces[1] if len(pieces) > 1 else ""
        try:
            target = await target_from_event(event, raw)
        except (ValueError, errors.RPCError):
            target = None
        if target is None:
            await event.reply(tr('请引用用户消息，或填写用户名/用户ID。'))
            return
        if action == "add":
            label = " ".join(filter(None, [getattr(target, "first_name", None), getattr(target, "last_name", None)])) or getattr(target, "username", None) or "未知昵称"
            db.execute("INSERT OR REPLACE INTO whitelist(user_id,label,added_by,created_at) VALUES(?,?,?,?)",
                       (target.id, label, actor.id, utc_now()))
            db.execute("DELETE FROM auto_recent WHERE user_id=?", (target.id,))
        else:
            db.execute("DELETE FROM whitelist WHERE user_id=?", (target.id,))
        db.commit()
        message = tr("已添加白名单用户 {0}。", target.id) if action == "add" else tr("已移除白名单用户 {0}。", target.id)
        if event.is_group:
            await group_reply(event, message)
        else:
            await event.respond(message)

    async def change_ban(event, actor, target, unban=False):
        if not await group_admin(event.chat_id, actor.id) or not await bot_group_admin(event.chat_id):
            await group_reply(event, tr('封禁操作需要您和 Bot 都拥有本群管理员权限。'))
            return
        if not unban:
            protection = await ban_protection_reason(event.chat_id, target.id)
            if protection:
                await group_reply(event, protection)
                return
        method = "unbanChatMember" if unban else "banChatMember"
        payload = {"chat_id": event.chat_id, "user_id": target.id}
        if unban:
            payload["only_if_banned"] = True
        else:
            payload["revoke_messages"] = False
        try:
            await api(method, payload)
        except RuntimeError as exc:
            await group_reply(event, tr('操作失败：{0}' ,html.escape(str(exc))), parse_mode="html")
            return
        if unban:
            db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (event.chat_id, target.id))
        else:
            label = " ".join(filter(None, [getattr(target, "first_name", None), getattr(target, "last_name", None)])) or getattr(target, "username", None) or "未知昵称"
            db.execute("INSERT OR REPLACE INTO bans(chat_id,user_id,label,banned_by,created_at) VALUES(?,?,?,?,?)",
                       (event.chat_id, target.id, label, actor.id, utc_now()))
        db.commit()
        await group_reply(event, tr('已{0}用户 <code>{1}</code>。' ,tr('解除封禁') if unban else tr('封禁'), target.id), parse_mode="html")

    @actor_language(db)
    async def run_bulk_job(chat_id, actor_id, chat_title, reply_to=None):
        if not is_admin(db, actor_id) or not may_manage_group(actor_id, chat_id):
            db.execute("DELETE FROM bulk_jobs WHERE chat_id=?", (chat_id,))
            db.commit()
            return
        if scan_lock.locked():
            if reply_to is not None:
                message = await bot.send_message(chat_id, tr('已有全员检查正在运行。'), reply_to=reply_to)
                expire(message, reply_to)
            return
        async with scan_lock:
            if not await bot_group_admin(chat_id):
                message = await bot.send_message(chat_id, tr('全员检查需要 Bot 在本群拥有管理员权限。'), reply_to=reply_to)
                expire(message, reply_to)
                db.execute("DELETE FROM bulk_jobs WHERE chat_id=?", (chat_id,))
                db.commit()
                return
            now = utc_now()
            existing_job = db.execute("SELECT request_message_id FROM bulk_jobs WHERE chat_id=?", (chat_id,)).fetchone()
            request_message_id = reply_to if reply_to is not None else (existing_job[0] if existing_job else None)
            db.execute("""INSERT INTO bulk_jobs(chat_id,actor_id,phase,started_at,updated_at,request_message_id)
                VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                actor_id=excluded.actor_id,phase=excluded.phase,done=0,total=0,
                eta_seconds=NULL,updated_at=excluded.updated_at,request_message_id=excluded.request_message_id""",
                (chat_id, actor_id, "读取群成员", now, now, request_message_id))
            db.commit()
            progress_message = await bot.send_message(chat_id, tr('正在读取群成员，预计用时：计算中…'), reply_to=reply_to)
            progress = {"phase": "读取群成员", "enumerated": 0, "reported": 0,
                        "direct_done": 0, "direct_total": 0, "source_done": 0,
                        "source_total": 0, "source_seen": 0, "source_expected": 0,
                        "source_known": 0, "direct_skipped": 0, "direct_wait": 0,
                        "hits": 0, "phase_started": time.monotonic(),
                        "direct_started": None, "source_started": None,
                        "phase_deadline": time.monotonic() + BULK_PREP_TIMEOUT}
            progress_edit_lock = asyncio.Lock()

            def estimate(done, total, started):
                if not total or done < 2 or started is None:
                    return None
                return int(max(0, total - done) * (time.monotonic() - started) / done)

            def duration(seconds):
                if seconds is None:
                    return tr('计算中')
                if seconds < 60:
                    return tr('约 {0} 秒' ,max(1, seconds))
                if seconds < 3600:
                    return tr('约 {0} 分钟' ,seconds // 60 + 1)
                return tr('约 {0} 小时 {1} 分钟' ,seconds // 3600, seconds % 3600 // 60)

            async def progress_loop():
                previous = None
                while True:
                    CURRENT_LANGUAGE.set(group_language(db, chat_id))
                    if progress["phase"] in ("读取群成员", "准备用户查询"):
                        eta = (estimate(progress["enumerated"], progress["reported"], progress["phase_started"])
                               if progress["phase"] == "读取群成员" else None)
                        lines = [tr('正在{0}…' ,tr(progress['phase'])),
                                 tr('已读取：{0} / {1}' ,progress['enumerated'], progress['reported'] or tr('未知'))]
                        done, total = progress["enumerated"], progress["reported"]
                    else:
                        direct_eta = estimate(progress["direct_done"], progress["direct_total"], progress["direct_started"])
                        source_eta = estimate(progress["source_done"], progress["source_total"], progress["source_started"])
                        if progress["source_total"] and progress["source_done"] < progress["source_total"]:
                            if progress["source_seen"] >= 100 and progress["source_known"]:
                                projected = (progress["source_expected"] * progress["source_total"]
                                             / progress["source_known"])
                                source_eta = int(max(0, projected - progress["source_seen"])
                                                 * (time.monotonic() - progress["source_started"])
                                                 / progress["source_seen"])
                        job_remaining = max(0, BULK_JOB_TIMEOUT - (time.monotonic() - progress['phase_started']))
                        if progress["direct_started"] is not None:
                            direct_eta = bulk_eta(direct_eta,
                                BULK_DIRECT_TIMEOUT - (time.monotonic() - progress["direct_started"]), job_remaining)
                        if progress["source_started"] is not None:
                            source_eta = bulk_eta(source_eta,
                                BULK_SOURCE_TIMEOUT - (time.monotonic() - progress["source_started"]), job_remaining)
                        direct_pending = progress["direct_done"] < progress["direct_total"]
                        source_pending = progress["source_done"] < progress["source_total"]
                        pending_etas = ([direct_eta] if direct_pending else []) + ([source_eta] if source_pending else [])
                        eta = max(pending_etas) if pending_etas and all(value is not None for value in pending_etas) else (None if pending_etas else 0)
                        lines = [tr('正在全员检查…'),
                                 tr('当前阶段：{0}', tr(progress['phase'])),
                                 tr('已读取群成员：{0}' ,progress['enumerated']),
                                 tr('直接查询：{0} / {1}' ,progress['direct_done'], progress['direct_total'])]
                        if progress["direct_wait"]:
                            lines.append(tr('已自动处理接口等待，最长 {0}' ,duration(progress['direct_wait'])))
                        if direct_pending and direct_eta is not None:
                            lines.append(tr('直接查询预计剩余：{0}' ,duration(direct_eta)))
                        if progress["source_total"]:
                            lines.append(tr('来源群交集扫描：{0} / {1}' ,progress['source_done'], progress['source_total']))
                            lines.append(tr('来源群成员已读取：{0}' ,progress['source_seen']))
                            if source_pending and source_eta is not None:
                                lines.append(tr('来源群预计剩余：{0}' ,duration(source_eta)))
                        if progress["phase"] == "补充查询":
                            eta = None
                        lines.append(tr('当前命中：{0}' ,progress['hits']))
                        done = progress["direct_done"] + progress["source_done"]
                        total = progress["direct_total"] + progress["source_total"]
                    remaining = max(0, progress['phase_deadline'] - time.monotonic())
                    job_remaining = max(0, BULK_JOB_TIMEOUT - (time.monotonic() - progress['phase_started']))
                    eta = bulk_eta(eta, remaining, job_remaining)
                    lines.append(tr('预计剩余：{0}' ,duration(eta)))
                    message = "\n".join(lines)
                    # Persist progress even while a stable reference batch holds the edit lock.
                    db.execute("UPDATE bulk_jobs SET phase=?,done=?,total=?,eta_seconds=?,updated_at=? WHERE chat_id=?",
                               (progress["phase"], done, total, eta, utc_now(), chat_id))
                    db.commit()
                    if message != previous and not progress_edit_lock.locked():
                        try:
                            async with progress_edit_lock:
                                await asyncio.wait_for(progress_message.edit(message), 10)
                        except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError):
                            LOG.warning("Could not update bulk progress in %s", chat_id)
                        else:
                            previous = message
                    await asyncio.sleep(10)

            progress_task = asyncio.create_task(progress_loop())
            source_task = None
            try:
                try:
                    reported_members = int(await api("getChatMemberCount", {"chat_id": chat_id}))
                except (RuntimeError, TypeError, ValueError):
                    reported_members = getattr(await bot.get_entity(chat_id), "participants_count", None)
                progress["reported"] = reported_members or 0
                visible_by_id = {}
                input_peer = await bot.get_input_entity(chat_id)
                if isinstance(input_peer, types.InputPeerChannel):
                    channel = utils.get_input_channel(input_peer)
                    first_page = await asyncio.wait_for(bot(functions.channels.GetParticipantsRequest(
                        channel, types.ChannelParticipantsSearch(""), 0, 200, 0)), 20)
                    for member in first_page.users:
                        visible_by_id[member.id] = member
                    progress["enumerated"] = len(visible_by_id)
                    reported_members = max(reported_members or 0, first_page.count) or None
                    progress["reported"] = reported_members or 0
                    offsets = iter(range(200, first_page.count, 200))

                    async def page_worker():
                        while True:
                            try:
                                offset = next(offsets)
                            except StopIteration:
                                return
                            for attempt in range(2):
                                try:
                                    page = await asyncio.wait_for(bot(functions.channels.GetParticipantsRequest(
                                        channel, types.ChannelParticipantsSearch(""), offset, 200, 0)), 20)
                                    for member in page.users:
                                        visible_by_id[member.id] = member
                                    progress["enumerated"] = len(visible_by_id)
                                    break
                                except errors.FloodWaitError as exc:
                                    if attempt == 0:
                                        await asyncio.sleep(exc.seconds)
                                        continue
                                    LOG.warning("Target group page %s delayed by %ss", offset, exc.seconds)
                                except (errors.RPCError, OSError, asyncio.TimeoutError):
                                    LOG.exception("Could not read target group page %s", offset)
                                break

                    await bulk_phase(asyncio.gather(*(page_worker() for _ in range(8))), 90, "target member pages")
                else:
                    async for member in bot.iter_participants(chat_id):
                        visible_by_id[member.id] = member
                        progress["enumerated"] = len(visible_by_id)
                visible_members = list(visible_by_id.values())
                skipped_deleted = sum(bool(getattr(member, "deleted", False)) for member in visible_members)
                members = [member for member in visible_members
                           if not getattr(member, "bot", False) and not getattr(member, "deleted", False)
                           and not whitelisted(member.id)]
                partial = reported_members is not None and reported_members > len(visible_members)
                sources = await refresh_sources()
                if not sources and not all_sources_exempt():
                    raise ValueError("No source groups available")
                account_scopes = {id(client): set(source_accounts.get(id(client), ())) & sources
                                  for client in users}
                member_ids = {member.id for member in members}
                cached = {}
                source_hits = {}
                complete_sources = set()
                source_errors = {}
                direct_results = {member.id: {"groups": {}, "covered": set(), "errors": []}
                                  for member in members}
                hit_ids = set()

                def on_wait(client, method, seconds):
                    account = users.index(client) + 1
                    LOG.warning("Bulk account %s %s waiting %ss before retry", account, method, seconds)
                    progress["direct_wait"] = max(progress["direct_wait"], seconds)

                rpc = account_rpc.with_observer(on_wait)
                bulk_pool = account_pool
                progress["phase"] = "准备用户查询"
                seed_counts = {id(client): 0 for client in users}
                async def prepare_entities():
                    seed_counts.update(await seed_bulk_users(bot, users, bot_me, members, rpc,
                                                             context_message=progress_message,
                                                             edit_lock=progress_edit_lock))
                await bulk_phase(prepare_entities(), BULK_PREP_TIMEOUT, "all account preparation")
                LOG.info("Bulk entity preparation in %s: %s", chat_id,
                         {"account%s" % (index + 1): seed_counts[id(client)]
                          for index, client in enumerate(users)})

                # User access hashes must stay with the account that obtained them.
                async def prepare_history(client):
                    for member in members:
                        try:
                            cached[(id(client), member.id)] = client.session.get_input_entity(member.id)
                        except ValueError:
                            pass
                    unresolved_ids = {uid for uid in member_ids if (id(client), uid) not in cached}
                    if not unresolved_ids:
                        return
                    try:
                        try:
                            history_peer = client.session.get_input_entity(chat_id)
                        except ValueError:
                            target_chat = await bot.get_entity(chat_id)
                            history_peer = await asyncio.wait_for(client.get_input_entity(target_chat.username), 15)
                        offset_id = 0
                        for _ in range(10):
                            page = await rpc.call(client, functions.messages.GetHistoryRequest(
                                history_peer, offset_id, None, 0, 100, 0, 0, 0), max_wait=5)
                            if not page.messages:
                                break
                            for item in page.messages:
                                sender_id = getattr(item, "sender_id", None)
                                if sender_id in unresolved_ids:
                                    cached[(id(client), sender_id)] = InputUserFromMessage(history_peer, item.id, sender_id)
                                    unresolved_ids.remove(sender_id)
                            for uid in list(unresolved_ids):
                                try:
                                    cached[(id(client), uid)] = client.session.get_input_entity(uid)
                                    unresolved_ids.remove(uid)
                                except ValueError:
                                    pass
                            if not unresolved_ids:
                                break
                            next_id = min(item.id for item in page.messages)
                            if next_id == offset_id:
                                break
                            offset_id = next_id
                    except (ValueError, TypeError, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                        LOG.info("Bulk account %s history unavailable in %s: %s",
                                 users.index(client) + 1, chat_id, type(exc).__name__)

                await asyncio.gather(*(bulk_phase(prepare_history(client), 30,
                                                  "account%s history" % (index + 1))
                                       for index, client in enumerate(users)))

                async def check_member(member):
                    outcome = direct_results[member.id]
                    async def lookup(client):
                        try:
                            resolved = client.session.get_input_entity(member.id)
                        except ValueError:
                            resolved = cached.get((id(client), member.id))
                        if resolved is None:
                            if not member.username:
                                raise ValueError("Target hash not available in this account")
                            response = await rpc.call(client, functions.contacts.ResolveUsernameRequest(
                                member.username), max_wait=0, timeout=10)
                            entity = next((item for item in response.users if item.id == member.id), None)
                            if entity is None:
                                raise ValueError("Username no longer belongs to the target ID")
                            resolved = utils.get_input_user(entity)
                            cached[(id(client), member.id)] = resolved
                        groups = await fetch_common_groups_resolved(client, resolved, sources, rpc=rpc)
                        # Preserve completed evidence even if a slower account is cancelled.
                        outcome["groups"].update({group["id"]: group for group in groups})
                        outcome["covered"].update(account_scopes[id(client)])
                        if groups:
                            hit_ids.add(member.id)
                        return groups
                    groups, covered, failures = await bulk_pool.query(sources - outcome["covered"],
                                                                     account_scopes, lookup, member.id)
                    outcome["groups"].update({group["id"]: group for group in groups})
                    outcome["covered"].update(covered)
                    for exc in failures:
                        reason = type(exc).__name__
                        if reason not in outcome["errors"]:
                            outcome["errors"].append(reason)
                    if groups:
                        hit_ids.add(member.id)
                    progress["hits"] = len(hit_ids)

                progress["phase"] = "并行查询"
                progress["phase_deadline"] = time.monotonic() + BULK_DIRECT_TIMEOUT
                progress["direct_total"] = len(members)
                progress["direct_started"] = time.monotonic()
                iterator = iter(members)

                async def direct_worker():
                    for member in iterator:
                        if not await bulk_phase(check_member(member), 45, "member query"):
                            direct_results[member.id]["errors"].append("member_timeout")
                        progress["direct_done"] += 1
                        await asyncio.sleep(0)

                await bulk_phase(asyncio.gather(*(direct_worker() for _ in range(min(account_rpc.settings.bulk_workers(len(users)), len(members))))),
                                 BULK_DIRECT_TIMEOUT, "direct queries")
                needed_sources = set().union(*(
                    sources - result["covered"] for result in direct_results.values()
                )) if members else set()

                async def scan_sources():
                    semaphore = asyncio.Semaphore(account_rpc.settings.source_workers(len(users)))

                    async def scan_source(source_id):
                        async with semaphore:
                            found = set()
                            seen_ids = set()
                            expected = None
                            complete = False
                            reasons = []
                            async def enumerate_accounts():
                                nonlocal expected, complete
                                candidates = bulk_pool.rank(
                                    [client for client in users if source_id in account_scopes[id(client)]],
                                    account_scopes, {source_id}, method=functions.channels.GetParticipantsRequest)
                                for client in candidates:
                                    try:
                                        peer = client.session.get_input_entity(source_id)
                                        if isinstance(peer, types.InputPeerChannel):
                                            channel = utils.get_input_channel(peer)
                                            full = await rpc.call(client, functions.channels.GetFullChannelRequest(channel), max_wait=0)
                                            full_count = getattr(full.full_chat, "participants_count", None)
                                            hidden = bool(getattr(full.full_chat, "participants_hidden", False))
                                            offset = 0
                                            while True:
                                                page = await rpc.call(client, functions.channels.GetParticipantsRequest(
                                                    channel, types.ChannelParticipantsSearch(""), offset, 200, 0), max_wait=0)
                                                if expected is None:
                                                    expected = max(page.count, full_count or 0)
                                                    progress["source_expected"] += expected
                                                    progress["source_known"] += 1
                                                page_users = {candidate.id: candidate for candidate in page.users}
                                                fresh_ids = set(page_users) - seen_ids
                                                seen_ids.update(page_users)
                                                progress["source_seen"] += len(fresh_ids)
                                                matched_ids = member_ids & set(page_users)
                                                found.update(matched_ids)
                                                for uid in matched_ids:
                                                    source_hits.setdefault(uid, set()).add(source_id)
                                                    hit_ids.add(uid)
                                                returned = len(page.participants)
                                                if len(seen_ids) >= max(page.count, expected) and (not hidden or full_count is not None):
                                                    complete = True
                                                    break
                                                if returned == 0 or not fresh_ids:
                                                    break
                                                offset += returned
                                        elif isinstance(peer, types.InputPeerChat):
                                            page = await rpc.call(client, functions.messages.GetFullChatRequest(peer.chat_id), max_wait=0)
                                            participants = page.full_chat.participants
                                            if isinstance(participants, types.ChatParticipants):
                                                seen_ids.update(p.user_id for p in participants.participants)
                                                found.update(member_ids & seen_ids)
                                                for uid in found:
                                                    source_hits.setdefault(uid, set()).add(source_id)
                                                    hit_ids.add(uid)
                                                complete = True
                                                progress["source_seen"] += len(seen_ids)
                                        if complete:
                                            break
                                        reasons.append("account%s:members_hidden_or_truncated" % (users.index(client) + 1))
                                    except (ValueError, errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                                        reasons.append("account%s:%s" % (users.index(client) + 1, type(exc).__name__))
                            if not await bulk_phase(enumerate_accounts(), BULK_SOURCE_SINGLE_TIMEOUT, "source %s" % source_id):
                                reasons.append("source_scan_timeout")
                            for uid in found:
                                source_hits.setdefault(uid, set()).add(source_id)
                                hit_ids.add(uid)
                            if complete:
                                complete_sources.add(source_id)
                            else:
                                source_errors[source_id] = reasons or ["no_account_in_source_group"]
                                LOG.warning("Bulk source %s incomplete: %s; read=%s expected=%s",
                                            source_id, ",".join(source_errors[source_id]), len(seen_ids), expected)
                            progress["source_done"] += 1
                            progress["hits"] = len(hit_ids)

                    await asyncio.gather(*(scan_source(source_id) for source_id in needed_sources))

                if needed_sources:
                    progress["phase"] = "来源群扫描"
                    progress["phase_deadline"] = time.monotonic() + BULK_SOURCE_TIMEOUT
                    progress["source_total"] = len(needed_sources)
                    progress["source_started"] = time.monotonic()
                    source_task = asyncio.create_task(scan_sources())
                    if not await bulk_phase(source_task, BULK_SOURCE_TIMEOUT, "source intersection scan"):
                        for source_id in needed_sources - complete_sources:
                            source_errors.setdefault(source_id, ["source_scan_timeout"])
                    # Source pages populate each account's own session cache. Retry members
                    # discovered there to obtain all common groups, including hidden sources.
                    retry_members = [member for member in members
                                     if sources - direct_results[member.id]["covered"] - complete_sources
                                     and source_hits.get(member.id)]
                    retry_iterator = iter(retry_members)

                    async def retry_worker():
                        for member in retry_iterator:
                            if not await bulk_phase(check_member(member), 30, "member retry"):
                                direct_results[member.id]["errors"].append("retry_timeout")

                    progress["phase"] = "补充查询"
                    progress["phase_deadline"] = time.monotonic() + 60
                    await bulk_phase(asyncio.gather(*(retry_worker() for _ in range(min(8, len(retry_members))))),
                                     60, "final retries")

                exempt=exempt_source_ids(db)
                sources=set(sources)-exempt
                source_failures = len(set(source_errors)-exempt)
                hits = []
                failures = 0
                records = []
                timestamp = utc_now()
                for member in members:
                    outcome = direct_results[member.id]
                    matched=filter_bulk_hits(outcome,source_hits.get(member.id,set()),exempt)
                    matches, missing = bulk_result_state(sources, outcome["covered"], complete_sources, matched,available_only=True)
                    failed = matches is None
                    if matches:
                        hits.append(member.id)
                    failures += int(failed)
                    reasons = list(outcome["errors"])
                    detail = list(outcome["groups"].values())
                    detail.extend({"id": source_id, "title": str(source_id)}
                                  for source_id in matched - set(outcome["groups"]))
                    records.append((timestamp, actor_id, member.id, "群组check all", chat_id, matches,
                                    "失败" if failed else "完成", json.dumps(detail, ensure_ascii=False),
                                    "; ".join(reasons)))
                db.executemany("""INSERT INTO queries(created_at,requester_id,target_id,method,chat_id,matches,status,details_json,result_text)
                    VALUES(?,?,?,?,?,?,?,?,?)""", records)
                db.commit()
                LOG.info("Bulk result in %s: checked=%s hits=%s failed=%s source_incomplete=%s retries=%s reasons=%s",
                         chat_id, len(members), len(hits), failures, source_failures, rpc.retries,
                         json.dumps(source_errors, ensure_ascii=False))
                owner = setting(db, "superadmin")
                CURRENT_LANGUAGE.set(group_language(db, chat_id))
                coverage = tr('，仅枚举 {0}/{1} 位成员' ,len(visible_members), reported_members) if partial else ""
                header = (tr('群聊 {0} 检查完成：{1} 人命中，成功检查 {2} 人，未完成 {3} 人{4}。跳过已注销账号 {5} 人。\n' ,html.escape(chat_title), len(hits), len(members) - failures, failures, coverage, skipped_deleted))
                delivery_failures = await deliver_bulk_result(bot, owner, actor_id, header, hits, db=db)
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
                coverage_note = tr('仅枚举 {0}/{1} 位成员。' ,len(visible_members), reported_members) if partial else ""
                result = (tr('全员检查完成，用时 {0}。命中 {1} 人，结果已私发超级管理员及发起者。查询未完成 {2} 人。跳过已注销账号 {3} 人。{4}' ,duration(int(time.monotonic() - progress['phase_started'])), len(hits), failures, skipped_deleted, coverage_note))
                if delivery_failures:
                    result += tr('部分收件人私信发送失败，请先私聊 Bot 发送 /start。')
                await progress_message.edit(result)
                expire(progress_message, request_message_id)
                db.execute("DELETE FROM bulk_jobs WHERE chat_id=?", (chat_id,))
                db.commit()
            except asyncio.CancelledError:
                expire(progress_message)
                raise
            except Exception:
                LOG.exception("Bulk scan failed in %s", chat_id)
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
                await progress_message.edit(tr('全员检查失败，请稍后重试。'))
                expire(progress_message, request_message_id)
                db.execute("DELETE FROM bulk_jobs WHERE chat_id=?", (chat_id,))
                db.commit()
            finally:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
                if source_task is not None and not source_task.done():
                    source_task.cancel()
                    await asyncio.gather(source_task, return_exceptions=True)

    @actor_language(db)
    async def run_bulk_core(chat_id, actor_id, chat_title, reply_to=None):
        try:
            await asyncio.wait_for(run_bulk_job(chat_id, actor_id, chat_title, reply_to), BULK_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            LOG.error("Bulk job in %s timed out after %ss", chat_id, BULK_JOB_TIMEOUT)
            row = db.execute("SELECT request_message_id FROM bulk_jobs WHERE chat_id=?", (chat_id,)).fetchone()
            db.execute("DELETE FROM bulk_jobs WHERE chat_id=?", (chat_id,))
            db.commit()
            try:
                message = await asyncio.wait_for(bot.send_message(chat_id,
                    tr('全员检查已超时停止，尚未完成的用户不视为检查通过，请稍后重试。'), reply_to=reply_to), 10)
                expire(message, row[0] if row else reply_to)
            except (errors.RPCError, OSError, asyncio.TimeoutError):
                LOG.warning("Could not send bulk timeout notice in %s", chat_id)

    async def run_bulk(event, actor, chat):
        await run_bulk_core(event.chat_id, actor.id, group_title(chat), reply_to=event.id)

    async def resume_bulk_jobs():
        jobs = db.execute("SELECT chat_id,actor_id FROM bulk_jobs ORDER BY started_at").fetchall()
        for chat_id, actor_id in jobs:
            try:
                chat = await bot.get_entity(chat_id)
                LOG.info("Resuming bulk check in group %s", chat_id)
                await run_bulk_core(chat_id, actor_id, group_title(chat))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Could not resume bulk check in group %s", chat_id)

    auto_inflight = set()
    auto_retry_after = OrderedDict()

    async def auto_check(chat_id, target, reply_to, method, reference=None):
        if target is None or getattr(target, "bot", False) or whitelisted(target.id):
            return
        row = db.execute("SELECT expires_at FROM auto_recent WHERE chat_id=? AND user_id=?", (chat_id, target.id)).fetchone()
        if row and row[0] > int(time.time()):
            return
        key = (chat_id, target.id)
        if auto_retry_after.get(key, 0) > time.monotonic():
            return
        if key in auto_inflight:
            return
        auto_inflight.add(key)
        try:
            if reference is None and method == "群组auto消息":
                reference = (chat_id, reply_to)
            found = await publish_check(chat_id, bot_me.id, target, method, reply_to=reply_to,
                                        positive_only=True, reference=reference, context_chat_id=chat_id)
            if found is False:
                db.execute("INSERT OR REPLACE INTO auto_recent(chat_id,user_id,expires_at) VALUES(?,?,?)",
                           (chat_id, target.id, int(time.time()) + 86400))
                db.commit()
            elif found is None:
                auto_retry_after[key] = time.monotonic() + 60
                auto_retry_after.move_to_end(key)
                if len(auto_retry_after) > 4096:
                    auto_retry_after.popitem(last=False)
        finally:
            auto_inflight.discard(key)

    @bot.on(events.NewMessage)
    @language_handler(db)
    async def on_message(event):
        text = (event.raw_text or "").strip()
        sender = await event.get_sender()
        if sender is None or sender.id == bot_me.id:
            return
        match = COMMAND.fullmatch(text)
        command = match.group(1).lower() if match else None
        args = (match.group(2) or "").strip() if match else ""
        if event.is_private:
            menu_message_id = None
            if text == "/start" or text.startswith("/start "):
                parts = text.split(maxsplit=1)
                if setting(db, "superadmin") is None and len(parts) == 2 and parts[1] == bootstrap:
                    db.execute("INSERT INTO settings(key,value) VALUES('superadmin',?)", (str(sender.id),))
                    db.commit()
                if user_language(db, sender.id) is None:
                    await choose_language(event)
                else:
                    await welcome(event, sender)
                return
            if sender.id in pending and time.monotonic() < pending[sender.id][1] and not text.startswith(("/", "!")):
                state = pending.pop(sender.id)
                action = state[0]
                menu_message_id = state[2] if len(state) > 2 else None
                if action == "check":
                    command, args = "check", text
                elif action == "submit":
                    command, args = "submit", text
                elif action == "addgroup" and is_admin(db, sender.id):
                    text = "/add group " + text
                elif action == "add" and str(sender.id) == setting(db, "superadmin"):
                    text = "/add " + text
                elif action in ("whiteadd", "whiteremove") and is_admin(db, sender.id):
                    command, args = "whitelist", ("add " if action == "whiteadd" else "remove ") + text
                elif action in ('groupwhiteadd','groupwhiteremove') and is_admin(db,sender.id):
                    command,args='whitelist',('group ' if action=='groupwhiteadd' else 'group remove ')+text
            if command == "submit":
                await submit_group(event, sender, args, menu_message_id=menu_message_id)
                return
            add_group = re.fullmatch(r"/add(?:@\w+)?\s+group(?:\s+(.*))?", text, re.I | re.S)
            if add_group:
                await submit_group(event, sender, (add_group.group(1) or "").strip(), direct=True, menu_message_id=menu_message_id)
                return
            if command == "check":
                if args.lower() == "all":
                    await event.reply(tr('请在您管理的群聊中发送 /check all。'))
                    return
                try:
                    target = await target_from_event(event, args, use_cache=True)
                except (ValueError, errors.RPCError):
                    target = None
                if target is None or getattr(target, "bot", False):
                    await event.reply(tr('请输入要查询的username或用户ID，也可引用用户消息。'))
                    return
                await publish_check(event.chat_id, sender.id, target, "bot私聊", reply_to=event.id)
                return
            if command == "whitelist" and is_admin(db, sender.id):
                await change_whitelist(event, sender, args, menu_message_id=menu_message_id)
                return
            if text in ("/group", "/groups") and is_admin(db, sender.id):
                await show_groups(event, sender.id)
            elif text == "/admins" and str(sender.id) == setting(db, "superadmin"):
                await show_admins(event)
            elif text == "/logs" and str(sender.id) == setting(db, "superadmin"):
                await show_logs(event)
            elif text == "/logs clear" and str(sender.id) == setting(db, "superadmin"):
                db.execute("DELETE FROM queries")
                db.commit()
                await event.respond(tr('查询日志已清空。'))
            elif text.startswith(("/add ", "/revoke ")) and str(sender.id) == setting(db, "superadmin"):
                try:
                    action, raw_id = text.split(maxsplit=1)
                    target_id = int(raw_id)
                    if target_id <= 0 or str(target_id) == setting(db, "superadmin"):
                        raise ValueError
                except ValueError:
                    await event.respond(tr('格式：/add 用户ID 或 /revoke 用户ID'))
                    return
                if action == "/add":
                    db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (target_id,))
                else:
                    db.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
                db.commit()
                await event.respond(tr('已{0}管理员 {1}。' ,tr('授权') if action == '/add' else tr('撤销'), target_id), buttons=menu_buttons(sender.id))
                try:
                    changed = await bot.get_entity(target_id)
                    await sync_menu(changed)
                    for (group_id,) in db.execute("SELECT chat_id FROM groups WHERE owner_id=?", (target_id,)):
                        await sync_group_menu(group_id, changed)
                except (ValueError, errors.RPCError):
                    pass
            return

        if not event.is_group:
            return
        chat = await event.get_chat()
        remember_user_message(event.chat_id, sender.id, event.id)
        remember_group(event.chat_id, group_title(chat))
        # Public checks do not require group ownership or member-role lookups.
        # Management commands may discover an owner, but must verify that role.
        if match and (command != "check" or args.lower() == "all"):
            await claim_group_owner(event.chat_id, sender.id)
        if match:
            LOG.info("Group command %s from %s in %s", command, sender.id, event.chat_id)
            if command != "check":
                asyncio.create_task(sync_group_menu_background(event.chat_id, sender))
        if command == "check":
            if not enabled(event.chat_id):
                return
            if args.lower() == "all":
                await run_bulk(event, sender, chat)
                return
            progress_message = await event.reply(tr('正在查询，请稍候…'))
            asyncio.create_task(claim_group_owner(event.chat_id, sender.id))
            asyncio.create_task(sync_group_menu_background(event.chat_id, sender))
            try:
                target = await asyncio.wait_for(target_from_event(event, args, use_cache=True), timeout=10)
            except (ValueError, errors.RPCError, asyncio.TimeoutError):
                target = None
            if target is None or getattr(target, "bot", False):
                message = await progress_message.edit(tr('未能解析目标用户。请引用目标消息，或发送 /check 用户名/用户ID。'))
                expire(message, event.id)
                return
            reference = None
            if not args:
                replied = await event.get_reply_message()
                if replied is not None and replied.sender_id == target.id:
                    reference = (event.chat_id, replied.id)
            await publish_check(event.chat_id, sender.id, target, "群组check指令", reply_to=event.id,
                                reference=reference, context_chat_id=event.chat_id,
                                progress_message=progress_message)
            return
        if command in ("ban", "unban"):
            if not await group_admin(event.chat_id, sender.id):
                return
            try:
                target = await target_from_event(event, args)
            except (ValueError, errors.RPCError):
                target = None
            if target is None:
                await group_reply(event, tr('请引用目标消息，或发送 /{0} 用户名/用户ID。' ,command))
                return
            await change_ban(event, sender, target, unban=command == "unban")
            return
        if command == "whitelist":
            if is_admin(db, sender.id) and may_manage_group(sender.id, event.chat_id):
                await change_whitelist(event, sender, args)
            return
        if command == "auto":
            if not await group_admin(event.chat_id, sender.id):
                return
            if args.lower() not in ("check on", "check off"):
                await group_reply(event, tr('格式：/auto check on 或 /auto check off'))
                return
            on = args.lower() == "check on"
            if on and not await bot_group_admin(event.chat_id):
                await group_reply(event, tr('自动查询需要 Bot 在本群拥有管理员权限。'))
                return
            if on:
                db.execute("UPDATE groups SET enabled=1 WHERE chat_id=?", (event.chat_id,))
            db.execute("INSERT INTO auto_groups(chat_id,enabled) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
                       (event.chat_id, int(on)))
            db.commit()
            await group_reply(event, tr('已经开启自动查询。') if on else tr('已经关闭自动查询。'))
            return
        if not command and enabled(event.chat_id) and auto_enabled(event.chat_id):
            await auto_check(event.chat_id, sender, event.id, "群组auto消息")

    @bot.on(events.CallbackQuery(pattern=rb"^toggle:-?\d+(?::\d+)?$"))
    @language_handler(db)
    async def on_toggle(event):
        if not event.is_private or not is_admin(db, event.sender_id):
            await event.answer(tr('无管理权限'), alert=True)
            return
        parts = event.data.split(b":")
        _, raw_id = parts[:2]
        group_id = int(raw_id)
        page = int(parts[2]) if len(parts) > 2 else 0
        if not may_manage_group(event.sender_id, group_id):
            await event.answer(tr('无权管理该群聊'), alert=True)
            return
        row = db.execute("SELECT enabled FROM groups WHERE chat_id=?", (group_id,)).fetchone()
        db.execute("UPDATE groups SET enabled=? WHERE chat_id=?", (0 if row[0] else 1, group_id))
        db.commit()
        await event.answer(tr('设置已更新'))
        await event.edit(tr('选择允许查询的群聊：'), buttons=group_buttons(event.sender_id, page))

    @bot.on(events.CallbackQuery(pattern=rb"^groups:\d+$"))
    @language_handler(db)
    async def on_group_page(event):
        if not event.is_private or not is_admin(db, event.sender_id):
            await event.answer(tr('无管理权限'), alert=True)
            return
        page = int(event.data.split(b":", 1)[1])
        buttons = group_buttons(event.sender_id, page)
        if not buttons:
            await event.answer(tr('已到末页'), alert=True)
            return
        await event.answer()
        await event.edit(tr('选择允许查询的群聊：'), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"^(?:detail:\d+:\d+|result:\d+)$"))
    @language_handler(db)
    async def on_details(event):
        pieces = event.data.decode().split(":")
        query_id = int(pieces[1])
        row = db.execute("SELECT chat_id,target_id,details_json,result_text FROM queries WHERE id=?", (query_id,)).fetchone()
        if row is None or row[0] != event.chat_id:
            await event.answer(tr('该结果不可查看'), alert=True)
            return
        if pieces[0] == "result":
            await event.answer()
            buttons = [[Button.inline(tr('查看详情群组'), f"detail:{query_id}:0".encode())]]
            if event.chat_id < 0 and await bot_group_admin(event.chat_id):
                buttons.append([Button.inline(tr('封禁用户（仅管理员）'), f"ban:{query_id}".encode())])
            await event.edit(saved_result_text(row[3]), parse_mode="html", buttons=buttons)
            return
        groups = json.loads(row[2])
        page = int(pieces[2])
        start = page * 15
        if start >= len(groups):
            await event.answer(tr('已到末页'), alert=True)
            return
        await event.answer()
        lines = [f"{index}. {html.escape(group['title'])} (<code>{group['id']}</code>)"
                 for index, group in enumerate(groups[start:start + 15], start + 1)]
        buttons = []
        if page:
            buttons.append(Button.inline(tr('上一页'), f"detail:{query_id}:{page-1}".encode()))
        if start + 15 < len(groups):
            buttons.append(Button.inline(tr('下一页'), f"detail:{query_id}:{page+1}".encode()))
        buttons.append(Button.inline(tr('返回结果'), f"result:{query_id}".encode()))
        button_rows = [buttons]
        if event.chat_id < 0 and await bot_group_admin(event.chat_id):
            button_rows.append([Button.inline(tr('封禁用户（仅管理员）'), f"ban:{query_id}".encode())])
        await event.edit(tr('共同群组列表\n') + "\n".join(lines), parse_mode="html", buttons=button_rows)

    @bot.on(events.CallbackQuery(pattern=rb"^menu:(?:home|language|check|submit|addgroup|reviews|groups|all|admins|logs|add|bans|whitelist|whiteadd|whiteremove|groupwhitelist|groupwhiteadd|groupwhiteremove)$"))
    @language_handler(db)
    async def on_menu(event):
        if not event.is_private:
            await event.answer(tr('请在 Bot 私信中操作'), alert=True)
            return
        action = event.data.decode().split(":", 1)[1]
        actor_id = event.sender_id
        if action in ("groups", "all", "bans", "whitelist", "whiteadd", "whiteremove", "groupwhitelist", "groupwhiteadd", "groupwhiteremove", "addgroup", "reviews") and not is_admin(db, actor_id):
            await event.answer(tr('无管理权限'), alert=True)
            return
        if action in ("admins", "logs", "add") and str(actor_id) != setting(db, "superadmin"):
            await event.answer(tr('无管理权限'), alert=True)
            return
        await event.answer()
        pending.pop(actor_id, None)
        with db:
            db.execute("DELETE FROM submission_messages WHERE recipient_id=? AND message_id=?", (actor_id, event.query.msg_id))
        if action == "language":
            await choose_language(event)
        elif action == "home":
            pending.pop(actor_id, None)
            sender = await event.get_sender()
            await welcome(event, sender)
        elif action == "check":
            pending[actor_id] = ("check", time.monotonic() + 120)
            await event.edit(tr('请输入要查询的username：\n也可直接发送 /check 用户ID。'),
                             buttons=[[Button.inline(tr('返回主页'), b"menu:home")]])
        elif action in ("submit", "addgroup"):
            pending[actor_id] = (action, time.monotonic() + 120, event.query.msg_id)
            await event.edit(tr('请输入作弊群组的username：\n') +
                             (tr('群组将直接添加，并安排所有协议号入群。') if action == "addgroup" else tr('提交后等待管理员审核，通过后所有协议号自动入群。')),
                             buttons=[[Button.inline(tr('返回主页'), b"menu:home")]])
        elif action == "reviews":
            await show_reviews(event)
        elif action == "groups":
            await show_groups(event, actor_id)
        elif action == "all":
            await event.edit(tr('请在您管理且已启用的群聊中发送 /check all；Bot 需要是该群管理员。'),
                             buttons=[[Button.inline(tr('返回主页'), b"menu:home")]])
        elif action == "admins":
            await show_admins(event)
        elif action == "logs":
            await show_logs(event)
        elif action == "bans":
            await show_bans(event, actor_id)
        elif action == "whitelist":
            await show_whitelist(event)
        elif action=='groupwhitelist':
            await show_group_whitelist(event)
        elif action in ('groupwhiteadd','groupwhiteremove'):
            pending[actor_id]=(action,time.monotonic()+120,event.query.msg_id)
            await event.edit(tr('请输入要添加到白名单的群组username：') if action=='groupwhiteadd' else tr('请输入要移出白名单的群组username：'),
                             buttons=[[Button.inline(tr('返回群组白名单'),b'menu:groupwhitelist')]])
        elif action in ("whiteadd", "whiteremove"):
            pending[actor_id] = (action, time.monotonic() + 120)
            await event.edit(tr('请输入要添加的用户名或用户ID：') if action == "whiteadd" else tr('请输入要移除的用户名或用户ID：'),
                             buttons=[[Button.inline(tr('返回主页'), b"menu:home")]])
        else:
            pending[actor_id] = ("add", time.monotonic() + 120)
            await event.edit(tr('请输入要授权的用户 ID：'), buttons=[[Button.inline(tr('返回主页'), b"menu:home")]])

    @bot.on(events.CallbackQuery(pattern=rb"^revoke:\d+$"))
    @language_handler(db)
    async def on_revoke(event):
        if not event.is_private or str(event.sender_id) != setting(db, "superadmin"):
            await event.answer(tr('无管理权限'), alert=True)
            return
        target_id = int(event.data.split(b":", 1)[1])
        db.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
        db.commit()
        await event.answer(tr('已撤销'))
        await show_admins(event)
        try:
            changed = await bot.get_entity(target_id)
            await sync_menu(changed)
            for (group_id,) in db.execute("SELECT chat_id FROM groups WHERE owner_id=?", (target_id,)):
                await sync_group_menu(group_id, changed)
        except (ValueError, errors.RPCError):
            pass

    @bot.on(events.CallbackQuery(pattern=rb"^logs:\d+$"))
    @language_handler(db)
    async def on_logs_page(event):
        if not event.is_private or str(event.sender_id) != setting(db, "superadmin"):
            await event.answer(tr('无管理权限'), alert=True)
            return
        await event.answer()
        await show_logs(event, int(event.data.split(b":", 1)[1]))

    @bot.on(events.CallbackQuery(pattern=rb"^bans:\d+$"))
    @language_handler(db)
    async def on_bans_page(event):
        if not event.is_private or not is_admin(db, event.sender_id):
            await event.answer(tr('无管理权限'), alert=True)
            return
        await event.answer()
        await show_bans(event, event.sender_id, int(event.data.split(b":", 1)[1]))

    @bot.on(events.CallbackQuery(pattern=rb"^whitelist:\d+$"))
    @language_handler(db)
    async def on_whitelist_page(event):
        if not event.is_private or not is_admin(db, event.sender_id):
            await event.answer(tr('无管理权限'), alert=True)
            return
        await event.answer()
        await show_whitelist(event, int(event.data.split(b":", 1)[1]))

    @bot.on(events.CallbackQuery(pattern=rb'^groupwhite:(?:page:\d+|remove:-?\d+:\d+)$'))
    @language_handler(db)
    async def on_group_whitelist(event):
        if not event.is_private or not is_admin(db,event.sender_id):
            await event.answer(tr('无管理权限'),alert=True)
            return
        pieces=event.data.decode().split(':')
        page=int(pieces[-1])
        if pieces[1]=='remove':
            try:
                changed=submissions.group_whitelist.remove(int(pieces[2]),event.sender_id,str(event.sender_id)==setting(db,'superadmin'))
            except PermissionError:
                await event.answer(tr('无管理权限'),alert=True)
                return
            if changed:apply_group_whitelist()
        await event.answer()
        await show_group_whitelist(event,page)

    @bot.on(events.CallbackQuery(pattern=rb"^ban:\d+$"))
    @language_handler(db)
    async def on_ban_button(event):
        query_id = int(event.data.split(b":", 1)[1])
        row = db.execute("SELECT chat_id,target_id FROM queries WHERE id=? AND matches>0", (query_id,)).fetchone()
        if row is None or row[0] != event.chat_id or event.chat_id >= 0:
            await event.answer(tr('该结果不可操作'), alert=True)
            return
        if not await group_admin(event.chat_id, event.sender_id) or not await bot_group_admin(event.chat_id):
            await event.answer(tr('需要您和 Bot 都拥有群管理员权限'), alert=True)
            return
        protection = await ban_protection_reason(event.chat_id, row[1])
        if protection:
            await event.answer(protection, alert=True)
            return
        try:
            await api("banChatMember", {"chat_id": event.chat_id, "user_id": row[1], "revoke_messages": False})
        except RuntimeError as exc:
            await event.answer(str(exc)[:180], alert=True)
            return
        db.execute("INSERT OR REPLACE INTO bans(chat_id,user_id,label,banned_by,created_at) VALUES(?,?,?,?,?)",
                   (event.chat_id, row[1], "由查询结果封禁", event.sender_id, utc_now()))
        db.commit()
        await event.answer(tr('已封禁'))
        confirmation = await bot.send_message(event.chat_id, tr('已封禁用户 <code>{0}</code>。' ,row[1]), parse_mode="html", reply_to=event.query.msg_id)
        expire(confirmation)

    @bot.on(events.Raw)
    @language_handler(db)
    async def on_raw_reaction(update):
        if not isinstance(update, types.UpdateBotMessageReaction) or not update.new_reactions:
            return
        chat_id = utils.get_peer_id(update.peer)
        if chat_id >= 0 or not enabled(chat_id) or not auto_enabled(chat_id):
            return
        actor_id = utils.get_peer_id(update.actor)
        if actor_id <= 0:
            return
        try:
            reacted_message = await bot.get_messages(chat_id, ids=update.msg_id)
            if reacted_message is None:
                return
            actor = await resolve_target(str(actor_id), chat_id)
            await auto_check(chat_id, actor, update.msg_id, "群组auto表情")
        except (ValueError, errors.RPCError, RuntimeError):
            LOG.exception("Reaction auto check failed in %s", chat_id)

    @bot.on(events.Raw)
    @language_handler(db)
    async def on_raw_member_change(update):
        if isinstance(update, (types.UpdateChannelParticipant, types.UpdateChatParticipant)) and update.user_id == bot_me.id:
            chat_id = (-1000000000000 - update.channel_id if isinstance(update, types.UpdateChannelParticipant)
                       else -update.chat_id)
            def member_present(participant):
                if participant is None or isinstance(participant, types.ChannelParticipantLeft):
                    return False
                if isinstance(participant, types.ChannelParticipantBanned):
                    return not participant.left and not participant.banned_rights.view_messages
                return True
            event_at = int(update.date.timestamp()) if getattr(update, 'date', None) else None
            if not member_present(update.new_participant):
                onboarding.left(chat_id, event_at=event_at)
            elif not member_present(update.prev_participant):
                try:
                    chat = await asyncio.wait_for(bot.get_entity(chat_id), 10)
                    if isinstance(chat, (types.Channel, types.ChannelForbidden)) and not getattr(chat, 'megagroup', False):
                        return
                    actor = None
                    if getattr(update, 'actor_id', None):
                        try:
                            actor = await asyncio.wait_for(bot.get_entity(update.actor_id), 5)
                        except (ValueError, errors.RPCError, asyncio.TimeoutError):
                            pass
                    inviter = ({'id': actor.id, 'username': getattr(actor, 'username', None),
                                'first_name': getattr(actor, 'first_name', None),
                                'last_name': getattr(actor, 'last_name', None)} if actor else None)
                    onboarding.joined(chat_id, group_title(chat), getattr(chat, 'username', None), inviter, event_at)
                except (ValueError, errors.RPCError, asyncio.TimeoutError):
                    LOG.warning('Bot membership metadata unavailable in %s', chat_id)
            return
        superadmin = setting(db, "superadmin")
        if not superadmin or not isinstance(update, (types.UpdateChannelParticipant, types.UpdateChatParticipant)):
            return
        if update.user_id != int(superadmin):
            return
        if isinstance(update, types.UpdateChannelParticipant):
            if update.new_participant is not None and not isinstance(update.new_participant, types.ChannelParticipantBanned):
                return
            chat_id = -1000000000000 - update.channel_id
        else:
            if update.new_participant is not None:
                return
            chat_id = -update.chat_id
        await protect_superadmin(chat_id)

    try:
        await refresh_sources(force=True)
        for language, code in (("zh", ""), ("en", "en")):
            with language_context(language):
                await bot(functions.bots.SetBotCommandsRequest(
                    types.BotCommandScopeDefault(), code,
                    [types.BotCommand("start", tr("查看向导和身份")), types.BotCommand("check", tr("查询用户名或用户ID")),
                     types.BotCommand("submit", tr("提交作弊群组：/submit 群组username"))],
                ))
                await bot(functions.bots.SetBotCommandsRequest(
                    types.BotCommandScopeChats(), code, [types.BotCommand("check", tr("查询群成员或用户ID"))],
                ))
        known_ids = [int(row[0]) for row in db.execute("SELECT value FROM settings WHERE key='superadmin'")]
        known_ids += [row[0] for row in db.execute("SELECT user_id FROM admins")]
        for known_id in known_ids:
            try:
                known_user = await bot.get_entity(known_id)
            except (ValueError, errors.RPCError):
                continue
            await sync_menu(known_user)
            for (group_id,) in db.execute("SELECT chat_id FROM groups WHERE owner_id=?", (known_id,)):
                await sync_group_menu(group_id, known_user)
        expiry_task = asyncio.create_task(expire_loop())
        protection_task = asyncio.create_task(protection_loop())
        member_update_task = asyncio.create_task(member_update_loop())
        onboarding_task = asyncio.create_task(onboarding.run())
        bulk_resume_task = asyncio.create_task(resume_bulk_jobs())
        secondary_sync_task = asyncio.create_task(secondary_group_sync_loop())
        submission_task = asyncio.create_task(submissions.run())
        await bot.run_until_disconnected()
    finally:
        if "expiry_task" in locals():
            expiry_task.cancel()
        if "protection_task" in locals():
            protection_task.cancel()
        if "member_update_task" in locals():
            member_update_task.cancel()
        if "onboarding_task" in locals():
            onboarding_task.cancel()
            await asyncio.gather(onboarding_task, return_exceptions=True)
        if "bulk_resume_task" in locals():
            bulk_resume_task.cancel()
        if "secondary_sync_task" in locals():
            secondary_sync_task.cancel()
        if "submission_task" in locals():
            submission_task.cancel()
            await asyncio.gather(submission_task, return_exceptions=True)
        await bot.disconnect()
        await asyncio.gather(*(client.disconnect() for client in users), return_exceptions=True)
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
