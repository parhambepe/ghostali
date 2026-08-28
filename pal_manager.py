import os
import json
import asyncio
import random
from config import Config

class PalManager:
    def __init__(self, state_file=Config.PAL_STATE_FILE):
        self.state_file = state_file
        self.active_chats = {} # dict: chat_id -> mode ("normal" or "lust")
        self.auto_engage_chats = {} # dict: chat_id -> duration_minutes
        self._locks = {}
        self._debounce_tasks = {}
        self.load_state()

    def load_state(self):
        """Loads active chat IDs and auto-engage chat IDs from disk."""
        if os.path.exists(self.state_file) and os.path.getsize(self.state_file) > 0:
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.active_chats = {int(chat_id): "normal" for chat_id in data}
                        self.auto_engage_chats = {}
                    elif isinstance(data, dict):
                        raw_active = data.get("active_chats", {})
                        if isinstance(raw_active, list):
                            self.active_chats = {int(chat_id): "normal" for chat_id in raw_active}
                        else:
                            self.active_chats = {int(k): str(v) for k, v in raw_active.items()}
                            
                        raw_engage = data.get("auto_engage_chats", {})
                        if isinstance(raw_engage, list):
                            self.auto_engage_chats = {int(chat_id): 20 for chat_id in raw_engage}
                        else:
                            self.auto_engage_chats = {int(k): int(v) for k, v in raw_engage.items()}
            except Exception as e:
                print(f"\u26a0\ufe0f Error loading Pal state: {e}")
                self.active_chats = {}
                self.auto_engage_chats = {}
        else:
            self.active_chats = {}
            self.auto_engage_chats = {}

    def save_state(self):
        """Persists active chat IDs and auto-engage IDs to disk atomically."""
        try:
            data = {
                "active_chats": self.active_chats,
                "auto_engage_chats": self.auto_engage_chats
            }
            tmp_file = f"{self.state_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            print(f"\u26a0\ufe0f Error saving Pal state: {e}")

    def is_active(self, chat_id: int) -> bool:
        return chat_id in self.active_chats
        
    def get_mode(self, chat_id: int) -> str:
        return self.active_chats.get(chat_id, "normal")
        
    def is_auto_engage_active(self, chat_id: int) -> bool:
        return chat_id in self.auto_engage_chats

    def activate(self, chat_id: int, mode: str = "normal") -> bool:
        """Activates Pal for a chat. Returns True if changed."""
        if self.active_chats.get(chat_id) != mode:
            self.active_chats[chat_id] = mode
            self.save_state()
            return True
        return False
        
    def activate_auto_engage(self, chat_id: int, duration_minutes: int = 20) -> bool:
        """Activates Auto-Engage (Lurker) for a chat with a specific duration."""
        if self.auto_engage_chats.get(chat_id) != duration_minutes:
            self.auto_engage_chats[chat_id] = duration_minutes
            self.save_state()
            return True
        return False

    def deactivate(self, chat_id: int) -> bool:
        """Deactivates Pal for a chat. Returns True if changed."""
        if chat_id in self.active_chats:
            del self.active_chats[chat_id]
            self.save_state()
            return True
        return False
        
    def deactivate_auto_engage(self, chat_id: int) -> bool:
        """Deactivates Auto-Engage for a chat."""
        if chat_id in self.auto_engage_chats:
            del self.auto_engage_chats[chat_id]
            self.save_state()
            return True
        return False

    def deactivate_all(self) -> int:
        """Deactivates Pal globally for all chats. Returns the number of deactivated chats."""
        count = len(self.active_chats) + len(self.auto_engage_chats)
        self.active_chats.clear()
        self.auto_engage_chats.clear()
        self.save_state()
        return count

    def deactivate_all_engages(self) -> int:
        """Deactivates Auto-Engage globally for all chats."""
        count = len(self.auto_engage_chats)
        self.auto_engage_chats.clear()
        self.save_state()
        return count

    def get_active_count(self) -> int:
        return len(self.active_chats)
        
    def get_auto_engage_count(self) -> int:
        return len(self.auto_engage_chats)

    def calculate_typing_delay(self, text: str) -> float:
        """Calculates a realistic typing duration based on text length and punctuation."""
        from typing_helper import calculate_human_typing_delay
        return calculate_human_typing_delay(text)

    async def send_human_message(self, client, chat_id, text: str, reply_to=None):
        """
        Simulates typing status + sends message naturally.
        """
        if not text or not text.strip():
            return None
        
        text = text.strip()
        from typing_helper import ContinuousTyping, calculate_human_typing_delay
        typing_delay = calculate_human_typing_delay(text)
        
        async with ContinuousTyping(client, chat_id):
            await asyncio.sleep(typing_delay)
            if reply_to:
                return await client.send_message(chat_id, text, reply_to=reply_to)
            else:
                return await client.send_message(chat_id, text)

# Global singleton instance
pal_manager = PalManager()

# ---------------------------------------------------------------------------
# Behaviour add-on bootstrap.
#
# Both modules install themselves on import and register their own secret codes
# lazily on the Telethon client, so main.py needs no changes at all:
#
#   quiet_hours    -> code 121: sleep schedule. Wraps the auto-reply decision
#                     predicates (PalManager.is_active / is_auto_engage_active /
#                     AssistantManager.is_active_for_chat).
#   human_behavior -> code 122: typo simulation + reaction-instead-of-reply +
#                     burst guard. Wraps TelegramClient.send_message on top of
#                     typing_helper's humanized sending patch, so it must be
#                     imported after quiet_hours and it imports typing_helper
#                     itself to guarantee patch ordering.
#
# Imports live at the very end of this module (after PalManager and the
# singleton exist) and every failure is non-fatal: the bot keeps running
# exactly as before if an add-on cannot load.
# ---------------------------------------------------------------------------
try:
    import quiet_hours as _quiet_hours  # noqa: F401
except Exception as _quiet_hours_error:  # pragma: no cover - defensive
    print(f"\u26a0\ufe0f Quiet Hours disabled (import failed): {_quiet_hours_error}")

try:
    import human_behavior as _human_behavior  # noqa: F401
except Exception as _human_behavior_error:  # pragma: no cover - defensive
    print(f"\u26a0\ufe0f Human Behavior disabled (import failed): {_human_behavior_error}")

try:
    import stats_command as _stats_command  # noqa: F401
except Exception as _stats_command_error:  # pragma: no cover - defensive
    print(f"\u26a0\ufe0f Stats dashboard (130) disabled (import failed): {_stats_command_error}")

try:
    import memory_backup as _memory_backup  # noqa: F401
except Exception as _memory_backup_error:  # pragma: no cover - defensive
    print(f"\u26a0\ufe0f Memory backup (133) disabled (import failed): {_memory_backup_error}")
