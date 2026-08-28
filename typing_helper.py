import asyncio
import os
import random

from config import Config


def _float_env(name: str, default: float) -> float:
    """Read a float env var, falling back to a humanized default."""
    try:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return default
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# --- Humanized typing tuning -------------------------------------------------
# The old defaults (MIN 1.5s / MAX 7.0s) made long replies obviously robotic:
# no human types a 400-character Farsi paragraph in 7 seconds. These knobs are
# read independently so the humanized behaviour works without touching config.py,
# while still honouring explicit overrides in .env.
#
#   HUMAN_MIN_TYPING_DELAY   floor for the typing indicator (seconds)
#   HUMAN_MAX_TYPING_DELAY   ceiling for the typing indicator (seconds)
#   HUMAN_TYPING_CPS_SCALE   global speed multiplier (>1 = faster typist)
#   HUMAN_READ_DELAY_MAX     max pause before typing starts (seconds)
#   HUMAN_THINK_PAUSE_CHANCE probability of a mid-typing pause (0..1)

MIN_TYPING_DELAY = _float_env("HUMAN_MIN_TYPING_DELAY", 1.2)
MAX_TYPING_DELAY = _float_env("HUMAN_MAX_TYPING_DELAY", 45.0)
TYPING_CPS_SCALE = max(0.2, _float_env("HUMAN_TYPING_CPS_SCALE", 1.0))
READ_DELAY_MAX = _float_env("HUMAN_READ_DELAY_MAX", 9.0)
THINK_PAUSE_CHANCE = _float_env("HUMAN_THINK_PAUSE_CHANCE", 0.18)


def _base_cps(length: int) -> float:
    """Realistic characters-per-second for a human on a mobile Farsi keyboard.

    Short reflexive replies are fast; long paragraphs slow down because of
    fatigue, re-reading and typo correction.
    """
    if length <= 12:
        cps = 5.0
    elif length <= 60:
        cps = 3.6
    elif length <= 200:
        cps = 3.0
    else:
        cps = 2.6
    return cps * TYPING_CPS_SCALE


def calculate_human_typing_delay(text: str) -> float:
    """How long the '... is typing' indicator should stay visible.

    Derived from actual human typing throughput plus punctuation pauses and
    natural variance, instead of a flat sub-linear curve capped at a few
    seconds. Long messages now take realistically long to "type".
    """
    if not text:
        return 1.0

    text = text.strip()
    length = len(text)

    base_time = length / _base_cps(length)

    # Natural pauses at sentence breaks, commas and newlines.
    punctuation_count = (
        text.count("\n")
        + text.count(".")
        + text.count("!")
        + text.count("?")
        + text.count("\u061f")  # Arabic question mark
        + text.count("\u060c")  # Arabic comma
    )
    pause_time = min(punctuation_count * 0.6, 6.0)

    # Proportional jitter so two same-length messages never take the same time.
    jitter = base_time * random.uniform(-0.15, 0.25)

    total_delay = base_time + pause_time + jitter

    return max(MIN_TYPING_DELAY, min(total_delay, MAX_TYPING_DELAY))


def calculate_reading_delay(incoming_text: str) -> float:
    """Pause before the typing indicator appears at all.

    A human reads the incoming message first. Turning on 'typing' the same
    instant a message arrives is one of the strongest robot tells.
    """
    if not incoming_text:
        return random.uniform(1.0, 2.5)

    length = len(str(incoming_text).strip())
    delay = 1.2 + (length / 22.0) + random.uniform(0.0, 1.5)
    return min(delay, READ_DELAY_MAX)


def split_into_human_segments(text: str, max_chars: int = None) -> list:
    """Split a long reply into a few shorter chat bubbles.

    Humans rarely send one giant paragraph; they send 2-3 shorter messages.
    Splitting happens on sentence boundaries where possible.
    """
    if not text:
        return []

    text = text.strip()
    if max_chars is None:
        max_chars = getattr(Config, "MAX_MESSAGE_SEGMENT_CHARS", 200) or 200

    if len(text) <= max_chars:
        return [text]

    segments = []
    current = ""

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        for chunk in _sentence_chunks(line):
            candidate = (current + " " + chunk).strip() if current else chunk
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    segments.append(current)
                current = chunk

    if current:
        segments.append(current)

    return segments or [text]


def _sentence_chunks(line: str) -> list:
    """Break a line into sentence-ish chunks, keeping terminators attached."""
    terminators = {".", "!", "?", "\u061f"}
    chunks = []
    buffer = ""

    for char in line:
        buffer += char
        if char in terminators:
            chunks.append(buffer.strip())
            buffer = ""

    if buffer.strip():
        chunks.append(buffer.strip())

    return chunks


async def human_typing_pause(text: str, incoming_text: str = None) -> None:
    """Sleep for the full humanized lifecycle: reading, then typing.

    Call this inside a ContinuousTyping() context if you want the indicator
    visible during the typing phase only, or wrap it entirely for a simpler
    integration.
    """
    if incoming_text is not None:
        await asyncio.sleep(calculate_reading_delay(incoming_text))

    total = calculate_human_typing_delay(text)

    # For longer messages, occasionally stop mid-way like a real person who
    # pauses to think, deletes a word, and resumes.
    if total > 8.0 and random.random() < THINK_PAUSE_CHANCE:
        first = total * random.uniform(0.35, 0.6)
        await asyncio.sleep(first)
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await asyncio.sleep(max(0.0, total - first))
    else:
        await asyncio.sleep(total)


def ContinuousTyping(client, input_chat_or_id):
    """
    Returns an asynchronous context manager that ensures Telegram's '... is typing' action
    is continuously active at the top of the chat (both DMs and supergroups/groups)
    throughout the entire thinking + typing lifecycle.

    This leverages Telethon's native background task manager which flawlessly handles
    auto-cancellations and heartbeats.
    """
    return client.action(input_chat_or_id, "typing")
