import json
import os
import time
from datetime import datetime, timezone

from config import Config

class APIUsageTracker:
    def __init__(self, filename=None):
        self.filename = filename or Config.API_USAGE_FILE
        self.limit = 490  # 10 requests safety buffer below Google's 500 limit
        self.rpm_limit = 15 # Google's 15 requests per minute limit per key

        # Daily usage + circuit-breaker bans are persisted; RPM stays in-memory.
        self.usage_data = self._load()

        # Circuit Breaker: api_key -> consecutive_errors (in-memory only)
        self.consecutive_errors = {}
        # Ban expiry: api_key -> timestamp (seconds since epoch) — PERSISTED across restarts
        self.banned_until = self.usage_data.get("_bans", {})
        if not isinstance(self.banned_until, dict):
            self.banned_until = {}

        # RPM Tracking: api_key -> list of timestamps (seconds)
        self.rpm_timestamps = {}

        # ---- Per-model quota tracking (GEMINI_MODELS) ----
        # model name -> {"rpm": int, "rpd": int}
        self.model_specs = {m["name"]: {"rpm": m.get("rpm", 15), "rpd": m.get("rpd", 200)}
                            for m in Config.GEMINI_MODELS}
        # model name -> list of request timestamps in the last 60s (in-memory)
        self.model_rpm_timestamps = {}
        # model name -> timestamp until which the model is skipped (in-memory)
        self.model_cooldowns = {}
        # Round-robin pointer over the configured model list
        self.model_idx = 0

    def _load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        try:
            data = dict(self.usage_data)
            data["_bans"] = self.banned_until
            tmp = f"{self.filename}.tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, self.filename)
        except Exception:
            pass

    def _get_today_str(self):
        # Google resets limits at midnight Pacific Time usually, but UTC is a safe standard
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def is_key_available(self, api_key: str) -> bool:
        now = time.time()

        # 1. Circuit Breaker Check (survives restarts now)
        if api_key in self.banned_until:
            if now < self.banned_until[api_key]:
                return False
            else:
                # Ban expired → clear it from memory AND disk
                del self.banned_until[api_key]
                self.consecutive_errors[api_key] = 0
                self._save()

        # 2. RPM Check
        if api_key in self.rpm_timestamps:
            # Keep only timestamps from the last 60 seconds
            recent = [ts for ts in self.rpm_timestamps[api_key] if now - ts < 60]
            self.rpm_timestamps[api_key] = recent
            if len(recent) >= self.rpm_limit:
                return False

        # 3. Daily Limit Check (ignore the reserved "_bans" key)
        today = self._get_today_str()
        key_data = self.usage_data.get(api_key, {})
        if not isinstance(key_data, dict):
            return True

        # If the key was used on a previous day, it is available (and starts at 0)
        if key_data.get("date") != today:
            return True

        return key_data.get("count", 0) < self.limit

    def record_usage(self, api_key: str):
        now = time.time()

        # Update RPM
        if api_key not in self.rpm_timestamps:
            self.rpm_timestamps[api_key] = []
        self.rpm_timestamps[api_key].append(now)

        # Update Daily
        today = self._get_today_str()
        key_data = self.usage_data.get(api_key, {})
        if not isinstance(key_data, dict):
            key_data = {}

        if key_data.get("date") != today:
            key_data = {"date": today, "count": 1}
        else:
            key_data["count"] = key_data.get("count", 0) + 1

        self.usage_data[api_key] = key_data
        self._save()

    def record_success(self, api_key: str):
        """Reset consecutive errors on success."""
        self.consecutive_errors[api_key] = 0

    def record_error(self, api_key: str):
        """Increment consecutive errors, ban if >= 3."""
        errors = self.consecutive_errors.get(api_key, 0) + 1
        self.consecutive_errors[api_key] = errors

        if errors >= 3:
            # Ban duration: with several keys a 3h ban gives the others room.
            # With a SINGLE key a 3h ban means total blindness — cap it short so
            # the bot degrades instead of dying (rate limits usually clear fast).
            try:
                key_count = max(1, len(Config.GEMINI_API_KEYS))
            except Exception:
                key_count = 1
            ban_seconds = (3 * 3600) if key_count > 1 else (15 * 60)
            print(f"🛑 Circuit Breaker TRIPPED for key {api_key[:8]}...! Banning for {ban_seconds // 60} minutes.")
            self.banned_until[api_key] = time.time() + ban_seconds
            self._save()  # persist ban so a restart doesn't resurrect dead keys

    def lift_all_bans_if_blind(self):
        """Self-heal: if EVERY configured key is banned, clear the bans.

        A fully-blind bot is worse than a rate-limited one; Google's per-minute
        limits recover on their own, so retrying is the better failure mode.
        """
        if not self.banned_until or not Config.GEMINI_API_KEYS:
            return
        now = time.time()
        active_bans = [k for k in Config.GEMINI_API_KEYS if self.banned_until.get(k, 0) > now]
        if active_bans and len(active_bans) >= len(Config.GEMINI_API_KEYS):
            print("🔓 ALL keys banned — lifting circuit-breaker bans to avoid total blindness.")
            for k in active_bans:
                del self.banned_until[k]
            self.consecutive_errors.clear()
            self._save()

    # ==================================================================
    # 🧠 Per-model quotas (GEMINI_MODELS="model:rpm:rpd,...")
    # ==================================================================
    def _models_bucket(self) -> dict:
        """Persisted per-model daily counters, stored under the "_models" key."""
        bucket = self.usage_data.get("_models")
        if not isinstance(bucket, dict):
            bucket = {}
            self.usage_data["_models"] = bucket
        return bucket

    def _spec(self, model: str) -> dict:
        return self.model_specs.get(model, {"rpm": self.rpm_limit, "rpd": self.limit})

    def model_daily_count(self, model: str) -> int:
        data = self._models_bucket().get(model, {})
        if not isinstance(data, dict) or data.get("date") != self._get_today_str():
            return 0
        return int(data.get("count", 0) or 0)

    def is_model_available(self, model: str) -> bool:
        """True when the model is neither cooling down nor over its RPM/RPD."""
        now = time.time()

        cooldown = self.model_cooldowns.get(model, 0)
        if cooldown:
            if now < cooldown:
                return False
            del self.model_cooldowns[model]

        spec = self._spec(model)

        rpm = int(spec.get("rpm") or 0)
        if rpm:
            recent = [ts for ts in self.model_rpm_timestamps.get(model, []) if now - ts < 60]
            self.model_rpm_timestamps[model] = recent
            if len(recent) >= rpm:
                return False

        rpd = int(spec.get("rpd") or 0)
        if rpd and self.model_daily_count(model) >= rpd:
            return False

        return True

    def record_model_usage(self, model: str):
        """Count one request against this model's per-minute and daily quota."""
        now = time.time()
        self.model_rpm_timestamps.setdefault(model, []).append(now)

        today = self._get_today_str()
        bucket = self._models_bucket()
        data = bucket.get(model)
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today, "count": 1}
        else:
            data["count"] = int(data.get("count", 0) or 0) + 1
        bucket[model] = data
        self._save()

    def cooldown_model(self, model: str, seconds: int = 120, reason: str = ""):
        """Temporarily skip a model (rate limited, unsupported, 5xx, ...)."""
        self.model_cooldowns[model] = time.time() + max(1, int(seconds))
        note = f" ({reason})" if reason else ""
        print(f"⏭️ Model {model} on cooldown for {int(seconds)}s{note}")

    def get_model_order(self) -> list:
        """Configured models, round-robin, available ones first.

        Exhausted/cooling-down models are kept at the end of the list as a last
        resort so the bot never ends up with zero candidates.
        """
        names = list(Config.MODEL_NAMES) or [Config.MODEL_NAME]
        if len(names) > 1:
            self.model_idx = (self.model_idx + 1) % len(names)
            names = names[self.model_idx:] + names[:self.model_idx]

        available = [m for m in names if self.is_model_available(m)]
        blocked = [m for m in names if m not in available]
        return available + blocked

    def model_report(self) -> str:
        """One-line-per-model usage summary (handy for the 555 status card)."""
        now = time.time()
        lines = []
        for name in (Config.MODEL_NAMES or [Config.MODEL_NAME]):
            spec = self._spec(name)
            used = self.model_daily_count(name)
            rpd = int(spec.get("rpd") or 0)
            recent = len([ts for ts in self.model_rpm_timestamps.get(name, []) if now - ts < 60])
            rpm = int(spec.get("rpm") or 0)
            cooldown = self.model_cooldowns.get(name, 0)
            if cooldown and now < cooldown:
                state = f"cooldown {int(cooldown - now)}s"
            elif self.is_model_available(name):
                state = "ok"
            else:
                state = "limit"
            lines.append(f"{name}: {used}/{rpd or '∞'} today, {recent}/{rpm or '∞'} rpm — {state}")
        return "\n".join(lines)

# Global singleton instance
api_tracker = APIUsageTracker()
