# 🌙 Quiet Hours & 🧬 Human Behavior — Full Reference

> Detailed reference for the two anti-detection add-ons.
> For the project overview see [`README.md`](../README.md) · [`README_FA.md`](../README_FA.md).

Both modules are **self-installing**. They are imported at the end of
`pal_manager.py`, patch what they need, and register their own secret codes on
the Telethon client lazily. `main.py` is **not modified at all**, and every
failure path is non-fatal — if an add-on cannot load, the bot keeps running
exactly as it did before.

| Module | Code | State file | What it does |
|---|:---:|---|---|
| `quiet_hours.py` | `121` | `quiet_hours_state.json` | Sleep schedule — the AI goes silent at night |
| `human_behavior.py` | `122` | `human_behavior_state.json` | Typo simulation, reaction-instead-of-reply, burst guard |

---

## 🌙 Quiet Hours (`121`)

The single loudest "this is a bot" signal is a perfectly composed reply at
04:00, two seconds after the message arrived. Quiet Hours silences the
**automatic** reply paths during a configurable night window.

### Commands

| Command | Effect |
|---|---|
| `121` | Show full status: window, the bot's local clock, time until it wakes/sleeps |
| `121 01:00 09:00` | Set the sleep window **and enable it** |
| `121 on` / `121 off` | Enable / disable |
| `121 tz +03:30` | Set the timezone offset used for the window |
| `121 sleep 90` | Sleep **right now** for 90 minutes (ad-hoc, e.g. you are in a meeting) |
| `121 wake 60` | Stay awake for 60 minutes, even inside the sleep window |
| `121 now` | Cancel any manual sleep/wake override and return to the schedule |
| `121 allow` | Toggle an exemption for the current chat — it gets replies even at night |

Persian aliases are accepted for the switches (`روشن`, `خاموش`, `بخواب`,
`بیدار`, `لغو`, `معاف`) and Persian/Arabic digits are normalised, so
`۱۲۱ ۰۱:۳۰ ۰۹:۰۰` works.

### Time formats

`23:30`, `23.30`, `2330` and `23` are all accepted. `24:00` is normalised to
`00:00`. Windows that cross midnight (`23:00 → 07:00`) are handled correctly.
Setting start equal to end is rejected, because that would mean either always
or never asleep.

### Timezone

The container clock is almost always UTC, so the window is evaluated against an
explicit offset instead. `QUIET_HOURS_TZ_OFFSET` sets the default and
`121 tz +03:30` changes it live. `121` always prints the bot's current local
time so you can verify it matches your own wall clock.

### What is silenced — and what is not

**Silenced:** Pal Mode auto-replies, Auto-Engage group chime-ins, and Assistant
Mode replies. This works by wrapping the three decision predicates:

- `PalManager.is_active`
- `PalManager.is_auto_engage_active`
- `AssistantManager.is_active_for_chat`

**Never silenced:** your own secret codes. `111`, `112`, `222`, `333`, `555`,
`999` and friends work at 4 AM exactly as they do at noon, because they are
outgoing commands, not automatic replies. The `555` dashboard counters also
keep reporting the true configured state, since they read different methods
(`active_chat_count`, `status_summary`, `get_active_count`).

### Environment defaults

```ini
QUIET_HOURS_ENABLED=0          # off by default — nothing changes until you opt in
QUIET_HOURS_START=01:00
QUIET_HOURS_END=09:00
QUIET_HOURS_TZ_OFFSET=+03:30   # Tehran
```

These are only **initial defaults**. Once you use `121`, the persisted state in
`quiet_hours_state.json` wins, so you never need to redeploy to change your
sleep schedule.

---

## 🧬 Human Behavior (`122`)

Three behaviours that no bot does and every human does.

### 1. Typo simulation and correction

With a small probability the bot sends a slightly misspelled word, then a few
seconds later fixes it. Two correction styles:

- **`edit`** — the message is edited to the correct text. Telegram then shows
  the `edited` marker, which is a very strong human signal.
- **`star`** — a follow-up `*correctword` message, the way people correct
  themselves in chat.
- **`mixed`** (default) — randomly picks between them, weighted towards `edit`.

The mutation is deliberately gentle and realistic: swapping two adjacent
letters, dropping a letter, or doubling a letter. Only "safe" words are
touched — 4 to 18 characters, purely alphabetic. URLs, mentions, hashtags,
numbers, and words that already contain punctuation are never mutated.

> [!NOTE]
> Typos are only injected into replies short enough that the humanized sending
> engine will **not** split them into multiple bubbles, and never into
> multi-line replies. That guarantees the returned message object is the one
> holding the typo, so the correction always edits the right message.

### 2. Reaction instead of reply

When the AI's reply is a tiny throwaway line — `خخخ`, `باشه`, `مرسی`, `👍` —
and it is a reply to a specific message, the text is dropped and the bot simply
**reacts with an emoji** instead. The emoji is chosen from the intent of the
reply it was about to send:

| Reply looks like | Reaction |
|---|---|
| laughter (`خخ`, `lol`, `😂`) | 😂 / 🤣 |
| thanks (`مرسی`, `ممنون`, `thanks`) | ❤️ / 🙏 |
| sympathy (`اوه`, `متاسفم`, `😢`) | 😢 / 😔 |
| agreement (`باشه`, `اوکی`, `آره`) | 👍 |
| anything else | 👍 / 👌 |

Questions are never converted into reactions — if the reply contains `?` or
`؟`, it is always sent as text. If the chat does not allow the chosen reaction,
or the reaction API call fails for any reason, the module silently falls back to
sending the normal text message.

### 3. Burst guard

If ten people message you at once, a bot answers all ten within the same
second. The burst guard enforces two global limits across **all** chats:

- at most `burst_max` automatic replies inside a rolling `burst_window`
- a minimum `burst_spacing` gap between any two automatic replies

When a limit is hit, the reply simply waits. Combined with the typing
simulation this reads as "they are working through their messages", not "a
server flushed a queue".

### Commands

| Command | Effect |
|---|---|
| `122` | Show full status of all three behaviours |
| `122 on` / `122 off` | Enable / disable the whole package |
| `122 typo 8` | Typo probability in percent (`0` disables typos only) |
| `122 style edit\|star\|mixed` | Typo correction style |
| `122 react 20` | Reaction-instead-of-reply probability in percent |
| `122 burst 3 60` | At most 3 automatic replies per 60 seconds |
| `122 spacing 6` | Minimum 6 seconds between two automatic replies |
| `122 reset` | Restore all defaults |

### Environment defaults

```ini
HUMAN_BEHAVIOR_ENABLED=1

# 1) typo simulation
HUMAN_TYPO_CHANCE=0.08          # 8% of eligible replies (hard-capped at 0.5)
HUMAN_TYPO_MAX_CHARS=170        # never mutate replies longer than this
HUMAN_TYPO_FIX_DELAY_MIN=2.0    # wait at least this long before fixing
HUMAN_TYPO_FIX_DELAY_MAX=6.5
HUMAN_TYPO_FIX_STYLE=mixed      # mixed | edit | star

# 2) reaction instead of reply
HUMAN_REACTION_CHANCE=0.22
HUMAN_REACTION_MAX_CHARS=26     # only "throwaway" replies qualify

# 3) burst guard
HUMAN_BURST_MAX=3
HUMAN_BURST_WINDOW=60.0
HUMAN_BURST_SPACING=6.0
```

As with Quiet Hours, these are initial defaults; the persisted state written by
`122` takes precedence afterwards.

---

## 🔬 How the send pipeline fits together

`human_behavior` wraps `TelegramClient.send_message` **on top of** the patch
installed by `typing_helper`, so the full outgoing path is:

```
[AI reply text]
      │
      ▼
[human_behavior]  burst gate ──► reaction shortcut? ──► inject typo?
      │
      ▼
[typing_helper]   reading delay ──► typing… ──► think pauses ──► split into bubbles
      │
      ▼
[Telethon]        real send  ──► (a few seconds later) edit / *correction
```

`human_behavior.install()` imports `typing_helper` itself before wrapping, which
guarantees this ordering no matter how `main.py` orders its own imports.

### Which messages are touched

Only plain conversational text going to somebody else. A message is **skipped**
when any of the following is true:

- it contains `**` or backticks — that means it is an owner dashboard or a
  command confirmation, not a chat reply
- it is being sent to **Saved Messages** (your own account)
- it is the sticker placeholder `[استیکر]`
- it already starts with `*` — that is our own typo correction
- it is not a string (media, files, and so on)

This heuristic is intentionally conservative: worst case a real AI reply is
treated as a command confirmation and simply goes out unmodified.

---

## 🩺 Troubleshooting

**The startup log should contain both lines:**

```
🌙 Quiet Hours ready — ❌ غیرفعال | کد 121
🧬 Human Behavior ready — ✅ غلط 8٪ | واکنش 22٪ | سقف 3/60ث
```

| Symptom | Cause / fix |
|---|---|
| Neither line appears | `pal_manager.py` failed to import the add-ons. The log prints `⚠️ … disabled (import failed): …` with the reason. |
| Settings reset after every restart | `DATA_DIR` is not pointing at a mounted volume. On Railway mount a Volume at `/app/data`. |
| Bot sleeps at the wrong hour | The offset is wrong. Run `121` and compare the printed local time with your own clock, then fix it with `121 tz +03:30`. |
| Never see a typo | Expected — the default is 8% of short, single-line, alphabetic-word replies. Raise it temporarily with `122 typo 40` to verify, then set it back. |
| Reactions never happen | Reactions require the reply to be a reply (`reply_to`), under 26 characters, and without a question mark. Some chats also restrict reactions. |
| Replies feel too slow now | The burst guard adds up to `burst_spacing` seconds. Lower it with `122 spacing 2`, or disable the package with `122 off`. |
| Assistant answers nothing at all | Unrelated to these modules — the per-chat migration turns the assistant off on first boot. Send `666` in the chat, or `666 all` once. |

### Turning everything off

```
121 off      # disable the sleep schedule
122 off      # disable typos, reactions, and the burst guard
```

Both are instant, persist across restarts, and restore the exact pre-add-on
behaviour.
