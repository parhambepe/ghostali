"""
quiet_hours.py - Sleep schedule / Quiet Hours for GhostGram PRO.

Why this exists
---------------
The single biggest "this is a bot" tell is answering at 04:00 in two seconds.
This module makes the AI go silent during a configurable night window, while
leaving YOUR own secret commands fully working (they are outgoing messages and
are never gated).

Design notes
------------
* Fully runtime-configurable with the `121` secret code - no redeploy needed.
* State is persisted next to the other *_state.json files, so it survives
  restarts (as long as DATA_DIR is on a volume).
* Installation is done by monkeypatching the *decision* predicates
  (`PalManager.is_active`, `PalManager.is_auto_engage_active`,
  `AssistantManager.is_active_for_chat`) instead of the send helpers.
  That way status dashboards (`555`) and explicit owner commands
  (`111`, `112`, `222`) keep working at night.
* The event handler is registered lazily by wrapping `TelegramClient.start`
  and `TelegramClient.run_until_disconnected`, so `main.py` needs no edits.
"""

import os
import json
import re
import asyncio
import time
from datetime import datetime, timedelta, timezone

try:
    from config import Config
except Exception:  # pragma: no cover - config should always import
    Config = None


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env(name, default=""):
    value = os.getenv(name)
    return default if value is None else value.strip()


def _bool_env(name, default=False):
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
    """Resolve the directory the other state files already live in."""
    base = _env("DATA_DIR")
    if not base and Config is not None:
        reference = getattr(Config, "PAL_STATE_FILE", "") or ""
        base = os.path.dirname(reference)
    return base or "."


STATE_FILE = os.path.join(_state_dir(), "quiet_hours_state.json")
STATE_VERSION = 1

DEFAULT_START = _env("QUIET_HOURS_START", "01:00") or "01:00"
DEFAULT_END = _env("QUIET_HOURS_END", "09:00") or "09:00"
DEFAULT_ENABLED = _bool_env("QUIET_HOURS_ENABLED", False)
DEFAULT_TZ_OFFSET = _env("QUIET_HOURS_TZ_OFFSET", "+03:30") or "+03:30"

STEALTH_CONFIRM = _bool_env("STEALTH_CONFIRM", True)
CONFIRM_AUTO_DELETE_SECONDS = int(_float_env("CONFIRM_AUTO_DELETE_SECONDS", 10))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_PERSIAN_DIGITS = str.maketrans("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669", "01234567890123456789")


def _normalize_digits(text):
    return (text or "").translate(_PERSIAN_DIGITS)


def parse_clock(raw):
    """Parse "23:30", "23.30", "2330" or "23" into minutes since midnight."""
    text = _normalize_digits(str(raw)).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2})[:.\u066b]?(\d{2})?", text)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if hours == 24 and minutes == 0:
        hours = 0
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def parse_tz_offset(raw):
    """Parse "+03:30", "-5", "3.5" into minutes east of UTC."""
    text = _normalize_digits(str(raw)).strip().upper().replace("UTC", "").replace("GMT", "")
    if not text:
        return None
    sign = 1
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:].strip()
    if ":" in text:
        hours, _, minutes = text.partition(":")
        try:
            total = int(hours) * 60 + int(minutes or 0)
        except ValueError:
            return None
    else:
        try:
            total = int(round(float(text) * 60))
        except ValueError:
            return None
    total *= sign
    if not (-14 * 60 <= total <= 14 * 60):
        return None
    return total


def format_clock(total_minutes):
    total_minutes = int(total_minutes) % (24 * 60)
    return "{:02d}:{:02d}".format(total_minutes // 60, total_minutes % 60)


def format_offset(total_minutes):
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(int(total_minutes))
    return "{}{:02d}:{:02d}".format(sign, total_minutes // 60, total_minutes % 60)


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return "{} \u0633\u0627\u0639\u062a \u0648 {} \u062f\u0642\u06cc\u0642\u0647".format(hours, minutes)
    if hours:
        return "{} \u0633\u0627\u0639\u062a".format(hours)
    return "{} \u062f\u0642\u06cc\u0642\u0647".format(max(1, minutes))


# ---------------------------------------------------------------------------
# Core manager
# ---------------------------------------------------------------------------

class QuietHours:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self._reset_defaults()
        self.load_state()

    # -- state ---------------------------------------------------------------

    def _reset_defaults(self):
        self.enabled = DEFAULT_ENABLED
        self.start_minutes = parse_clock(DEFAULT_START)
        self.end_minutes = parse_clock(DEFAULT_END)
        if self.start_minutes is None:
            self.start_minutes = 60
        if self.end_minutes is None:
            self.end_minutes = 540
        self.tz_offset_minutes = parse_tz_offset(DEFAULT_TZ_OFFSET)
        if self.tz_offset_minutes is None:
            self.tz_offset_minutes = 210
        self.exempt_chats = set()
        self.snooze_until = 0.0
        self.awake_until = 0.0

    def load_state(self):
        if not (os.path.exists(self.state_file) and os.path.getsize(self.state_file) > 0):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return
            self.enabled = bool(data.get("enabled", self.enabled))
            start = parse_clock(data.get("start", ""))
            end = parse_clock(data.get("end", ""))
            if start is not None:
                self.start_minutes = start
            if end is not None:
                self.end_minutes = end
            offset = data.get("tz_offset_minutes")
            if isinstance(offset, (int, float)):
                self.tz_offset_minutes = int(offset)
            self.exempt_chats = {int(chat_id) for chat_id in data.get("exempt_chats", []) or []}
            self.snooze_until = float(data.get("snooze_until", 0) or 0)
            self.awake_until = float(data.get("awake_until", 0) or 0)
        except Exception as error:
            print("\u26a0\ufe0f Error loading Quiet Hours state: {}".format(error))

    def save_state(self):
        try:
            data = {
                "version": STATE_VERSION,
                "enabled": self.enabled,
                "start": format_clock(self.start_minutes),
                "end": format_clock(self.end_minutes),
                "tz_offset_minutes": int(self.tz_offset_minutes),
                "exempt_chats": sorted(self.exempt_chats),
                "snooze_until": self.snooze_until,
                "awake_until": self.awake_until,
            }
            directory = os.path.dirname(self.state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp_file = "{}.tmp".format(self.state_file)
            with open(tmp_file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as error:
            print("\u26a0\ufe0f Error saving Quiet Hours state: {}".format(error))

    # -- time ----------------------------------------------------------------

    @property
    def tzinfo(self):
        return timezone(timedelta(minutes=int(self.tz_offset_minutes)))

    def now_local(self):
        return datetime.now(tz=self.tzinfo)

    def _minutes_now(self):
        now = self.now_local()
        return now.hour * 60 + now.minute

    @property
    def crosses_midnight(self):
        return self.start_minutes > self.end_minutes

    def in_window(self, minute_of_day=None):
        """Is the given wall-clock minute inside the configured night window?"""
        if self.start_minutes == self.end_minutes:
            return False
        current = self._minutes_now() if minute_of_day is None else int(minute_of_day)
        if self.crosses_midnight:
            return current >= self.start_minutes or current < self.end_minutes
        return self.start_minutes <= current < self.end_minutes

    def seconds_until_wake(self):
        """Seconds left until replies resume (0 when already awake)."""
        now = time.time()
        if self.snooze_until > now:
            return int(self.snooze_until - now)
        if not (self.enabled and self.in_window()):
            return 0
        current = self._minutes_now()
        if self.crosses_midnight and current >= self.start_minutes:
            remaining = (24 * 60 - current) + self.end_minutes
        else:
            remaining = self.end_minutes - current
        return max(0, int(remaining * 60))

    def seconds_until_sleep(self):
        """Seconds left until the night window starts (0 when disabled/asleep)."""
        if not self.enabled or self.in_window():
            return 0
        current = self._minutes_now()
        remaining = (self.start_minutes - current) % (24 * 60)
        return max(0, int(remaining * 60))

    # -- decision ------------------------------------------------------------

    def is_quiet_now(self, chat_id=None):
        """True when the AI should stay silent right now."""
        now = time.time()
        if chat_id is not None:
            try:
                if int(chat_id) in self.exempt_chats:
                    return False
            except (TypeError, ValueError):
                pass
        if self.awake_until > now:
            return False
        if self.snooze_until > now:
            return True
        if not self.enabled:
            return False
        return self.in_window()

    # -- mutations -----------------------------------------------------------

    def set_window(self, start_raw, end_raw):
        start = parse_clock(start_raw)
        end = parse_clock(end_raw)
        if start is None or end is None:
            return False
        if start == end:
            return False
        self.start_minutes = start
        self.end_minutes = end
        self.enabled = True
        self.snooze_until = 0.0
        self.awake_until = 0.0
        self.save_state()
        return True

    def enable(self):
        self.enabled = True
        self.awake_until = 0.0
        self.save_state()

    def disable(self):
        self.enabled = False
        self.snooze_until = 0.0
        self.awake_until = 0.0
        self.save_state()

    def set_tz(self, raw):
        offset = parse_tz_offset(raw)
        if offset is None:
            return False
        self.tz_offset_minutes = offset
        self.save_state()
        return True

    def toggle_exempt(self, chat_id):
        """Returns True when the chat is now exempt from Quiet Hours."""
        chat_id = int(chat_id)
        if chat_id in self.exempt_chats:
            self.exempt_chats.discard(chat_id)
            self.save_state()
            return False
        self.exempt_chats.add(chat_id)
        self.save_state()
        return True

    def sleep_for(self, minutes):
        minutes = max(1, int(minutes))
        self.snooze_until = time.time() + minutes * 60
        self.awake_until = 0.0
        self.save_state()
        return minutes

    def wake_for(self, minutes):
        minutes = max(1, int(minutes))
        self.awake_until = time.time() + minutes * 60
        self.snooze_until = 0.0
        self.save_state()
        return minutes

    def clear_overrides(self):
        self.snooze_until = 0.0
        self.awake_until = 0.0
        self.save_state()

    # -- reporting -----------------------------------------------------------

    def short_status(self):
        if self.is_quiet_now():
            return "\U0001f634 \u062e\u0648\u0627\u0628"
        if self.enabled:
            return "\u23f0 \u0641\u0639\u0627\u0644 ({} \u2192 {})".format(
                format_clock(self.start_minutes), format_clock(self.end_minutes)
            )
        return "\u274c \u063a\u06cc\u0631\u0641\u0639\u0627\u0644"

    def status_text(self):
        now = self.now_local()
        lines = ["\U0001f634 **\u0633\u0627\u0639\u0627\u062a \u062e\u0648\u0627\u0628 (Quiet Hours)**", ""]
        lines.append("\u2022 \u0648\u0636\u0639\u06cc\u062a: {}".format(
            "\u0631\u0648\u0634\u0646 \u2705" if self.enabled else "\u062e\u0627\u0645\u0648\u0634 \u274c"
        ))
        lines.append("\u2022 \u0628\u0627\u0632\u0647: `{}` \u062a\u0627 `{}`".format(
            format_clock(self.start_minutes), format_clock(self.end_minutes)
        ))
        lines.append("\u2022 \u0633\u0627\u0639\u062a \u0645\u062d\u0644\u06cc \u0631\u0628\u0627\u062a: `{}` (UTC{})".format(
            now.strftime("%H:%M"), format_offset(self.tz_offset_minutes)
        ))
        lines.append("\u2022 \u0627\u0644\u0627\u0646: {}".format(
            "\u062f\u0631 \u062d\u0627\u0644 \u062e\u0648\u0627\u0628 \U0001f634" if self.is_quiet_now() else "\u0628\u06cc\u062f\u0627\u0631 \u2615"
        ))

        remaining_wake = self.seconds_until_wake()
        if remaining_wake:
            lines.append("\u2022 \u0628\u06cc\u062f\u0627\u0631 \u0645\u06cc\u200c\u0634\u0648\u062f \u062a\u0627: {} \u062f\u06cc\u06af\u0631".format(format_duration(remaining_wake)))
        elif self.enabled:
            remaining_sleep = self.seconds_until_sleep()
            if remaining_sleep:
                lines.append("\u2022 \u0645\u06cc\u200c\u062e\u0648\u0627\u0628\u062f \u062a\u0627: {} \u062f\u06cc\u06af\u0631".format(format_duration(remaining_sleep)))

        now_epoch = time.time()
        if self.awake_until > now_epoch:
            lines.append("\u2022 \u06a9\u0627\u0641\u0626\u06cc\u0646 \u0645\u0648\u0642\u062a \u2615: {} \u062f\u06cc\u06af\u0631".format(
                format_duration(self.awake_until - now_epoch)
            ))
        if self.snooze_until > now_epoch:
            lines.append("\u2022 \u062e\u0648\u0627\u0628 \u062f\u0633\u062a\u06cc \U0001f634: {} \u062f\u06cc\u06af\u0631".format(
                format_duration(self.snooze_until - now_epoch)
            ))
        if self.exempt_chats:
            lines.append("\u2022 \u0686\u062a\u200c\u0647\u0627\u06cc \u0645\u0639\u0627\u0641 (\u0634\u0628 \u0647\u0645 \u062c\u0648\u0627\u0628 \u0645\u06cc\u200c\u06af\u06cc\u0631\u0646\u062f): {}".format(
                len(self.exempt_chats)
            ))

        lines.append("")
        lines.append(HELP_TEXT)
        return "\n".join(lines)


HELP_TEXT = (
    "**\u062f\u0633\u062a\u0648\u0631\u0647\u0627\u06cc `121`**\n"
    "\u2022 `121` \u2014 \u0646\u0645\u0627\u06cc\u0634 \u0647\u0645\u06cc\u0646 \u0648\u0636\u0639\u06cc\u062a\n"
    "\u2022 `121 01:00 09:00` \u2014 \u062a\u0646\u0637\u06cc\u0645 \u0628\u0627\u0632\u0647\u0654 \u062e\u0648\u0627\u0628 (\u0648 \u0631\u0648\u0634\u0646 \u06a9\u0631\u062f\u0646)\n"
    "\u2022 `121 on` / `121 off` \u2014 \u0631\u0648\u0634\u0646 \u06cc\u0627 \u062e\u0627\u0645\u0648\u0634 \u06a9\u0631\u062f\u0646\n"
    "\u2022 `121 tz +03:30` \u2014 \u062a\u0646\u0637\u06cc\u0645 \u0645\u0646\u0637\u0642\u0647\u0654 \u0632\u0645\u0627\u0646\u06cc\n"
    "\u2022 `121 sleep 90` \u2014 \u0647\u0645\u06cc\u0646 \u0627\u0644\u0627\u0646 \u06f9\u06f0 \u062f\u0642\u06cc\u0642\u0647 \u0628\u062e\u0648\u0627\u0628\n"
    "\u2022 `121 wake 60` \u2014 \u06f6\u06f0 \u062f\u0642\u06cc\u0642\u0647 \u0628\u06cc\u062f\u0627\u0631 \u0628\u0645\u0627\u0646 (\u062d\u062a\u06cc \u062f\u0631 \u0628\u0627\u0632\u0647\u0654 \u062e\u0648\u0627\u0628)\n"
    "\u2022 `121 now` \u2014 \u0644\u063a\u0648 \u062e\u0648\u0627\u0628/\u0628\u06cc\u062f\u0627\u0631\u06cc \u062f\u0633\u062a\u06cc\n"
    "\u2022 `121 allow` \u2014 \u0645\u0639\u0627\u0641 \u06a9\u0631\u062f\u0646 \u0647\u0645\u06cc\u0646 \u0686\u062a (\u062f\u0648\u0628\u0627\u0631\u0647 \u0628\u0632\u0646\u06cc \u0644\u063a\u0648 \u0645\u06cc\u200c\u0634\u0648\u062f)"
)


# Global singleton instance
quiet_hours = QuietHours()


# ---------------------------------------------------------------------------
# Gate installation (monkeypatch the auto-reply decision predicates)
# ---------------------------------------------------------------------------

def _wrap_predicate(owner, method_name, chat_arg_index=0):
    original = getattr(owner, method_name, None)
    if original is None or getattr(original, "_quiet_wrapped", False):
        return

    def wrapper(self, *args, **kwargs):
        chat_id = None
        if len(args) > chat_arg_index:
            chat_id = args[chat_arg_index]
        elif "chat_id" in kwargs:
            chat_id = kwargs["chat_id"]
        try:
            if quiet_hours.is_quiet_now(chat_id):
                return False
        except Exception:
            pass
        return original(self, *args, **kwargs)

    wrapper._quiet_wrapped = True
    wrapper.__name__ = getattr(original, "__name__", method_name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    setattr(owner, method_name, wrapper)


def _install_gates():
    try:
        from pal_manager import PalManager
        _wrap_predicate(PalManager, "is_active")
        _wrap_predicate(PalManager, "is_auto_engage_active")
    except Exception as error:
        print("\u26a0\ufe0f Quiet Hours could not gate PalManager: {}".format(error))

    try:
        from assistant_manager import AssistantManager
        _wrap_predicate(AssistantManager, "is_active_for_chat")
    except Exception as error:
        print("\u26a0\ufe0f Quiet Hours could not gate AssistantManager: {}".format(error))


# ---------------------------------------------------------------------------
# Telegram command handler (registered lazily, no main.py edits required)
# ---------------------------------------------------------------------------

async def _confirm(client, text):
    """Send a self-destructing confirmation to Saved Messages."""
    try:
        message = await client.send_message("me", text)
    except Exception as error:
        print("\u26a0\ufe0f Quiet Hours confirm failed: {}".format(error))
        return
    if STEALTH_CONFIRM and CONFIRM_AUTO_DELETE_SECONDS > 0:
        async def _cleanup():
            try:
                await asyncio.sleep(CONFIRM_AUTO_DELETE_SECONDS)
                await message.delete()
            except Exception:
                pass
        asyncio.create_task(_cleanup())


ON_WORDS = ("on", "\u0631\u0648\u0634\u0646", "\u0641\u0639\u0627\u0644")
OFF_WORDS = ("off", "\u062e\u0627\u0645\u0648\u0634", "\u063a\u06cc\u0631\u0641\u0639\u0627\u0644")
TZ_WORDS = ("tz", "timezone", "\u0645\u0646\u0637\u0642\u0647")
SLEEP_WORDS = ("sleep", "\u0628\u062e\u0648\u0627\u0628", "\u062e\u0648\u0627\u0628")
WAKE_WORDS = ("wake", "\u0628\u06cc\u062f\u0627\u0631", "\u06a9\u0627\u0641\u0626\u06cc\u0646")
NOW_WORDS = ("now", "reset", "\u0644\u063a\u0648", "\u0627\u0644\u0627\u0646")
ALLOW_WORDS = ("allow", "exempt", "\u0645\u0639\u0627\u0641", "\u0627\u0633\u062a\u062b\u0646\u0627")


async def handle_command(event, raw_args):
    """Handle a `121 ...` command. Kept public so main.py may call it too."""
    client = event.client
    chat_id = event.chat_id
    args = _normalize_digits(raw_args or "").strip().split()

    if not args:
        await _confirm(client, quiet_hours.status_text())
        return

    head = args[0].lower()

    if head in ON_WORDS:
        quiet_hours.enable()
        await _confirm(client, "\U0001f634 \u0633\u0627\u0639\u0627\u062a \u062e\u0648\u0627\u0628 \u0631\u0648\u0634\u0646 \u0634\u062f: `{}` \u062a\u0627 `{}`".format(
            format_clock(quiet_hours.start_minutes), format_clock(quiet_hours.end_minutes)
        ))
        return

    if head in OFF_WORDS:
        quiet_hours.disable()
        await _confirm(client, "\u2615 \u0633\u0627\u0639\u0627\u062a \u062e\u0648\u0627\u0628 \u062e\u0627\u0645\u0648\u0634 \u0634\u062f \u2014 \u0631\u0628\u0627\u062a \u06f2\u06f4 \u0633\u0627\u0639\u062a\u0647 \u062c\u0648\u0627\u0628 \u0645\u06cc\u200c\u062f\u0647\u062f.")
        return

    if head in NOW_WORDS:
        quiet_hours.clear_overrides()
        await _confirm(client, "\u267b\ufe0f \u062e\u0648\u0627\u0628/\u0628\u06cc\u062f\u0627\u0631\u06cc \u062f\u0633\u062a\u06cc \u0644\u063a\u0648 \u0634\u062f.\n\n{}".format(quiet_hours.short_status()))
        return

    if head in ALLOW_WORDS:
        is_exempt = quiet_hours.toggle_exempt(chat_id)
        if is_exempt:
            await _confirm(client, "\u2705 \u0686\u062a `{}` \u0627\u0632 \u0633\u0627\u0639\u0627\u062a \u062e\u0648\u0627\u0628 \u0645\u0639\u0627\u0641 \u0634\u062f \u2014 \u0634\u0628\u200c\u0647\u0627 \u0647\u0645 \u062c\u0648\u0627\u0628 \u0645\u06cc\u200c\u06af\u06cc\u0631\u062f.".format(chat_id))
        else:
            await _confirm(client, "\U0001f6ab \u0645\u0639\u0627\u0641\u06cc\u062a \u0686\u062a `{}` \u0644\u063a\u0648 \u0634\u062f.".format(chat_id))
        return

    if head in TZ_WORDS:
        if len(args) < 2 or not quiet_hours.set_tz(args[1]):
            await _confirm(client, "\u26a0\ufe0f \u0645\u0646\u0637\u0642\u0647\u0654 \u0632\u0645\u0627\u0646\u06cc را نفهمیدم. \u0645\u062b\u0627\u0644: `121 tz +03:30`")
            return
        await _confirm(client, "\U0001f30d \u0645\u0646\u0637\u0642\u0647\u0654 \u0632\u0645\u0627\u0646\u06cc \u0631\u0648\u06cc UTC{} \u062a\u0646\u0637\u06cc\u0645 \u0634\u062f. \u0633\u0627\u0639\u062a \u0645\u062d\u0644\u06cc \u0631\u0628\u0627\u062a \u0627\u0644\u0627\u0646 `{}` \u0627\u0633\u062a.".format(
            format_offset(quiet_hours.tz_offset_minutes), quiet_hours.now_local().strftime("%H:%M")
        ))
        return

    if head in SLEEP_WORDS:
        minutes = 60
        if len(args) > 1:
            try:
                minutes = int(float(args[1]))
            except ValueError:
                await _confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `121 sleep 90`")
                return
        quiet_hours.sleep_for(minutes)
        await _confirm(client, "\U0001f634 \u0631\u0628\u0627\u062a \u0628\u0647 \u0645\u062f\u062a {} \u062e\u0648\u0627\u0628\u06cc\u062f. \u0628\u0631\u0627\u06cc \u0644\u063a\u0648: `121 now`".format(
            format_duration(minutes * 60)
        ))
        return

    if head in WAKE_WORDS:
        minutes = 60
        if len(args) > 1:
            try:
                minutes = int(float(args[1]))
            except ValueError:
                await _confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `121 wake 60`")
                return
        quiet_hours.wake_for(minutes)
        await _confirm(client, "\u2615 \u0631\u0628\u0627\u062a \u0628\u0647 \u0645\u062f\u062a {} \u0628\u06cc\u062f\u0627\u0631 \u0645\u06cc\u200c\u0645\u0627\u0646\u062f. \u0628\u0631\u0627\u06cc \u0644\u063a\u0648: `121 now`".format(
            format_duration(minutes * 60)
        ))
        return

    if len(args) >= 2 and quiet_hours.set_window(args[0], args[1]):
        await _confirm(client, "\U0001f634 \u0628\u0627\u0632\u0647\u0654 \u062e\u0648\u0627\u0628 \u0631\u0648\u06cc `{}` \u062a\u0627 `{}` \u062a\u0646\u0637\u06cc\u0645 \u0648 \u0641\u0639\u0627\u0644 \u0634\u062f.\n\u0633\u0627\u0639\u062a \u0645\u062d\u0644\u06cc \u0631\u0628\u0627\u062a: `{}` (UTC{})".format(
            format_clock(quiet_hours.start_minutes),
            format_clock(quiet_hours.end_minutes),
            quiet_hours.now_local().strftime("%H:%M"),
            format_offset(quiet_hours.tz_offset_minutes),
        ))
        return

    await _confirm(client, "\u26a0\ufe0f \u062f\u0633\u062a\u0648\u0631 \u0631\u0627 \u0646\u0641\u0647\u0645\u06cc\u062f\u0645.\n\n{}".format(HELP_TEXT))


def register(client):
    """Register the `121` handler on a Telethon client (idempotent)."""
    if getattr(client, "_quiet_hours_registered", False):
        return
    try:
        from telethon import events
    except Exception as error:
        print("\u26a0\ufe0f Quiet Hours could not import telethon events: {}".format(error))
        return

    client._quiet_hours_registered = True

    @client.on(events.NewMessage(outgoing=True, pattern=r"^121(?:\s+(.*))?$"))
    async def _quiet_hours_handler(event):
        raw_args = event.pattern_match.group(1) or ""
        try:
            await event.delete()
        except Exception:
            pass
        try:
            await handle_command(event, raw_args)
        except Exception as error:
            print("\u26a0\ufe0f Quiet Hours command failed: {}".format(error))

    print("\U0001f634 Quiet Hours ready \u2014 {} | \u06a9\u062f `121`".format(quiet_hours.short_status()))


def _patch_client_bootstrap():
    """Wrap TelegramClient lifecycle methods so `register` always runs."""
    try:
        from telethon import TelegramClient
    except Exception as error:
        print("\u26a0\ufe0f Quiet Hours could not patch TelegramClient: {}".format(error))
        return

    for method_name in ("start", "run_until_disconnected"):
        original = getattr(TelegramClient, method_name, None)
        if original is None or getattr(original, "_quiet_patched", False):
            continue

        def make_wrapper(original_method):
            def wrapper(self, *args, **kwargs):
                try:
                    register(self)
                except Exception as error:
                    print("\u26a0\ufe0f Quiet Hours registration failed: {}".format(error))
                return original_method(self, *args, **kwargs)

            wrapper._quiet_patched = True
            return wrapper

        setattr(TelegramClient, method_name, make_wrapper(original))


_INSTALLED = False


def install_quiet_hours(force=False):
    """Install gates + lazy handler registration. Safe to call repeatedly."""
    global _INSTALLED
    if _INSTALLED and not force:
        return
    _INSTALLED = True
    _install_gates()
    _patch_client_bootstrap()


# Auto-install on import.
install_quiet_hours()
