import os
import json
import asyncio
import random
from config import Config


# Bumped when the activation model changes so stale state files do not silently
# keep the old (universal) behaviour after an upgrade.
STATE_VERSION = 2


class AssistantManager:
    def __init__(self, state_file=Config.ASSISTANT_STATE_FILE):
        self.state_file = state_file
        self.dm_enabled = False      # True only after "666 all": handles every DM
        self.enabled_chats = set()   # Chats explicitly enabled with a plain "666"
        self.active_chats = set()    # Kept for backward compatibility
        self.muted_chats = set()     # Chats temporarily paused by the owner (444)
        self.blacklist = set()       # User IDs the assistant must NEVER talk to
        self._locks = {}
        self.load_state()

    def load_state(self):
        """Loads assistant mode settings from disk."""
        self._reset_defaults()

        if not (os.path.exists(self.state_file) and os.path.getsize(self.state_file) > 0):
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict):
                version = int(data.get("version", 1) or 1)

                self.muted_chats = set(int(x) for x in data.get("muted_chats", []))
                self.blacklist = set(int(x) for x in data.get("blacklist", []))
                self.active_chats = set(data.get("active_chats", []))

                if version >= STATE_VERSION:
                    self.dm_enabled = bool(data.get("dm_enabled", False))
                    self.enabled_chats = set(int(x) for x in data.get("enabled_chats", []))
                else:
                    # Migration from the old model, where "666" meant "all DMs".
                    # Do not silently keep the assistant live in every chat:
                    # start clean and let the owner opt in per chat.
                    self.dm_enabled = False
                    self.enabled_chats = set()
                    print(
                        "ℹ️ Assistant state migrated to per-chat mode: "
                        "use `666` for this chat, `666 all` for every DM."
                    )
                    self.save_state()

            elif isinstance(data, list):
                self.active_chats = set(data)

        except Exception as e:
            print(f"⚠️ Error loading Assistant state: {e}")
            self._reset_defaults()

    def _reset_defaults(self):
        self.dm_enabled = False
        self.enabled_chats = set()
        self.active_chats = set()
        self.muted_chats = set()
        self.blacklist = set()

    def save_state(self):
        """Persists assistant state to disk atomically."""
        try:
            data = {
                "version": STATE_VERSION,
                "dm_enabled": self.dm_enabled,
                "enabled_chats": [str(x) for x in self.enabled_chats],
                "active_chats": list(self.active_chats),
                "muted_chats": [str(x) for x in self.muted_chats],
                "blacklist": [str(x) for x in self.blacklist],
            }
            tmp_file = f"{self.state_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            print(f"⚠️ Error saving Assistant state: {e}")

    def is_active_for_chat(self, chat_id: int, is_private: bool = True) -> bool:
        """
        Checks if Assistant mode is active for this chat.
        Strictly applies ONLY to 1-on-1 private DMs (never in groups/channels).
        Active when the chat was enabled with `666`, or universally with `666 all`.
        """
        if not is_private:
            return False
        chat_id = int(chat_id)
        if chat_id in self.muted_chats:
            return False
        return self.dm_enabled or chat_id in self.enabled_chats

    def is_blacklisted(self, user_id) -> bool:
        """True if this user must never receive assistant replies."""
        try:
            return int(user_id) in self.blacklist
        except (TypeError, ValueError):
            return False

    def blacklist_add(self, chat_id: int):
        self.blacklist.add(int(chat_id))
        # Also mute so the current chat goes quiet immediately
        self.muted_chats.add(int(chat_id))
        self.enabled_chats.discard(int(chat_id))
        self.save_state()

    def blacklist_remove(self, chat_id: int):
        self.blacklist.discard(int(chat_id))
        self.muted_chats.discard(int(chat_id))
        self.save_state()

    def mute_chat(self, chat_id: int):
        """Stops the Assistant ONLY in this chat (444), leaving other chats untouched."""
        chat_id = int(chat_id)
        self.muted_chats.add(chat_id)
        self.enabled_chats.discard(chat_id)
        self.save_state()
        return True

    def activate_chat(self, chat_id: int):
        """Enables the Assistant for THIS chat only (plain `666`)."""
        chat_id = int(chat_id)
        self.enabled_chats.add(chat_id)
        self.muted_chats.discard(chat_id)
        self.save_state()
        return True

    def activate_all(self, chat_id: int = None):
        """Enables universal Assistant mode for ALL DMs (`666 all`)."""
        self.dm_enabled = True
        if chat_id is not None:
            self.muted_chats.discard(int(chat_id))
        self.save_state()
        return True

    # Backward-compatible alias for older call sites.
    def activate_global(self, chat_id: int = None):
        return self.activate_all(chat_id=chat_id)

    def deactivate_chat(self, chat_id: int):
        """Turns the Assistant off for a single chat without touching the rest."""
        return self.mute_chat(chat_id)

    def deactivate_global(self):
        """Globally disables Assistant mode across all chats (`444 all`)."""
        self.dm_enabled = False
        self.enabled_chats.clear()
        self.muted_chats.clear()
        self.save_state()
        return True

    def active_chat_count(self) -> int:
        """Number of chats explicitly enabled with a plain `666`."""
        return len(self.enabled_chats)

    def status_summary(self) -> str:
        """Short human-readable activation scope, for status reports and logs."""
        if self.dm_enabled:
            return "ON (all DMs)"
        if self.enabled_chats:
            return f"ON ({len(self.enabled_chats)} chats)"
        return "OFF"

    def calculate_typing_delay(self, text: str) -> float:
        """Calculates a realistic typing duration based on text length and punctuation."""
        from typing_helper import calculate_human_typing_delay
        return calculate_human_typing_delay(text)

    async def send_assistant_message(self, client, chat_id, text: str, reply_to=None):
        """Simulates natural reading and typing delay before sending assistant response."""
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
assistant_manager = AssistantManager()
