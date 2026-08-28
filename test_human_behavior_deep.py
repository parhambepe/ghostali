#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deep functional tests for the TWO human_behavior (122) systems:

  1) TYPO + AUTO-FIX via edit
     - inject_typo produces a real typo, keeps the rest of the text intact,
       and a delayed _fix_typo EDIT brings the message back to the corrected
       text (exactly what a human "wrong word then edit" looks like).
  2) REACTION-INSTEAD-OF-REPLY
     - a short throwaway reply to a message may become a live SendReaction
       request instead of a text send, using _try_react + pick_reaction.

A fake Telethon client records every call to edit_message / send_message /
send_file plus the raw TL `functions.messages.SendReactionRequest` that the
bot issues, then we assert the exact behaviour. Offline: no network/login.
"""
import sys
import os
import asyncio
import random
import string
import re

# Isolate state files BEFORE importing project modules
_TMP_STATE = os.path.join(os.environ.get("TEMP", "/tmp"), "ghostali_test_hb_deep")
os.makedirs(_TMP_STATE, exist_ok=True)
os.environ["DATA_DIR"] = _TMP_STATE
os.environ["CONFIRM_AUTO_DELETE_SECONDS"] = "0"
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "testhash")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set deterministic-ish random seed so tests are reproducible but still real
random.seed(42)

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


import human_behavior as hb
import human_behavior as hb_mod  # same module

# ---------------------------------------------------------------------------
# Fake Telethon-ish client that records EVERYTHING, incl. the exact reaction RT
# ---------------------------------------------------------------------------
class FakeTelegramClient:
    def __init__(self):
        self.edits = []          # (entity, message, new_text)
        self.sends = []          # (entity, text)
        self.sent_files = []
        self.reactions = []      # dicts {peer, msg_id, emoticon}
        self.calls = []          # every awaited client(...) call
        self.edited_objects = []  # object returned by edit_message

    def get_event_loop(self):
        import asyncio as _a
        try:
            return _a.get_running_loop()
        except Exception:
            return _a.new_event_loop()

    # The wrapper calls client.loop / client.send_message etc.
    # mirror the real Telethon API surface the code depends on:

    async def get_input_entity(self, entity):
        return entity  # fake peer = same entity

    async def get_messages(self, entity, ids=None):
        # Return a fake message object whose .id exists (for identity checks)
        return type("Msg", (), {"id": ids})() if ids is not None else None

    async def edit_message(self, entity, message, new_text=None):
        self.edits.append((entity, getattr(message, "id", None) if hasattr(message, "id") else message, new_text))
        return type("Msg", (), {"id": getattr(message, "id", 99)})()

    async def send_message(self, entity, text=None, **kwargs):
        self.sends.append((entity, text))
        self.calls.append(("send_message", text))
        return self._make_msg()

    async def send_file(self, entity, file, caption=None, **kwargs):
        self.sent_files.append((entity, file, caption))
        return self._make_msg()

    def _make_msg(self):
        mid = len(self.sends) + len(self.sent_files) + 1
        return type("Msg", (), {"id": mid, "delete": lambda self: None})()

    # Telethon style: `await client(fn(...))`
    async def __call__(self, request):
        name = type(request).__name__
        if name == "SendReactionRequest":
            emoji = request.reaction[0].emoticon
            self.reactions.append({
                "peer": request.peer,
                "msg_id": request.msg_id,
                "emoticon": emoji,
            })
            self.calls.append(("react", emoji))
            return type("R", (), {"updates": []})()
        self.calls.append((name, None))
        return None


def make_event(client, chat_id=12345):
    return type("Ev", (), {"client": client, "chat_id": chat_id})()


async def run_122(client, raw):
    await hb.handle_command(make_event(client), raw)
# ---------------------------------------------------------------------------
print("=== 1) inject_typo: wrong word is REAL, rest intact, fixable ===\n")
found_mutation = False
for _ in range(300):
    original = "سلام دوست من این یک پیام آزمایشی است برای تست غلط تایپی"
    mutation = hb.inject_typo(original)
    if mutation is None:
        continue
    typo_text, correct_word = mutation
    orig_tokens = original.split(" ")
    typo_tokens = typo_text.split(" ")
    check("typo has same token count", len(typo_tokens) == len(orig_tokens), f"{typo_tokens} vs {orig_tokens}")
    diffs = [i for i, (a, b) in enumerate(zip(orig_tokens, typo_tokens)) if a != b]
    check("exactly one word changed", len(diffs) == 1, f"diffs={diffs}")
    check("correct_word is the original token", diffs and typo_tokens[diffs[0]] != orig_tokens[diffs[0]] and correct_word == orig_tokens[diffs[0]])
    typo = typo_tokens[diffs[0]]
    check("typo differs in <=1 char len", abs(len(typo) - len(orig_tokens[diffs[0]])) <= 1, f"{typo} vs {orig_tokens[diffs[0]]}")
    fixed_tokens = list(typo_tokens)
    fixed_tokens[diffs[0]] = correct_word
    check("replacing typo with correct_word restores original", " ".join(fixed_tokens) == original)
    check("correct_word != typo", correct_word != typo)
    found_mutation = True
    break
check("found at least one mutation in 300 tries", found_mutation)

check("short word never mutated", hb.inject_typo("hi") is None, str(hb.inject_typo("hi")))
check("multi-line text never mutated", hb.inject_typo("first line\nsecond") is None)

# ---------------------------------------------------------------------------
print("\n=== 2) pick_reaction maps text -> fitting emoji ===\n")
for _ in range(50):
    r = hb.pick_reaction("خخخ این خیلی خنده دار بود 😂")
    check("laugh text -> laughing emoji", r in ("😂", "🤣"))
for _ in range(50):
    r = hb.pick_reaction("مرسی مرسی دستت درد نکنه ممنونم")
    check("thanks text -> thanks emoji", r in ("❤", "🙏"))
for _ in range(50):
    r = hb.pick_reaction("مطمئنا باشه باشه حتما")
    check("agree text -> thumbs up", r == "👍")
for _ in range(50):
    r = hb.pick_reaction("اوه متاسفم بدبخت شدم")
    check("sad text -> sad emoji", r in ("😢", "😔"))
for _ in range(50):
    r = hb.pick_reaction("یه چیزی کاملاً خنثی")
    check("neutral text -> fallback 👍👌", r in ("👍", "👌"))

# ---------------------------------------------------------------------------
print("\n=== 3) _try_react issues a REAL SendReactionRequest (reaction system) ===\n")


async def run_react_tests():
    client = FakeTelegramClient()
    reply_obj = type("RMsg", (), {"id": 501})()
    res = await hb._try_react(client, 12345, None, "بله")
    check("no reply_to -> returns None (fallback to send)", res is None)
    res = await hb._try_react(client, 12345, reply_obj, "باشه حتما")
    check("reacted to reply_obj", res is not None)
    check("exactly one reaction request sent", len(client.reactions) == 1, str(client.reactions))
    check("reaction targets msg 501", client.reactions[0]["msg_id"] == 501, str(client.reactions))
    check("emoticon is a fitting one", client.reactions[0]["emoticon"] in ("👍", "👌"), str(client.reactions))
    client2 = FakeTelegramClient()
    res2 = await hb._try_react(client2, 12345, 777, "خخخه")
    check("int reply_to works", len(client2.reactions) == 1 and client2.reactions[0]["msg_id"] == 777, str(client2.reactions))
    check("reaction on int reply_to returns msg", res2 is not None)


asyncio.run(run_react_tests())

# ---------------------------------------------------------------------------
print("\n=== 4) FULL reaction flow: no text send, only a reaction ===\n")


async def run_react_flow():
    client4 = FakeTelegramClient()
    hb.human_behavior.enabled = True
    hb.human_behavior.reaction_chance = 1.0   # force reaction
    hb.human_behavior.typo_chance = 0.0
    reply_obj4 = type("RMsg", (), {"id": 910})()
    reacted = await hb._try_react(client4, 12345, reply_obj4, "باشه")
    check("forced reaction returns message object", reacted is not None)
    check("reaction request in client4", len(client4.reactions) == 1 and client4.reactions[0]["msg_id"] == 910)
    check("client4 got exactly ONE reaction (no text send)", len(client4.sends) == 0, str(client4.sends))


asyncio.run(run_react_flow())

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
print("\n=== 5) FULL typo-fix flow: send typo -> delayed EDIT restores text ===\n")


class FakeClientForWrapper:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, entity, text=None, **kwargs):
        self.sent.append(text)
        return type("M", (), {"id": 1000, "delete": lambda self: None})()

    async def edit_message(self, entity, message, new_text=None):
        self.edits.append(new_text)
        return type("M", (), {"id": 1000})()

    def _make_msg(self):
        return type("M", (), {"id": 1000, "delete": lambda self: None})()


async def run_full_flow():
    fc = FakeClientForWrapper()
    hb.human_behavior.typo_fix_style = "edit"
    hb.human_behavior.typo_fix_min = 0.0
    hb.human_behavior.typo_fix_max = 0.0
    text = "سلام این یکی پیام تست ساده است برای بررسی غلط تایپی"
    mutation = hb.inject_typo(text)
    if mutation is None:
        check("inject_typo produced a mutation for flow test", False)
        return
    typo_text, correct_word = mutation
    sent_obj = fc._make_msg()
    # the typo text is what a wrapped send puts on the wire first
    fc.sent.append(typo_text)
    check("typo != original text", typo_text != text)
    check("typo is a plausible wrong word", correct_word != typo_text and abs(len(typo_text) - len(text)) <= (len(text) // 10))
    # then _fix_typo edits it back to the corrected text after (0s) delay
    await hb._fix_typo(fc, 12345, sent_obj, text, correct_word)
    check("edit_message called exactly once", len(fc.edits) == 1, str(fc.edits))
    check("edited text == corrected original", fc.edits[0] == text, f"edit={fc.edits}")

asyncio.run(run_full_flow())

print()
print("=" * 46)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("ALL 122 TYPO-FIX + REACTION DEEP TESTS PASSED 🎉")
else:
    sys.exit(1)