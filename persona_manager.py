import os
import glob

from config import Config

_FALLBACK_PROMPT = "تو خودت «{owner_name}» هستی و مثل یک آدم عادی، کوتاه و خودمونی جواب می‌دهی."


class PersonaManager:
    """Loads chat personas from plain .txt files inside `personas/`.

    The file name is the switch name, so `personas/sard.txt` is activated with
    `777 sard` in Telegram. Files are re-read on every request, which means you
    can add or edit a persona while the bot is running and the next message
    already uses it — no restart, no deploy.
    """

    def __init__(self, dir_path="personas"):
        self.dir_path = dir_path
        self.personas = {}
        self._mtimes = {}
        self.load_personas()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    def _folder_signature(self):
        """Cheap fingerprint of the folder so we only re-read when something changed."""
        sig = {}
        try:
            for file_path in glob.glob(os.path.join(self.dir_path, "*.txt")):
                try:
                    sig[file_path] = os.path.getmtime(file_path)
                except OSError:
                    continue
        except Exception:
            pass
        return sig

    def load_personas(self, force: bool = True):
        """Loads personas from the .txt files in the personas directory."""
        if not os.path.exists(self.dir_path):
            try:
                os.makedirs(self.dir_path, exist_ok=True)
                with open(os.path.join(self.dir_path, "normal.txt"), "w", encoding="utf-8") as f:
                    f.write(_FALLBACK_PROMPT)
            except Exception as e:
                print(f"⚠️ Could not create personas dir: {e}")

        signature = self._folder_signature()
        if not force and signature == self._mtimes and self.personas:
            return  # nothing changed on disk

        loaded = {}
        for file_path in signature or self._folder_signature():
            persona_name = os.path.splitext(os.path.basename(file_path))[0].lower()
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    loaded[persona_name] = content
            except Exception as e:
                print(f"⚠️ Error loading persona {os.path.basename(file_path)}: {e}")

        # Ensure fallback exists
        if "normal" not in loaded:
            loaded["normal"] = _FALLBACK_PROMPT

        self.personas = loaded
        self._mtimes = signature

    # ------------------------------------------------------------------
    # identity placeholders
    # ------------------------------------------------------------------
    @staticmethod
    def _fill_identity(prompt: str) -> str:
        """Replaces {owner_*} placeholders with the OWNER_* values from .env.

        Anything that is not configured is replaced with a neutral phrase instead
        of an empty gap, so the model never sees a dangling sentence or a raw
        placeholder.
        """
        owner_name = Config.OWNER_NAME or Config.PERSONA_NAME or "خودت"
        mapping = {
            "{owner_name}": owner_name,
            "{persona_name}": Config.PERSONA_NAME or owner_name,
            "{owner_bio}": Config.OWNER_BIO or "چیز خاصی برای گفتن نیست",
            "{owner_services}": Config.OWNER_SERVICES or "کارهای معمول خودم",
            "{owner_interests}": Config.OWNER_INTERESTS or "چیزهای معمولی",
            "{owner_schedule}": Config.OWNER_SCHEDULE or "بسته به روز",
            "{owner_website}": Config.OWNER_WEBSITE or "لینکی فعلاً ندارم",
            "{owner_extra}": Config.OWNER_EXTRA or "",
        }
        for key, value in mapping.items():
            if key in prompt:
                prompt = prompt.replace(key, value)
        return prompt

    # ------------------------------------------------------------------
    # public API (unchanged signatures)
    # ------------------------------------------------------------------
    def get_prompt(self, command_name: str) -> str:
        """Returns the prompt for a persona name, falling back to 'normal'."""
        self.load_personas(force=False)  # hot-reload new/edited files instantly
        command_name = str(command_name or "").lower().strip()
        raw = self.personas.get(command_name)
        if raw is None:
            if command_name and command_name != "normal":
                print(f"ℹ️ Persona '{command_name}' not found — using 'normal'. "
                      f"Available: {', '.join(sorted(self.personas))}")
            raw = self.personas.get("normal", _FALLBACK_PROMPT)
        return self._fill_identity(raw)

    def has_persona(self, command_name: str) -> bool:
        """True when a personas/<name>.txt file exists for this switch name."""
        self.load_personas(force=False)
        return str(command_name or "").lower().strip() in self.personas

    def get_all_persona_names(self):
        """Returns a list of all registered persona commands."""
        self.load_personas(force=False)
        return sorted(self.personas.keys())


# Singleton instance
persona_manager = PersonaManager()
