"""
memory_backup.py - Secret code 133: automatic backups of the bot's state files.

Why this exists
---------------
GitHub only stores the CODE. Everything the bot LEARNS lives in the runtime
state files (memory, reminders, pal chats, stickers, quiet hours, api usage)
inside DATA_DIR on the Railway volume. If that volume is ever wiped, months
of memory die with it. `133` fixes that:

  * Every day at a configurable local time it zips ALL state files into
    DATA_DIR/backups/ (rotating, keeps the newest N).
  * Optionally sends the zip to Saved Messages (off-server copy).
  * `133 now` / `133 restore` do it on demand.

Like quiet_hours / human_behavior / stats_command, this module self-installs:
importing it patches TelegramClient.start / run_until_disconnected so the
command handler AND the daily loop start without touching main.py.
pal_manager.py imports it last.
"""

import os
import time
import asyncio
import zipfile
from datetime import datetime

try:
    from config import Config
except Exception:  # pragma: no cover - config should always import
    Config = None

STEALTH_CONFIRM = (os.getenv("STEALTH_CONFIRM", "1").lower() not in ("0", "false", "no", "off"))
CONFIRM_AUTO_DELETE_SECONDS = int(float(os.getenv("CONFIRM_AUTO_DELETE_SECONDS", "10") or 10))

DEFAULT_TIME = (os.getenv("MEMORY_BACKUP_TIME", "04:00") or "04:00").strip()
DEFAULT_ENABLED = (os.getenv("MEMORY_BACKUP_ENABLED", "1").lower() not in ("0", "false", "no", "off"))
DEFAULT_SEND = (os.getenv("MEMORY_BACKUP_SEND", "1").lower() not in ("0", "false", "no", "off"))
DEFAULT_KEEP = max(1, int(float(os.getenv("MEMORY_BACKUP_KEEP", "7") or 7)))
CHECK_INTERVAL = max(10, int(float(os.getenv("MEMORY_BACKUP_CHECK_SECONDS", "120") or 120)))
MAX_SEND_BYTES = 1_900_000_000  # stay under Telegram's 2 GB account upload cap

BACKUP_PREFIX = "ghostali_state_backup_"
MANIFEST_NAME = "BACKUP_INFO.txt"
STATE_VERSION = 1

_PERSIAN_DIGITS = str.maketrans(
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
    "01234567890123456789",
)


def _env(name, default=""):
    value = os.getenv(name)
    return default if value is None else value.strip()


def _float_env(name, default):
    try:
        raw = os.getenv(name)
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


STATE_FILE = os.path.join(_state_dir(), "memory_backup_state.json")


def _normalize_digits(text):
    return (text or "").translate(_PERSIAN_DIGITS)


def _bot_now():
    """Bot-local time — reuse quiet_hours' timezone when available."""
    try:
        import quiet_hours
        return quiet_hours.now_local()
    except Exception:
        return datetime.now()


def _parse_clock(text):
    """'04:00' / '4:0' / '۰۴:۰۰' -> minutes since midnight, else None."""
    raw = _normalize_digits((text or "").strip())
    if ":" not in raw:
        return None
    try:
        hours_raw, minutes_raw = raw.split(":", 1)
        hours = int(hours_raw)
        minutes = int(minutes_raw)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def _format_clock(minutes):
    minutes = int(minutes or 0) % 1440
    return "{:02d}:{:02d}".format(minutes // 60, minutes % 60)


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


def _fmt_ts(ts):
    """epoch -> 'YYYY-MM-DD HH:MM' bot-local, or a friendly 'never yet'."""
    if not ts:
        return "\u0647\u0646\u0648\u0632 \u0628\u06a9\u0627\u067e\u06cc \u0646\u06cc\u0633\u062a"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "\u061f"


def _state_files():
    """[(label, path)] for every persistent state file we know about."""
    files = []
    if Config is not None:
        for attr, label in (
            ("PAL_STATE_FILE", "pal"),
            ("ASSISTANT_STATE_FILE", "assistant"),
            ("MEMORY_STATE_FILE", "memory"),
            ("REMINDERS_STATE_FILE", "reminders"),
            ("STICKERS_STATE_FILE", "stickers"),
            ("API_USAGE_FILE", "api_usage"),
        ):
            path = getattr(Config, attr, None)
            if path:
                files.append((label, path))
    try:
        import quiet_hours
        files.append(("quiet_hours", quiet_hours.STATE_FILE))
    except Exception:
        pass
    return files


def backup_dir():
    return os.path.join(_state_dir(), "backups")


# ---------------------------------------------------------------------------
# Persisted settings (survive restarts, like every other *_state.json)
# ---------------------------------------------------------------------------

_DEFAULT_STATE = {
    "version": STATE_VERSION,
    "enabled": DEFAULT_ENABLED,
    "send": DEFAULT_SEND,
    "keep": DEFAULT_KEEP,
    "time": DEFAULT_TIME if _parse_clock(DEFAULT_TIME) is not None else "04:00",
    "last_run": "",
    "last_backup": 0,
    "last_size": 0,
}


def _load_state():
    import json
    state = dict(_DEFAULT_STATE)
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in ("enabled", "send", "keep", "time", "last_run", "last_backup", "last_size"):
                    if key in data:
                        state[key] = data[key]
    except Exception as error:
        print("\u26a0\ufe0f Memory backup state unreadable ({}), using defaults.".format(error))
    state["keep"] = max(1, min(30, int(state.get("keep") or DEFAULT_KEEP)))
    if _parse_clock(state.get("time")) is None:
        state["time"] = "04:00"
    return state


def _save_state(state):
    import json
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as error:
        print("\u26a0\ufe0f Memory backup state could not be saved: {}".format(error))


state = _load_state()

def create_backup():
    """Zip every state file into DATA_DIR/backups/. Returns (path, size, files_in_zip)."""
    target_dir = backup_dir()
    os.makedirs(target_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    zip_path = os.path.join(target_dir, "{}{}.zip".format(BACKUP_PREFIX, stamp))

    included = 0
    total_bytes = 0
    manifest_lines = [
        "GhostGram PRO - STATE BACKUP",
        "Created: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "Source dir: {}".format(_state_dir()),
        "",
    ]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for label, path in _state_files():
            try:
                if path and os.path.exists(path) and os.path.getsize(path) > 0:
                    zf.write(path, os.path.basename(path))
                    included += 1
                    total_bytes += os.path.getsize(path)
                    manifest_lines.append("- {} ({}): {} bytes".format(
                        label, os.path.basename(path), os.path.getsize(path)))
            except OSError as error:
                print("\u26a0\ufe0f Memory backup skipped {} ({}): {}".format(label, path, error))
        manifest_lines.append("")
        manifest_lines.append("RESTORE: unzip these files over DATA_DIR and restart the bot.")
        zf.writestr("_" + MANIFEST_NAME, "\n".join(manifest_lines))

    size = os.path.getsize(zip_path)
    state["last_backup"] = time.time()
    state["last_size"] = size
    _save_state(state)
    prune_backups()
    return zip_path, size, included


def prune_backups(keep=None):
    """Delete the oldest state backups, keeping the newest `keep` ones."""
    keep = state.get("keep", DEFAULT_KEEP) if keep is None else max(1, int(keep))
    target_dir = backup_dir()
    if not os.path.isdir(target_dir):
        return 0
    backups = sorted(
        f for f in os.listdir(target_dir)
        if f.startswith(BACKUP_PREFIX) and f.endswith(".zip")
    )
    removed = 0
    for old in backups[:-keep] if len(backups) > keep else []:
        try:
            os.remove(os.path.join(target_dir, old))
            removed += 1
        except OSError:
            pass
    return removed


def list_backups():
    """Existing state backups, newest first: [(name, size, mtime)]."""
    target_dir = backup_dir()
    if not os.path.isdir(target_dir):
        return []
    result = []
    for name in os.listdir(target_dir):
        if name.startswith(BACKUP_PREFIX) and name.endswith(".zip"):
            full = os.path.join(target_dir, name)
            try:
                result.append((name, os.path.getsize(full), os.path.getmtime(full)))
            except OSError:
                continue
    return sorted(result, reverse=True)


def latest_backup():
    entries = list_backups()
    if not entries:
        return None
    return os.path.join(backup_dir(), entries[0][0]), entries[0][1], entries[0][2]


async def _send_backup(client, path):
    """Send a backup zip to Saved Messages. Returns True when sent."""
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size > MAX_SEND_BYTES:
        print("\u26a0\ufe0f Memory backup too large to send: {}".format(_human_size(size)))
        return False
    try:
        caption = "\U0001f4be \u0628\u06a9\u0627\u067e \u062d\u0627\u0641\u0638\u0647 GhostGram \u2014 {} ({})".format(
            datetime.now().strftime("%Y-%m-%d %H:%M"), _human_size(size))
        await client.send_file("me", path, caption=caption)
        return True
    except Exception as error:
        print("\u26a0\ufe0f Memory backup send failed: {}".format(error))
        return False


def _due(now=None):
    """True when today's scheduled backup has not run yet and its time passed."""
    now = now or _bot_now()
    if not state.get("enabled"):
        return False
    minutes = _parse_clock(state.get("time"))
    if minutes is None:
        return False
    scheduled = now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    return now >= scheduled and state.get("last_run") != today


async def _backup_loop(client):
    """Daily backup watchdog. Catches up after restarts, runs once per day."""
    print("\U0001f4be Memory backup loop running \u2014 daily at {} (bot-local)".format(state.get("time")))
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            if not _due():
                continue
            zip_path, size, included = create_backup()
            state["last_run"] = _bot_now().strftime("%Y-%m-%d")
            _save_state(state)
            print("\U0001f4be Memory backup created: {} ({} files, {})".format(
                os.path.basename(zip_path), included, _human_size(size)))
            if state.get("send"):
                await _send_backup(client, zip_path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print("\u26a0\ufe0f Memory backup loop error: {}".format(error))


def _ensure_loop(client):
    """Start the watchdog exactly once per client."""
    if getattr(client, "_memory_backup_loop_started", False):
        return
    client._memory_backup_loop_started = True
    try:
        client.loop.create_task(_backup_loop(client))
    except Exception as error:
        print("\u26a0\ufe0f Memory backup loop failed to start: {}".format(error))


def status_text():
    """Persian status card for `133` (and the ready line at startup)."""
    lines = [
        "\U0001f4be **\u0628\u06a9\u0627\u067e \u062e\u0648\u062f\u06a9\u0627\u0631 \u062d\u0627\u0641\u0638\u0647**",
        "\u2022 \u0648\u0636\u0639\u06cc\u062a: {}".format(
            "\u2705 \u0641\u0639\u0627\u0644" if state.get("enabled") else "\u274c \u062e\u0627\u0645\u0648\u0634"),
        "\u2022 \u0632\u0645\u0627\u0646 \u0631\u0648\u0632\u0627\u0646\u0647: `{}` (\u0633\u0627\u0639\u062a \u0645\u062d\u0644\u06cc \u0631\u0628\u0627\u062a: `{}`)".format(
            _format_clock(_parse_clock(state.get("time")) or 240),
            _bot_now().strftime("%H:%M")),
        "\u2022 \u0627\u0631\u0633\u0627\u0644 \u0628\u0647 \u067e\u06cc\u0627\u0645\u200c\u0647\u0627\u06cc \u0630\u062e\u06cc\u0631\u0647\u200c\u0634\u062f\u0647: {}".format(
            "\u2705" if state.get("send") else "\u274c"),
        "\u2022 \u0646\u0633\u062e\u0647\u200c\u0647\u0627\u06cc \u0646\u06af\u0647\u200c\u062f\u0627\u0634\u062a\u0647\u200c\u0634\u062f\u0647: {}".format(state.get("keep")),
        "\u2022 \u0622\u062e\u0631\u06cc\u0646 \u0628\u06a9\u0627\u067e: {} ({})".format(
            _fmt_ts(state.get("last_backup")),
            _human_size(state.get("last_size")) if state.get("last_size") else "\u2014"),
        "\u2022 \u0628\u06a9\u0627\u067e\u200c\u0647\u0627\u06cc \u0645\u0648\u062c\u0648\u062f: {}".format(len(list_backups())),
    ]
    return "\n".join(lines)


HELP_TEXT = (
    "\U0001f4be **\u0628\u06a9\u0627\u067e \u062d\u0627\u0641\u0638\u0647 (MEMORY BACKUP)**\n"
    "\u2022 `133` : \u0648\u0636\u0639\u06cc\u062a \u0628\u06a9\u0627\u067e \u062e\u0648\u062f\u06a9\u0627\u0631\n"
    "\u2022 `133 now` : \u0628\u06a9\u0627\u067e \u0641\u0648\u0631\u06cc \u0648 \u0627\u0631\u0633\u0627\u0644 \u0628\u0647 \u067e\u06cc\u0627\u0645\u200c\u0647\u0627\u06cc \u0630\u062e\u06cc\u0631\u0647\u200c\u0634\u062f\u0647\n"
    "\u2022 `133 time 04:00` : \u0633\u0627\u0639\u062a \u0628\u06a9\u0627\u067e \u0631\u0648\u0632\u0627\u0646\u0647\n"
    "\u2022 `133 send on/off` : \u0627\u0631\u0633\u0627\u0644 \u062e\u0648\u062f\u06a9\u0627\u0631 \u0628\u06a9\u0627\u067e \u0631\u0648\u0632\u0627\u0646\u0647\n"
    "\u2022 `133 keep 7` : \u062a\u0639\u062f\u0627\u062f \u0646\u0633\u062e\u0647\u200c\u0647\u0627\u06cc \u0646\u06af\u0647\u200c\u062f\u0627\u0634\u062a\u0647\u200c\u0634\u062f\u0647 \u0631\u0648\u06cc \u0633\u0631\u0648\u0631\n"
    "\u2022 `133 list` : \u0644\u06cc\u0633\u062a \u0628\u06a9\u0627\u067e\u200c\u0647\u0627\u06cc \u0645\u0648\u062c\u0648\u062f\n"
    "\u2022 `133 restore` : \u0627\u0631\u0633\u0627\u0644 \u0622\u062e\u0631\u06cc\u0646 \u0628\u06a9\u0627\u067e \u0628\u0631\u0627\u06cc \u062f\u0627\u0646\u0644\u0648\u062f\n"
    "\u2022 `133 on` / `133 off` : \u0631\u0648\u0634\u0646/\u062e\u0627\u0645\u0648\u0634 \u06a9\u0631\u062f\u0646 \u0628\u06a9\u0627\u067e \u062e\u0648\u062f\u06a9\u0627\u0631"
)

NOW_WORDS = ("now", "\u0627\u0644\u0627\u0646", "\u0641\u0648\u0631\u06cc")
ON_WORDS = ("on", "\u0631\u0648\u0634\u0646", "\u0641\u0639\u0627\u0644")
OFF_WORDS = ("off", "\u062e\u0627\u0645\u0648\u0634", "\u063a\u06cc\u0631\u0641\u0639\u0627\u0644")
SEND_WORDS = ("send", "\u0627\u0631\u0633\u0627\u0644")
RESTORE_WORDS = ("restore", "\u0628\u0627\u0632\u06cc\u0627\u0628\u06cc", "\u0628\u0627\u0632\u06af\u0631\u062f\u0627\u0646\u06cc")
LIST_WORDS = ("list", "\u0644\u06cc\u0633\u062a")
KEEP_WORDS = ("keep", "\u0646\u06af\u0647\u062f\u0627\u0631\u0634")
TIME_WORDS = ("time", "\u0633\u0627\u0639\u062a")


async def _confirm(client, text):
    """Send a self-destructing confirmation to Saved Messages (same as 121)."""
    try:
        message = await client.send_message("me", text)
    except Exception as error:
        print("\u26a0\ufe0f Memory backup confirm failed: {}".format(error))
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
    """Handle a `133 ...` command."""
    client = event.client
    args = _normalize_digits(raw_args or "").strip().split()
    head = args[0].lower() if args else ""

    if not args:
        await _confirm(client, status_text())
        return

    if head in NOW_WORDS:
        zip_path, size, included = create_backup()
        state["last_run"] = _bot_now().strftime("%Y-%m-%d")
        _save_state(state)
        sent = await _send_backup(client, zip_path)
        await _confirm(client,
                       "\u2705 \u0628\u06a9\u0627\u067e \u0633\u0627\u062e\u062a\u0647 \u0634\u062f: `{}` \u2014 {} \u0641\u0627\u06cc\u0644 ({})\n\u0627\u0631\u0633\u0627\u0644: {}".format(
                           os.path.basename(zip_path), included, _human_size(size),
                           "\u2705 \u0628\u0647 \u067e\u06cc\u0627\u0645\u200c\u0647\u0627\u06cc \u0630\u062e\u06cc\u0631\u0647\u200c\u0634\u062f\u0647" if sent else "\u274c \u0646\u0627\u0645\u0648\u0641\u0642 (\u0644\u0627\u06af \u0631\u0627 \u0628\u0628\u06cc\u0646\u06cc\u062f)"))
        return

    if head in ON_WORDS:
        state["enabled"] = True
        _save_state(state)
        await _confirm(client, "\u2705 \u0628\u06a9\u0627\u067e \u062e\u0648\u062f\u06a9\u0627\u0631 \u0631\u0648\u0632\u0627\u0646\u0647 \u0631\u0648\u0634\u0646 \u0634\u062f.\n\n{}".format(status_text()))
        return

    if head in OFF_WORDS:
        state["enabled"] = False
        _save_state(state)
        await _confirm(client, "\u274c \u0628\u06a9\u0627\u067e \u062e\u0648\u062f\u06a9\u0627\u0631 \u062e\u0627\u0645\u0648\u0634 \u0634\u062f (\u0628\u06a9\u0627\u067e \u062f\u0633\u062a\u06cc \u0628\u0627 `133 now` \u0647\u0645\u0686\u0646\u0627\u0646 \u06a9\u0627\u0631 \u0645\u06cc\u200c\u06a9\u0646\u062f).")
        return

    if head in SEND_WORDS:
        if len(args) > 1 and args[1].lower() in ON_WORDS:
            state["send"] = True
            _save_state(state)
            await _confirm(client, "\u2705 \u0627\u0631\u0633\u0627\u0644 \u062e\u0648\u062f\u06a9\u0627\u0631 \u0628\u06a9\u0627\u067e \u0631\u0648\u0632\u0627\u0646\u0647 \u0631\u0648\u0634\u0646 \u0634\u062f.")
            return
        if len(args) > 1 and args[1].lower() in OFF_WORDS:
            state["send"] = False
            _save_state(state)
            await _confirm(client, "\u274c \u0627\u0631\u0633\u0627\u0644 \u062e\u0648\u062f\u06a9\u0627\u0631 \u0628\u06a9\u0627\u067e \u0631\u0648\u0632\u0627\u0646\u0647 \u062e\u0627\u0645\u0648\u0634 \u0634\u062f (\u0628\u06a9\u0627\u067e \u0645\u062d\u0644\u06cc \u0645\u06cc\u200c\u0645\u0627\u0646\u062f).")
            return
        latest = latest_backup()
        if latest is None:
            await _confirm(client, "\u26a0\ufe0f \u0647\u0646\u0648\u0632 \u0628\u06a9\u0627\u067e\u06cc \u0646\u06cc\u0633\u062a \u2014 \u0627\u0628\u062a\u062f\u0627 `133 now` \u0631\u0627 \u0628\u0632\u0646\u06cc\u062f.")
            return
        sent = await _send_backup(client, latest[0])
        await _confirm(client, "\u2705 \u0622\u062e\u0631\u06cc\u0646 \u0628\u06a9\u0627\u067e \u0627\u0631\u0633\u0627\u0644 \u0634\u062f." if sent
                       else "\u274c \u0627\u0631\u0633\u0627\u0644 \u0646\u0627\u0645\u0648\u0641\u0642 \u0628\u0648\u062f.")
        return

    if head in RESTORE_WORDS:
        latest = latest_backup()
        if latest is None:
            await _confirm(client, "\u26a0\ufe0f \u0647\u0646\u0648\u0632 \u0628\u06a9\u0627\u067e\u06cc \u0646\u06cc\u0633\u062a \u2014 \u0627\u0628\u062a\u062f\u0627 `133 now` \u0631\u0627 \u0628\u0632\u0646\u06cc\u062f.")
            return
        sent = await _send_backup(client, latest[0])
        await _confirm(client,
                       "\u2705 \u0622\u062e\u0631\u06cc\u0646 \u0628\u06a9\u0627\u067e \u0627\u0631\u0633\u0627\u0644 \u0634\u062f \u2014 \u0628\u0631\u0627\u06cc \u0628\u0627\u0632\u06af\u0631\u062f\u0627\u0646\u06cc\u060c \u0645\u062d\u062a\u0648\u0627\u06cc ZIP \u0631\u0627 \u0631\u0648\u06cc DATA_DIR \u0628\u0627\u0632 \u06a9\u0646\u06cc\u062f \u0648 \u0631\u0628\u0627\u062a \u0631\u0627 \u0631\u06cc\u0633\u062a\u0627\u0631\u062a \u06a9\u0646\u06cc\u062f." if sent
                       else "\u274c \u0627\u0631\u0633\u0627\u0644 \u0646\u0627\u0645\u0648\u0641\u0642 \u0628\u0648\u062f.")
        return

    if head in LIST_WORDS:
        entries = list_backups()
        if not entries:
            await _confirm(client, "\u26a0\ufe0f \u0647\u0646\u0648\u0632 \u0628\u06a9\u0627\u067e\u06cc \u0631\u0648\u06cc \u0633\u0631\u0648\u0631 \u0646\u06cc\u0633\u062a.")
            return
        lines = ["\U0001f4c2 **\u0628\u06a9\u0627\u067e\u200c\u0647\u0627\u06cc \u0645\u0648\u062c\u0648\u062f (\u062c\u062f\u06cc\u062f \u2192 \u0642\u062f\u06cc\u0645):**"]
        for name, size, _mtime in entries[:10]:
            lines.append("  \u2022 `{}` ({})".format(name, _human_size(size)))
        await _confirm(client, "\n".join(lines))
        return

    if head in KEEP_WORDS:
        try:
            keep = int(args[1]) if len(args) > 1 else None
        except ValueError:
            keep = None
        if keep is None:
            await _confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `133 keep 7`")
            return
        state["keep"] = max(1, min(30, keep))
        _save_state(state)
        removed = prune_backups()
        await _confirm(client, "\u2705 \u062d\u0641\u0638 {} \u0646\u0633\u062e\u0647 \u0622\u062e\u0631 (\u0646\u0633\u062e\u0647\u200c\u0647\u0627\u06cc \u0627\u0636\u0627\u0641\u0647: {} \u062d\u0630\u0641 \u0634\u062f).".format(
            state["keep"], removed))
        return

    if head in TIME_WORDS:
        minutes = _parse_clock(args[1]) if len(args) > 1 else None
        if minutes is None:
            await _confirm(client, "\u26a0\ufe0f \u0645\u062b\u0627\u0644 \u062f\u0631\u0633\u062a: `133 time 04:00`")
            return
        state["time"] = _format_clock(minutes)
        _save_state(state)
        await _confirm(client, "\u2705 \u0628\u06a9\u0627\u067e \u0631\u0648\u0632\u0627\u0646\u0647 \u0631\u0648\u06cc `{}` \u062a\u0646\u0638\u06cc\u0645 \u0634\u062f (\u0633\u0627\u0639\u062a \u0645\u062d\u0644\u06cc \u0631\u0628\u0627\u062a: `{}`).".format(
            state["time"], _bot_now().strftime("%H:%M")))
        return

    await _confirm(client, "\u26a0\ufe0f \u062f\u0633\u062a\u0648\u0631 \u0631\u0627 \u0646\u0641\u0647\u0645\u06cc\u062f\u0645.\n\n{}".format(HELP_TEXT))


def register(client):
    """Register the `133` handler on a Telethon client (idempotent)."""
    if getattr(client, "_memory_backup_registered", False):
        return
    try:
        from telethon import events
    except Exception as error:
        print("\u26a0\ufe0f Memory backup could not import telethon events: {}".format(error))
        return

    client._memory_backup_registered = True

    @client.on(events.NewMessage(outgoing=True, pattern=r"^133(?:\s+(.*))?$"))
    async def _memory_backup_handler(event):
        raw_args = event.pattern_match.group(1) or ""
        try:
            await event.delete()
        except Exception:
            pass
        try:
            await handle_command(event, raw_args)
        except Exception as error:
            print("\u26a0\ufe0f Memory backup command failed: {}".format(error))

    print("\U0001f4be Memory backup ready \u2014 \u06a9\u062f `133` | {}".format(
        "\u2705 \u0641\u0639\u0627\u0644 \u0631\u0648\u0632\u0627\u0646\u0647 \u0631\u0648\u06cc " + str(state.get("time")) if state.get("enabled") else "\u274c \u062e\u0627\u0645\u0648\u0634"))


def _patch_client_bootstrap():
    """Wrap TelegramClient lifecycle methods so `register` + the loop always run."""
    try:
        from telethon import TelegramClient
    except Exception as error:
        print("\u26a0\ufe0f Memory backup could not patch TelegramClient: {}".format(error))
        return

    for method_name in ("start", "run_until_disconnected"):
        original = getattr(TelegramClient, method_name, None)
        if original is None or getattr(original, "_memory_backup_patched", False):
            continue

        def make_wrapper(original_method):
            def wrapper(self, *args, **kwargs):
                try:
                    register(self)
                    _ensure_loop(self)
                except Exception as error:
                    print("\u26a0\ufe0f Memory backup registration failed: {}".format(error))
                return original_method(self, *args, **kwargs)

            wrapper._memory_backup_patched = True
            return wrapper

        setattr(TelegramClient, method_name, make_wrapper(original))


_INSTALLED = False


def install_memory_backup(force=False):
    """Install lazy handler registration + loop bootstrap. Safe to call repeatedly."""
    global _INSTALLED
    if _INSTALLED and not force:
        return
    _INSTALLED = True
    _patch_client_bootstrap()


# Auto-install on import.
install_memory_backup()





