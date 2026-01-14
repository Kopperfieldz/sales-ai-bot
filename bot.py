import os
import json
import asyncio
import time
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 1) ENV + CONFIG
# =========================================================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не знайдено у .env")
if not OPENAI_KEY:
    raise ValueError("OPENAI_API_KEY не знайдено у .env")

client = OpenAI(api_key=OPENAI_KEY)

WELCOME_IMAGE_PATH = "welcome.png"
SUBSCRIPTIONS_FILE = "subscriptions.json"

MODEL_NAME = "gpt-4o-mini"
MAX_TOKENS = 520

COOLDOWN_SECONDS = 3
MAX_INPUT_CHARS = 900

# =========================================================
# 2) TIERS + MONO LINKS
# =========================================================
FREE_DAILY_LIMIT = 10
PRO_DAILY_LIMIT = 100  # PRO+ = unlimited

PRICE_PRO_UAH = 99
PRICE_PROPLUS_UAH = 199

PAY_URL_PRO = "https://send.monobank.ua/jar/29f2b26s2S"
PAY_URL_PROPLUS = "https://send.monobank.ua/jar/eJAqpyUHz"

# =========================================================
# 3) ADMIN (from .env)
# =========================================================
def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
            if v > 0:
                ids.add(v)
        except ValueError:
            continue
    return ids

ADMIN_IDS = parse_admin_ids(ADMIN_IDS_RAW)

def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return bool(uid) and uid in ADMIN_IDS


# =========================================================
# 4) SUBSCRIPTIONS (JSON storage)
# =========================================================
def _ensure_subscriptions_file():
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": {}}, f, ensure_ascii=False, indent=2)

def load_subscriptions() -> dict:
    _ensure_subscriptions_file()
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "users" not in data or not isinstance(data["users"], dict):
            return {"users": {}}
        return data
    except Exception:
        return {"users": {}}

def save_subscriptions(data: dict):
    tmp_path = SUBSCRIPTIONS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SUBSCRIPTIONS_FILE)

def set_user_tier(user_id: int, tier: str):
    data = load_subscriptions()
    data["users"][str(user_id)] = tier
    save_subscriptions(data)

def remove_user(user_id: int):
    data = load_subscriptions()
    data["users"].pop(str(user_id), None)
    save_subscriptions(data)

def get_user_tier(update: Update) -> str:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return "free"
    data = load_subscriptions()
    tier = data["users"].get(str(uid), "free")
    if tier not in ("free", "pro", "pro_plus"):
        return "free"
    return tier

def tier_label(tier: str) -> str:
    if tier == "pro_plus":
        return "PRO+ ✅ (безліміт)"
    if tier == "pro":
        return "PRO ✅ (до 100/день)"
    return "FREE (до 10/день)"

def tier_daily_limit(tier: str) -> int | None:
    if tier == "pro_plus":
        return None
    if tier == "pro":
        return PRO_DAILY_LIMIT
    return FREE_DAILY_LIMIT


# =========================================================
# 5) UI (menus)
# =========================================================
def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["🎯 DEMO", "⚡ Швидкі відповіді"],
            ["💬 Відповіді клієнтам", "✍️ Опис товару"],
            ["⚙️ Налаштування", "🧠 Профіль"],
            ["⭐ Тарифи", "📌 Приклади"],
            ["ℹ️ Допомога"],
        ],
        resize_keyboard=True,
    )

def settings_menu():
    return ReplyKeyboardMarkup(
        [
            ["🛒 Платформа", "🎛 Шаблон стилю"],
            ["🌐 Мова", "💎 Сегмент"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )

def platform_menu():
    return ReplyKeyboardMarkup(
        [
            ["OLX", "Prom"],
            ["Instagram", "Rozetka"],
            ["Site", "Telegram"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )

def style_template_menu():
    return ReplyKeyboardMarkup(
        [
            ["⚡ Коротко", "🔥 Продаюче"],
            ["🏢 Офіційно", "💎 Преміум"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )

def language_menu():
    return ReplyKeyboardMarkup(
        [
            ["🇺🇦 Українська", "🇬🇧 English"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )

def quick_replies_menu():
    return ReplyKeyboardMarkup(
        [
            ["💸 Дорого", "🚚 Доставка"],
            ["📦 Наявність", "🏷️ Знижка/торг"],
            ["💳 Оплата/оформлення", "🛡️ Повернення/гарантія"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )

def pro_upsell_menu():
    return ReplyKeyboardMarkup(
        [
            ["⭐ PRO 99 грн", "💎 PRO+ 199 грн"],
            ["🆔 Мій ID", "⬅️ Назад"],
        ],
        resize_keyboard=True,
    )


# =========================================================
# 6) HELPERS: profile + limits + prompts
# =========================================================
def ensure_defaults(context: ContextTypes.DEFAULT_TYPE):
    if "profile" not in context.user_data:
        context.user_data["profile"] = {
            "platform": "OLX",
            "style_template": "🔥 Продаюче",
            "segment": "середній",
            "language": "uk",  # uk / en
        }

    if "limits" not in context.user_data:
        context.user_data["limits"] = {
            "day": str(date.today()),
            "count": 0,
            "last_ts": 0.0,
        }

    # soft upsell 1 time/day (FREE only)
    if "upsell" not in context.user_data:
        context.user_data["upsell"] = {
            "day": str(date.today()),
            "shown_soft": False,
        }

def reset_daily_if_needed(context: ContextTypes.DEFAULT_TYPE):
    today = str(date.today())

    limits = context.user_data["limits"]
    if limits.get("day") != today:
        limits["day"] = today
        limits["count"] = 0

    upsell = context.user_data.get("upsell", {})
    if upsell.get("day") != today:
        context.user_data["upsell"] = {"day": today, "shown_soft": False}

def register_ai_call(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["limits"]["count"] += 1
    context.user_data["limits"]["last_ts"] = time.time()

def can_call_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    ensure_defaults(context)
    reset_daily_if_needed(context)

    limits = context.user_data["limits"]
    now = time.time()

    if now - float(limits.get("last_ts", 0.0)) < COOLDOWN_SECONDS:
        wait_s = int(COOLDOWN_SECONDS - (now - float(limits.get("last_ts", 0.0)))) + 1
        return False, f"⏳ Зачекай {wait_s} с і спробуй ще раз."

    tier = get_user_tier(update)
    limit = tier_daily_limit(tier)  # None => unlimited
    if limit is not None and int(limits.get("count", 0)) >= limit:
        return False, "LIMIT_REACHED"

    return True, ""

def language_label(profile: dict) -> str:
    return "українською" if profile.get("language") == "uk" else "English"

def style_instructions(style_template: str) -> str:
    mapping = {
        "⚡ Коротко": "Максимально стисло. Без зайвих слів.",
        "🔥 Продаюче": "Акцент на вигоді та призиві до дії. Впевнено, без тиску.",
        "🏢 Офіційно": "Діловий тон, коректно, без емодзі, структуровано.",
        "💎 Преміум": "Стримано-преміальний тон, підкреслюй якість і сервіс.",
    }
    return mapping.get(style_template, mapping["🔥 Продаюче"])

def build_system_prompt(profile: dict, mode: str) -> str:
    lang = language_label(profile)
    style = style_instructions(profile.get("style_template", "🔥 Продаюче"))
    platform = profile.get("platform", "OLX")
    segment = profile.get("segment", "середній")

    return (
        "You are an experienced sales assistant for online commerce.\n"
        f"Respond in {lang}.\n"
        "Do not invent facts or specs that the user didn't provide.\n"
        "If critical info is missing, ask 1–2 short clarifying questions at the end.\n"
        f"Context: platform={platform}, segment={segment}.\n"
        f"Tone/style rules: {style}\n"
        f"Task mode: {mode}\n"
    )

def description_format_for_platform(platform: str) -> str:
    p = (platform or "").lower()
    if p == "instagram":
        return (
            "Format:\n"
            "1) Hook/title (1 line)\n"
            "2) Short description (2–4 sentences)\n"
            "3) Benefits (5–7 bullets)\n"
            "4) Delivery/payment (generic, no invented details)\n"
            "5) Call to action (1 line)\n"
            "No hashtags."
        )
    if p == "site":
        return (
            "Format:\n"
            "1) Product name\n"
            "2) Short description (2–3 sentences)\n"
            "3) Key specs (bullets)\n"
            "4) Benefits/assurance (3–5 bullets)\n"
            "5) CTA (1 line)"
        )
    if p == "telegram":
        return (
            "Format:\n"
            "1) Short title\n"
            "2) Main text (3–5 sentences)\n"
            "3) Benefits (bullets)\n"
            "4) CTA + how to order (generic)"
        )
    return (
        "Format:\n"
        "1) Title\n"
        "2) Short description (2–4 sentences)\n"
        "3) Specs/condition/what's included (if known)\n"
        "4) Benefits (5–7 bullets)\n"
        "5) Delivery/payment (generic, no invented details)\n"
        "6) Call to action"
    )

def build_user_prompt(mode: str, text: str, profile: dict) -> str:
    platform = profile.get("platform", "OLX")

    if mode == "description":
        return (
            f"Write a sales-ready product description for platform: {platform}\n"
            f"{description_format_for_platform(platform)}\n\n"
            f"Input from seller:\n{text}"
        )

    return (
        "Create 5 short reply options to the customer message.\n"
        "1–3: universal replies\n"
        "4: reply with a clarifying question\n"
        "5: soft close (next step: order/reserve/contact)\n"
        "Each option on a new line. No pressure.\n\n"
        f"Customer message / situation:\n{text}"
    )

async def call_openai(system_prompt: str, user_prompt: str) -> str:
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=MAX_TOKENS,
    )
    return resp.choices[0].message.content

def quick_template_to_text(button_text: str) -> str:
    mapping = {
        "💸 Дорого": "Customer says: 'Too expensive' / 'It's pricey'.",
        "🚚 Доставка": "Customer asks about delivery: cost and time.",
        "📦 Наявність": "Customer asks if it's in stock / available options (size/color).",
        "🏷️ Знижка/торг": "Customer asks for a discount / negotiation.",
        "💳 Оплата/оформлення": "Customer asks how to pay and place an order.",
        "🛡️ Повернення/гарантія": "Customer asks about returns / warranty if it doesn't fit.",
    }
    return mapping.get(button_text, "")

def demo_used_today(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("demo_day") == str(date.today())

def mark_demo_used(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["demo_day"] = str(date.today())


# =========================================================
# 7) MONETIZATION MESSAGES
# =========================================================
async def send_pro_upsell(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str = "soft"):
    ensure_defaults(context)
    lang = context.user_data["profile"].get("language", "uk")

    if lang == "en":
        msg = (
            "Upgrade options:\n"
            f"• PRO — {PRICE_PRO_UAH} UAH/month (100 requests/day)\n"
            f"• PRO+ — {PRICE_PROPLUS_UAH} UAH/month (unlimited)\n\n"
            "Tap a plan below to get payment instructions."
        )
        if reason == "limit":
            msg = "You’ve reached your daily limit.\n\n" + msg
    else:
        msg = (
            "Варіанти підписки:\n"
            f"• ⭐ PRO — {PRICE_PRO_UAH} грн/міс (100 запитів/день)\n"
            f"• 💎 PRO+ — {PRICE_PROPLUS_UAH} грн/міс (безліміт)\n\n"
            "Натисни тариф нижче — я покажу, як оплатити."
        )
        if reason == "limit":
            msg = "❌ Ліміт на сьогодні вичерпано.\n\n" + msg

    await update.message.reply_text(msg, reply_markup=pro_upsell_menu())


async def send_payment_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: str):
    uid = update.effective_user.id if update.effective_user else None
    lang = context.user_data["profile"].get("language", "uk")

    if plan == "pro":
        price = PRICE_PRO_UAH
        plan_name = "PRO"
        limit_uk = "100 запитів/день"
        limit_en = "100 requests/day"
        pay_url = PAY_URL_PRO
    else:
        price = PRICE_PROPLUS_UAH
        plan_name = "PRO+"
        limit_uk = "безліміт"
        limit_en = "unlimited"
        pay_url = PAY_URL_PROPLUS

    if lang == "en":
        text = (
            f"{plan_name} activation\n\n"
            f"Price: {price} UAH / month\n"
            f"Limit: {limit_en}\n\n"
            "1) Pay via Monobank link:\n"
            f"{pay_url}\n\n"
            "2) After payment, send:\n"
            f"Paid {plan_name}\n"
            f"ID: {uid}\n"
            "and attach screenshot/receipt.\n\n"
            "3) I will activate your plan after verification."
        )
    else:
        text = (
            f"⭐ Підключення {plan_name}\n\n"
            f"Ціна: {price} грн / місяць\n"
            f"Ліміт: {limit_uk}\n\n"
            "1) Оплати через Monobank:\n"
            f"{pay_url}\n\n"
            "2) Після оплати надішли:\n"
            f"Оплатив {plan_name}\n"
            f"ID: {uid}\n"
            "і додай скрін/чек.\n\n"
            "3) Я перевірю оплату і активую тариф."
        )

    await update.message.reply_text(text, reply_markup=pro_upsell_menu())


# =========================================================
# 8) ADMIN COMMANDS: /activate /deactivate /list_paid
# =========================================================
async def activate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Немає доступу.", reply_markup=main_menu())
        return

    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Використання:\n"
            "/activate <user_id> pro\n"
            "/activate <user_id> pro_plus\n\n"
            "Приклад:\n"
            "/activate 123456789 pro",
            reply_markup=main_menu(),
        )
        return

    user_id_str, tier = args[0], args[1].lower()
    if tier not in ("pro", "pro_plus"):
        await update.message.reply_text("Тариф має бути: pro або pro_plus", reply_markup=main_menu())
        return

    try:
        user_id = int(user_id_str)
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некоректний user_id.", reply_markup=main_menu())
        return

    set_user_tier(user_id, tier)
    await update.message.reply_text(f"✅ Активовано {tier_label(tier)} для ID {user_id}", reply_markup=main_menu())


async def deactivate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Немає доступу.", reply_markup=main_menu())
        return

    args = context.args or []
    if len(args) != 1:
        await update.message.reply_text(
            "Використання:\n"
            "/deactivate <user_id>\n\n"
            "Приклад:\n"
            "/deactivate 123456789",
            reply_markup=main_menu(),
        )
        return

    user_id_str = args[0]
    try:
        user_id = int(user_id_str)
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некоректний user_id.", reply_markup=main_menu())
        return

    remove_user(user_id)
    await update.message.reply_text(f"✅ Деактивовано підписку для ID {user_id}", reply_markup=main_menu())


async def list_paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Немає доступу.", reply_markup=main_menu())
        return

    data = load_subscriptions()
    users = data.get("users", {})

    pro = sorted([uid for uid, t in users.items() if t == "pro"])
    pro_plus = sorted([uid for uid, t in users.items() if t == "pro_plus"])

    lines = ["📋 Платні користувачі\n"]
    lines.append(f"⭐ PRO ({len(pro)}):")
    lines.extend([f"• {uid}" for uid in pro] if pro else ["—"])
    lines.append("")
    lines.append(f"💎 PRO+ ({len(pro_plus)}):")
    lines.extend([f"• {uid}" for uid in pro_plus] if pro_plus else ["—"])

    msg = "\n".join(lines)
    # Telegram message length safety
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(обрізано)"
    await update.message.reply_text(msg, reply_markup=main_menu())


# =========================================================
# 9) USER COMMANDS
# =========================================================
async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(f"🆔 Твій Telegram ID: {uid}", reply_markup=main_menu())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    ensure_defaults(context)

    caption = (
        "👋 Welcome to Sales Bot\n\n"
        "Я допоможу:\n"
        "• писати продаючі описи товарів (OLX/Prom/Instagram/...)\n"
        "• швидко відповідати клієнтам\n\n"
        "Швидкий старт:\n"
        "1) Обери платформу в ⚙️ Налаштування\n"
        "2) Натисни ✍️ Опис товару або 💬 Відповіді клієнтам\n"
        "3) Спробуй 🎯 DEMO\n"
    )

    if os.path.exists(WELCOME_IMAGE_PATH):
        with open(WELCOME_IMAGE_PATH, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption=caption, reply_markup=main_menu())
    else:
        await update.message.reply_text(caption, reply_markup=main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Допомога\n\n"
        "• 🎯 DEMO — приклад\n"
        "• ⚡ Швидкі відповіді — теми одним кліком\n"
        "• 💬 Відповіді клієнтам — встав повідомлення\n"
        "• ✍️ Опис товару — встав товар + характеристики\n"
        "• ⚙️ Налаштування — платформа/мова/стиль\n\n"
        "Команди:\n"
        "/whoami — показати твій Telegram ID\n"
        "/pro — підписка PRO/PRO+\n"
        "/reset — скинути налаштування\n",
        reply_markup=main_menu(),
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    ensure_defaults(context)
    await update.message.reply_text("✅ Скинув налаштування до стандартних.", reply_markup=main_menu())


async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_pro_upsell(update, context, reason="soft")


async def examples_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 Приклади\n\n"
        "✍️ Опис товару:\n"
        "• «Кросівки Nike, 42, нові, чорні»\n"
        "• «Повербанк 20000mAh, швидка зарядка, новий»\n\n"
        "💬 Відповіді клієнтам:\n"
        "• «Дорого»\n"
        "• «Є доставка?»\n"
        "• «А можна знижку?»\n"
        "• «Є в наявності?»\n",
        reply_markup=main_menu(),
    )


async def tariffs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tier = get_user_tier(update)
    await update.message.reply_text(
        "⭐ Тарифи\n\n"
        f"FREE: до {FREE_DAILY_LIMIT} запитів/день\n"
        f"PRO: {PRICE_PRO_UAH} грн/міс — до {PRO_DAILY_LIMIT} запитів/день\n"
        f"PRO+: {PRICE_PROPLUS_UAH} грн/міс — безліміт\n\n"
        f"Твій тариф: {tier_label(tier)}\n\n"
        "Щоб підключити — натисни тариф нижче:",
        reply_markup=pro_upsell_menu(),
    )


# =========================================================
# 10) MAIN HANDLER
# =========================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_defaults(context)
    reset_daily_if_needed(context)

    text = (update.message.text or "").strip()
    profile = context.user_data["profile"]

    # Payment buttons
    if text == "⭐ PRO 99 грн":
        await send_payment_instructions(update, context, plan="pro")
        return
    if text == "💎 PRO+ 199 грн":
        await send_payment_instructions(update, context, plan="pro_plus")
        return
    if text == "🆔 Мій ID":
        await whoami_cmd(update, context)
        return

    # Main menu
    if text == "🎯 DEMO":
        if demo_used_today(context) and get_user_tier(update) == "free":
            await update.message.reply_text("✅ DEMO вже було сьогодні.", reply_markup=main_menu())
            return

        mark_demo_used(context)
        demo_text = "Customer says: 'Too expensive'."
        system_prompt = build_system_prompt(profile, "demo")
        user_prompt = build_user_prompt("demo", demo_text, profile)

        await update.message.reply_text("🎯 DEMO: генерую відповіді...", reply_markup=main_menu())
        try:
            answer = await call_openai(system_prompt, user_prompt)
            await update.message.reply_text(answer, reply_markup=main_menu())
        except Exception as e:
            print("OPENAI ERROR:", repr(e))
            await update.message.reply_text("⚠️ Помилка AI. Деталі в терміналі.", reply_markup=main_menu())
        return

    if text == "⚡ Швидкі відповіді":
        context.user_data["mode"] = "quick_replies"
        await update.message.reply_text("Обери тему:", reply_markup=quick_replies_menu())
        return

    if text == "💬 Відповіді клієнтам":
        context.user_data["mode"] = "replies"
        await update.message.reply_text("Встав повідомлення клієнта.", reply_markup=main_menu())
        return

    if text == "✍️ Опис товару":
        context.user_data["mode"] = "description"
        await update.message.reply_text("Надішли назву + характеристики товару.", reply_markup=main_menu())
        return

    if text == "📌 Приклади":
        await examples_cmd(update, context)
        return

    if text == "⭐ Тарифи":
        await tariffs_cmd(update, context)
        return

    if text == "ℹ️ Допомога":
        await help_cmd(update, context)
        return

    if text == "🧠 Профіль":
        tier = get_user_tier(update)
        await update.message.reply_text(
            "🧠 Профіль\n"
            f"• Платформа: {profile.get('platform')}\n"
            f"• Шаблон стилю: {profile.get('style_template')}\n"
            f"• Сегмент: {profile.get('segment')}\n"
            f"• Мова: {'Українська' if profile.get('language') == 'uk' else 'English'}\n"
            f"• Тариф: {tier_label(tier)}\n"
            f"• Використано сьогодні: {context.user_data['limits']['count']}\n",
            reply_markup=main_menu(),
        )
        return

    # Settings
    if text == "⚙️ Налаштування":
        context.user_data["mode"] = "settings"
        await update.message.reply_text("Налаштування:", reply_markup=settings_menu())
        return

    if text == "⬅️ Назад":
        context.user_data["mode"] = None
        await update.message.reply_text("Повернувся в меню.", reply_markup=main_menu())
        return

    if text == "🛒 Платформа":
        context.user_data["mode"] = "platform_pick"
        await update.message.reply_text("Обери платформу:", reply_markup=platform_menu())
        return

    if text == "🎛 Шаблон стилю":
        context.user_data["mode"] = "style_pick"
        await update.message.reply_text("Обери шаблон стилю:", reply_markup=style_template_menu())
        return

    if text == "🌐 Мова":
        context.user_data["mode"] = "lang_pick"
        await update.message.reply_text("Обери мову:", reply_markup=language_menu())
        return

    if text == "💎 Сегмент":
        context.user_data["mode"] = "segment_input"
        await update.message.reply_text("Введи сегмент (бюджет/середній/преміум):", reply_markup=settings_menu())
        return

    if context.user_data.get("mode") == "platform_pick":
        if text in ("OLX", "Prom", "Instagram", "Rozetka", "Site", "Telegram"):
            profile["platform"] = text
            context.user_data["mode"] = "settings"
            await update.message.reply_text("✅ Платформу збережено.", reply_markup=settings_menu())
            return
        await update.message.reply_text("Обери платформу з кнопок.", reply_markup=platform_menu())
        return

    if context.user_data.get("mode") == "style_pick":
        if text in ("⚡ Коротко", "🔥 Продаюче", "🏢 Офіційно", "💎 Преміум"):
            profile["style_template"] = text
            context.user_data["mode"] = "settings"
            await update.message.reply_text("✅ Шаблон стилю збережено.", reply_markup=settings_menu())
            return
        await update.message.reply_text("Обери стиль з кнопок.", reply_markup=style_template_menu())
        return

    if context.user_data.get("mode") == "lang_pick":
        if text == "🇺🇦 Українська":
            profile["language"] = "uk"
        elif text == "🇬🇧 English":
            profile["language"] = "en"
        context.user_data["mode"] = "settings"
        await update.message.reply_text("✅ Мову збережено.", reply_markup=settings_menu())
        return

    if context.user_data.get("mode") == "segment_input":
        profile["segment"] = text[:60]
        context.user_data["mode"] = "settings"
        await update.message.reply_text("✅ Сегмент збережено.", reply_markup=settings_menu())
        return

    # Quick replies
    if context.user_data.get("mode") == "quick_replies":
        template = quick_template_to_text(text)
        if not template:
            await update.message.reply_text("Обери тему з кнопок.", reply_markup=quick_replies_menu())
            return

        allowed, reason = can_call_ai(update, context)
        if not allowed:
            if reason == "LIMIT_REACHED":
                await send_pro_upsell(update, context, reason="limit")
            else:
                await update.message.reply_text(reason, reply_markup=main_menu())
            return

        register_ai_call(context)

        # soft upsell: after 3rd call, once/day, only FREE
        if get_user_tier(update) == "free":
            if context.user_data["limits"]["count"] >= 3 and not context.user_data["upsell"]["shown_soft"]:
                context.user_data["upsell"]["shown_soft"] = True
                await send_pro_upsell(update, context, reason="soft")

        system_prompt = build_system_prompt(profile, "quick_replies")
        user_prompt = build_user_prompt("quick_replies", template, profile)

        await update.message.reply_text("⏳ Генерую відповіді...", reply_markup=quick_replies_menu())
        try:
            answer = await call_openai(system_prompt, user_prompt)
            await update.message.reply_text(answer, reply_markup=quick_replies_menu())
        except Exception as e:
            print("OPENAI ERROR:", repr(e))
            await update.message.reply_text("⚠️ Помилка AI. Деталі в терміналі.", reply_markup=main_menu())
        return

    # AI modes
    mode = context.user_data.get("mode")
    if mode not in ("description", "replies"):
        await update.message.reply_text("Обери дію з меню.", reply_markup=main_menu())
        return

    if len(text) > MAX_INPUT_CHARS:
        await update.message.reply_text(
            f"✂️ Текст задовгий (>{MAX_INPUT_CHARS} символів). Стисни та надішли ще раз.",
            reply_markup=main_menu(),
        )
        return

    allowed, reason = can_call_ai(update, context)
    if not allowed:
        if reason == "LIMIT_REACHED":
            await send_pro_upsell(update, context, reason="limit")
        else:
            await update.message.reply_text(reason, reply_markup=main_menu())
        return

    register_ai_call(context)

    # soft upsell: after 3rd call, once/day, only FREE
    if get_user_tier(update) == "free":
        if context.user_data["limits"]["count"] >= 3 and not context.user_data["upsell"]["shown_soft"]:
            context.user_data["upsell"]["shown_soft"] = True
            await send_pro_upsell(update, context, reason="soft")

    system_prompt = build_system_prompt(profile, mode)
    user_prompt = build_user_prompt(mode, text, profile)

    await update.message.reply_text("⏳ Готую відповідь...", reply_markup=main_menu())
    try:
        answer = await call_openai(system_prompt, user_prompt)
        await update.message.reply_text(answer, reply_markup=main_menu())
    except Exception as e:
        print("OPENAI ERROR:", repr(e))
        await update.message.reply_text("⚠️ Помилка AI. Деталі в терміналі.", reply_markup=main_menu())


# =========================================================
# 11) APP ENTRY
# =========================================================
def main():
    _ensure_subscriptions_file()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("pro", pro_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("tariffs", tariffs_cmd))

    # admin commands
    app.add_handler(CommandHandler("activate", activate_cmd))
    app.add_handler(CommandHandler("deactivate", deactivate_cmd))
    app.add_handler(CommandHandler("list_paid", list_paid_cmd))

    # text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("BOT IS RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()
