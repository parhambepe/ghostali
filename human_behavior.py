"""
human_behavior.py - The anti-detection package for GhostGram PRO.

Three behaviours, all live-tunable with the `122` secret code:

1. Typo simulation      - occasionally send a small typo, then fix it a few
                          seconds later by editing the message (Telegram shows
                          the "edited" marker, which is extremely human) or by
                          sending a `*correction` follow-up.
2. Reaction instead of  - when the AI reply is a tiny throwaway line ("khkh",
   reply                  "bashe", "mersi") and it is a reply to a specific
                          message, drop the text and just react with an emoji.
                          Humans do this constantly; bots never do.
3. Burst guard          - never answer many chats within a few seconds. Replies
                          are spaced out and rate limited globally.

Implementation notes
--------------------
* We wrap whatever `TelegramClient.send_message` currently is, AFTER importing
  `typing_helper` so its humanized/segmenting patch is already in place. Our
  wrapper therefore sits on the outside: burst gate -> reaction shortcut ->
  typo injection -> humanized typing + segmentation -> real send.
* Only plain AI-looking replies are touched. Owner command confirmations are
  skipped by heuristic: anything with markdown (`**`, backticks), anything sent
  to Saved Messages, and sticker placeholders are left alone.
* Typo injection only runs on messages short enough that `typing_helper` will
  not split them into multiple bubbles, so the returned message object is
  always the one holding the typo and can be edited safely.
* Every step is defensive: on any failure we fall back to the plain send. The
  bot must never break because of cosmetics.
"""

import os
import re
import json
import time
import random
import asyncio
from collections import deque

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

try:
    from quiet_hours import _confirm as _send_confirm, _normalize_digits
except Exception:  # pragma: no cover - keep working standalone
    _send_confirm = None

    def _normalize_digits(text):
        return text or ""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env(name, default=""):
    value = os.getenv(name)
    return default if value is None else value.strip()


def _bool_env(name, default=True):
    raw = _env(name).lower()
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return default


def _float_env(name, default):
    try:
        raw = _env(name)
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


def _state_dir():
    base = _env("DATA_DIR")
    if not base and Config is not None:
        reference = getattr(Config, "PAL_STATE_FILE", "") or ""
        base = os.path.dirname(reference)
    return base or "."


STATE_FILE = os.path.join(_state_dir(), "human_behavior_state.json")
STATE_VERSION = 1

FIX_STYLES = ("mixed", "edit", "star")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class HumanBehavior:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self._reset_defaults()
        self.load_state()

    def _reset_defaults(self):
        self.enabled = _bool_env("HUMAN_BEHAVIOR_ENABLED", True)

        # 1) typo simulation
        self.typo_chance = min(max(_float_env("HUMAN_TYPO_CHANCE", 0.08), 0.0), 0.5)
        self.typo_max_chars = int(_float_env("HUMAN_TYPO_MAX_CHARS", 170))
        self.typo_fix_min = _float_env("HUMAN_TYPO_FIX_DELAY_MIN", 2.0)
        self.typo_fix_max = _float_env("HUMAN_TYPO_FIX_DELAY_MAX", 6.5)
        style = _env("HUMAN_TYPO_FIX_STYLE", "mixed").lower()
        self.typo_fix_style = style if style in FIX_STYLES else "mixed"

        # 2) reaction instead of reply
        self.reaction_chance = min(max(_float_env("HUMAN_REACTION_CHANCE", 0.22), 0.0), 1.0)
        self.reaction_max_chars = int(_float_env("HUMAN_REACTION_MAX_CHARS", 26))

        # 3) burst guard
        self.burst_max = max(1, int(_float_env("HUMAN_BURST_MAX", 3)))
        self.burst_window = max(5.0, _float_env("HUMAN_BURST_WINDOW", 60.0))
        self.burst_spacing = max(0.0, _float_env("HUMAN_BURST_SPACING", 6.0))

    def load_state(self):
        if not (os.path.exists(self.state_file) and os.path.getsize(self.state_file) > 0):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return
            self.enabled = bool(data.get("enabled", self.enabled))
            self.typo_chance = float(data.get("typo_chance", self.typo_chance))
            self.typo_max_chars = int(data.get("typo_max_chars", self.typo_max_chars))
            self.typo_fix_min = float(data.get("typo_fix_min", self.typo_fix_min))
            self.typo_fix_max = float(data.get("typo_fix_max", self.typo_fix_max))
            style = str(data.get("typo_fix_style", self.typo_fix_style)).lower()
            if style in FIX_STYLES:
                self.typo_fix_style = style
            self.reaction_chance = float(data.get("reaction_chance", self.reaction_chance))
            self.reaction_max_chars = int(data.get("reaction_max_chars", self.reaction_max_chars))
            self.burst_max = max(1, int(data.get("burst_max", self.burst_max)))
            self.burst_window = max(5.0, float(data.get("burst_window", self.burst_window)))
            self.burst_spacing = max(0.0, float(data.get("burst_spacing", self.burst_spacing)))
        except Exception as error:
            print("\u26a0\ufe0f Error loading Human Behavior state: {}".format(error))

    def save_state(self):
        try:
            data = {
                "version": STATE_VERSION,
                "enabled": self.enabled,
                "typo_chance": self.typo_chance,
                "typo_max_chars": self.typo_max_chars,
                "typo_fix_min": self.typo_fix_min,
                "typo_fix_max": self.typo_fix_max,
                "typo_fix_style": self.typo_fix_style,
                "reaction_chance": self.reaction_chance,
                "reaction_max_chars": self.reaction_max_chars,
                "burst_max": self.burst_max,
                "burst_window": self.burst_window,
                "burst_spacing": self.burst_spacing,
            }
            directory = os.path.dirname(self.state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp_file = "{}.tmp".format(self.state_file)
            with open(tmp_file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as error:
            print("\u26a0\ufe0f Error saving Human Behavior state: {}".format(error))

    def short_status(self):
        if not self.enabled:
            return "\u274c \u063a\u06cc\u0631\u0641\u0639\u0627\u0644"
        return "\u2705 \u063a\u0644\u0637 {}\u066a | \u0648\u0627\u06a9\u0646\u0634 {}\u066a | \u0633\u0642\u0641 {}/{:.0f}\u062b".format(
            int(round(self.typo_chance * 100)),
            int(round(self.reaction_chance * 100)),
            self.burst_max,
            self.burst_window,
        )

    def status_text(self):
        lines = ["\U0001f9ec **\u0631\u0641\u062a\u0627\u0631 \u0627\u0646\u0633\u0627\u0646\u06cc (\u0636\u062f\u0634\u0646\u0627\u0633\u0627\u06cc\u06cc)**", ""]
        lines.append("\u2022 \u0648\u0636\u0639\u06cc\u062a: {}".format(
            "\u0631\u0648\u0634\u0646 \u2705" if self.enabled else "\u062e\u0627\u0645\u0648\u0634 \u274c"
        ))
        lines.append("")
        lines.append("**\u06f1) \u063a\u0644\u0637 \u062a\u0627\u06cc\u067e\u06cc \u0648 \u062a\u0635\u062d\u06cc\u062d**")
        lines.append("\u2022 \u0627\u062d\u062a\u0645\u0627\u0644: `{}\u066a`".format(int(round(self.typo_chance * 100))))
        lines.append("\u2022 \u0641\u0642\u0637 \u062a\u0627 `{}` \u06a9\u0627\u0631\u0627\u06a9\u062a\u0631".format(self.typo_max_chars))
        lines.append("\u2022 \u062a\u0623\u062e\u06cc\u0631 \u062a\u0635\u062d\u06cc\u062d: `{:.1f}` \u062a\u0627 `{:.1f}` \u062b\u0627\u0646\u06cc\u0647".format(
            self.typo_fix_min, self.typo_fix_max
        ))
        lines.append("\u2022 \u0633\u0628\u06a9 \u062a\u0635\u062d\u06cc\u062d: `{}`".format(self.typo_fix_style))
        lines.append("")
        lines.append("**\u06f2) \u0648\u0627\u06a9\u0646\u0634 \u0628\u0647\u200c\u062c\u0627\u06cc \u062c\u0648\u0627\u0628**")
        lines.append("\u2022 \u0627\u062d\u062a\u0645\u0627\u0644: `{}\u066a`".format(int(round(self.reaction_chance * 100))))
        lines.append("\u2022 \u0641\u0642\u0637 \u0628\u0631\u0627\u06cc \u062c\u0648\u0627\u0628\u200c\u0647\u0627\u06cc \u062a\u0627 `{}` \u06a9\u0627\u0631\u0627\u06a9\u062a\u0631".format(self.reaction_max_chars))
        lines.append("")
        lines.append("**\u06f3) \u0645\u062d\u062f\u0648\u062f\u06a9\u0646\u0646\u062f\u0647\u0654 \u0627\u0646\u0641\u062c\u0627\u0631**")
        lines.append("\u2022 \u062d\u062f\u0627\u06a9\u062b\u0631 `{}` \u062c\u0648\u0627\u0628 \u062f\u0631 `{:.0f}` \u062b\u0627\u0646\u06cc\u0647".format(
            self.burst_max, self.burst_window
        ))
        lines.append("\u2022 \u0641\u0627\u0635\u0644\u0647\u0654 \u062d\u062f\u0627\u0642\u0644 \u0628\u06cc\u0646 \u062f\u0648 \u062c\u0648\u0627\u0628: `{:.1f}` \u062b\u0627\u0646\u06cc\u0647".format(self.burst_spacing))
        lines.append("")
        lines.append(HELP_TEXT)
        return "\n".join(lines)


HELP_TEXT = (
    "**\u062f\u0633\u062a\u0648\u0631\u0647\u0627\u06cc `122`**\n"
    "\u2022 `122` \u2014 \u0646\u0645\u0627\u06cc\u0634 \u0648\u0636\u0639\u06cc\u062a\n"
    "\u2022 `122 on` / `122 off` \u2014 \u0631\u0648\u0634\u0646 \u06cc\u0627 \u062e\u0627\u0645\u0648\u0634 \u06a9\u0631\u062f\u0646 \u06a9\u0644 \u067e\u06a9\u06cc\u062c\n"
    "\u2022 `122 typo 8` \u2014 \u0627\u062d\u062a\u0645\u0627\u0644 \u063a\u0644\u0637 \u062a\u0627\u06cc\u067e\u06cc (\u062f\u0631\u0635\u062f\u061b \u06f0 = \u062e\u0627\u0645\u0648\u0634)\n"
    "\u2022 `122 react 20` \u2014 \u0627\u062d\u062a\u0645\u0627\u0644 \u0648\u0627\u06a9\u0646\u0634 \u0628\u0647\u200c\u062c\u0627\u06cc \u062c\u0648\u0627\u0628 (\u062f\u0631\u0635\u062f)\n"
    "\u2022 `122 burst 3 60` \u2014 \u062d\u062f\u0627\u06a9\u062b\u0631 \u06f3 \u062c\u0648\u0627\u0628 \u062f\u0631 \u06f6\u06f0 \u062b\u0627\u0646\u06cc\u0647\n"
    "\u2022 `122 spacing 6` \u2014 \u062d\u062f\u0627\u0642\u0644 \u0641\u0627\u0635\u0644\u0647\u0654 \u062f\u0648 \u062c\u0648\u0627\u0628 (\u062b\u0627\u0646\u06cc\u0647)\n"
    "\u2022 `122 style edit|star|mixed` \u2014 \u0633\u0628\u06a9 \u062a\u0635\u062d\u06cc\u062d \u063a\u0644\u0637\n"
    "\u2022 `122 reset` \u2014 \u0628\u0627\u0632\u06af\u0634\u062a \u0628\u0647 \u067e\u06cc\u0634\u200c\u0641\u0631\u0636\u200c\u0647\u0627"
)


human_behavior = HumanBehavior()


# ---------------------------------------------------------------------------
# 1) Typo simulation
# ---------------------------------------------------------------------------

_SKIP_PREFIXES = ("http", "www.", "@", "#", "/", "*", "_", "`")


def _is_typo_candidate(word):
    if len(word) < 4 or len(word) > 18:
        return False
    lowered = word.lower()
    if lowered.startswith(_SKIP_PREFIXES):
        return False
    return word.isalpha()


def _mutate(word):
    if len(word) < 4:
        return None
    kind = random.choice(("swap", "swap", "drop", "dup"))
    index = random.randrange(1, len(word) - 1)
    if kind == "swap":
        chars = list(word)
        chars[index], chars[index + 1] = chars[index + 1], chars[index]
        return "".join(chars)
    if kind == "drop":
        return word[:index] + word[index + 1:]
    return word[:index] + word[index] + word[index:]


def inject_typo(text):
    """Return (text_with_typo, correct_word) or None when not possible."""
    if "\n" in text:
        # keep multi-line / structured replies clean
        return None
    tokens = text.split(" ")
    candidates = [i for i, token in enumerate(tokens) if _is_typo_candidate(token)]
    if not candidates:
        return None
    index = random.choice(candidates)
    correct = tokens[index]
    typo = _mutate(correct)
    if not typo or typo == correct:
        return None
    tokens[index] = typo
    return " ".join(tokens), correct


async def _fix_typo(client, entity, sent_message, correct_text, correct_word):
    state = human_behavior
    low = min(state.typo_fix_min, state.typo_fix_max)
    high = max(state.typo_fix_min, state.typo_fix_max)
    await asyncio.sleep(random.uniform(low, high))

    style = state.typo_fix_style
    if style == "mixed":
        style = random.choice(("edit", "edit", "star"))

    try:
        if style == "edit" and sent_message is not None:
            await client.edit_message(entity, sent_message, correct_text)
            return
    except Exception:
        pass

    try:
        if _ORIGINAL_SEND_MESSAGE is not None:
            await _ORIGINAL_SEND_MESSAGE(client, entity, "*{}".format(correct_word))
    except Exception as error:
        print("\u26a0\ufe0f Typo correction failed: {}".format(error))


# ---------------------------------------------------------------------------
# 2) Reaction instead of reply
# ---------------------------------------------------------------------------

_LAUGH_HINTS = ("\u062e\u062e", "lol", "lmao", "\U0001f602", "\U0001f923", "haha", "\u0647\u0647\u0647")
_THANKS_HINTS = ("\u0645\u0631\u0633\u06cc", "\u0645\u0645\u0646\u0648\u0646", "\u062f\u0633\u062a\u062a \u062f\u0631\u062f", "thanks", "thx", "\u2764", "\U0001f60d")
_AGREE_HINTS = ("\u0628\u0627\u0634\u0647", "\u0627\u0648\u06a9\u06cc", "ok", "okay", "\u062d\u062a\u0645\u0627", "\u062d\u062a\u0645\u0627\u064b", "\u0622\u0631\u0647", "\u0628\u0644\u0647", "\U0001f44d")
_SAD_HINTS = ("\u0627\u0648\u0647", "\u0645\u062a\u0627\u0633\u0641", "\u0645\u062a\u0623\u0633\u0641", "\u0628\u062f\u0628\u062e\u062a", "\U0001f622", "\U0001f614")


def pick_reaction(text):
    lowered = (text or "").lower()
    if any(hint in lowered for hint in _LAUGH_HINTS):
        return random.choice(("\U0001f602", "\U0001f923"))
    if any(hint in lowered for hint in _THANKS_HINTS):
        return random.choice(("\u2764", "\U0001f64f"))
    if any(hint in lowered for hint in _SAD_HINTS):
        return random.choice(("\U0001f622", "\U0001f614"))
    if any(hint in lowered for hint in _AGREE_HINTS):
        return "\U0001f44d"
    return random.choice(("\U0001f44d", "\U0001f44c"))


def _resolve_message_id(reply_to):
    if reply_to is None:
        return None
    if isinstance(reply_to, int):
        return reply_to
    message_id = getattr(reply_to, "id", None)
    return message_id if isinstance(message_id, int) else None


async def _try_react(client, entity, reply_to, text):
    """React to `reply_to` instead of sending `text`.

    Returns the reacted-to Message on success (so callers that use the return
    value keep working), or None to signal "fall back to a normal send".
    """
    message_id = _resolve_message_id(reply_to)
    if message_id is None:
        return None
    try:
        from telethon.tl import functions, types

        peer = await client.get_input_entity(entity)
        await client(functions.messages.SendReactionRequest(
            peer=peer,
            msg_id=message_id,
            reaction=[types.ReactionEmoji(emoticon=pick_reaction(text))],
        ))
    except Exception:
        return None

    try:
        return await client.get_messages(entity, ids=message_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3) Burst guard
# ---------------------------------------------------------------------------

_recent_sends = deque()
_burst_lock = None


def _get_burst_lock():
    global _burst_lock
    if _burst_lock is None:
        _burst_lock = asyncio.Lock()
    return _burst_lock


async def _burst_gate():
    state = human_behavior
    try:
        lock = _get_burst_lock()
    except Exception:
        return
    async with lock:
        now = time.time()
        while _recent_sends and (now - _recent_sends[0]) > state.burst_window:
            _recent_sends.popleft()

        wait = 0.0
        if len(_recent_sends) >= state.burst_max:
            wait = state.burst_window - (now - _recent_sends[0])
        if _recent_sends and state.burst_spacing > 0:
            wait = max(wait, state.burst_spacing - (now - _recent_sends[-1]))

        if wait > 0:
            await asyncio.sleep(min(wait, state.burst_window))
        _recent_sends.append(time.time())


# ---------------------------------------------------------------------------
# send_message wrapper
# ---------------------------------------------------------------------------

_ORIGINAL_SEND_MESSAGE = None
_STICKER_PLACEHOLDER = "[\u0627\u0633\u062a\u06cc\u06a9\u0631]"


def _is_saved_messages(client, entity):
    if isinstance(entity, str) and entity.lower() in ("me", "self"):
        return True
    self_id = getattr(client, "_self_id", None)
    if self_id is None:
        return False
    try:
        if isinstance(entity, int) and entity == self_id:
            return True
        entity_id = getattr(entity, "user_id", None) or getattr(entity, "id", None)
        return entity_id == self_id
    except Exception:
        return False


def _looks_like_ai_reply(client, entity, text):
    """Heuristic: plain conversational text going to somebody else."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if "**" in stripped or "`" in stripped:
        return False  # owner command confirmations / dashboards
    if _STICKER_PLACEHOLDER in stripped:
        return False
    if stripped.startswith("*"):
        return False  # our own typo correction
    if _is_saved_messages(client, entity):
        return False
    return True


def install_human_behavior(force=False):
    """Wrap TelegramClient.send_message on top of typing_helper's patch."""
    global _ORIGINAL_SEND_MESSAGE

    try:
        import typing_helper  # noqa: F401  (ensures its patch is applied first)
    except Exception as error:
        print("\u26a0\ufe0f Human Behavior: typing_helper import failed: {}".format(error))

    try:
        from telethon import TelegramClient
    except Exception as error:
        print("\u26a0\ufe0f Human Behavior could not import telethon: {}".format(error))
        return

    current = getattr(TelegramClient, "send_message", None)
    if current is None:
        return
    if getattr(current, "_human_behavior_wrapped", False) and not force:
        return

    _ORIGINAL_SEND_MESSAGE = current

    async def send_message(self, *args, **kwargs):
        entity = args[0] if len(args) > 0 else kwargs.get("entity")
        text = args[1] if len(args) > 1 else kwargs.get("message")

        state = human_behavior
        if not state.enabled or not _looks_like_ai_reply(self, entity, text):
            return await current(self, *args, **kwargs)

        try:
            await _burst_gate()
        except Exception:
            pass

        # 2) reaction instead of reply
        reply_to = kwargs.get("reply_to")
        if (
            reply_to is not None
            and state.reaction_chance > 0
            and len(text.strip()) <= state.reaction_max_chars
            and "?" not in text
            and "\u061f" not in text
            and random.random() < state.reaction_chance
        ):
            reacted = await _try_react(self, entity, reply_to, text)
            if reacted is not None:
                return reacted

        # 1) typo simulation
        if (
            state.typo_chance > 0
            and len(text) <= state.typo_max_chars
            and random.random() < state.typo_chance
        ):
            mutation = inject_typo(text)
            if mutation:
                typo_text, correct_word = mutation
                new_args = list(args)
                new_kwargs = dict(kwargs)
                if len(new_args) > 1:
                    new_args[1] = typo_text
                else:
                    new_kwargs["message"] = typo_text
                sent = await current(self, *new_args, **new_kwargs)
                try:
                    asyncio.create_task(_fix_typo(self, entity, sent, text, correct_word))
                except Exception:
                    pass
                return sent

        return await current(self, *args, **kwargs)

    send_message._human_behavior_wrapped = True
    TelegramClient.send_message = send_message


# ---------------------------------------------------------------------------
# `122` command handler
# ---------------------------------------------------------------------------

ON_WORDS = ("on", "\u0631\u0648\u0634\u0646", "\u0641\u0639\u0627\u0644")
OFF_WORDS = ("off", "\u062e\u0627\u0645\u0648\u0634", "\u063a\u06cc\u0631\u0641\u0639\u0627\u0644")


async def _reply_confirm(client, text):
    if _send_confirm is not None:
        await _send_confirm(client, text)
        return
    try:
        await client.send_message("me", text)
    except Exception:
        pass


def _parse_percent(raw):
    try:
        value = float(_normalize_digits(raw))
    except (TypeError, ValueError):
        return None
    if value > 1:
        value = value / 100.0
    if not (0.0 <= value <= 1.0):
        return None
    return value


async def handle_command(event, raw_args):
    client = event.client
    state = human_behavior
    args = _normalize_digits(raw_args or "").strip().split()

    if not args:
        await _reply_confirm(client, state.status_text())
        return

    head = args[0].lower()

    if head in ON_WORDS:
        state.enabled = True
        state.save_state()
        await _reply_confirm(client, "\U0001f9ec \u0631\u0641\u062a\u0627\u0631 \u0627\u0646\u0633\u0627\u0646\u06cc \u0631\u0648\u0634\u0646 \u0634\u062f.\n{}".format(state.short_status()))
        return

    if head in OFF_WORDS:
        state.enabled = False
        state.save_state()
        await _reply_confirm(client, "\u274c \u0631\u0641\u062a\u0627\u0631 \u0627\u0646\u0633\u0627\u0646\u06cc \u062e\u0627\u0645\u0648\u0634 \u0634\u062f \u2014 \u062c\u0648\u0627\u0628\u200c\u0647\u0627 \u0628\u062f\u0648\u0646 \u063a\u0644\u0637 \u0648 \u0648\u0627\u06a9\u0646\u0634 \u0645\u06cc\u200c\u0631\u0648\u0646\u062f.")
        return

    if head == "reset":
        state._reset_defaults()
        state.save_state()
        await _reply_confirm(client, "\u267b\ufe0f \u0628\u0647 \u067e\u06cc\u0634\u200c\u0641\u0631\u0636\u200c\u0647\u0627 \u0628\u0631\u06af\u0634\u062a.\n{}".format(state.short_status()))
        return

    if head == "typo":
        value = _parse_percent(args[1]) if len(args) > 1 else None
        if value is None:
            await _reply_confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `122 typo 8`")
            return
        state.typo_chance = min(value, 0.5)
        state.save_state()
        await _reply_confirm(client, "\u2328\ufe0f \u0627\u062d\u062a\u0645\u0627\u0644 \u063a\u0644\u0637 \u062a\u0627\u06cc\u067e\u06cc \u0631\u0648\u06cc `{}\u066a` \u062a\u0646\u0637\u06cc\u0645 \u0634\u062f.".format(
            int(round(state.typo_chance * 100))
        ))
        return

    if head in ("react", "reaction"):
        value = _parse_percent(args[1]) if len(args) > 1 else None
        if value is None:
            await _reply_confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `122 react 20`")
            return
        state.reaction_chance = value
        state.save_state()
        await _reply_confirm(client, "\U0001f44d \u0627\u062d\u062a\u0645\u0627\u0644 \u0648\u0627\u06a9\u0646\u0634 \u0631\u0648\u06cc `{}\u066a` \u062a\u0646\u0637\u06cc\u0645 \u0634\u062f.".format(
            int(round(state.reaction_chance * 100))
        ))
        return

    if head == "burst":
        try:
            burst_max = int(float(args[1]))
            window = float(args[2]) if len(args) > 2 else state.burst_window
        except (IndexError, ValueError):
            await _reply_confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `122 burst 3 60`")
            return
        state.burst_max = max(1, burst_max)
        state.burst_window = max(5.0, window)
        state.save_state()
        await _reply_confirm(client, "\U0001f6a6 \u062d\u062f\u0627\u06a9\u062b\u0631 `{}` \u062c\u0648\u0627\u0628 \u062f\u0631 `{:.0f}` \u062b\u0627\u0646\u06cc\u0647 \u062a\u0646\u0637\u06cc\u0645 \u0634\u062f.".format(
            state.burst_max, state.burst_window
        ))
        return

    if head in ("spacing", "gap"):
        try:
            spacing = float(args[1])
        except (IndexError, ValueError):
            await _reply_confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `122 spacing 6`")
            return
        state.burst_spacing = max(0.0, spacing)
        state.save_state()
        await _reply_confirm(client, "\u23f1 \u0641\u0627\u0635\u0644\u0647\u0654 \u062d\u062f\u0627\u0642\u0644 \u0631\u0648\u06cc `{:.1f}` \u062b\u0627\u0646\u06cc\u0647 \u062a\u0646\u0637\u06cc\u0645 \u0634\u062f.".format(
            state.burst_spacing
        ))
        return

    if head == "style":
        style = args[1].lower() if len(args) > 1 else ""
        if style not in FIX_STYLES:
            await _reply_confirm(client, "\u26a0\ufe0f \u0633\u0628\u06a9\u200c\u0647\u0627\u06cc \u0645\u062c\u0627\u0632: `edit`\u060c `star`\u060c `mixed`")
            return
        state.typo_fix_style = style
        state.save_state()
        await _reply_confirm(client, "\u270f\ufe0f \u0633\u0628\u06a9 \u062a\u0635\u062d\u06cc\u062d \u0631\u0648\u06cc `{}` \u062a\u0646\u0637\u06cc\u0645 \u0634\u062f.".format(style))
        return

    await _reply_confirm(client, "\u26a0\ufe0f \u062f\u0633\u062a\u0648\u0631 \u0631\u0627 \u0646\u0641\u0647\u0645\u06cc\u062f\u0645.\n\n{}".format(HELP_TEXT))


def register(client):
    """Register the `122` handler on a Telethon client (idempotent)."""
    if getattr(client, "_human_behavior_registered", False):
        return
    try:
        from telethon import events
    except Exception as error:
        print("\u26a0\ufe0f Human Behavior could not import telethon events: {}".format(error))
        return

    client._human_behavior_registered = True

    @client.on(events.NewMessage(outgoing=True, pattern=r"^122(?:\s+(.*))?$"))
    async def _human_behavior_handler(event):
        raw_args = event.pattern_match.group(1) or ""
        try:
            await event.delete()
        except Exception:
            pass
        try:
            await handle_command(event, raw_args)
        except Exception as error:
            print("\u26a0\ufe0f Human Behavior command failed: {}".format(error))

    print("\U0001f9ec Human Behavior ready \u2014 {} | \u06a9\u062f `122`".format(human_behavior.short_status()))


def _patch_client_bootstrap():
    try:
        from telethon import TelegramClient
    except Exception:
        return

    for method_name in ("start", "run_until_disconnected"):
        original = getattr(TelegramClient, method_name, None)
        if original is None or getattr(original, "_human_behavior_patched", False):
            continue

        def make_wrapper(original_method):
            def wrapper(self, *args, **kwargs):
                try:
                    register(self)
                except Exception as error:
                    print("\u26a0\ufe0f Human Behavior registration failed: {}".format(error))
                return original_method(self, *args, **kwargs)

            wrapper._human_behavior_patched = True
            return wrapper

        setattr(TelegramClient, method_name, make_wrapper(original))


_INSTALLED = False


def install(force=False):
    global _INSTALLED
    if _INSTALLED and not force:
        return
    _INSTALLED = True
    install_human_behavior(force=force)
    _patch_client_bootstrap()


install()
