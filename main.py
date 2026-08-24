import os
import json
import base64
import requests
from flask import Flask, request

app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

PROMPTINO_CHANNEL = os.getenv(
    "PROMPTINO_CHANNEL",
    "https://t.me/PromptinoChannel"
).strip()

# Telegram username of the owner, without @.
# Example: AmirsK13
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip().lstrip("@")

# GitHub repository used as a tiny persistent store for the ad-channel catalog.
# Example: AmirsK13/promptino-bot
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "Amirsk13/promptino-bot").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
CHANNELS_FILE = "channels.json"

API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
GITHUB_API = "https://api.github.com"


PROMPTS = {
    "p1": {
        "title": "پرتره سینمایی حرفه‌ای",
        "variants": {
            "chatgpt": {
                "label": "🤖 ChatGPT",
                "prompt": """📋 پرامپت Promptino #1 — ChatGPT

Create a cinematic professional portrait from the uploaded photo.
Keep the person's identity and facial features consistent.
Use dramatic cinematic lighting, realistic skin texture, shallow depth
of field, premium editorial photography, high detail, natural colors,
and a professional camera look."""
            },
            "gemini": {
                "label": "✨ Gemini",
                "prompt": """📋 پرامپت Promptino #1 — Gemini

Create a cinematic professional portrait from the uploaded photo.
Keep the person's identity and facial features consistent.
Use dramatic cinematic lighting, realistic skin texture, shallow depth
of field, premium editorial photography, high detail, natural colors,
and a professional camera look."""
            }
        }
    }
}

WELCOME = f"""🤖 سلام! به پرامپتینو خوش اومدی 👋

اینجا می‌تونی به پرامپت‌های کاربردی و تست‌شده هوش مصنوعی دسترسی داشته باشی.

📌 برای دریافت هر پرامپت:
برو داخل کانال پرامپتینو و پرامپتی که می‌خوای رو انتخاب کن؛
بعد روی دکمه «🚀 دریافت پرامپت» همون پست بزن.

👇 از اینجا وارد کانال شو:"""


def api(method, data=None):
    if not API:
        return {}
    try:
        r = requests.post(
            f"{API}/{method}",
            json=data or {},
            timeout=20
        )
        return r.json()
    except Exception as e:
        print("Telegram API error:", e)
        return {}


def send(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard:
        data["reply_markup"] = keyboard
    return api("sendMessage", data)


def is_admin(chat_id):
    return chat_id in ADMIN_IDS

def prompt_button(prompt_id, variant_key, label):
    """Create a URL button for any prompt variant."""
    bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    return {
        "text": label,
        "url": f"https://t.me/{bot_username}?start={prompt_id}_{variant_key}"
    }


def owner_button():
    if not OWNER_USERNAME:
        return None
    return {
        "text": "👤 ارتباط با مالک",
        "url": f"https://t.me/{OWNER_USERNAME}"
    }


# ---------------- Persistent channel catalog ----------------
def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def load_channels():
    """
    Load the advertising/mandatory-membership channel list from GitHub.
    If GitHub persistence is not configured or the file doesn't exist,
    return an empty list.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return []

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{CHANNELS_FILE}"
    try:
        r = requests.get(
            url,
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=20
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        parsed = json.loads(content)
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        print("GitHub load_channels error:", e)
        return []


def save_channels(channels, commit_message):
    """
    Persist the channel list into channels.json in the GitHub repository.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("WARNING: GitHub persistence is not configured.")
        return False

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{CHANNELS_FILE}"

    try:
        current = requests.get(
            url,
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=20
        )

        sha = None
        if current.status_code == 200:
            sha = current.json().get("sha")
        elif current.status_code != 404:
            current.raise_for_status()

        content = base64.b64encode(
            json.dumps(
                channels,
                ensure_ascii=False,
                indent=2
            ).encode("utf-8")
        ).decode("ascii")

        payload = {
            "message": commit_message,
            "content": content,
            "branch": GITHUB_BRANCH
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(
            url,
            headers=github_headers(),
            json=payload,
            timeout=20
        )
        r.raise_for_status()
        return True

    except Exception as e:
        print("GitHub save_channels error:", e)
        return False


REQUIRED_CHANNELS = load_channels()


# ---------------- Membership ----------------
def is_member(user_id, channel):
    result = api("getChatMember", {
        "chat_id": channel["username"],
        "user_id": user_id
    })

    if not result.get("ok"):
        print("Membership check failed:", result)
        return False

    status = result.get("result", {}).get("status", "")
    return (
        status in {"creator", "administrator", "member"}
        or (
            status == "restricted"
            and result.get("result", {}).get("is_member", False)
        )
    )


def require_membership(chat_id, user_id):
    missing = [
        channel for channel in REQUIRED_CHANNELS
        if not is_member(user_id, channel)
    ]

    if not missing:
        return True

    buttons = []
    for channel in missing:
        buttons.append([{
            "text": f"📢 عضویت در {channel['title']}",
            "url": channel["url"]
        }])

    buttons.append([{
        "text": "✅ بررسی عضویت",
        "callback_data": "check_membership"
    }])

    text = (
        "🔒 برای دریافت این پرامپت، ابتدا در کانال‌های زیر عضو شو:\n\n"
        + "\n".join(f"• {c['title']}" for c in missing)
        + "\n\nبعد از عضویت روی «✅ بررسی عضویت» بزن."
    )

    send(chat_id, text, {"inline_keyboard": buttons})
    return False


# ---------------- Admin channel management ----------------
ADD_CHANNEL_STATES = {}

def cancel_add_channel(chat_id):
    ADD_CHANNEL_STATES.pop(chat_id, None)

def add_channel_step(chat_id, text):
    state = ADD_CHANNEL_STATES.get(chat_id)
    if state is None:
        ADD_CHANNEL_STATES[chat_id] = {"step": "username"}
        send(chat_id, "📢 مرحله ۱ از ۳\n\nیوزرنیم کانال را ارسال کنید:\nمثال: @ProxyZone_URL\n\nبرای لغو: /cancel")
        return
    value=text.strip()
    if value.lower()=="/cancel":
        cancel_add_channel(chat_id); send(chat_id,"❌ عملیات افزودن کانال لغو شد."); return
    step=state["step"]
    if step=="username":
        if not value.startswith("@") or len(value)<2:
            send(chat_id,"❌ یوزرنیم درست نیست.\n\nمثال: @ProxyZone_URL"); return
        if any(c["username"].lower()==value.lower() for c in REQUIRED_CHANNELS):
            cancel_add_channel(chat_id); send(chat_id,"⚠️ این کانال قبلاً اضافه شده."); return
        state.update(username=value, step="title")
        send(chat_id,"✅ یوزرنیم دریافت شد.\n\n✏️ مرحله ۲ از ۳\n\nعنوان کانال را ارسال کنید:\nمثال: پروکسی\n\nبرای لغو: /cancel"); return
    if step=="title":
        if not value:
            send(chat_id,"❌ عنوان نمی‌تواند خالی باشد."); return
        state.update(title=value, step="url")
        send(chat_id,"✅ عنوان دریافت شد.\n\n🔗 مرحله ۳ از ۳\n\nلینک کانال را ارسال کنید:\nمثال: https://t.me/ProxyZone_URL\n\nبرای لغو: /cancel"); return
    if step=="url":
        if not value.startswith("https://t.me/"):
            send(chat_id,"❌ لینک درست نیست.\n\nمثال: https://t.me/ProxyZone_URL"); return
        state["channel"]={"username":state["username"],"title":state["title"],"url":value}
        send(chat_id,"📋 اطلاعات کانال:\n\n📢 نام کاربری: {}\n📝 عنوان: {}\n🔗 لینک: {}\n\nآیا اطلاعات صحیح است؟".format(state["username"],state["title"],value), {"inline_keyboard":[[{"text":"✅ تأیید و ذخیره","callback_data":"confirm_add_channel"},{"text":"❌ لغو","callback_data":"cancel_add_channel"}]]})

def confirm_add_channel(chat_id):
    state=ADD_CHANNEL_STATES.get(chat_id)
    if not state or "channel" not in state:
        send(chat_id,"⚠️ عملیات پیدا نشد. دوباره /addchannel را بزن."); return
    ch=state["channel"]
    REQUIRED_CHANNELS.append(ch)
    if not save_channels(REQUIRED_CHANNELS, f"Add required channel: {ch['username']}"):
        REQUIRED_CHANNELS.pop(); cancel_add_channel(chat_id); send(chat_id,"❌ ذخیره‌سازی GitHub خطا داد."); return
    cancel_add_channel(chat_id)
    send(chat_id,f"✅ کانال «{ch['title']}» اضافه شد و ذخیره شد.\n📊 تعداد کانال‌های فعلی: {len(REQUIRED_CHANNELS)}")

def remove_channel(chat_id, text):
    username=text.replace("/removechannel","",1).strip()
    if not username.startswith("@"): send(chat_id,"فرمت درست:\n/removechannel @ChannelUsername"); return
    old=list(REQUIRED_CHANNELS)
    REQUIRED_CHANNELS[:]=[c for c in REQUIRED_CHANNELS if c["username"].lower()!=username.lower()]
    if len(REQUIRED_CHANNELS)==len(old): send(chat_id,"⚠️ این کانال در لیست وجود نداشت."); return
    if not save_channels(REQUIRED_CHANNELS,f"Remove required channel: {username}"):
        REQUIRED_CHANNELS[:]=old; send(chat_id,"❌ حذف دائمی انجام نشد چون ذخیره‌سازی GitHub خطا داد."); return
    send(chat_id,f"✅ کانال حذف شد و تغییر ذخیره شد.\n📊 تعداد کانال‌های فعلی: {len(REQUIRED_CHANNELS)}")

def list_channels(chat_id):
    if not REQUIRED_CHANNELS: send(chat_id,"📭 فعلاً هیچ کانال تبلیغاتی ثبت نشده."); return
    lines=["📋 کانال‌های فعلی:\n"]
    for i,c in enumerate(REQUIRED_CHANNELS,1): lines.append(f"{i}. {c['title']} — {c['username']}")
    lines.append(f"\n📊 مجموع: {len(REQUIRED_CHANNELS)}"); send(chat_id,"\n".join(lines))

# ---------------- Telegram update handling ----------------
def handle_update(update):
    callback = update.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        user_id = callback.get("from", {}).get("id")

        api("answerCallbackQuery", {
            "callback_query_id": callback_id
        })

        data=callback.get("data")
        if data == "check_membership":
            if require_membership(chat_id,user_id): send(chat_id,"✅ عضویتت تأیید شد!\n\nحالا دوباره از کانال، روی دکمه «🚀 دریافت پرامپت» بزن.")
        elif data == "confirm_add_channel" and is_admin(chat_id): confirm_add_channel(chat_id)
        elif data == "cancel_add_channel" and is_admin(chat_id): cancel_add_channel(chat_id); send(chat_id,"❌ عملیات افزودن کانال لغو شد.")
        return

    message = update.get("message")
    if not message:
        return

    text = message.get("text", "")
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    if text == "/myid":
        send(chat_id, f"🆔 Telegram ID شما:\n{user_id}")
        return

    # Admin-only catalog management
    if text.startswith("/addchannel"):
        if not is_admin(chat_id):
            send(chat_id, "⛔ این دستور فقط برای مدیر ربات است.")
            return
        add_channel_step(chat_id, text)
        return

    if is_admin(chat_id) and chat_id in ADD_CHANNEL_STATES:
        add_channel_step(chat_id, text)
        return

    if text.startswith("/removechannel"):
        if not is_admin(chat_id):
            send(chat_id, "⛔ این دستور فقط برای مدیر ربات است.")
            return
        remove_channel(chat_id, text)
        return

    if text == "/channels":
        if not is_admin(chat_id):
            send(chat_id, "⛔ این دستور فقط برای مدیر ربات است.")
            return
        list_channels(chat_id)
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)

        # Deep-link from a channel post:
        # https://t.me/PromptinoPromptsBot?start=p1_chatgpt
        # Each prompt can have 2, 4, or any number of variants.
        if len(parts) == 2:
            token = parts[1].strip().lower()
            prompt_id, _, variant_key = token.partition("_")

            if prompt_id in PROMPTS:
                prompt = PROMPTS[prompt_id]
                variants = prompt.get("variants", {})

                if not variant_key:
                    variant_key = next(iter(variants), "")

                variant = variants.get(variant_key)
                if not variant:
                    send(chat_id, "⚠️ این نسخه از پرامپت پیدا نشد.")
                    return

                if not require_membership(chat_id, user_id):
                    return

                keyboard = [[{
                    "text": "📢 کانال پرامپتینو",
                    "url": PROMPTINO_CHANNEL
                }]]

                owner = owner_button()
                if owner:
                    keyboard.append([owner])

                send(
                    chat_id,
                    variant["prompt"],
                    {"inline_keyboard": keyboard}
                )
                return

        # Normal /start: no automatic prompt, just guide the user to the channel.
        keyboard = [[{
            "text": "📢 ورود به کانال پرامپتینو",
            "url": PROMPTINO_CHANNEL
        }]]

        owner = owner_button()
        if owner:
            keyboard.append([owner])

        send(
            chat_id,
            WELCOME,
            {"inline_keyboard": keyboard}
        )


@app.get("/")
def home():
    return "Promptino Bot is running ✅", 200


@app.post("/webhook")
def webhook():
    handle_update(request.get_json(silent=True) or {})
    return "OK", 200


# Render supplies RENDER_EXTERNAL_URL.
# Register the Telegram webhook automatically on startup.
if TOKEN and os.getenv("RENDER_EXTERNAL_URL"):
    webhook_url = os.getenv("RENDER_EXTERNAL_URL").rstrip("/") + "/webhook"
    result = api("setWebhook", {"url": webhook_url})
    print("Webhook:", result)
