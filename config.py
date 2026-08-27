import os
from dotenv import load_dotenv

load_dotenv()


def _data_dir() -> str:
    """Persistent storage dir (Railway volume mounted at /app/data, or DATA_DIR override)."""
    d = os.getenv("DATA_DIR")
    if d:
        d = d.rstrip("/")
        os.makedirs(d, exist_ok=True)
        return d
    if os.path.isdir("/app/data"):
        return "/app/data"
    return ""


def _state_path(name: str) -> str:
    d = _data_dir()
    return os.path.join(d, name) if d else name


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


# Defaults applied to a model entry when the .env line omits its quotas.
DEFAULT_MODEL_RPM = _int_env("DEFAULT_MODEL_RPM", 15)
DEFAULT_MODEL_RPD = _int_env("DEFAULT_MODEL_RPD", 200)


def _load_models():
    """Parse GEMINI_MODELS into an ordered list of model specs.

    Format (comma separated, quotes optional):
        GEMINI_MODELS="gemini-3.5-flash-lite:15:1000,gemini-2.5-flash:10:250"
                       ^model name        ^rpm ^requests per day

    `rpm` and `rpd` are optional; missing values fall back to
    DEFAULT_MODEL_RPM / DEFAULT_MODEL_RPD. When GEMINI_MODELS is empty we keep
    full backwards compatibility and use the single MODEL_NAME value.
    """
    raw = (os.getenv("GEMINI_MODELS") or "").strip().strip('"').strip("'")
    models = []
    seen = set()

    for chunk in raw.split(","):
        chunk = chunk.strip().strip('"').strip("'")
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        name = parts[0]
        if not name or name in seen:
            continue

        def _part(index: int, default: int) -> int:
            if len(parts) > index and parts[index]:
                try:
                    return max(0, int(parts[index]))
                except ValueError:
                    return default
            return default

        seen.add(name)
        models.append({
            "name": name,
            "rpm": _part(1, DEFAULT_MODEL_RPM),
            "rpd": _part(2, DEFAULT_MODEL_RPD),
        })

    if not models:
        models.append({
            "name": os.getenv("MODEL_NAME", "gemini-3.5-flash-lite"),
            "rpm": DEFAULT_MODEL_RPM,
            "rpd": DEFAULT_MODEL_RPD,
        })

    return models


class Config:
    API_ID = int(os.getenv("API_ID") or 0)
    API_HASH = os.getenv("API_HASH") or ""

    @staticmethod
    def _load_keys():
        keys = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
        if os.path.exists("apis.txt"):
            try:
                with open("apis.txt", "r", encoding="utf-8") as f:
                    file_keys = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                    for k in file_keys:
                        if k not in keys:
                            keys.append(k)
            except Exception:
                pass
        return keys

    GEMINI_API_KEYS = _load_keys()

    # --- Model rotation ---
    # GEMINI_MODELS lets several models share the load, each with its own quota.
    # When a model hits its RPM/RPD (or gets rate limited) the engine moves to
    # the next one instead of failing the request.
    GEMINI_MODELS = _load_models()
    MODEL_NAME = os.getenv("MODEL_NAME", GEMINI_MODELS[0]["name"])
    MODEL_NAMES = [m["name"] for m in GEMINI_MODELS]

    SESSION_NAME = os.getenv("SESSION_NAME", "teleagent_session")
    # Railway/Cloud deployment: full Telethon session as a base64 string (takes priority over session file)
    SESSION_STRING = os.getenv("SESSION_STRING", "")
    OWNER_ID = int(os.getenv("OWNER_ID") or 0)

    # Persona display name used in group mention detection and prompts
    PERSONA_NAME = os.getenv("PERSONA_NAME", "شایان")

    # --- Owner identity profile (used to ground Pal/Assistant answers) ---
    # Everything here is optional. Whatever you fill in gets woven into the
    # system prompt as short, casual first-person notes, so the bot can answer
    # "who are you / what do you do / how can I reach you" without inventing.
    OWNER_NAME = (os.getenv("OWNER_NAME") or "").strip()
    OWNER_BIO = (os.getenv("OWNER_BIO") or "").strip()
    OWNER_SERVICES = (os.getenv("OWNER_SERVICES") or "").strip()
    OWNER_INTERESTS = (os.getenv("OWNER_INTERESTS") or "").strip()
    OWNER_WEBSITE = (os.getenv("OWNER_WEBSITE") or "").strip()
    OWNER_SCHEDULE = (os.getenv("OWNER_SCHEDULE") or "").strip()
    OWNER_EXTRA = (os.getenv("OWNER_EXTRA") or "").strip()
    # Set OWNER_PROFILE_IN_PROMPTS=0 to disable the injection entirely.
    OWNER_PROFILE_IN_PROMPTS = os.getenv("OWNER_PROFILE_IN_PROMPTS", "1").lower() not in ("0", "false", "no")

    @classmethod
    def owner_profile_block(cls) -> str:
        """Compact, human-sounding profile of the account owner for prompts.

        Returns an empty string when nothing is configured, so prompts stay
        exactly as they were before this feature existed.
        """
        if not cls.OWNER_PROFILE_IN_PROMPTS:
            return ""

        lines = []
        if cls.OWNER_NAME:
            lines.append(f"اسم صاحب این اکانت {cls.OWNER_NAME} است.")
        if cls.OWNER_BIO:
            lines.append(cls.OWNER_BIO)
        if cls.OWNER_SERVICES:
            lines.append(f"کاری که انجام می‌دهد: {cls.OWNER_SERVICES}")
        if cls.OWNER_INTERESTS:
            lines.append(f"چیزهایی که دوست دارد و راحت درباره‌شان حرف می‌زند: {cls.OWNER_INTERESTS}")
        if cls.OWNER_SCHEDULE:
            lines.append(f"وقت‌هایی که معمولاً در دسترس است: {cls.OWNER_SCHEDULE}")
        if cls.OWNER_WEBSITE:
            lines.append(f"اگر کسی لینک یا نمونه‌کار خواست: {cls.OWNER_WEBSITE}")
        if cls.OWNER_EXTRA:
            lines.append(cls.OWNER_EXTRA)

        if not lines:
            return ""

        header = (
            "چند نکتهٔ واقعی دربارهٔ صاحب این اکانت. اینها را فقط وقتی لازم شد و کاملاً طبیعی "
            "در حرف بیاور؛ هیچ‌وقت مثل یک بیوگرافی یا لیست پشت سر هم تعریفشان نکن، و چیزی که "
            "اینجا نیامده از خودت نساز:"
        )
        return header + "\n- " + "\n- ".join(lines)

    # --- Persistent state files (survive restarts when DATA_DIR/volume is set) ---
    PAL_STATE_FILE = _state_path(os.getenv("PAL_STATE_FILE", "pal_state.json"))
    ASSISTANT_STATE_FILE = _state_path(os.getenv("ASSISTANT_STATE_FILE", "assistant_state.json"))
    MEMORY_STATE_FILE = _state_path(os.getenv("MEMORY_STATE_FILE", "memory_state.json"))
    REMINDERS_STATE_FILE = _state_path(os.getenv("REMINDERS_STATE_FILE", "reminders_state.json"))
    STICKERS_STATE_FILE = _state_path(os.getenv("STICKERS_STATE_FILE", "stickers_state.json"))
    API_USAGE_FILE = _state_path(os.getenv("API_USAGE_FILE", "api_usage.json"))

    SHORT_TERM_MEMORY_LIMIT = int(os.getenv("SHORT_TERM_MEMORY_LIMIT", "30"))
    LONG_TERM_SUMMARY_INTERVAL = int(os.getenv("LONG_TERM_SUMMARY_INTERVAL", "30"))
    MAX_LONG_TERM_SUMMARY_CHARS = int(os.getenv("MAX_LONG_TERM_SUMMARY_CHARS", "600"))
    MAX_MESSAGE_SEGMENT_CHARS = int(os.getenv("MAX_MESSAGE_SEGMENT_CHARS", "200"))
    TYPING_SPEED_CPS = float(os.getenv("TYPING_SPEED_CPS", "18.0"))  # characters typed per second
    MIN_TYPING_DELAY = float(os.getenv("MIN_TYPING_DELAY", "1.5"))   # seconds
    MAX_TYPING_DELAY = float(os.getenv("MAX_TYPING_DELAY", "7.0"))   # seconds

    # Gemini hard timeout per key attempt (web-search grounded calls need longer)
    GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "15"))
    SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "45"))

    # Stealth feedback: command confirmations sent to Saved Messages and auto-deleted
    STEALTH_CONFIRM = os.getenv("STEALTH_CONFIRM", "1").lower() not in ("0", "false", "no")
    CONFIRM_AUTO_DELETE_SECONDS = float(os.getenv("CONFIRM_AUTO_DELETE_SECONDS", "10"))
    # Push engine/runtime errors to Saved Messages instead of dying silently in logs
    NOTIFY_ERRORS = os.getenv("NOTIFY_ERRORS", "1").lower() not in ("0", "false", "no")
