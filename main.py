import asyncio
import re
import json
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError, FloodWaitError, SessionPasswordNeededError
from config import Config
from text import Text
from prompt import Prompt
from persona_manager import persona_manager
from gemini_engine import gemini
from pal_manager import pal_manager
from assistant_manager import assistant_manager
from memory_manager import memory_manager
from typing_helper import ContinuousTyping, calculate_human_typing_delay
from time_utils import get_current_persian_datetime
from media_processor import media_processor
from reminder_manager import reminder_manager
from reminder_parser import reminder_parser, AI_PARSE_PROMPT, extract_json_block
from sticker_manager import sticker_manager
from health_server import start_health_server
from notifier import notifier
from web_search import web_search, format_context
import random

client = (
    TelegramClient(StringSession(Config.SESSION_STRING), Config.API_ID, Config.API_HASH)
    if Config.SESSION_STRING
    else TelegramClient(Config.SESSION_NAME, Config.API_ID, Config.API_HASH)
)
my_info = None

# Scope keywords accepted by commands that can target every chat at once.
ALL_SCOPE_WORDS = ("all", "کل", "همه", "همه‌چت‌ها")


async def safe_delete(event):
    """Stealth-delete a command message; never raises."""
    try:
        await event.delete()
    except Exception:
        pass


async def confirm(text: str):
    """Stealth feedback: goes to Saved Messages and auto-deletes there."""
    await notifier.confirm(text)


def is_owner(event) -> bool:
    """Strict check to ensure commands only run for the owner (outgoing messages from this account)."""
    return bool(event and event.out)


async def get_response(user_message: str, system_prompt: str = None, is_json: bool = False, parts=None, use_search: bool = False) -> str:
    if system_prompt is None:
        system_prompt = persona_manager.get_prompt("normal")
    return await gemini.get_response(user_message, system_prompt, is_json=is_json, parts=parts, use_search=use_search)

async def format_sender_name(sender, my_id: int) -> str:
    if not sender:
        return Text.UNKNOWN_SENDER
    if sender.id == my_id:
        return Text.ME_LABEL
    if hasattr(sender, 'first_name') and sender.first_name:
        name = sender.first_name
        if hasattr(sender, 'last_name') and sender.last_name:
            name += f" {sender.last_name}"
        return name
    if hasattr(sender, 'title') and sender.title:
        return sender.title
    return Text.UNKNOWN_SENDER

async def get_recent_chat_history(chat_id: int, limit: int = None, include_id: bool = False) -> str:
    """Fetches up to 30 recent messages with smart long-message segmentation and reset cutoff."""
    global my_info
    my_id = my_info.id if my_info else Config.OWNER_ID
    if limit is None:
        limit = Config.SHORT_TERM_MEMORY_LIMIT
    return await memory_manager.get_chat_history(client, chat_id, format_sender_name, my_id, limit=limit, include_id=include_id)


# ==========================================================
# 📜 COMMAND: راهنما / HELP (888)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^888$'))
async def help_handler(event):
    if not is_owner(event):
        return
    await event.edit(Text.HELP)

# ==========================================================
# 🤖 COMMAND: روشن کردن رفیق (PAL ON / 777)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^777(?:\s+(?!engage\b)(.+))?$'))
async def pal_on_handler(event):
    if not is_owner(event):
        return
        
    mode_arg = event.pattern_match.group(1)
    if mode_arg:
        mode = mode_arg.strip().lower()
    else:
        mode = "normal"
        
    chat_id = event.chat_id
    pal_manager.activate(chat_id, mode=mode)
    
    # Instant stealth delete + confirmation in Saved Messages
    await safe_delete(event)
    await confirm(f"🔮 رفیق ({mode.upper()} Mode) برای چت `{chat_id}` فعال شد.")
    print(f"🔮 Stealth Pal ({mode.upper()} Mode) ACTIVATED for chat {chat_id}")

# ==========================================================
# 💤 COMMAND: خاموش کردن رفیق (PAL OFF / 000)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^000(?:\s+(all))?$'))
async def pal_off_handler(event):
    if not is_owner(event):
        return
    
    scope_arg = event.pattern_match.group(1)
    if scope_arg == "all":
        count = pal_manager.deactivate_all()
        print(f"💤 Stealth Pal DEACTIVATED globally for all {count} chats")
        await confirm(f"💤 رفیق به‌صورت سراسری در {count} چت خاموش شد.")
    else:
        chat_id = event.chat_id
        pal_manager.deactivate(chat_id)
        print(f"💤 Stealth Pal DEACTIVATED for chat {chat_id}")
        await confirm(f"💤 رفیق در چت `{chat_id}` خاموش شد.")
        
    # Instant stealth delete
    await safe_delete(event)

# ==========================================================
# 🕵️ COMMAND: پراکنش / تعامل خودکار (AUTO ENGAGE ON / 777 engage)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^777\s+engage(?:\s+(\d+|auto))?$'))
async def auto_engage_on_handler(event):
    if not is_owner(event):
        return
    
    chat_id = event.chat_id
    val = event.pattern_match.group(1)

    if val == "auto":
        # Smart mode: measure chat speed and target ~5% bot presence
        await confirm("⏳ در حال سنجش سرعت چت برای تنطیم هوشمند زمان تعامل...")
        duration = await calculate_dynamic_engage_duration(chat_id)
    else:
        duration = int(val) if val else 20
        if duration < 1:
            duration = 1
    
    pal_manager.activate_auto_engage(chat_id, duration)
    await safe_delete(event)
    await confirm(f"🕵️ Auto-Engage در چت `{chat_id}` فعال شد (هر ~{duration} دقیقه).")
    print(f"🕵️ Auto-Engage (Lurker) ACTIVATED for chat {chat_id} with duration {duration}m")


async def calculate_dynamic_engage_duration(chat_id) -> int:
    """Calculates the best auto-engage duration based on chat speed (targeting ~5% bot presence)."""
    try:
        messages = await client.get_messages(chat_id, limit=50)
        if len(messages) < 10:
            return 20  # Fallback if there are too few messages

        oldest_msg = messages[-1]
        newest_msg = messages[0]

        timespan_minutes = (newest_msg.date - oldest_msg.date).total_seconds() / 60.0
        if timespan_minutes <= 0:
            return 2

        mins_per_msg = timespan_minutes / len(messages)

        # Target: 1 bot message for every ~20 human messages (5% presence)
        target_duration = int(mins_per_msg * 20)

        # Clamp between 2 and 120 minutes
        return max(2, min(target_duration, 120))
    except Exception as e:
        print(f"⚠️ Error calculating dynamic duration for {chat_id}: {e}")
        return 20  # Safe fallback

# ==========================================================
# 🛑 COMMAND: خاموش کردن تعامل (AUTO ENGAGE OFF / 777 engage off)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^777\s+engage\s+off(?:\s+(all))?$'))
async def auto_engage_off_handler(event):
    if not is_owner(event):
        return
        
    scope = event.pattern_match.group(1)
    if scope == "all":
        count = pal_manager.deactivate_all_engages()
        print(f"🛑 Auto-Engage DEACTIVATED globally for all {count} chats")
        await confirm(f"🛑 Auto-Engage سراسری در {count} چت خاموش شد.")
    else:
        chat_id = event.chat_id
        pal_manager.deactivate_auto_engage(chat_id)
        print(f"🛑 Auto-Engage DEACTIVATED for chat {chat_id}")
        await confirm(f"🛑 Auto-Engage در چت `{chat_id}` خاموش شد.")
        
    await safe_delete(event)

# ==========================================================
# 💼 COMMAND: روشن کردن دستیار شخصی (ASSISTANT ON / 666)
#   666      -> فقط همین چت
#   666 all  -> تمام پیوی‌ها
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^666(?:\s+(\S+))?$'))
async def assistant_on_handler(event):
    if not is_owner(event):
        return
    chat_id = event.chat_id
    scope_arg = (event.pattern_match.group(1) or "").strip().lower()

    if scope_arg in ALL_SCOPE_WORDS:
        assistant_manager.activate_all(chat_id=chat_id)
        await safe_delete(event)
        await confirm("💼 دستیار شخصی برای تمام پیوی‌ها (فعلی و آینده) فعال شد.")
        print(f"💼 Universal Assistant Mode ACTIVATED for all DMs (un-muted {chat_id})")
    elif scope_arg:
        # Unknown argument: do not silently enable a wider scope than intended.
        await safe_delete(event)
        await confirm("⚠️ دستور را نفهمیدم. `666` برای همین چت، `666 all` برای تمام چت‌ها.")
        print(f"⚠️ Unknown 666 scope argument: {scope_arg!r}")
    else:
        assistant_manager.activate_chat(chat_id)
        await safe_delete(event)
        await confirm(f"💼 دستیار شخصی فقط در چت `{chat_id}` فعال شد (برای تمام چت‌ها: `666 all`).")
        print(f"💼 Assistant Mode ACTIVATED only for chat {chat_id}")


# ==========================================================
# 🛑 COMMAND: خاموش کردن یا توقف دستیار شخصی (ASSISTANT OFF / 444)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^444(?:\s+(\S+))?$'))
async def assistant_off_handler(event):
    if not is_owner(event):
        return
    chat_id = event.chat_id
    scope_arg = (event.pattern_match.group(1) or "").strip().lower()
    
    if scope_arg in ALL_SCOPE_WORDS:
        assistant_manager.deactivate_global()
        print(f"🛑 Universal Assistant Mode DEACTIVATED globally for all DMs")
        await confirm("🛑 دستیار شخصی در تمام چت‌ها خاموش شد.")
    else:
        # Default behavior: Stop assistant ONLY in this specific chat
        assistant_manager.mute_chat(chat_id)
        print(f"🤫 Assistant MUTED only in chat {chat_id} (All other DMs remain active)")
        await confirm(f"🤫 دستیار فقط در چت `{chat_id}` متوقف شد (سایر پیوی‌ها فعال ماندند).")
        
    await safe_delete(event)

# ==========================================================
# 🚫 COMMAND: لیست سیاه دستیار (!مسدود / !آزاد روی پیام مخاطب)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^!(مسدود|آزاد|blacklist|unblock)\b'))
async def blacklist_handler(event):
    if not is_owner(event):
        return
    action = event.pattern_match.group(1)
    target_user_id = None

    reply_msg = await event.get_reply_message() if event.is_reply else None
    if reply_msg and reply_msg.sender_id:
        target_user_id = reply_msg.sender_id
    else:
        # Allow explicit ID: !مسدود 123456
        m = re.search(r'\d{4,}', event.raw_text or "")
        if m:
            target_user_id = int(m.group(0))

    await safe_delete(event)
    if not target_user_id:
        await confirm("⚠️ روی پیام مخاطب ریپلای کن یا ID بده: `!مسدود 123456`")
        return

    if action in ("مسدود", "blacklist"):
        assistant_manager.blacklist_add(target_user_id)
        print(f"🚫 Blacklisted user {target_user_id}")
        await confirm(Text.BLACKLIST_ADDED.format(user_id=target_user_id))
    else:
        assistant_manager.blacklist_remove(target_user_id)
        print(f"✅ Un-blacklisted user {target_user_id}")
        await confirm(Text.BLACKLIST_REMOVED.format(user_id=target_user_id))

# ==========================================================
# 🎭 COMMAND: آموزش استیکر (!استیکر <توضیح> روی پیام استیکر)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^!استیکر(?:\s+(.+))?$'))
async def sticker_teach_handler(event):
    if not is_owner(event):
        return
    meaning = (event.pattern_match.group(1) or "").strip()
    reply_msg = await event.get_reply_message() if event.is_reply else None

    await safe_delete(event)

    # List mode
    if meaning in ("لیست", "list", ""):
        listing = sticker_manager.list_known()
        if listing:
            await confirm(f"🎭 استیکرهای آموخته‌شده:\n\n{listing}")
        else:
            await confirm("🎭 هنوز هیچ استیکری آموزش داده نشده.\nروی یک استیکر ریپلای کن و بنویس: `!استیکر <توضیح>`")
        return

    if not reply_msg or getattr(reply_msg, "sticker", None) is None:
        await confirm("⚠️ باید روی پیامِ **استیکر** ریپلای کنی.")
        return

    if meaning in ("حذف", "remove", "پاک"):
        removed = sticker_manager.unteach(reply_msg)
        if removed:
            print(f"🎭 Sticker un-taught: {removed[:50]}")
            await confirm(f"🗑 استیکر از پول ارسال حذف شد ({removed[:60]}).")
        else:
            await confirm("⚠️ این استیکر توی پول ارسال نبود.")
        return

    info = sticker_manager.sticker_info(reply_msg)
    fid = info[0]
    newly = sticker_manager.teach(reply_msg, meaning)
    sticker_manager.remember_document(client, reply_msg)
    emoji = info[1]["emoji"] if info and info[1] else ""
    print(f"🎭 Sticker {'taught' if newly else 'updated'}: {emoji} -> {meaning[:60]}")
    await confirm(f"🎭 یاد گرفتم! {emoji} یعنی: {meaning[:100]}")

# ==========================================================
# 📊 COMMAND: وضعیت (STATUS / 555)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^555$'))
async def status_handler(event):
    if not is_owner(event):
        return
    
    is_pal = pal_manager.is_active(event.chat_id)
    is_engage = pal_manager.is_auto_engage_active(event.chat_id)
    pal_count = pal_manager.get_active_count()
    engage_count = pal_manager.get_auto_engage_count()
    
    pal_status = Text.PAL_STATUS_ACTIVE if is_pal else Text.PAL_STATUS_INACTIVE
    engage_status = "🟢 **وضعیت تعامل خودکار:** در این چت **فعال** است." if is_engage else "⚪ **وضعیت تعامل خودکار:** در این چت **غیرفعال** است."
    
    if event.chat_id in assistant_manager.muted_chats:
        ast_status = "🟡 **دستیار در این چت:** 🤫 **متوقف شده** (برای سایر پیوی‌ها همچنان فعال است)"
    elif assistant_manager.dm_enabled:
        ast_status = "🟢 **دستیار شخصی (666 all):** برای **تمام پیوی‌ها (مخاطبان فعلی و آینده)** فعال است."
    elif event.chat_id in assistant_manager.enabled_chats:
        ast_status = "🟢 **دستیار شخصی (666):** فقط در **همین چت** فعال است."
    else:
        ast_status = "⚪ **دستیار شخصی (666):** در این چت **غیرفعال** است."
    
    ast_count_note = f"\n💼 تعداد چت‌های فعال دستیار (666): `{len(assistant_manager.enabled_chats)}`" if assistant_manager.enabled_chats else ""
    bl_note = f"\n🚫 تعداد کاربران مسدود: `{len(assistant_manager.blacklist)}`" if assistant_manager.blacklist else ""
    
    report = (
        f"📊 **گزارش وضعیت هوش مصنوعی:**\n\n"
        f"{pal_status}\n"
        f"📱 تعداد چت‌های فعال برای رفیق (777): `{pal_count}`\n\n"
        f"{engage_status}\n"
        f"🕵️ تعداد چت‌های فعال تعامل خودکار (engage): `{engage_count}`\n\n"
        f"{ast_status}{ast_count_note}{bl_note}"
    )
    msg = await event.edit(report)
    await asyncio.sleep(4)
    try:
        await msg.delete()
    except Exception:
        pass


# ==========================================================
# 🧠 COMMAND: ریست حافظه کوتاه‌مدت (RESET MEMORY / 333)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^333$'))
async def reset_memory_handler(event):
    if not is_owner(event):
        return
    chat_id = event.chat_id
    memory_manager.reset_chat_memory(chat_id)
    # Instant stealth delete
    await safe_delete(event)
    await confirm(f"🧠 حافظه چت `{chat_id}` ریست شد.")
    print(f"🧠 Short-term memory RESET for chat {chat_id}")

# ==========================================================
# 🧹 COMMAND: پاکسازی پیام‌های من (GHOST PURGE / 999)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^999(?:\s+(\d+))?$'))
async def purge_handler(event):
    if not is_owner(event):
        return
    
    limit_arg = event.pattern_match.group(1)
    limit = int(limit_arg) if limit_arg else None
    chat_id = event.chat_id
    
    # Instant delete trigger message for stealth
    trigger_id = event.id
    await safe_delete(event)
    
    global my_info
    my_id = my_info.id if my_info else (await client.get_me()).id
    
    deleted_count = 0
    message_ids = []
    
    try:
        input_chat = await event.get_input_chat()
        # If no limit specified, limit is None (searches entire history without cap)
        search_limit = limit
        
        async for msg in client.iter_messages(input_chat, limit=search_limit):
            if msg.id == trigger_id:
                continue
            
            # Check if message is sent by me (supporting all chat and supergroup types)
            is_mine = False
            if msg.out:
                is_mine = True
            elif msg.sender_id and msg.sender_id == my_id:
                is_mine = True
            elif hasattr(msg, 'from_id') and getattr(msg.from_id, 'user_id', None) == my_id:
                is_mine = True
            
            if is_mine:
                message_ids.append(msg.id)
            
            # Delete in batches of 50
            if len(message_ids) >= 50:
                deleted_count += await _delete_batch(input_chat, message_ids)
                message_ids = []
                await asyncio.sleep(0.2)
        
        # Delete remaining messages
        if message_ids:
            deleted_count += await _delete_batch(input_chat, message_ids)
            
        print(f"🧹 Stealth Purged {deleted_count} messages from chat {chat_id}")
        await confirm(f"🧹 {deleted_count} پیام شما در این چت پاکسازی شد.")
    except Exception as e:
        print(f"⚠️ Purge error in chat {chat_id}: {e}")
        await notifier.error("purge", str(e))


async def _delete_batch(input_chat, message_ids) -> int:
    """Deletes a batch of messages handling FloodWait, falls back to one-by-one."""
    try:
        await client.delete_messages(input_chat, message_ids, revoke=True)
        return len(message_ids)
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds + 1, 120))
        try:
            await client.delete_messages(input_chat, message_ids, revoke=True)
            return len(message_ids)
        except Exception:
            pass
    except Exception:
        pass
    # Batch failed → delete individually (e.g. >48h old in non-admin groups)
    count = 0
    for mid in message_ids:
        try:
            await client.delete_messages(input_chat, [mid], revoke=True)
            count += 1
        except Exception:
            pass
    return count

# ==========================================================
# 💬 COMMAND: پاسخ هوشمند سفارشی (SMART SPEAK / 111)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^111(?:\s+(.*))?$'))
async def custom_ask_handler(event):
    if not is_owner(event):
        return
    
    user_instruction = (event.pattern_match.group(1) or "").strip()
    reply_to_id = event.reply_to_msg_id
    chat_id = event.chat_id
    
    # Delete the command message instantly to keep it stealth
    await safe_delete(event)
    
    if not user_instruction and not reply_to_id:
        return
    
    history_text = await get_recent_chat_history(chat_id)
    target_text = ""
    sender_name = "مخاطب"
    
    if reply_to_id:
        reply_msg = await event.get_reply_message()
        if reply_msg:
            target_text = reply_msg.text or Text.NO_TEXT
            sender = await reply_msg.get_sender()
            sender_name = await format_sender_name(sender, my_info.id if my_info else Config.OWNER_ID)
    
    now_persian = get_current_persian_datetime()
    ltm = memory_manager.get_long_term_summary(chat_id)
    ltm_context = f"\n[خلاصه سوابق مهم قبلی]:\n{ltm}\n" if ltm else ""
    
    prompt_input = Prompt.ASK_TEMPLATE.format(
        current_time=now_persian,
        long_term_context=ltm_context,
        history_text=history_text,
        sender=sender_name,
        target_text=target_text or "گفت‌وگوی جاری",
        user_instruction=user_instruction or "پاسخ طبیعی، خودمونی و مناسب بده.",
        persona_name=Config.PERSONA_NAME
    )
    
    input_chat = await event.get_input_chat()
    async with ContinuousTyping(client, input_chat):
        response = await get_response(prompt_input, persona_manager.get_prompt("normal"))
        if response and response != Text.ERROR:
            human_typing_time = calculate_human_typing_delay(response)
            await asyncio.sleep(human_typing_time)
            await client.send_message(input_chat, response, reply_to=reply_to_id)
            print(f"⚡ Handled 111 in chat {chat_id}")
            # Record message for rolling long-term memory summary check
            memory_manager.record_message_and_check_summary(client, chat_id, gemini, format_sender_name, my_info.id if my_info else Config.OWNER_ID)
        else:
            await notifier.error("111", "پاسخ AI دریافت نشد")


# ==========================================================
# 🚀 INCOMING: پردازش پیام‌های دریافتی (PAL & ASSISTANT MODES)
# ==========================================================

# Concurrency management to prevent API spam and overlapping replies
chat_locks = {}
chat_latest_msg = {}

def get_chat_lock(chat_id):
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]


def _pick_sticker_for_context(context_text: str):
    """Chooses the best taught sticker for the conversation context (keyword overlap)."""
    from sticker_manager import sticker_manager
    # pick_best is sync (no IO); call directly
    doc = sticker_manager.pick_best(client, context_text)
    return doc

@client.on(events.NewMessage(incoming=True))
async def incoming_message_handler(event):
    chat_id = event.chat_id
    
    global my_info
    my_id = my_info.id if my_info else Config.OWNER_ID
    
    # Ignore messages from myself
    if event.out or event.sender_id == my_id:
        return

    # Ignore messages from other bots to prevent endless AI-to-AI loops
    try:
        sender = await event.get_sender()
        if sender and getattr(sender, 'bot', False):
            return
    except Exception:
        pass

    # 🚫 Assistant blacklist: this user must never receive automated replies
    if assistant_manager.is_blacklisted(event.sender_id):
        return

    # Determine active mode: Pal Mode has precedence for specifically activated chats
    if pal_manager.is_active(chat_id):
        mode = "pal"
    elif assistant_manager.is_active_for_chat(chat_id, is_private=event.is_private):
        mode = "assistant"
    else:
        # Neither mode is active for this chat
        return
    
    # For group chats: only respond if replied to me, or mentioned
    if event.is_group or event.is_channel:
        is_reply_to_me = False
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg:
                if reply_msg.out or reply_msg.sender_id == my_id or getattr(reply_msg.from_id, 'user_id', None) == my_id:
                    is_reply_to_me = True
        
        is_mentioned = False
        raw_lower = (event.raw_text or "").lower()
        if my_info and my_info.username and f"@{my_info.username.lower()}" in raw_lower:
            is_mentioned = True
        if my_info and my_info.first_name and my_info.first_name.lower() in raw_lower:
            is_mentioned = True
        if Config.PERSONA_NAME and Config.PERSONA_NAME in (event.raw_text or ""):
            is_mentioned = True
                
        # If it's a group, only reply if directly addressed or explicitly mentioned/replied
        if not (is_reply_to_me or is_mentioned):
            return

    # Check incoming content
    incoming_text = event.text or ""

    # 🎭 Sticker handling: taught stickers are understood (and remembered for re-send)
    sticker_desc = None
    if getattr(event, "sticker", None) is not None:
        from sticker_manager import sticker_manager
        sticker_manager.remember_document(client, event)
        sticker_desc = sticker_manager.describe_for_prompt(event)
        incoming_text = f"[{sticker_desc}]" if sticker_desc else "[استیکر]"

    # 👁️🎙️🎬 Media support: photos, voice notes & video notes are understood even without caption
    media_part = None
    has_supported_media = bool(
        getattr(event, "photo", None)
        or getattr(event, "voice", None)
        or getattr(event, "audio", None)
        or getattr(event, "video", None)
        or getattr(event, "video_note", None)
    )
    if has_supported_media:
        media_part = await media_processor.build_part(client, event)
        if media_part is None:
            # Media exists but download failed/unsupported → treat as text-only message
            if not incoming_text.strip():
                return
    elif not incoming_text.strip():
        # Other media types (files etc.) without caption → skip
        return

    media_note = ""
    if media_part is not None:
        kind = media_processor.describe_media(event)
        media_note = f"\n(مخاطب {kind} فرستاده است"
        if not incoming_text.strip():
            media_note += " بدون توضیح متنی"
        else:
            media_note += f" با این توضیح: {incoming_text[:200]}"
        media_note += ". محتوای آن در ادامه به‌صورت داده چندرسانه‌ای ضمیمه شده است)\n"

    # Track the latest message ID for this chat to debounce rapid spam
    chat_latest_msg[chat_id] = event.id

    # Natural reading delay proportional to incoming text length (plus a bit of random jitter)
    base_reading_time = max(1.0, len(incoming_text) * 0.04) # e.g. 50 chars = 2 seconds reading
    reading_delay = min(base_reading_time, 8.0) # max 8 seconds reading time
    await asyncio.sleep(random.uniform(reading_delay, reading_delay + 1.0))
    
    lock = get_chat_lock(chat_id)
    async with lock:
        # If a newer message arrived from this chat while we were waiting/processing,
        # skip this event. The newer event's handler will process the combined history!
        if chat_latest_msg.get(chat_id, 0) > event.id:
            return

        input_chat = await event.get_input_chat()
        
        # Mark messages as read naturally
        try:
            await client.send_read_acknowledge(input_chat, max_id=event.id)
        except Exception:
            pass

        # Start continuous typing immediately at the top of the chat (DMs and groups)
        async with ContinuousTyping(client, input_chat):
            # Gather history, long-term memory, and sender info
            sender = await event.get_sender()
            sender_name = await format_sender_name(sender, my_id)
            history_text = await get_recent_chat_history(chat_id)
            now_persian = get_current_persian_datetime()
            ltm = memory_manager.get_long_term_summary(chat_id)
            ltm_context = f"\n[خلاصه سوابق مهم قبلی]:\n{ltm}\n" if ltm else ""
            
            if mode == "pal":
                pal_variant = pal_manager.get_mode(chat_id)
                prompt_input = Prompt.AUTOPILOT_TEMPLATE.format(
                    current_time=now_persian,
                    long_term_context=ltm_context,
                    history_text=history_text,
                    sender=sender_name,
                    target_text=(media_note + incoming_text) if media_note else incoming_text,
                    persona_name=Config.PERSONA_NAME
                )
                system_prompt = persona_manager.get_prompt(pal_variant)
                print(f"🤖 Pal Autopilot ({pal_variant.upper()}) thinking & typing for chat {chat_id} (from {sender_name})...")
            else:
                prompt_input = Prompt.ASSISTANT_TEMPLATE.format(
                    current_time=now_persian,
                    long_term_context=ltm_context,
                    history_text=history_text,
                    sender=sender_name,
                    target_text=(media_note + incoming_text) if media_note else incoming_text,
                    persona_name=Config.PERSONA_NAME
                )
                system_prompt = persona_manager.get_prompt("assistant")
                print(f"💼 Personal Assistant thinking & typing for chat {chat_id} (from {sender_name})...")
            
            # Pass the photo/voice/video bytes alongside the prompt when present
            parts = [media_part] if media_part is not None else None

            # 🎭 Sticker awareness: tell the model it MAY answer with a taught sticker
            from sticker_manager import sticker_manager
            known_list = sticker_manager.list_known(limit=12)
            if mode == "pal" and (sticker_desc or known_list):
                sticker_hint = "\n\n[🎭 استیکرها: "
                if known_list:
                    sticker_hint += f"استیکرهای زیر رو یاد گرفتی و می‌تونی هر وقت به‌جا بود باهاشون جواب بدی:\n{known_list}\n"
                if sticker_desc:
                    sticker_hint += f"مخاطب الان این استیکر رو فرستاده: {sticker_desc}."
                sticker_hint += ("\nاگر خواستی جواب‌ت فقط یک استیکر باشه، به‌جای متن، دقیقاً بنویس: [استیکر]\n"
                                 "وگرنه مثل همیشه متنی جواب بده.]")
                prompt_input = prompt_input + sticker_hint
            elif mode == "pal" and not incoming_text.strip():
                pass  # unreachable, kept for clarity

            response = await get_response(prompt_input, system_prompt, parts=parts)
            
            if response and response != Text.ERROR:
                human_typing_time = calculate_human_typing_delay(response)
                await asyncio.sleep(human_typing_time)
                
                reply_target = event.id if (event.is_group or event.is_channel) else None
                try:
                    # 🎭 Sticker reply: model answered with the sticker marker → send best taught sticker
                    def _norm_sticker_reply(s: str) -> str:
                        s = re.sub(r'[\u200c\u200f\u200e\ufeff]', '', s or "")
                        s = s.translate(str.maketrans({'ي': 'ی', 'ك': 'ک', 'ى': 'ی'}))
                        return re.sub(r'\[\s*استیکر\s*\]', '[استیکر]', s.strip())

                    if _norm_sticker_reply(response) == "[استیکر]":
                        doc = await sticker_manager.resolve_document_async(client, _pick_sticker_for_context(history_text + " " + incoming_text))
                        if doc is not None:
                            await client.send_file(input_chat, doc, reply_to=reply_target)
                            print(f"🎭 Replied with a taught sticker in chat {chat_id}")
                            memory_manager.record_message_and_check_summary(client, chat_id, gemini, format_sender_name, my_id)
                            return
                        # Could not resolve any sticker → NEVER send the raw marker.
                        # Re-ask the model for a plain text answer (sticker option removed).
                        print("🎭 Marker received but no resolvable sticker; re-asking as text...")
                        prompt_input = prompt_input + "\n\n[مهم: استیکری در دسترس نیست. حتماً متنی و طبیعی جواب بده.]"
                        response = await get_response(prompt_input, system_prompt, parts=parts)
                        if not response or response == Text.ERROR or _norm_sticker_reply(response) == "[استیکر]":
                            response = "..."
                    await client.send_message(input_chat, response, reply_to=reply_target)
                except FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds + 1, 300))
                    await client.send_message(input_chat, response, reply_to=reply_target)
                if mode == "pal":
                    print(f"✅ Pal replied naturally in chat {chat_id}")
                else:
                    print(f"✅ Assistant replied politely in chat {chat_id}")
                    
                # Record message for rolling long-term memory summary check
                memory_manager.record_message_and_check_summary(client, chat_id, gemini, format_sender_name, my_id)
            elif response == Text.ERROR:
                print(f"⚠️ AI error for chat {chat_id}; notifying owner")
                await notifier.error("auto-reply", f"پاسخ هوش مصنوعی برای چت {chat_id} ناموفق بود")


# ==========================================================
# 📋 COMMAND: خلاصه چت (CATCH-UP SUMMARY / 222)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^222(?:\s+(\d+))?$'))
async def catchup_summary_handler(event):
    if not is_owner(event):
        return

    hours_arg = event.pattern_match.group(1)
    hours = int(hours_arg) if hours_arg else 12
    if hours < 1:
        hours = 1
    if hours > 72:
        hours = 72
    chat_id = event.chat_id

    await safe_delete(event)

    global my_info
    my_id = my_info.id if my_info else Config.OWNER_ID
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    lines = []
    try:
        async for msg in client.iter_messages(chat_id, limit=200):
            msg_ts = msg.date.replace(tzinfo=timezone.utc)
            if msg_ts < since:
                break
            if not msg or not msg.text:
                continue
            sender = await msg.get_sender()
            name = await format_sender_name(sender, my_id)
            time_str = msg.date.strftime("%H:%M")
            content = memory_manager.truncate_segment(msg.text, 150)
            lines.append(f"[{time_str}] {name}: {content}")
    except Exception as e:
        print(f"⚠️ Catch-up fetch error in chat {chat_id}: {e}")

    input_chat = await event.get_input_chat()
    if len(lines) < 3:
        await client.send_message(input_chat, "در این بازه پیام قابل‌توجهی نبود.")
        return

    history_text = "\n".join(reversed(lines))
    now_persian = get_current_persian_datetime()
    prompt = Prompt.CATCHUP_TEMPLATE.format(
        current_time=now_persian,
        hours=hours,
        history_text=history_text,
        persona_name=Config.PERSONA_NAME
    )

    system_prompt = persona_manager.get_prompt("normal")
    status = await client.send_message(input_chat, "در حال جمع‌بندی گفتگو...")
    async with ContinuousTyping(client, input_chat):
        summary = await get_response(prompt, system_prompt)
    try:
        await status.delete()
    except Exception:
        pass
    if summary and summary != Text.ERROR:
        await client.send_message(input_chat, f"📋 خلاصه {hours} ساعت اخیر:\n\n{summary}")
        print(f"📋 Catch-up summary sent for chat {chat_id} ({len(lines)} msgs)")
    else:
        await notifier.error("222", "خلاصه‌سازی گفتگو ناموفق بود")

# ==========================================================
# 🌐 COMMAND: جستجوی وب (WEB SEARCH / 112)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^112\s+(.+)$'))
async def web_search_handler(event):
    try:
        if not is_owner(event):
            return
        query = (event.pattern_match.group(1) or "").strip()
        chat_id = event.chat_id
        reply_to_id = event.reply_to_msg_id

        await safe_delete(event)

        if not query:
            return

        input_chat = await event.get_input_chat()

        # If replying to a message, include its content as context for the query
        context_note = ""
        if reply_to_id:
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.text:
                    context_note = f"\n[متن پیام ریپلای‌شده که سـؤال درباره آن است]:\n{reply_msg.text[:1000]}\n"
            except Exception:
                pass

        print(f"🌐 Web search requested in chat {chat_id}: {query[:60]}... [MARKER_e9def36]")
        async with ContinuousTyping(client, input_chat):
            # 1) Real web search (Bing scrape, no Google grounding tool needed)
            results, search_err = await web_search.search_async(query, max_results=5)
            if search_err or not results:
                err_msg = f"سرچ وب نتیجه‌ای نداد: {search_err or 'no results'}"
                print(f"⚠️ {err_msg}")
                await notifier.error("112", f"[v2] {err_msg}")
                return

            # 2) Feed real snippets to Gemini for a clean Farsi summary
            search_ctx = format_context(results)
            prompt = f"""سـؤال کاربر: {query}
{context_note}
نتایج جستجوی وب (واقعی):
{search_ctx}

با استفاده از نتایج بالا، پاسخ کوتاه، دقیق و خودمونی فارسی بده. اگه عدد/قیمتی هست دقیق بنویس. بدون ایموجی."""
            system_prompt = "تو دستیار شخصی هستی که با استفاده از نتایج جستجوی وب اطلاعات دقیق و به‌روز پیدا می‌کند. پاسخ فارسی، روان و کوتاه."

            response = await gemini.get_response(prompt, system_prompt, use_search=False)
            if response and response != Text.ERROR:
                human_typing_time = calculate_human_typing_delay(response)
                await asyncio.sleep(human_typing_time)
                await client.send_message(input_chat, response, reply_to=reply_to_id)
                print(f"🌐 Web answer sent in chat {chat_id}")
            else:
                last_err = getattr(gemini, "_last_error", None)
                err_detail = str(last_err) if last_err else "unknown (Gemini returned Text.ERROR)"
                print(f"⚠️ Web search summary failed: {err_detail[:300]}")
                await notifier.error("112", f"[v2] جستجو اوکی بود ولی خلاصه‌سازی شکست خورد:\n{err_detail[:300]}")
    except Exception as exc:
        err_msg = f"112 handler crashed: {type(exc).__name__}: {exc}"
        print(f"🚨 {err_msg}")
        try:
            await notifier.error("112", err_msg[:500])
        except Exception:
            pass

# ==========================================================
# ⏰ COMMAND: یادآور هوشمند (SMART REMINDER / 555 <دستور>)
# ==========================================================
@client.on(events.NewMessage(outgoing=True, pattern=r'^555\s+(.+)$'))
async def smart_reminder_handler(event):
    if not is_owner(event):
        return
    instruction = (event.pattern_match.group(1) or "").strip()
    chat_id = event.chat_id

    await safe_delete(event)
    if not instruction:
        return

    input_chat = await event.get_input_chat()

    # Step 1: deterministic parsing (fast + free)
    due_dt, text_rem = reminder_parser.parse(instruction)

    # Step 2: AI fallback for complex expressions
    if due_dt is None:
        now_str = get_current_persian_datetime()
        ai_prompt = AI_PARSE_PROMPT.format(text=instruction, now=now_str)
        raw = await gemini.get_response(
            ai_prompt,
            "تو یک پارسر زمان فارسی هستی. فقط و فقط JSON خروجی بده.",
            is_json=True
        )
        data = extract_json_block(raw or "")
        if data and data.get("due_time"):
            try:
                due_dt = datetime.strptime(str(data["due_time"]).strip(), "%Y-%m-%d %H:%M")
                text_rem = str(data.get("text") or instruction)[:300]
            except ValueError:
                due_dt = None
        if due_dt is None:
            await client.send_message(input_chat, "زمان یادآور رو متوجه نشدم. مثلاً بگو: 555 تا ۲ ساعت دیگه به علی بگو بیاد")
            return

    rem = reminder_manager.add_reminder(chat_id, due_dt, text_rem or instruction)
    local_due = datetime.fromtimestamp(rem["due_ts"], tz=reminder_manager._iran_tz())
    await client.send_message(
        input_chat,
        f"⏰ باشه، یادت می‌ندازم:\n«{(text_rem or instruction)[:200]}»\n🕒 موعده: {local_due.strftime('%H:%M')} ({local_due.strftime('%Y-%m-%d')})"
    )
    print(f"⏰ Reminder #{reminder_manager._next_id - 1} set for chat {chat_id} at {local_due}")

async def reminder_loop():
    """Background task that fires due reminders."""
    while True:
        try:
            await asyncio.sleep(15)
            for r in reminder_manager.pop_due():
                try:
                    input_chat = await client.get_input_entity(r["chat_id"])
                    await client.send_message(input_chat, f"⏰ یادآوری: {r['text']}")
                    print(f"⏰ Reminder fired for chat {r['chat_id']}: {r['text'][:50]}")
                except FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds + 1, 300))
                    try:
                        input_chat = await client.get_input_entity(r["chat_id"])
                        await client.send_message(input_chat, f"⏰ یادآوری: {r['text']}")
                    except Exception as e2:
                        print(f"⚠️ Reminder delivery failed after floodwait: {e2}")
                        await notifier.error("reminder", str(e2))
                except Exception as e:
                    print(f"⚠️ Reminder delivery failed: {e}")
                    await notifier.error("reminder", str(e))
        except Exception as e:
            print(f"⚠️ Reminder Loop Error: {e}")
            await asyncio.sleep(30)

auto_engage_schedule = {} # dict: chat_id -> (next_engage_timestamp, configured_duration_minutes)

async def auto_engage_loop():
    """Background task that manages auto-engage scheduling per chat."""
    global auto_engage_schedule
    while True:
        try:
            # Smart Dispatcher Loop: Wake up every 60 seconds
            await asyncio.sleep(60)
            
            global my_info
            if not my_info:
                continue
            my_id = my_info.id
            now_ts = datetime.now(timezone.utc).timestamp()
            
            # Iterate through configured auto-engage chats and their durations
            for chat_id, duration_minutes in list(pal_manager.auto_engage_chats.items()):
                schedule_data = auto_engage_schedule.get(chat_id)
                
                # If we don't have a schedule for this chat yet, OR if the duration changed!
                if not schedule_data or schedule_data[1] != duration_minutes:
                    # Initial delay is randomized safely
                    min_delay = min(2, duration_minutes * 0.5) * 60
                    max_delay = duration_minutes * 60
                    auto_engage_schedule[chat_id] = (now_ts + random.uniform(min_delay, max_delay), duration_minutes)
                    
                next_time, _ = auto_engage_schedule[chat_id]
                    
                # Is it time to engage for this specific chat?
                if now_ts < next_time:
                    continue # Not time yet
                    
                # IT'S TIME! Reschedule for the next cycle immediately
                next_delay = random.uniform(duration_minutes * 0.75, duration_minutes * 1.25) * 60
                auto_engage_schedule[chat_id] = (now_ts + next_delay, duration_minutes)
                
                try:
                    # Check if I have sent a message recently to avoid talking too much
                    recent_my_msgs = await client.get_messages(chat_id, limit=30, from_user="me")
                    if recent_my_msgs:
                        last_mine = recent_my_msgs[0].date.replace(tzinfo=timezone.utc).timestamp()
                        # If I spoke recently (relative to the configured duration), skip
                        if now_ts - last_mine < (duration_minutes * 60 * 0.75):
                            continue # I already talked recently, skip engaging.
                    
                    # Also, only engage if there is actually some recent conversation!
                    latest_msgs = await client.get_messages(chat_id, limit=1)
                    if not latest_msgs:
                        continue
                    last_msg_time = latest_msgs[0].date.replace(tzinfo=timezone.utc).timestamp()
                    # A chat is dead if no one spoke in 30 mins OR 1.5x the configured duration
                    dead_threshold = max(30 * 60, duration_minutes * 60 * 1.5)
                    if now_ts - last_msg_time > dead_threshold:
                        continue # Chat is dead, don't randomly talk to nobody.
                    
                    history_text = await get_recent_chat_history(chat_id, limit=30, include_id=True)
                    now_persian = get_current_persian_datetime()
                    ltm = memory_manager.get_long_term_summary(chat_id)
                    ltm_context = f"\n[خلاصه سوابق مهم قبلی]:\n{ltm}\n" if ltm else ""
                    
                    prompt_input = Prompt.AUTO_ENGAGE_TEMPLATE.format(
                        current_time=now_persian,
                        long_term_context=ltm_context,
                        history_text=history_text,
                        duration_minutes=duration_minutes,
                        persona_name=Config.PERSONA_NAME
                    )
                    
                    response = await get_response(prompt_input, persona_manager.get_prompt("normal"), is_json=True)
                    if not response or response == Text.ERROR:
                        continue
                        
                    try:
                        # Extract JSON block
                        json_match = re.search(r'\{.*\}', response, re.DOTALL)
                        if json_match:
                            data = json.loads(json_match.group(0))
                            target_id = data.get("selected_id")
                            reply_text = data.get("reply_text")
                            
                            if target_id is not None and str(target_id).lower() != "null" and reply_text:
                                try:
                                    target_id = int(target_id)
                                except (ValueError, TypeError):
                                    print(f"⚠️ Invalid target_id from AI: {target_id}")
                                    continue
                                
                                # Prevent the AI from replying to its own messages!
                                target_msg = None
                                try:
                                    target_msgs = await client.get_messages(chat_id, ids=[target_id])
                                    if target_msgs:
                                        target_msg = target_msgs[0]
                                except Exception:
                                    pass
                                    
                                if target_msg and (target_msg.sender_id == my_id or target_msg.out):
                                    print(f"⚠️ AI tried to reply to its own message ({target_id}). Ignoring!")
                                    continue
                                
                                # Prevent the AI from replying to other bots or blacklisted users!
                                if target_msg:
                                    try:
                                        target_sender = await target_msg.get_sender()
                                        if target_sender and getattr(target_sender, 'bot', False):
                                            print(f"⚠️ AI tried to reply to a bot ({target_id}). Ignoring!")
                                            continue
                                        if target_sender and assistant_manager.is_blacklisted(target_sender.id):
                                            print(f"⚠️ Auto-engage skipped blacklisted user ({target_id}).")
                                            continue
                                    except Exception:
                                        pass
                                
                                human_typing_time = calculate_human_typing_delay(reply_text)
                                input_chat = await client.get_input_entity(chat_id)
                                async with ContinuousTyping(client, input_chat):
                                    await asyncio.sleep(human_typing_time)
                                    await client.send_message(input_chat, reply_text, reply_to=target_id)
                                    print(f"🕵️ Auto-Engaged naturally in chat {chat_id}")
                                    memory_manager.record_message_and_check_summary(client, chat_id, gemini, format_sender_name, my_id)
                    except json.JSONDecodeError:
                        pass # Ignore if AI failed to output valid JSON
                        
                except Exception as e:
                    print(f"⚠️ Auto-Engage error in chat {chat_id}: {e}")
                    
        except Exception as e:
            print(f"⚠️ Auto-Engage Loop Error: {e}")
            await asyncio.sleep(60) # Sleep before retrying loop on fatal error

# ==========================================================
# 🌟 MAIN STARTUP
# ==========================================================
def main():
    global my_info

    # Fail fast on missing credentials instead of hanging on interactive login
    if not Config.SESSION_STRING and (not Config.API_ID or not Config.API_HASH):
        print("❌ API_ID/API_HASH تنطیم نشده است. در Railway حتماً SESSION_STRING ست کنید.")
        raise SystemExit(1)
    if Config.SESSION_STRING and (not Config.API_ID or not Config.API_HASH):
        print("❌ حتی با SESSION_STRING هم API_ID و API_HASH لازم است.")
        raise SystemExit(1)

    start_health_server()  # Railway: keep an HTTP port open so the container stays healthy
    try:
        client.start()
    except SessionPasswordNeededError:
        print("❌ حساب دو مرحله‌ای دارد؛ SESSION_STRING باید با تأیید رمز ساخته شود (gen_session_string.py).")
        raise SystemExit(1)
    except RuntimeError as e:
        print(f"❌ لاگین تلگرام ناموفق: {e}")
        print("   راه‌حل: لوکال «python3 gen_session_string.py» را اجرا کنید و خروجی را در SESSION_STRING بگذارید.")
        raise SystemExit(1)
    my_info = client.loop.run_until_complete(client.get_me())
    notifier.bind(client, my_info)
    
    # Start background loops
    client.loop.create_task(auto_engage_loop())
    client.loop.create_task(reminder_loop())
    
    print("=" * 50)
    print(f"👻 GhostGram (روح‌گرام) is ONLINE & READY!")
    print(f"👤 Logged in as: {my_info.first_name} (@{my_info.username}) [ID: {my_info.id}]")
    print(f"🧠 Model: {Config.MODEL_NAME}")
    print(f"💾 Data dir: {Config.PAL_STATE_FILE.rsplit('/', 1)[0] if '/' in Config.PAL_STATE_FILE else '(cwd)'}")
    print(f"📱 Active Pal Chats (777): {pal_manager.get_active_count()}")
    print(f"🕵️ Auto-Engage Chats (777 engage): {pal_manager.get_auto_engage_count()}")
    print(f"💼 Assistant Mode (666): {assistant_manager.status_summary()}")
    pending_rem = len(reminder_manager.list_pending())
    print(f"⏰ Pending reminders: {pending_rem}")
    print("🚀 Listening for secret codes (777, 777 engage, 666, 666 all, 444, 555 <یادآور>, 333, 999, 222 خلاصه, 112 جستجو, 111, 121 خواب, 122 رفتار انسانی, 888, !مسدود)...")
    print("=" * 50)
    
    client.run_until_disconnected()

if __name__ == '__main__':
    main()
