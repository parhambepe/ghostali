"""
stats_command.py - Secret code 130: usage & health dashboard for GhostGram PRO.

Why this exists
---------------
The owner had no way to see HOW MUCH of the daily Gemini quota was spent,
which model ate it, or whether circuit-breaker bans are active - without
opening a shell on the Railway box. `130` answers all of that inside Telegram.

What `130` shows (100% read-only - it never mutates any state):
  * Bot uptime (time since this module was imported = process start)
  * Today's Gemini request count per API key (keys are MASKED) and the total
  * Per-model daily counters from the GEMINI_MODELS rotation
  * Circuit-breaker bans: which keys are banned and for how long
  * Sizes of the persistent state files (memory, reminders, pal, ...)

Installation is identical to quiet_hours / human_behavior: importing this
module patches TelegramClient.start / run_until_disconnected so register()
runs on any client without touching main.py. pal_manager.py imports it last.
"""

import os
import time
import asyncio
from datetime import datetime, timezone

try:
    from config import Config
except Exception:  # pragma: no cover - config should always import
    Config = None

try:
    from api_tracker import api_tracker
except Exception:  # pragma: no cover - stats still work without it
    api_tracker = None

STEALTH_CONFIRM = (os.getenv("STEALTH_CONFIRM", "1").lower() not in ("0", "false", "no", "off"))
CONFIRM_AUTO_DELETE_SECONDS = int(float(os.getenv("CONFIRM_AUTO_DELETE_SECONDS", "10") or 10))

# Process start marker: pal_manager imports this module at the very beginning
# of main.py's import chain, so this is a good uptime proxy.
START_TIME = time.time()

_PERSIAN_DIGITS = str.maketrans(
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
    "01234567890123456789",
)


def _normalize_digits(text):
    return (text or "").translate(_PERSIAN_DIGITS)


def _mask_key(key):
    """AIzaSyAbc123xyz... -> `AIzaSyAbc1…` — never print a full key into a chat."""
    key = (key or "").strip()
    if not key:
        return "\u061f"
    if len(key) <= 10:
        return key[:4] + "\u2026"
    return key[:10] + "\u2026"


def _human_size(num_bytes):
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "\u061f"
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return "{:.0f} {}".format(n, unit) if unit == "B" else "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "{:.1f} GB".format(n)


def fmt_uptime(seconds=None):
    """12345 -> '3 ساعت و 25 دقیقه و 45 ثانیه' (only non-zero parts)."""
    seconds = int(seconds if seconds is not None else (time.time() - START_TIME))
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append("{} \u0631\u0648\u0632".format(days))
    if hours:
        parts.append("{} \u0633\u0627\u0639\u062a".format(hours))
    if minutes:
        parts.append("{} \u062f\u0642\u06cc\u0642\u0647".format(minutes))
    if secs or not parts:
        parts.append("{} \u062b\u0627\u0646\u06cc\u0647".format(secs))
    return " \u0648 ".join(parts)


def _bar(percent, width=10):
    percent = max(0, min(100, int(percent)))
    filled = int(round(percent * width / 100.0))
    return "\u25ae" * filled + "\u25af" * (width - filled)


def _state_files():
    """[(persian label, path)] for every persistent state file we know about."""
    files = []
    if Config is not None:
        for attr, label in (
            ("PAL_STATE_FILE", "\u0631\u0641\u06cc\u0642 (pal)"),
            ("ASSISTANT_STATE_FILE", "\u062f\u0633\u062a\u06cc\u0627\u0631 (assistant)"),
            ("MEMORY_STATE_FILE", "\u062d\u0627\u0641\u0638\u0647 (memory)"),
            ("REMINDERS_STATE_FILE", "\u06cc\u0627\u062f\u0622\u0648\u0631\u0647\u0627 (reminders)"),
            ("STICKERS_STATE_FILE", "\u0627\u0633\u062a\u06cc\u06a9\u0631\u0647\u0627 (stickers)"),
            ("API_USAGE_FILE", "\u0645\u0635\u0631\u0641 API (api_usage)"),
        ):
            path = getattr(Config, attr, None)
            if path:
                files.append((label, path))
    try:
        import quiet_hours
        files.append(("\u0633\u0627\u0639\u0627\u062a \u062e\u0648\u0627\u0628 (quiet hours)", quiet_hours.STATE_FILE))
    except Exception:
        pass
    return files


def key_usage_lines():
    """Per-key usage lines (keys masked). Returns (lines, total_today, total_cap)."""
    lines = []
    total_today = 0
    total_cap = 0
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    keys = list(getattr(Config, "GEMINI_API_KEYS", []) or []) if Config else []
    usage = getattr(api_tracker, "usage_data", {}) or {}
    daily_limit = getattr(api_tracker, "limit", 0) or 0
    banned = getattr(api_tracker, "banned_until", {}) or {}

    # configured keys first, then any extra keys found in the usage file
    ordered = keys + [k for k in usage.keys()
                      if isinstance(k, str) and not k.startswith("_") and k not in keys]

    seen = set()
    for key in ordered:
        if key in seen:
            continue
        seen.add(key)

        data = usage.get(key)
        count = 0
        if isinstance(data, dict) and data.get("date") == today:
            try:
                count = int(data.get("count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
        total_today += count
        if key in keys and daily_limit:
            total_cap += daily_limit

        until = banned.get(key, 0)
        if until and until > now:
            state = "\U0001f6d1 \u0628\u0646\u200c\u0634\u062f\u0647 \u2014 {} \u0645\u0627\u0646\u062f\u0647".format(fmt_uptime(until - now))
        elif key in keys and daily_limit and count >= daily_limit:
            state = "\u26d4 \u0633\u0642\u0641 \u0631\u0648\u0632\u0627\u0646\u0647 \u067e\u0631 \u0634\u062f\u0647"
        else:
            state = "\u2705"

        cap_txt = "/{}".format(daily_limit) if (key in keys and daily_limit) else ""
        lines.append("  \u2022 `{}` : {}{} \u062f\u0631\u062e\u0648\u0627\u0633\u062a \u0627\u0645\u0631\u0648\u0632 \u2014 {}".format(
            _mask_key(key), count, cap_txt, state))

    if not lines:
        lines.append("  \u2022 \u0647\u0646\u0648\u0632 \u0647\u06cc\u0686 \u06a9\u0644\u06cc\u062f\u06cc \u062b\u0628\u062a \u0646\u0634\u062f\u0647 \u0627\u0633\u062a.")

    return lines, total_today, total_cap


def build_report():
    """Assemble the full Persian `130` report. 100% read-only."""
    now_utc = datetime.now(timezone.utc)
    parts = ["\U0001f4ca **\u06af\u0632\u0627\u0631\u0634 \u0645\u0635\u0631\u0641 \u0648 \u0648\u0636\u0639\u06cc\u062a \u0631\u0628\u0627\u062a**", ""]

    parts.append("\u23f1\ufe0f \u0622\u067e\u200c\u062a\u0627\u06cc\u0645 \u0631\u0628\u0627\u062a: {}".format(fmt_uptime()))
    parts.append("\U0001f4c5 \u0627\u0645\u0631\u0648\u0632 (UTC): `{}`".format(now_utc.strftime("%Y-%m-%d %H:%M")))
    parts.append("")

    # --- Gemini keys ---
    parts.append("**\U0001f511 \u06a9\u0644\u06cc\u062f\u0647\u0627\u06cc Gemini:**")
    key_lines, total_today, total_cap = key_usage_lines()
    parts.extend(key_lines)
    if total_cap:
        percent = int(total_today * 100 / total_cap) if total_cap else 0
        parts.append("  \u062c\u0645\u0639: {}/{} (\u066a{}) {}".format(
            total_today, total_cap, percent, _bar(percent)))
    parts.append("")

    # --- Per-model rotation ---
    parts.append("**\U0001f9e0 \u0645\u062f\u0644\u200c\u0647\u0627 (\u0686\u0631\u062e\u0634 GEMINI_MODELS):**")
    if api_tracker is not None:
        try:
            parts.append("```")
            parts.extend(api_tracker.model_report().splitlines())
            parts.append("```")
        except Exception as error:
            parts.append("  \u26a0\ufe0f \u06af\u0632\u0627\u0631\u0634 \u0645\u062f\u0644\u200c\u0647\u0627 \u0646\u0627\u0645\u0648\u0641\u0642 \u0628\u0648\u062f: {}".format(error))
    else:
        parts.append("  \u26a0\ufe0f api_tracker \u062f\u0631 \u062f\u0633\u062a\u0631\u0633 \u0646\u06cc\u0633\u062a.")
    parts.append("")

    # --- Active bans, called out explicitly ---
    bans = (getattr(api_tracker, "banned_until", {}) or {}) if api_tracker else {}
    active = [k for k, until in bans.items() if until > time.time()]
    if active:
        parts.append("\U0001f6d1 \u0628\u0646 \u0641\u0639\u0627\u0644: {} \u06a9\u0644\u06cc\u062f \u2014 \u0628\u0646\u200c\u0647\u0627 \u062e\u0648\u062f\u0628\u062e\u0648\u062f \u062a\u0645\u0627\u0645 \u0645\u06cc\u200c\u0634\u0648\u0646\u062f.".format(len(active)))
        parts.append("")

    # --- State files ---
    parts.append("**\U0001f4be \u0641\u0627\u06cc\u0644\u200c\u0647\u0627\u06cc \u0648\u0636\u0639\u06cc\u062a:**")
    for label, path in _state_files():
        try:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            note = _human_size(size) if size else "\u062e\u0627\u0644\u06cc/\u0646\u062f\u0627\u0631\u062f"
        except Exception:
            note = "\u062e\u0637\u0627 \u062f\u0631 \u062e\u0648\u0627\u0646\u062f\u0646"
        parts.append("  \u2022 {}: {}".format(label, note))
    parts.append("")

    parts.append("\u2139\ufe0f \u0641\u0642\u0637 \u062e\u0648\u0627\u0646\u062f\u0646\u06cc \u2014 \u0686\u06cc\u0632\u06cc \u062a\u063a\u06cc\u06cc\u0631 \u0646\u0645\u06cc\u200c\u06a9\u0646\u062f. \u0631\u0627\u0647\u0646\u0645\u0627: `888`")
    return "\n".join(parts)


async def _confirm(client, text):
    """Send a self-destructing confirmation to Saved Messages (same as 121)."""
    try:
        message = await client.send_message("me", text)
    except Exception as error:
        print("\u26a0\ufe0f Stats (130) confirm failed: {}".format(error))
        return
    if STEALTH_CONFIRM and CONFIRM_AUTO_DELETE_SECONDS > 0:
        async def _cleanup():
            try:
                await asyncio.sleep(CONFIRM_AUTO_DELETE_SECONDS)
                await message.delete()
            except Exception:
                pass
        asyncio.create_task(_cleanup())


async def handle_command(event, raw_args):
    """Handle a `130 ...` command. Read-only: never mutates any state."""
    args = _normalize_digits(raw_args or "").strip().split()
    if args:
        await _confirm(event.client,
                       "\u26a0\ufe0f `130` \u0648\u0631\u0648\u062f\u06cc \u0646\u0645\u06cc\u200c\u06af\u06cc\u0631\u062f \u2014 \u0641\u0642\u0637 \u06af\u0632\u0627\u0631\u0634 \u0645\u06cc\u200c\u062f\u0647\u062f.\n\n" + build_report())
        return
    await _confirm(event.client, build_report())


def register(client):
    """Register the `130` handler on a Telethon client (idempotent)."""
    if getattr(client, "_stats_registered", False):
        return
    try:
        from telethon import events
    except Exception as error:
        print("\u26a0\ufe0f Stats (130) could not import telethon events: {}".format(error))
        return

    client._stats_registered = True

    @client.on(events.NewMessage(outgoing=True, pattern=r"^130(?:\s+(.*))?$"))
    async def _stats_handler(event):
        raw_args = event.pattern_match.group(1) or ""
        try:
            await event.delete()
        except Exception:
            pass
        try:
            await handle_command(event, raw_args)
        except Exception as error:
            print("\u26a0\ufe0f Stats (130) command failed: {}".format(error))

    print("\U0001f4ca Stats dashboard ready \u2014 \u06a9\u062f `130`")


def _patch_client_bootstrap():
    """Wrap TelegramClient lifecycle methods so `register` always runs."""
    try:
        from telethon import TelegramClient
    except Exception as error:
        print("\u26a0\ufe0f Stats (130) could not patch TelegramClient: {}".format(error))
        return

    for method_name in ("start", "run_until_disconnected"):
        original = getattr(TelegramClient, method_name, None)
        if original is None or getattr(original, "_stats_patched", False):
            continue

        def make_wrapper(original_method):
            def wrapper(self, *args, **kwargs):
                try:
                    register(self)
                except Exception as error:
                    print("\u26a0\ufe0f Stats (130) registration failed: {}".format(error))
                return original_method(self, *args, **kwargs)

            wrapper._stats_patched = True
            return wrapper

        setattr(TelegramClient, method_name, make_wrapper(original))


_INSTALLED = False


def install_stats(force=False):
    """Install lazy handler registration. Safe to call repeatedly."""
    global _INSTALLED
    if _INSTALLED and not force:
        return
    _INSTALLED = True
    _patch_client_bootstrap()


# Auto-install on import.
install_stats()

