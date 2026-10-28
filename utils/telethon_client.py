"""Telethon 小号客户端：洗数据中转。
流程：
1. 我们的 bot 把要洗的文件私聊发给小号
2. 小号自动转发到洗数据群
3. 小号监听洗数据群，洗好的文件自动私聊发回给我们的 bot
4. 我们的 bot 收到后，从文件名提取用户ID，发给用户
"""
import asyncio

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from config import settings
from database import SessionLocal, get_config, set_config


# 全局小号客户端
userbot = None
# 我们 bot 的 ID（启动后自动获取）
bot_id = None
# 我们的 bot 实例（登录小号后自动传入）
bot_instance = None
# 防并发启动的锁：启动时和登录成功后都可能触发 start_userbot，串行化避免竞态
_userbot_lock: asyncio.Lock | None = None
# 当前心跳任务：重启客户端前先取消旧任务，避免任务无限累积
_heartbeat_task: asyncio.Task | None = None


def setup_handlers():
    """设置小号的事件处理器（只设置一次）。"""
    # ---------------- 1. 收到我们 bot 发的文件 → 转发到对应处理方 ----------------
    @userbot.on(events.NewMessage(from_users=bot_id, incoming=True))
    async def forward_to_handler(event):
        print(f"📥 收到来自 bot 的消息: {event.message.file.name if event.document else '文字'}")
        if not event.document:
            return

        caption = event.message.message or ""
        filename = event.message.file.name or "unnamed"

        # 洗数据：caption 是 im
        if caption.strip() == "im":
            with SessionLocal() as session:
                wash_chat_id = get_config(session, "wash_chat_id", "")
            if not wash_chat_id:
                print("⚠️ 未绑定洗数据群，无法转发")
                return

            try:
                chat_id = int(wash_chat_id)
                await event.forward_to(chat_id)
                print(f"✅ 已转发文件到洗数据群（{chat_id}）: {filename}")
            except Exception as e:
                print(f"❌ 转发到洗数据群失败: {e}")
            return

        # 空号检测：caption 是 phone_check
        if caption.strip() == "phone_check":
            print(f"📱 开始空号检测流程: {filename}")
            try:
                # 1. 把文件发给空号检测机器人
                await userbot.send_file(
                    "@haomamofang_bot",
                    event.message.document,
                    caption=filename,
                )
                print("✅ 已发送文件到空号检测机器人")

                # 2. 等待机器人返回菜单，点击「空号检测」按钮
                # 多等几次，确保菜单加载出来
                clicked_empty = False
                for retry in range(5):
                    await asyncio.sleep(2)
                    print(f"⏳ 第{retry+1}次尝试找空号检测按钮...")
                    async for msg in userbot.iter_messages("@haomamofang_bot", limit=10):
                        if msg.buttons:
                            for row in msg.buttons:
                                for btn in row:
                                    if "空号检测" in btn.text:
                                        await msg.click(data=btn.data)
                                        print(f"✅ 点击了按钮: {btn.text}")
                                        clicked_empty = True
                                        break
                                if clicked_empty:
                                    break
                        if clicked_empty:
                            break
                    if clicked_empty:
                        break

                if not clicked_empty:
                    print("❌ 没找到空号检测按钮")
                    return

                # 3. 等待下一步，点击「开始空号检测」
                clicked_start = False
                for retry in range(10):
                    await asyncio.sleep(3)
                    print(f"⏳ 第{retry+1}次尝试找开始空号检测按钮...")
                    async for msg in userbot.iter_messages("@haomamofang_bot", limit=10):
                        if msg.buttons:
                            for row in msg.buttons:
                                for btn in row:
                                    print(f"  按钮: {btn.text}")
                                    if "开始空号检测" in btn.text or "开始" in btn.text:
                                        await msg.click(data=btn.data)
                                        print(f"✅ 点击了按钮: {btn.text}")
                                        clicked_start = True
                                        break
                                if clicked_start:
                                    break
                        if clicked_start:
                            break
                    if clicked_start:
                        break

                if not clicked_start:
                    print("❌ 没找到开始空号检测按钮")
                    return

            except Exception as e:
                print(f"❌ 空号检测流程失败: {e}")
                import traceback
                traceback.print_exc()
            return

    # ---------------- 2. 监听回传的文件 → 转发回我们的 bot ----------------
    @userbot.on(events.NewMessage(incoming=True))
    async def forward_back_to_bot(event):
        with SessionLocal() as session:
            wash_chat_id = get_config(session, "wash_chat_id", "")

        chat_id_str = str(event.chat_id)
        chat_username = event.chat.username if event.chat else "unknown"
        filename = event.message.file.name if event.document else "无文件"
        caption = event.message.message or ""
        search_text = filename + " " + caption
        print(f"📥 收到消息: chat_id={chat_id_str}, username={chat_username}, 文件={filename}, caption={caption[:80]}")

        import re

        # 情况1：洗数据群回传
        if wash_chat_id:
            # 兼容正负号
            if chat_id_str == wash_chat_id or f"-100{chat_id_str}" == wash_chat_id:
                # 只处理带7位随机码的文件，不管在什么位置
                if event.document and re.search(r"[a-zA-Z0-9]{7}_", search_text):
                    try:
                        await event.forward_to(bot_id)
                        print(f"✅ 已把洗好的文件转发回给 bot: {search_text[:50]}")
                    except Exception as e:
                        print(f"❌ 转发文件失败: {e}")
                return

        # 情况2：空号检测机器人回传（@haomamofang_bot）
        # 不管机器人 username 是什么，只要文件名里有"号码检测"就转发
        if event.document and "号码检测" in filename:
            print(f"📱 收到空号检测结果文件: {filename}")
            try:
                await event.forward_to(bot_id)
                print(f"✅ 已把空号检测结果转发回给 bot: {filename}")
            except Exception as e:
                print(f"❌ 转发空号检测结果失败: {e}")
            return


async def start_userbot(bot=None):
    """启动小号客户端。可以在 bot 运行过程中调用，自动启动。

    用锁串行化并发调用（启动时 + 登录成功后都可能触发），
    避免两个客户端实例同时连接、handler 重复注册。
    """
    global _userbot_lock
    if _userbot_lock is None:
        _userbot_lock = asyncio.Lock()
    async with _userbot_lock:
        await _start_userbot_inner(bot)


async def _start_userbot_inner(bot=None):
    """start_userbot 的锁内实现，不要直接调用。"""
    global userbot, bot_id, bot_instance, _heartbeat_task

    if bot:
        bot_instance = bot

    # 从数据库读会话
    with SessionLocal() as session:
        session_str = get_config(session, "alt_session", "")

    if not session_str or not settings.TELETHON_API_ID or not settings.TELETHON_API_HASH:
        print("⚠️ 未登录小号，洗数据中转未启动，请在管理面板登录")
        return

    # 如果已经在运行，先停止
    if userbot and userbot.is_connected():
        print("ℹ️ 小号客户端已在运行，先停止旧的")
        await userbot.disconnect()

    # 获取我们 bot 的 ID
    if not bot_id and bot_instance:
        me = await bot_instance.get_me()
        bot_id = me.id
        print(f"✅ 我们的 bot ID: {bot_id}")

    # 用数据库里的会话字符串登录
    userbot = TelegramClient(StringSession(session_str), settings.TELETHON_API_ID, settings.TELETHON_API_HASH)
    setup_handlers()

    await userbot.start()

    # 把小号的用户名和 user_id 存到数据库
    me = await userbot.get_me()
    with SessionLocal() as session:
        set_config(session, "alt_username", me.username or "")
        set_config(session, "alt_user_id", str(me.id))
        set_config(session, "alt_status", "online")

    print(f"✅ 小号客户端已启动，洗数据中转已开启... 小号用户名: @{me.username}（ID: {me.id}）")

    # 心跳检测：每2分钟检查一次是否在线，断线自动重连
    async def heartbeat():
        while True:
            await asyncio.sleep(120)  # 2分钟
            try:
                if not userbot:
                    continue

                if not userbot.is_connected():
                    with SessionLocal() as session:
                        set_config(session, "alt_status", "offline")
                    print("⚠️ 小号心跳检测：已断开，正在重连...")
                    try:
                        await userbot.connect()
                        # 验证是否真的登录了
                        me = await userbot.get_me()
                        if me:
                            with SessionLocal() as session:
                                set_config(session, "alt_status", "online")
                                set_config(session, "alt_username", me.username or "")
                                set_config(session, "alt_user_id", str(me.id))
                            print(f"✅ 小号已重新连接成功: @{me.username}")
                    except Exception as e:
                        print(f"❌ 小号重连失败：{e}")
                        # 重连失败，标记离线
                        with SessionLocal() as session:
                            set_config(session, "alt_status", "offline")
            except Exception as e:
                print(f"⚠️ 小号心跳检测异常：{e}")

    # 重启客户端前先取消旧的心跳任务，防止任务无限累积
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
    _heartbeat_task = asyncio.create_task(heartbeat())


async def stop_userbot():
    """停止小号客户端（删除小号时调用）。"""
    global userbot, _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        _heartbeat_task = None
    if userbot and userbot.is_connected():
        await userbot.disconnect()
        userbot = None
        with SessionLocal() as session:
            set_config(session, "alt_status", "offline")
        print("✅ 小号客户端已停止")
