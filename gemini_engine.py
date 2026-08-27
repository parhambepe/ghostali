import threading
import time
import re
import html
import asyncio
import os
from google import genai
from google.genai import types
from config import Config
from text import Text

try:
    from telethon.errors import FloodWaitError
except Exception:  # pragma: no cover - telethon always present in prod, guard anyway
    class FloodWaitError(Exception):
        @property
        def seconds(self):
            return 5


# Error fingerprints that mean "this MODEL is the problem, not the key".
_QUOTA_MARKERS = ("429", "RESOURCE_EXHAUSTED", "quota", "rate limit", "RATE_LIMIT")
_BAD_MODEL_MARKERS = ("404", "NOT_FOUND", "is not found", "not supported", "unsupported",
                      "does not exist", "is not available")


class GeminiEngine:
    # Retry policy: after ALL keys fail, retry the whole cycle at most this many times.
    MAX_GLOBAL_RETRIES = 3
    RETRY_SLEEP_SECONDS = 8  # short pause between cycles; Google hangs are per-call, not per-minute

    def __init__(self):
        self.keys = Config.GEMINI_API_KEYS
        if not self.keys:
            print("⚠️ No GEMINI_API_KEYS found in config!")
        self._clients = {}
        self._client_lock = threading.Lock()
        self.model = Config.MODEL_NAME
        # 🧠 Multi-model rotation: every model in GEMINI_MODELS carries its own
        # RPM/RPD budget (tracked by api_tracker). When one is exhausted or rate
        # limited we simply continue with the next one.
        self.models = list(Config.MODEL_NAMES) or [self.model]
        if len(self.models) > 1:
            print(f"🧠 Model rotation enabled: {', '.join(self.models)}")
        # 🔎 Web-search fallback model: the `google_search` grounding tool is NOT
        # supported on *-flash-lite models. When use_search=True we temporarily
        # route the call through this search-capable model, then return to self.model.
        # Override via SEARCH_MODEL_NAME env if your account uses a different gen.
        self.search_model = os.getenv("SEARCH_MODEL_NAME", "gemini-2.5-flash")

        # Round-Robin State
        self.current_key_idx = 0
        self._idx_lock = threading.Lock()
        # Global request queue lock (lazily bound to the running loop on first use)
        self._queue_lock = None
        # Last error captured (for downstream diagnostics, e.g. web-search failures)
        self._last_error = None

    def _client(self, api_key: str):
        """
        Return a cached, REUSED genai.Client for this key.
        We also inject a strict timeout at the HTTP level.
        """
        with self._client_lock:
            c = self._clients.get(api_key)
            if c is None:
                c = genai.Client(api_key=api_key)
                self._clients[api_key] = c
            return c

    def _get_next_key(self) -> str:
        """Returns the next available API key in round-robin fashion, respecting daily limits."""
        with self._idx_lock:
            from api_tracker import api_tracker
            # Self-heal: if every key is circuit-breaker-banned, lift the bans —
            # a blind bot is worse than a rate-limited one.
            api_tracker.lift_all_bans_if_blind()
            num_keys = len(self.keys)
            for _ in range(num_keys):
                self.current_key_idx = (self.current_key_idx + 1) % num_keys
                key = self.keys[self.current_key_idx]
                if api_tracker.is_key_available(key):
                    return key
            return None

    @staticmethod
    def _sanitize(text: str) -> str:
        """Strips control characters that break REST APIs (keeps newlines/tabs)."""
        control_char_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
        return control_char_re.sub('', text or "")

    @staticmethod
    def _with_owner_profile(system_prompt: str) -> str:
        """Append the owner identity notes to conversational system prompts.

        Returns the prompt untouched when no OWNER_* variables are configured,
        so behaviour stays identical to before for existing deployments.
        """
        try:
            block = Config.owner_profile_block()
        except Exception:
            return system_prompt
        if not block:
            return system_prompt
        if not system_prompt:
            return block
        if block in system_prompt:  # never inject twice
            return system_prompt
        return f"{system_prompt}\n\n{block}"

    def _clean_output(self, raw_text: str) -> str:
        """Removes AI giveaways: emojis, HTML, formatting, diacritics."""
        emoji_pattern = re.compile(
            r'[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]',
            flags=re.UNICODE
        )
        clean_text = emoji_pattern.sub('', raw_text or "").strip()
        clean_text = html.unescape(clean_text)
        clean_text = re.sub(r'<[^>]+>', '', clean_text)
        diacritics_pattern = re.compile(r'[\u064B-\u065F\u0670\u0617-\u061A\u06D6-\u06ED]')
        clean_text = diacritics_pattern.sub('', clean_text)
        clean_text = re.sub(r'[ \t]+', ' ', clean_text)
        clean_text = re.sub(r'\n\s*\n\s*\n+', '\n\n', clean_text).strip()
        return clean_text

    async def get_response(self, user_message: str, system_prompt: str = None, is_json: bool = False,
                           parts=None, use_search: bool = False) -> str:
        """
        Asynchronously fetches a response using Round-Robin key management,
        per-model quota rotation, a hard per-attempt timeout and a FINITE global
        retry cap.

        - `parts`: optional list of google.genai types.Part (e.g. images/audio) appended to the prompt.
        - `use_search`: enables Google Search grounding for real-time information
          (gets a longer timeout via SEARCH_TIMEOUT — grounded calls are slow).
        """
        from api_tracker import api_tracker

        # ==========================================
        # 🛡️ PRE-FLIGHT SAFETY SANITIZER
        # ==========================================
        safe_user_msg = self._sanitize(user_message or "")
        safe_sys_prompt = self._sanitize(system_prompt or "")

        # 👤 Owner identity profile: only for real conversations, never for the
        # internal JSON tasks (memory compression, auto-engage decisions, ...).
        if not is_json:
            safe_sys_prompt = self._with_owner_profile(safe_sys_prompt)

        timeout = Config.SEARCH_TIMEOUT if use_search else Config.GEMINI_TIMEOUT

        # Hard payload size limit (keep head+tail when truncating)
        MAX_CHARS = 50000
        if len(safe_user_msg) > MAX_CHARS:
            print(f"⚠️ Payload too large ({len(safe_user_msg)} chars). Truncating to {MAX_CHARS} chars.")
            safe_user_msg = safe_user_msg[:MAX_CHARS // 2] + "\n\n...[TRUNCATED]...\n\n" + safe_user_msg[-(MAX_CHARS // 2):]

        try:
            user_parts = [types.Part.from_text(text=safe_user_msg)]
            if parts:
                user_parts.extend(parts)
            contents = [
                types.Content(role="user", parts=user_parts)
            ]

            cfg = types.GenerateContentConfig(
                system_instruction=safe_sys_prompt,
                thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

            if is_json:
                cfg.response_mime_type = "application/json"

            if use_search:
                try:
                    cfg.tools = [types.Tool(google_search=types.GoogleSearch())]
                    print(f"🔎 google_search tool attached OK for model routing")
                except Exception as tool_err:
                    print(f"🚫 google_search tool FAILED to attach: {type(tool_err).__name__}: {tool_err}")
                    self._last_error = tool_err
                    self._tried_models = []
                    return Text.ERROR

            if not self.keys:
                raise RuntimeError("NO_KEYS_CONFIGURED")

            num_keys = len(self.keys)
            loop = asyncio.get_running_loop()

            # Lazy initialize the global queue lock
            if self._queue_lock is None:
                self._queue_lock = asyncio.Lock()

            # 🧠 Build the candidate model list.
            if use_search:
                # Web search needs a grounding-capable model, so quotas take a back seat.
                raw_candidates = [self.search_model, "gemini-2.0-flash", "gemini-2.5-flash-lite"]
            else:
                # Quota-aware rotation across every model in GEMINI_MODELS.
                raw_candidates = api_tracker.get_model_order()

            seen = set()
            model_candidates = []
            for m in raw_candidates:
                if m and m not in seen:
                    seen.add(m)
                    model_candidates.append(m)
            if not model_candidates:
                model_candidates = [self.model]

            # Global Queue: Process AI requests strictly one by one
            async with self._queue_lock:
                resp = None
                last_err = None
                tried_models = []

                for model_position, current_model in enumerate(model_candidates):
                    # Skip a model that is over its own RPM/RPD budget, unless it is
                    # the very last option we have left.
                    is_last_option = model_position == len(model_candidates) - 1
                    if not use_search and not is_last_option and not api_tracker.is_model_available(current_model):
                        print(f"⏩ Skipping {current_model} (quota/cooldown). Trying next model...")
                        continue

                    tried_models.append(current_model)
                    model_blocked = False

                    # 🔒 FINITE retry: MAX_GLOBAL_RETRIES full-cycles instead of infinite loop
                    for global_attempt in range(1, self.MAX_GLOBAL_RETRIES + 1):
                        for attempt in range(num_keys):
                            api_key = self._get_next_key()

                            if not api_key:
                                print("🚫 ALL API KEYS EXHAUSTED FOR TODAY! Dropping message.")
                                return Text.ERROR

                            client = self._client(api_key)

                            try:
                                # Increment usage counters right before making the API call
                                api_tracker.record_usage(api_key)
                                api_tracker.record_model_usage(current_model)

                                # Hard Timeout (longer for web-search grounded calls)
                                resp = await asyncio.wait_for(
                                    loop.run_in_executor(
                                        None,
                                        lambda c=client, m=current_model, cont=contents, conf=cfg: c.models.generate_content(model=m, contents=cont, config=conf)
                                    ),
                                    timeout=timeout
                                )

                                # Success! Reset circuit breaker
                                api_tracker.record_success(api_key)
                                break  # Success! Exit the round-robin loop

                            except asyncio.TimeoutError:
                                api_tracker.record_error(api_key)
                                last_err = Exception(f"{timeout}s strict timeout reached")
                                self._last_error = last_err
                                print(f"⚠️ Key timeout ({timeout}s). Moving to next key in cycle...")
                                continue
                            except FloodWaitError as e:
                                api_tracker.record_error(api_key)
                                last_err = e
                                self._last_error = e
                                wait_s = min(int(getattr(e, "seconds", 10) or 10), 60)
                                print(f"⏳ FloodWait {wait_s}s before next key attempt...")
                                await asyncio.sleep(wait_s)
                                continue
                            except Exception as e:
                                last_err = e
                                self._last_error = e
                                err_str = str(e)

                                # 400 INVALID_ARGUMENT on media (e.g. undecodable image) will never
                                # succeed by retrying the same payload — fail fast instead of
                                # burning retries and tripping the circuit breaker.
                                if "400 INVALID_ARGUMENT" in err_str or ("INVALID_ARGUMENT" in err_str and "Unable to process input image" in err_str):
                                    print(f"🚫 Permanent input error (bad/undecodable media). Dropping request: {err_str[:160]}")
                                    return Text.ERROR

                                # 🧠 Model-level failures: the key is fine, the MODEL is not.
                                # Cool it down and jump straight to the next model.
                                if any(marker in err_str for marker in _BAD_MODEL_MARKERS):
                                    api_tracker.cooldown_model(current_model, 6 * 3600, "model unavailable/unsupported")
                                    model_blocked = True
                                    print(f"🚫 Model {current_model} rejected the request. Switching model: {err_str[:160]}")
                                    break
                                if any(marker in err_str for marker in _QUOTA_MARKERS):
                                    api_tracker.cooldown_model(current_model, 300, "quota / rate limit")
                                    model_blocked = True
                                    print(f"📉 Model {current_model} hit a quota limit. Switching model: {err_str[:160]}")
                                    break

                                api_tracker.record_error(api_key)
                                print(f"⚠️ Key/Network Error ({type(e).__name__}). Moving to next key in cycle: {e}")
                                continue

                        if resp is not None or model_blocked:
                            break  # got a response, or this model must be abandoned

                        # All keys failed this cycle → bounded backoff (no infinite loop!)
                        if global_attempt < self.MAX_GLOBAL_RETRIES:
                            print(f"⚠️ Cycle {global_attempt}/{self.MAX_GLOBAL_RETRIES}: all {num_keys} keys failed. Retrying in {self.RETRY_SLEEP_SECONDS}s...")
                            await asyncio.sleep(self.RETRY_SLEEP_SECONDS)
                        else:
                            print(f"🚫 Giving up after {self.MAX_GLOBAL_RETRIES} full cycles on model {current_model}. Last error: {last_err}")

                    if resp is not None:
                        self._tried_models = tried_models
                        break  # got a response from this model, stop trying other models

                if resp is None:
                    self._last_error = last_err
                    self._tried_models = tried_models
                    print(f"🚫 Request failed on ALL models {tried_models}. Last error: {last_err}")
                    return Text.ERROR

                raw_text = (resp.text or "").strip()
                return self._clean_output(raw_text)

        except Exception as e:
            print(f"🚨 Error in GeminiEngine.get_response: {type(e).__name__}: {e}")
            self._last_error = e
            self._tried_models = getattr(self, "_tried_models", [])
            return Text.ERROR

# Global singleton instance
gemini = GeminiEngine()
