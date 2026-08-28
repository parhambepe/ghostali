#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for secret code 133 (Memory Backup) and its 888 documentation.
No network / no Telegram login required. State files -> temp DATA_DIR.
"""
import sys
import os
import asyncio
import zipfile
import json
import time
from datetime import datetime, timezone

# Isolate state files BEFORE importing any project module
_TMP_STATE = os.path.join(os.environ.get("TEMP", "/tmp"), "ghostali_test_state_133")
os.makedirs(_TMP_STATE, exist_ok=True)
os.environ["DATA_DIR"] = _TMP_STATE
os.environ["CONFIRM_AUTO_DELETE_SECONDS"] = "0"
os.environ["MEMORY_BACKUP_TIME"] = "04:00"
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
print("=== 1) 888 HELP text (text.py) documents 133 ===")
from text import Text

help_text = Text.HELP
check("HELP mentions code 133", "133" in help_text)
check("HELP has Memory Backup section", "بکاپ حافظه" in help_text)
check("HELP documents 133 now", "133 now" in help_text)
check("HELP documents 133 restore", "133 restore" in help_text)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8") as f:
    main_src = f.read()
listening_part = main_src.split("Listening for secret codes")[1].split(")")[0]
check("startup print mentions 133", "133" in listening_part)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pal_manager.py"), encoding="utf-8") as f:
    pal_src = f.read()
check("pal_manager imports memory_backup", "import memory_backup" in pal_src)

print()
print("=== 2) clock parsing & formatting ===")
import memory_backup as mb
check("_parse_clock('04:00') = 240", mb._parse_clock("04:00") == 240)
check("_parse_clock('4:0') = 240", mb._parse_clock("4:0") == 240)
check("_parse_clock('۰۴:۰۰') = 240 (persian digits)", mb._parse_clock("۰۴:۰۰") == 240)
check("_parse_clock('25:00') invalid -> None", mb._parse_clock("25:00") is None)
check("_parse_clock('4') invalid -> None", mb._parse_clock("4") is None)
check("_format_clock(240) = '04:00'", mb._format_clock(240) == "04:00")
check("_format_clock(1470) = '00:30' wraps", mb._format_clock(1470) == "00:30")

# ---------------------------------------------------------------------------
print()
print("=== 3) create_backup zips real state files, keeps manifest ===")
stub1 = os.path.join(_TMP_STATE, "pal_state.json")
stub2 = os.path.join(_TMP_STATE, "memory_state.json")
with open(stub1, "w", encoding="utf-8") as f:
    f.write('{"test": 1}')
with open(stub2, "w", encoding="utf-8") as f:
    f.write('{"chat": 123}')

from config import Config
_orig_state_attrs = {}
for attr, label in (
    ("PAL_STATE_FILE", "pal"),
    ("ASSISTANT_STATE_FILE", "assistant"),
    ("MEMORY_STATE_FILE", "memory"),
    ("REMINDERS_STATE_FILE", "reminders"),
    ("STICKERS_STATE_FILE", "stickers"),
    ("API_USAGE_FILE", "api_usage"),
):
    _orig_state_attrs[attr] = getattr(Config, attr, None)
    setattr(Config, attr, os.path.join(_TMP_STATE, f"{label}_state.json"))

mb.state["enabled"] = True
zip_path, size, included = mb.create_backup()
check("backup zip exists", os.path.isfile(zip_path))
check("zip is non-empty", size > 0)

with zipfile.ZipFile(zip_path, "r") as zf:
    names = zf.namelist()
    check("manifest inside zip", any(n.endswith("BACKUP_INFO.txt") for n in names))
    check("contains pal_state.json", "pal_state.json" in names)
    check("contains memory_state.json", "memory_state.json" in names)

check("last_backup timestamp updated", mb.state.get("last_backup", 0) > 0)
check("last_size matches file size", mb.state.get("last_size") == size)

# ---------------------------------------------------------------------------
print()
print("=== 3b) api_usage sanitization (API keys NEVER appear in backup) ===")
SECRET_KEY = "AIzaSySECRETSECRETSECRETSECRETSECRET"
api_usage_path = os.path.join(_TMP_STATE, "api_usage_state.json")
today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
with open(api_usage_path, "w", encoding="utf-8") as f:
    json.dump({SECRET_KEY: {"date": today_s, "count": 42}, "_models": {}}, f)

zip_path2, size2, included2 = mb.create_backup()
with zipfile.ZipFile(zip_path2, "r") as zf:
    names2 = zf.namelist()
    api_clean = [n for n in names2 if "api_usage" in n]
    check("api_usage.sanitized.json is in backup", "api_usage.sanitized.json" in api_clean, str(api_clean))
    raw = zf.read("api_usage.sanitized.json").decode("utf-8")
    check("real API key is NOT in backup content", SECRET_KEY not in raw)
    check("sanitized counter preserved", '"count": 42' in raw)
    check("hashed key marker present", "key:" in raw and SECRET_KEY[:8] not in raw)

print()
print("=== 4) prune keeps only `keep` newest ===")
for i in range(5):
    fake = os.path.join(mb.backup_dir(), f"ghostali_state_backup_2026-08-2{i}_000000.zip")
    with open(fake, "wb") as f:
        f.write(b"x")
mb.state["keep"] = 3
before_count = len(mb.list_backups())
removed = mb.prune_backups()
check("prune removes down to keep", removed == before_count - 3 and len(mb.list_backups()) == 3,
      f"removed={removed}, before={before_count}")
entries = mb.list_backups()
check("3 backups remain", len(entries) == 3, str(entries))
mb.state["keep"] = 7

print()
print("=== 5) _due scheduling logic ===")
from datetime import datetime

mb.state["enabled"] = True
mb.state["time"] = "04:00"
mb.state["last_run"] = ""
now = datetime.now()
before_4am = now.replace(hour=2, minute=0, second=0, microsecond=0)
after_4am = now.replace(hour=6, minute=0, second=0, microsecond=0)
check("before 04:00 -> not due", mb._due(before_4am) is False)
check("after 04:00 & never ran -> due", mb._due(after_4am) is True)
mb.state["last_run"] = after_4am.strftime("%Y-%m-%d")
check("already ran today -> not due", mb._due(after_4am) is False)
mb.state["enabled"] = False
check("disabled -> never due", mb._due(after_4am) is False)
mb.state["enabled"] = True
mb.state["last_run"] = ""

print()
print("=== 6) handle_command via mock ===")


class MockClient:
    def __init__(self):
        self.sent = []
        self.files = []

    async def send_message(self, entity, text=None, **kwargs):
        self.sent.append((entity, text))
        return type("M", (), {"delete": lambda self: None})()

    async def send_file(self, entity, file, caption=None, **kwargs):
        self.files.append((entity, file))
        return type("M", (), {"delete": lambda self: None})()


class MockEvent:
    def __init__(self, client):
        self.client = client
        self.chat_id = 777


async def run_133(raw):
    client = MockClient()
    await mb.handle_command(MockEvent(client), raw)
    return client


async def _main6():
    client = await run_133("")
    check("bare 133 sends status card", len(client.sent) == 1 and "بکاپ خودکار حافظه" in client.sent[0][1])

    client = await run_133("now")
    check("133 now sends confirm", len(client.sent) == 1)
    check("133 now sends backup file", len(client.files) == 1 and client.files[0][0] == "me")

    client = await run_133("off")
    check("133 off disables", mb.state.get("enabled") is False)
    client = await run_133("on")
    check("133 on enables", mb.state.get("enabled") is True)

    client = await run_133("time 06:30")
    check("133 time 06:30 sets time", mb.state.get("time") == "06:30")
    client = await run_133("time bogus")
    check("133 time bogus rejected", "مثال درست" in client.sent[0][1])

    client = await run_133("keep 5")
    check("133 keep 5 sets keep", mb.state.get("keep") == 5)
    client = await run_133("keep 100")
    check("133 keep 100 capped at 30", mb.state.get("keep") == 30)
    mb.state["keep"] = 7

    client = await run_133("list")
    check("133 list shows backups", "بکاپ‌های موجود" in client.sent[0][1] and "zip" in client.sent[0][1])

    client = await run_133("restore")
    check("133 restore sends latest file", len(client.files) >= 1)

    client = await run_133("send off")
    check("133 send off", mb.state.get("send") is False)
    client = await run_133("send on")
    check("133 send on", mb.state.get("send") is True)

    client = await run_133("blahblah")
    check("133 unknown shows help", "نفهمیدم" in client.sent[0][1] and "133" in client.sent[0][1])


asyncio.run(_main6())
print("  (133 handler OK)")

for attr, value in _orig_state_attrs.items():
    setattr(Config, attr, value)

print()
print("=== 7) handler registration on a real Telethon client (no login) ===")
from telethon import TelegramClient
from telethon.sessions import StringSession

client = TelegramClient(StringSession(), 1, "x")
mb.register(client)
mb.register(client)  # idempotent


def _pattern_str(p):
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
check("133 handler registered", any(p == r"^133(?:\s+(.*))?$" for p in patterns), str(patterns))
check("register is idempotent", sum(1 for p in patterns if "133" in p) == 1)

print()
print("=== 8) main.py imports & 133/130/121/122 all present ===")
try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    import main as main_mod
    check("main.py imports cleanly", True)
    import quiet_hours as _qh
    import human_behavior as _hb
    import stats_command as _sc
    _sc.register(main_mod.client)
    _qh.register(main_mod.client)
    _hb.register(main_mod.client)
    mb.register(main_mod.client)
    pats = [_pattern_str(getattr(ev, "pattern", None)) for _, ev in main_mod.client.list_event_handlers()]
    check("133 handler registered in main", any(p == r"^133(?:\s+(.*))?$" for p in pats), str([p for p in pats if p]))
    check("130 handler still registered", any(p == r"^130(?:\s+(.*))?$" for p in pats))
    check("888 handler still registered", any(p == r"^888$" for p in pats))
    check("121 handler still registered", any(p and p.startswith(r"^121") for p in pats))
    check("122 handler still registered", any(p and p.startswith(r"^122") for p in pats))
except Exception as e:
    check("main.py imports cleanly", False, f"-> {type(e).__name__}: {e}")

print()
print("=" * 46)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("ALL 133 TESTS PASSED 🎉")
else:
    sys.exit(1)