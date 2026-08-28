<div dir="rtl">

# ⚙️ مرجع کامل متغیرهای محیطی (.env)

مرجع کامل همهٔ متغیرهایی که می‌توانی در `.env` یا در Variables ریلوی تنطیم کنی.

برگشت: [`README_FA.md`](../README_FA.md) · [`README.md`](../README.md) · [مرجع ضدشناسایی](HUMANIZATION.md)

> [!TIP]
> متغیرهای `QUIET_HOURS_*` و `HUMAN_TYPO_*` و `HUMAN_REACTION_*` و `HUMAN_BURST_*` فقط **مقدار اولیه** می‌دهند. بعد از اینکه یک بار از کد `121` یا `122` استفاده کنی، وضعیت ذخیره‌شده اولویت دارد و دیگر هرگز لازم نیست Redeploy کنی.

---

## 📱 تلگرام

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `API_ID` | — | از <https://my.telegram.org/apps> |
| `API_HASH` | — | از همان صفحه |
| `PHONE_NUMBER` | — | فقط برای لاگین محلی |
| `OWNER_ID` | — | آیدی عددی خودت؛ فقط این شخص کدهای مخفی را اجرا کرده می‌تواند |
| `SESSION_NAME` | `ghostgram_session` | نام فایل سشن محلی |
| `SESSION_STRING` | — | برای ریلوی/داکر. یک بار `python3 gen_session_string.py` روی سیستم خودت |
| `PERSONA_NAME` | `شایان` | اسمی که هوش مصنوعی خودش را با آن معرفی می‌کند |

---

## 🧠 جمینای

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `GEMINI_API_KEYS` | — | چند کلید با کاما. یا خط‌به‌خط در `apis.txt` |
| `MODEL_NAME` | `gemini-3.5-flash-lite` | مدل پیش‌فرض |
| `GEMINI_MODELS` | — | سقف اختصاصی هر مدل با فرمت `نام:rpm:rpd,...` |
| `DEFAULT_MODEL_RPM` | `15` | سقف درخواست در دقیقه |
| `DEFAULT_MODEL_RPD` | `200` | سقف درخواست روزانه |
| `GEMINI_TIMEOUT` | `15` | تایم‌اوت پاسخ عادی (ثانیه) |
| `SEARCH_TIMEOUT` | `45` | تایم‌اوت جستجوی وب (`112`) |

---

## 🌙 ساعات خواب (کد `121`)

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `QUIET_HOURS_ENABLED` | `0` | پیش‌فرض **خاموش** است؛ تا `121` نزنی رفتار ربات عوض نمی‌شود |
| `QUIET_HOURS_START` | `01:00` | شروع بازهٔ خواب |
| `QUIET_HOURS_END` | `09:00` | پایان بازهٔ خواب |
| `QUIET_HOURS_TZ_OFFSET` | `+03:30` | اختلاف ساعت محلی با UTC (تهران) |

ساعت کانتینر تقریباً همیشه UTC است، پس بازه بر اساس این اختلاف محاسبه می‌شود. دستور `121` همیشه ساعت محلی فعلی ربات را چاپ می‌کند تا بتوانی با ساعت خودت مقایسه کنی.

---

## ⌨️ موتور تایپ انسانی (`typing_helper.py`)

مهم‌ترین متغیرها برای لو نرفتن. اگر مخاطب‌ها باز هم شک کردند تأخیرها را بیشتر کن؛ اگر خیلی کند شد کمتر.

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `HUMAN_MIN_TYPING_DELAY` | `1.2` | کف تأخیر برای جواب‌های خیلی کوتاه (ثانیه) |
| `HUMAN_MAX_TYPING_DELAY` | `45.0` | سقف تأخیر تایپ برای جواب‌های بلند |
| `HUMAN_TYPING_CPS_SCALE` | `1.0` | ضریب سرعت تایپ؛ بالاتر = تندتر، پایین‌تر = کندتر و انسانی‌تر |
| `HUMAN_READ_DELAY_MAX` | `9.0` | حداکثر زمان «خواندن» پیام پیش از شروع تایپ |
| `HUMAN_THINK_PAUSE_CHANCE` | `0.18` | احتمال مکس فکر کردن وسط تایپ |
| `HUMAN_SEGMENT_MESSAGES` | `1` | تکه‌تکه فرستادن جواب‌های بلند (چند حباب) |
| `HUMAN_SEGMENT_THRESHOLD` | `180` | از چند کاراکتر به بالا تکه شود |
| `HUMAN_SEGMENT_MAX_DELAY` | `12.0` | حداکثر مکس بین دو حباب |

---

## 🧬 رفتار ضدشناسایی (کد `122`)

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `HUMAN_BEHAVIOR_ENABLED` | `1` | روشن بودن کل پکیج |
| `HUMAN_TYPO_CHANCE` | `0.08` | احتمال غلط تایپی (سقف سخت `0.5`) |
| `HUMAN_TYPO_MAX_CHARS` | `170` | جواب‌های بلندتر از این هرگز غلط نمی‌گیرند |
| `HUMAN_TYPO_FIX_DELAY_MIN` | `2.0` | حداقل مکس قبل از تصحیح |
| `HUMAN_TYPO_FIX_DELAY_MAX` | `6.5` | حداکثر مکس قبل از تصحیح |
| `HUMAN_TYPO_FIX_STYLE` | `mixed` | `edit` (ویرایش پیام) · `star` (پیام `*درست`) · `mixed` |
| `HUMAN_REACTION_CHANCE` | `0.22` | احتمال ریاکشن به‌جای جواب دادن |
| `HUMAN_REACTION_MAX_CHARS` | `26` | فقط جواب‌های کوتاه واجد شرایطند |
| `HUMAN_BURST_MAX` | `3` | حداکثر جواب خودکار در هر بازه |
| `HUMAN_BURST_WINDOW` | `60.0` | طول بازهٔ محاسبه (ثانیه) |
| `HUMAN_BURST_SPACING` | `6.0` | حداقل فاصله بین دو جواب خودکار |

---

## 🧠 حافظه و رفتار

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `SHORT_TERM_MEMORY_LIMIT` | `30` | تعداد پیام در حافظهٔ کوتاه‌مدت |
| `LONG_TERM_SUMMARY_INTERVAL` | `30` | هر چند پیام خلاصه بسازد |
| `MAX_LONG_TERM_SUMMARY_CHARS` | `600` | سقف طول خلاصهٔ بلندمدت |
| `MAX_MESSAGE_SEGMENT_CHARS` | `200` | برش پیام‌های بلند در حافظه |

---

## 🔔 مخفی‌کاری و گزارش خطا

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `STEALTH_CONFIRM` | `1` | تأیید دستورها به «پیام‌های ذخیره‌شده» |
| `CONFIRM_AUTO_DELETE_SECONDS` | `10` | حذف خودکار تأییدها پس از چند ثانیه |
| `NOTIFY_ERRORS` | `1` | گزارش خطاهای موتور به خودت |

---

## 💾 مسیر و دیپلوی

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `DATA_DIR` | `/app/data` | محل ذخیرهٔ همهٔ فایل‌های وضعیت. روی ریلوی **باید** به یک Volume وصل باشد |
| `VPS_IP` | — | آی‌پی سرور برای اسکریپت دیپلوی |
| `SSH_USER` | `root` | کاربر SSH |
| `SSH_PORT` | `22` | پورت SSH |

---

## ⚠️ متغیرهای منقضی‌شده

این‌ها دیگر **خوانده نمی‌شوند** و جایشان را متغیرهای `HUMAN_*` گرفته. اگر تنطیمشان کنی هیچ اثری ندارد:

`TYPING_SPEED_CPS` · `MIN_TYPING_DELAY` · `MAX_TYPING_DELAY`

---

## 📄 نمونهٔ کامل `.env`

</div>

```ini
# 📱 تلگرام
API_ID=12345678
API_HASH=abcdef0123456789abcdef0123456789
PHONE_NUMBER=+989****6789
OWNER_ID=123456789
PERSONA_NAME=شایان
SESSION_NAME=ghostgram_session

# 🧠 جمینای
GEMINI_API_KEYS=AIzaSyA...Key1,AIzaSyB...Key2
MODEL_NAME=gemini-3.5-flash-lite
GEMINI_MODELS=
DEFAULT_MODEL_RPM=15
DEFAULT_MODEL_RPD=200
GEMINI_TIMEOUT=15
SEARCH_TIMEOUT=45

# ☁️ ریلوی / داکر
SESSION_STRING=
DATA_DIR=/app/data

# 🔔 مخفی‌کاری
STEALTH_CONFIRM=1
CONFIRM_AUTO_DELETE_SECONDS=10
NOTIFY_ERRORS=1

# 🧠 حافظه
SHORT_TERM_MEMORY_LIMIT=30
LONG_TERM_SUMMARY_INTERVAL=30
MAX_LONG_TERM_SUMMARY_CHARS=600
MAX_MESSAGE_SEGMENT_CHARS=200

# ⌨️ موتور تایپ انسانی
HUMAN_MIN_TYPING_DELAY=1.2
HUMAN_MAX_TYPING_DELAY=45.0
HUMAN_TYPING_CPS_SCALE=1.0
HUMAN_READ_DELAY_MAX=9.0
HUMAN_THINK_PAUSE_CHANCE=0.18
HUMAN_SEGMENT_MESSAGES=1
HUMAN_SEGMENT_THRESHOLD=180
HUMAN_SEGMENT_MAX_DELAY=12.0

# 🌙 ساعات خواب (کد 121)
QUIET_HOURS_ENABLED=0
QUIET_HOURS_START=01:00
QUIET_HOURS_END=09:00
QUIET_HOURS_TZ_OFFSET=+03:30

# 🧬 رفتار ضدشناسایی (کد 122)
HUMAN_BEHAVIOR_ENABLED=1
HUMAN_TYPO_CHANCE=0.08
HUMAN_TYPO_MAX_CHARS=170
HUMAN_TYPO_FIX_DELAY_MIN=2.0
HUMAN_TYPO_FIX_DELAY_MAX=6.5
HUMAN_TYPO_FIX_STYLE=mixed
HUMAN_REACTION_CHANCE=0.22
HUMAN_REACTION_MAX_CHARS=26
HUMAN_BURST_MAX=3
HUMAN_BURST_WINDOW=60.0
HUMAN_BURST_SPACING=6.0

# 🚀 دیپلوی VPS
VPS_IP=your.vps.ip.here
SSH_USER=root
SSH_PORT=22
```

<div dir="rtl">

---

## 💾 فایل‌های وضعیت

همه در `DATA_DIR` ذخیره می‌شوند:

| فایل | محتوا |
|---|---|
| `assistant_state.json` | دامنهٔ دستیار، سکوت‌ها، لیست سیاه |
| `pal_state.json` | چت‌های رفیق، پرسوناها، تعامل خودکار |
| `quiet_hours_state.json` | بازهٔ خواب، منطقهٔ زمانی، چت‌های معاف |
| `human_behavior_state.json` | احتمال غلط/ریاکشن و سقف انفجار |
| `memory_state.json` | حافظهٔ بلندمدت هر چت |
| `reminders_state.json` | یادآورهای در انتظار |
| `stickers_state.json` | استیکرهای آموزش‌دیده |
| `api_usage.json` | مصرف روزانهٔ هر کلید |

فایل‌های وضعیت **نسخه‌دار** هستند و در اولین اجرا پس از تغییر ساختار مهاجرت می‌کنند. مهاجرت‌ها محافظه‌کارانه‌اند — در صورت شک، یک مود **خاموش** می‌شود نه اینکه بی‌سروصدا فعال بماند.

</div>
