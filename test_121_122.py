#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for secret codes 121 (Quiet Hours), 122 (Human Behavior) and the
888 help command. No network / no Telegram login required.
State files are written to a temp dir (DATA_DIR) so the repo stays clean.
"""
import sys
import os
import io
import asyncio
import tempfile

# Isolate state files BEFORE importing any project module
_TMP_STATE = os.path.join(os.environ.get("TEMP", "/tmp"), "ghostali_test_state")
os.makedirs(_TMP_STATE, exist_ok=True)
os.environ["DATA_DIR"] = _TMP_STATE
# Dummy Telegram credentials so main.py's module-level client can be built (no login happens)
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "testhash")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import io as _io
sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


# ---------------------------------------------------------------------------
print("=== 1) 888 HELP text (text.py) contains 121 & 122 ===")
from text import Text

help_text = Text.HELP
check("HELP mentions code 121", "121" in help_text)
check("HELP mentions code 122", "122" in help_text)
check("HELP has Quiet Hours section", "ساعات خواب" in help_text)
check("HELP has Human Behavior section", "رفتار انسانی" in help_text)
check("HELP documents 121 sleep", "121 sleep 90" in help_text)
check("HELP documents 121 tz", "121 tz +03:30" in help_text)
check("HELP documents 121 allow", "121 allow" in help_text)
check("HELP documents 122 typo", "122 typo 8" in help_text)
check("HELP documents 122 react", "122 react 20" in help_text)
check("HELP documents 122 burst", "122 burst 3 60" in help_text)
check("HELP documents 122 spacing", "122 spacing 6" in help_text)
check("HELP documents 122 style", "122 style edit|star|mixed" in help_text)
check("HELP documents 122 reset", "122 reset" in help_text)
check("HELP still documents 888 itself", "888" in help_text)

# main.py startup line should also mention 121/122
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8") as f:
    main_src = f.read()
listening_part = main_src.split("Listening for secret codes")[1].split(")")[0]
check("main.py startup print mentions 121", "121" in listening_part)
check("main.py startup print mentions 122", "122" in listening_part)

# 888 handler must edit with Text.HELP
check("888 handler uses Text.HELP", "await event.edit(Text.HELP)" in main_src)
check("888 pattern is exact ^888$", "pattern=r'^888$'" in main_src)


print()
print("=== 2) quiet_hours (121) command handling ===")
import quiet_hours
from quiet_hours import quiet_hours as qh


class MockClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, entity, text=None, **kwargs):
        self.sent.append((entity, text))
        return type("M", (), {"delete": lambda self: None})()


class MockEvent:
    def __init__(self, client, chat_id=12345):
        self.client = client
        self.chat_id = chat_id


async def run_121(args):
    client = MockClient()
    ev = MockEvent(client)
    await quiet_hours.handle_command(ev, args)
    return client.sent


async def test_121():
    # status (no args)
    sent = await run_121("")
    check("121 (status) replies", len(sent) == 1 and "ساعات خواب" in sent[0][1])
    check("121 status includes HELP_TEXT", "121 on" in sent[0][1])

    # set window
    sent = await run_121("02:00 10:00")
    check("121 window set", qh.start_minutes == 120 and qh.end_minutes == 600 and qh.enabled)
    check("121 window confirm", "02:00" in sent[0][1] and "10:00" in sent[0][1])

    # on / off
    qh.disable()
    sent = await run_121("on")
    check("121 on enables", qh.enabled is True)
    sent = await run_121("off")
    check("121 off disables", qh.enabled is False)

    # tz
    sent = await run_121("tz +04:30")
    check("121 tz set", qh.tz_offset_minutes == 270)
    sent = await run_121("tz +03:30")
    check("121 tz back", qh.tz_offset_minutes == 210)

    # sleep / wake / now
    sent = await run_121("sleep 90")
    check("121 sleep sets snooze", qh.snooze_until > 0 and qh.is_quiet_now() is True)
    sent = await run_121("now")
    check("121 now clears override", qh.snooze_until == 0.0 and qh.is_quiet_now() is False)
    sent = await run_121("wake 60")
    check("121 wake sets awake", qh.awake_until > 0 and qh.is_quiet_now() is False)
    sent = await run_121("now")

    # allow (exempt current chat)
    sent = await run_121("allow")
    check("121 allow exempts chat", 12345 in qh.exempt_chats)
    sent = await run_121("allow")
    check("121 allow toggles off", 12345 not in qh.exempt_chats)

    # unknown command → help
    sent = await run_121("blahblah")
    check("121 unknown shows help", "121" in sent[0][1] and "نفهمیدم" in sent[0][1])

    # is_quiet_now honours exempt chat even when enabled+in-window
    qh.set_window("00:00", "23:59")
    qh.enable()
    check("quiet when enabled & in window", qh.is_quiet_now(999) is True)
    qh.exempt_chats.add(12345)
    check("exempt chat never quiet", qh.is_quiet_now(12345) is False)
    qh.exempt_chats.discard(12345)
    qh.disable()
    qh.clear_overrides()


asyncio.run(test_121())
print("  (121 handler OK)")


print()
print("=== 3) human_behavior (122) command handling ===")
import human_behavior
from human_behavior import human_behavior as hb


async def run_122(args):
    client = MockClient()
    ev = MockEvent(client)
    await human_behavior.handle_command(ev, args)
    return client.sent


async def test_122():
    # status (no args)
    sent = await run_122("")
    check("122 (status) replies", len(sent) == 1 and "رفتار انسانی" in sent[0][1])
    check("122 status includes HELP_TEXT", "122 typo 8" in sent[0][1])

    # on / off
    sent = await run_122("off")
    check("122 off disables", hb.enabled is False)
    sent = await run_122("on")
    check("122 on enables", hb.enabled is True)

    # typo
    sent = await run_122("typo 8")
    check("122 typo 8 sets 8%", abs(hb.typo_chance - 0.08) < 1e-9)
    sent = await run_122("typo 0")
    check("122 typo 0 disables typos", hb.typo_chance == 0.0)
    sent = await run_122("typo 40")
    check("122 typo capped at 50%", abs(hb.typo_chance - 0.4) < 1e-9)

    # react
    sent = await run_122("react 20")
    check("122 react 20 sets 20%", abs(hb.reaction_chance - 0.2) < 1e-9)

    # burst
    sent = await run_122("burst 3 60")
    check("122 burst 3 60", hb.burst_max == 3 and hb.burst_window == 60.0)

    # spacing
    sent = await run_122("spacing 6")
    check("122 spacing 6", hb.burst_spacing == 6.0)

    # style
    sent = await run_122("style edit")
    check("122 style edit", hb.typo_fix_style == "edit")
    sent = await run_122("style bogus")
    check("122 style rejects invalid", hb.typo_fix_style == "edit")

    # reset
    sent = await run_122("reset")
    check("122 reset restores defaults", abs(hb.typo_chance - 0.08) < 1e-9 and hb.typo_fix_style == "mixed")

    # unknown
    sent = await run_122("blahblah")
    check("122 unknown shows help", "122" in sent[0][1] and "نفهمیدم" in sent[0][1])


asyncio.run(test_122())
print("  (122 handler OK)")


print()
print("=== 3b) typo injection & reaction picker (122 internals) ===")
mutation = human_behavior.inject_typo("سلام این یک تست ساده است")
check("inject_typo returns (text, word) or None", mutation is None or (len(mutation) == 2 and mutation[0] != mutation[1]))
r = human_behavior.pick_reaction("خخخ چقدر خنده داره")
check("pick_reaction laughs", r in ("😂", "🤣"))
r = human_behavior.pick_reaction("مرسی داداش")
check("pick_reaction thanks", r in ("❤", "🙏"))


print()
print("=== 4) handler registration on a real Telethon client (no login) ===")
from telethon import TelegramClient
from telethon.sessions import StringSession

client = TelegramClient(StringSession(), 1, "x")
quiet_hours.register(client)
human_behavior.register(client)
quiet_hours.register(client)  # idempotent
human_behavior.register(client)  # idempotent

def _pattern_str(p):
    """Telethon stores re.compile(...).match (a bound method); get the source string."""
    if p is None:
        return None
    self_obj = getattr(p, "__self__", None)
    if self_obj is not None and hasattr(self_obj, "pattern"):
        return self_obj.pattern
    if hasattr(p, "pattern"):
        return p.pattern
    return str(p)


builtins = client.list_event_handlers()
patterns = []
for handler, ev in builtins:
    pat = _pattern_str(getattr(ev, "pattern", None))
    if pat:
        patterns.append(pat)

check("121 handler registered", any(p == r"^121(?:\s+(.*))?$" for p in patterns), str(patterns))
check("122 handler registered", any(p == r"^122(?:\s+(.*))?$" for p in patterns), str(patterns))
check("register is idempotent (no duplicates)", sum(1 for p in patterns if "121" in p) == 1 and sum(1 for p in patterns if "122" in p) == 1)


print()
print("=== 5) main.py imports & 888 handler present ===")
try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    import main as main_mod
    check("main.py imports cleanly", True)
    handlers = main_mod.client.list_event_handlers()
    pats = [_pattern_str(getattr(ev, "pattern", None)) for _, ev in handlers]
    check("888 handler registered in main", any(p == r"^888$" for p in pats), str([p for p in pats if p]))
    check("777 handler registered in main", any(p and "777" in p for p in pats))
    check("555 status handler registered", any(p == r"^555$" for p in pats))
    check("555 reminder handler registered", any(p == r"^555\s+(.+)$" for p in pats))
except Exception as e:
    check("main.py imports cleanly", False, f"-> {type(e).__name__}: {e}")


print()
print("=" * 46)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("ALL 121/122/888 TESTS PASSED 🎉")
else:
    sys.exit(1)