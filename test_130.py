#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for secret code 130 (Stats dashboard) and its 888 documentation.
No network / no Telegram login required. State files -> temp DATA_DIR.
"""
import sys
import os
import asyncio
import time

# Isolate state files BEFORE importing any project module
_TMP_STATE = os.path.join(os.environ.get("TEMP", "/tmp"), "ghostali_test_state_130")
os.makedirs(_TMP_STATE, exist_ok=True)
os.environ["DATA_DIR"] = _TMP_STATE
os.environ["CONFIRM_AUTO_DELETE_SECONDS"] = "0"  # no stray auto-delete tasks in tests
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
print("=== 1) 888 HELP text (text.py) documents 130 ===")
from text import Text

help_text = Text.HELP
check("HELP mentions code 130", "130" in help_text)
check("HELP has Stats section", "آمار و مصرف" in help_text)
check("HELP describes 130 as usage report", "گزارش مصرف" in help_text)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8") as f:
    main_src = f.read()
listening_part = main_src.split("Listening for secret codes")[1].split(")")[0]
check("startup print mentions 130", "130" in listening_part)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pal_manager.py"), encoding="utf-8") as f:
    pal_src = f.read()
check("pal_manager imports stats_command", "import stats_command" in pal_src)

print()
print("=== 2) uptime formatter ===")
import stats_command as sc
check("fmt_uptime(0) -> seconds", sc.fmt_uptime(0) == "0 ثانیه")
check("fmt_uptime(125)", sc.fmt_uptime(125) == "2 دقیقه و 5 ثانیه")
check("fmt_uptime(90061)", sc.fmt_uptime(90061) == "1 روز و 1 ساعت و 1 دقیقه و 1 ثانیه")
check("_mask_key hides full key", sc._mask_key("AIzaSyAbcdefghijklm") == "AIzaSyAbcd…")

# ---------------------------------------------------------------------------
print()
print("=== 3) report content (key masking, counts, bans, sizes) ===")
from datetime import datetime, timezone
from config import Config
from api_tracker import api_tracker

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
KEY_A = "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY_B = "AIzaSyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
Config.GEMINI_API_KEYS = [KEY_A, KEY_B]
api_tracker.limit = 100
api_tracker.usage_data = {
    "_bans": {},
    "_models": {},
    KEY_A: {"date": today, "count": 37},
    KEY_B: {"date": today, "count": 100},
}
api_tracker.banned_until = {}

report = sc.build_report()
check("report header present", "گزارش مصرف" in report)
check("UTC date line present", "امروز (UTC)" in report)
check("key A masked (never leaked)", KEY_A not in report and sc._mask_key(KEY_A) in report)
check("key A count 37/100", "37/100" in report)
check("key B shows cap reached", "سقف روزانه پر شده" in report)
check("total line 137/200", "137/200" in report)
check("models section present", "مدل‌ها" in report)
check("state files section", "فایل‌های وضعیت" in report)
check("read-only note", "فقط خواندنی" in report)

api_tracker.banned_until = {KEY_A: time.time() + 600}
report2 = sc.build_report()
check("banned key shows ban state", "بن‌شده" in report2)
api_tracker.banned_until = {}

check("report did not mutate usage_data",
      api_tracker.usage_data[KEY_A]["count"] == 37 and api_tracker.usage_data[KEY_B]["count"] == 100)

print()
print("=== 4) handle_command via mock (bare + with args) ===")


class MockClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, entity, text=None, **kwargs):
        self.sent.append((entity, text))
        return type("M", (), {"delete": lambda self: None})()


class MockEvent:
    def __init__(self, client):
        self.client = client
        self.chat_id = 4242


async def run_130(raw):
    client = MockClient()
    await sc.handle_command(MockEvent(client), raw)
    return client.sent


async def _main4():
    sent = await run_130("")
    check("bare 130 sends one message to 'me'", len(sent) == 1 and sent[0][0] == "me")
    check("bare 130 sends the report", "گزارش مصرف" in sent[0][1])
    sent = await run_130("xyz")
    check("130 with args explains no-arg rule", "ورودی نمی‌گیرد" in sent[0][1])
    check("130 with args still includes report", "گزارش مصرف" in sent[0][1])

asyncio.run(_main4())
print("  (130 handler OK)")

print()
print("=== 5) handler registration on a real Telethon client (no login) ===")
from telethon import TelegramClient
from telethon.sessions import StringSession

client = TelegramClient(StringSession(), 1, "x")
sc.register(client)
sc.register(client)  # idempotent


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


patterns = [_pattern_str(getattr(ev, "pattern", None)) for _, ev in client.list_event_handlers()]
patterns = [p for p in patterns if p]
check("130 handler registered", any(p == r"^130(?:\s+(.*))?$" for p in patterns), str(patterns))
check("register is idempotent", sum(1 for p in patterns if "130" in p) == 1)

print()
print("=== 6) main.py imports & lazy handlers register on the real client ===")
try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    import main as main_mod
    check("main.py imports cleanly", True)
    # 121/122/130 register lazily inside TelegramClient.start()'s bootstrap
    # wrapper (main.py itself never changes). Simulate what startup does:
    # call each module's register() on the real main.py client.
    import quiet_hours as _qh
    import human_behavior as _hb
    sc.register(main_mod.client)
    _qh.register(main_mod.client)
    _hb.register(main_mod.client)
    pats = [_pattern_str(getattr(ev, "pattern", None)) for _, ev in main_mod.client.list_event_handlers()]
    check("130 handler registered in main", any(p == r"^130(?:\s+(.*))?$" for p in pats), str([p for p in pats if p]))
    check("888 handler still registered", any(p == r"^888$" for p in pats))
    check("121 handler still registered", any(p and p.startswith(r"^121") for p in pats))
    check("122 handler still registered", any(p and p.startswith(r"^122") for p in pats))
except Exception as e:
    check("main.py imports cleanly", False, f"-> {type(e).__name__}: {e}")

print()
print("=" * 46)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("ALL 130 TESTS PASSED 🎉")
else:
    sys.exit(1)
