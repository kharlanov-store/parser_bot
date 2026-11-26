import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, urlencode

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6098417275"))
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "@kharlanovvstore")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

PLANS = {
    "free": ("FREE (Тест)", 0, 2),
    "pro": ("PRO", 10, 10),
    "vip": ("VIP", 25, 30),
}


class AddLinkState(StatesGroup):
    waiting_for_url = State()
    waiting_for_title = State()


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить поиск"), KeyboardButton(text="📋 Мои поиски")],
            [KeyboardButton(text="💎 Тарифы и подписка"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )


def build_kufar_api_url(browser_url: str) -> str:
    parsed = urlparse(browser_url)
    params = parse_qs(parsed.query)
    api_params = {
        "lang": "ru",
        "size": "15",
        "sort": "lst.d",
    }
    for k, v in params.items():
        if k not in ("sort", "size", "cursor"):
            api_params[k] = v[0] if len(v) == 1 else ",".join(v)
    return f"https://kufar.by?{urlencode(api_params)}"


def build_vinted_api_url(browser_url: str) -> str:
    parsed = urlparse(browser_url)
    params = parse_qs(parsed.query)
    api_params = {
        "page": "1",
        "per_page": "15",
        "order": "newest_first",
    }
    mapping = {
        "search_text": "search_text",
        "catalog[]": "catalog_ids",
        "brand_ids[]": "brand_ids",
        "size_ids[]": "size_ids",
        "color_ids[]": "color_ids",
        "status_ids[]": "status_ids",
        "price_from": "price_from",
        "price_to": "price_to",
        "currency": "currency",
    }
    for browser_key, api_key in mapping.items():
        if browser_key in params:
            api_params[api_key] = ",".join(params[browser_key])
    return f"https://vinted.pl?{urlencode(api_params)}"


async def fetch_kufar_items(session: AsyncSession, browser_url: str) -> list[dict]:
    api_url = build_kufar_api_url(browser_url)
    try:
        resp = await session.get(api_url, timeout=15)
        if resp.status_code != 200:
            return []
        ads = resp.json().get("ads", [])
        results = []
        for ad in ads:
            ad_id = str(ad.get("ad_id"))
            title = ad.get("subject", "Без названия")
            link = ad.get("ad_link", f"https://kufar.by{ad_id}")
            price_byn = ad.get("price_byn", "0")
            try:
                price_str = f"{int(price_byn) / 100:.2f} BYN" if int(price_byn) > 0 else "Договорная"
            except ValueError:
                price_str = "Не указана"

            size = "Не указан"
            for param in ad.get("ad_parameters", []):
                if param.get("p") in ("size", "clothing_size", "shoes_size"):
                    size = param.get("vl", "Не указан")
                    break

            images = ad.get("images", [])
            photo_url = f"https://kufar.by{images[0]['path']}" if images and images[0].get("path") else None

            results.append({
                "id": ad_id,
                "title": title,
                "price": price_str,
                "size": size,
                "url": link,
                "photo": photo_url
            })
        return results
    except Exception as e:
        logging.error(f"Kufar parse error: {e}")
        return []


async def fetch_vinted_items(session: AsyncSession, browser_url: str) -> list[dict]:
    api_url = build_vinted_api_url(browser_url)
    try:
        if "vinted.pl" not in str(session.cookies):
            await session.get("https://vinted.pl", timeout=15)
            await asyncio.sleep(1)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": browser_url,
        }
        resp = await session.get(api_url, headers=headers, timeout=15)
        if resp.status_code in (401, 403):
            await session.get("https://vinted.pl", timeout=15)
            await asyncio.sleep(1.5)
            resp = await session.get(api_url, headers=headers, timeout=15)

        if resp.status_code != 200:
            return []

        items = resp.json().get("items", [])
        results = []
        for item in items:
            item_id = str(item.get("id"))
            title = item.get("title", "Без названия")
            price_obj = item.get("price", {})
            price_str = f"{price_obj.get('amount', '?')} {price_obj.get('currency_code', 'PLN')}"
            size = item.get("size_title") or "Не указан"
            url = item.get("url", f"https://vinted.plitems/{item_id}")
            photo_url = item.get("photo", {}).get("url") if item.get("photo") else None

            results.append({
                "id": item_id,
                "title": title,
                "price": price_str,
                "size": size,
                "url": url,
                "photo": photo_url
            })
        return results
    except Exception as e:
        logging.error(f"Vinted parse error: {e}")
        return []


async def create_cryptobot_invoice(amount_usdt: int, payload: str) -> dict | None:
    if not CRYPTOBOT_TOKEN:
        return None
    url = "https://crypt.bot"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    data = {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": str(amount_usdt),
        "accepted_assets": "USDT,TON,BTC",
        "payload": payload,
        "expires_in": 3600
    }
    async with AsyncSession() as session:
        resp = await session.post(url, headers=headers, json=data, timeout=10)
        res = resp.json()
        if res.get("ok"):
            return res["result"]
    return None


async def check_cryptobot_invoice(invoice_id: int) -> str | None:
    if not CRYPTOBOT_TOKEN:
        return None
    url = f"https://crypt.bot{invoice_id}"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    async with AsyncSession() as session:
        resp = await session.get(url, headers=headers, timeout=10)
        res = resp.json()
        if res.get("ok") and res["result"]["items"]:
            return res["result"]["items"][0]["status"]
    return None


def get_or_create_user(user: types.User) -> dict:
    res = supabase.table("parser_users").select("*").eq("user_id", user.id).execute()
    if res.data:
        u = res.data[0]
        if u["plan_type"] != "free" and u.get("subscription_until"):
            sub_until = datetime.fromisoformat(u["subscription_until"].replace("Z", "+00:00"))
            if sub_until < datetime.now(timezone.utc):
                supabase.table("parser_users").update({
                    "plan_type": "free",
                    "max_links": PLANS["free"][2],
                    "subscription_until": None
                }).eq("user_id", user.id).execute()
                u["plan_type"] = "free"
                u["max_links"] = PLANS["free"][2]
        return u

    new_user = {
        "user_id": user.id,
        "username": user.username,
        "plan_type": "free",
        "max_links": PLANS["free"][2]
    }
    inserted = supabase.table("parser_users").insert(new_user).execute()
    return inserted.data[0]


def activate_subscription(user_id: int, plan_code: str, days: int = 30):
    _, _, max_links = PLANS[plan_code]
    res = supabase.table("parser_users").select("*").eq("user_id", user_id).execute()
    now = datetime.now(timezone.utc)

    if res.data and res.data[0].get("subscription_until"):
        current_until = datetime.fromisoformat(res.data[0]["subscription_until"].replace("Z", "+00:00"))
        base_date = current_until if current_until > now else now
    else:
        base_date = now

    new_until = (base_date + timedelta(days=days)).isoformat()
    supabase.table("parser_users").upsert({
        "user_id": user_id,
        "plan_type": plan_code,
        "max_links": max_links,
        "subscription_until": new_until,
        "is_active": True
    }).execute()


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    u = get_or_create_user(message.from_user)
    await message.answer(
        f"👋 Привет, **{message.from_user.first_name}**!\n\n"
        f"Я бот-снайпер для **Kufar.by** и **Vinted.pl**. Отслеживаю новые лоты каждые ~30 секунд.\n"
        f"Твой текущий тариф: **{u['plan_type'].upper()}** (лимит ссылок: **{u['max_links']}**).\n\n"
        f"Используй меню ниже для управления поисками 👇",
        reply_markup=main_kb(),
        parse_mode="Markdown"
    )


@dp.message(F.text == "ℹ️ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 **Как правильно добавить поиск:**\n\n"
        "1. Открой **Vinted.pl** или **Kufar.by** в браузере.\n"
        "2. Выбери нужный бренд, размер, цену и отсортируй **«Сначала новые»**.\n"
      "3. Скопируй ссылку из адресной строки браузера.\n"
"4. Нажми «➕ Добавить поиск» в боте и отправь ссылку.\n\n"
"🆔 Твой Telegram ID: {}".format(message.from_user.id),
parse_mode="Markdown"
)
@dp.message(F.text == "➕ Добавить поиск")
async def start_add_link(message: types.Message, state: FSMContext):
u = get_or_create_user(message.from_user)
existing = supabase.table("parser_links").select("id").eq("user_id", message.from_user.id).execute()
if len(existing.data) >= u["max_links"]:
await message.answer(
f"🚫 Достигнут лимит ссылок ({u['max_links']} шт.) на тарифе {u['plan_type'].upper()}.\n"
f"Удали старый поиск или перейди на тариф выше в разделе «💎 Тарифы и подписка».",
parse_mode="Markdown"
)
return
await state.set_state(AddLinkState.waiting_for_url)
await message.answer("🔗 Отправь ссылку с выбранными фильтрами с kufar.by или vinted.pl:", parse_mode="Markdown")
@dp.message(AddLinkState.waiting_for_url)
async def process_link_url(message: types.Message, state: FSMContext):
url = message.text.strip()
if "kufar.by" in url:
platform = "kufar"
elif "vinted.pl" in url:
platform = "vinted"
else:
await message.answer("❌ Неверная ссылка. Нужна ссылка именно с kufar.by или vinted.pl. Попробуй ещё раз:", parse_mode="Markdown")
return
await state.update_data(url=url, platform=platform)
await state.set_state(AddLinkState.waiting_for_title)
await message.answer("✏️ Как назвать этот поиск? (например: Stone Island L или Кроссовки 43):", parse_mode="Markdown")
@dp.message(AddLinkState.waiting_for_title)
async def process_link_title(message: types.Message, state: FSMContext):
title = message.text.strip()[:50]
data = await state.get_data()
url = data["url"]
platform = data["platform"]
await state.clear()
wait_msg = await message.answer("⏳ Проверяю ссылку и фиксирую текущие объявления...")
async with AsyncSession(impersonate="chrome124") as session:
items = await (fetch_kufar_items(session, url) if platform == "kufar" else fetch_vinted_items(session, url))
last_id = items[0]["id"] if items else None
supabase.table("parser_links").insert({
"user_id": message.from_user.id,
"platform": platform,
"title": title,
"original_url": url,
"last_item_id": last_id,
"is_active": True
}).execute()
await wait_msg.edit_text(
f"✅ Поиск {title} ({platform.upper()}) запущен!\n"
f"Новые лоты будут приходить сюда автоматически.",
parse_mode="Markdown"
)
@dp.message(F.text == "📋 Мои поиски")
@dp.message(Command("list"))
async def cmd_list(message: types.Message):
res = supabase.table("parser_links").select("*").eq("user_id", message.from_user.id).order("id").execute()
if not res.data:
await message.answer("У тебя пока нет активных поисков. Нажми «➕ Добавить поиск».", parse_mode="Markdown")
return
await message.answer("📋 Твои отслеживаемые поиски:", parse_mode="Markdown")
for row in res.data:
status = "🟢 Активен" if row["is_active"] else "⏸ На паузе"
toggle_text = "⏸ Пауза" if row["is_active"] else "▶️ Запустить"
kb = InlineKeyboardMarkup(inline_keyboard=[
[
InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_{row['id']}"),
InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{row['id']}")
]
])
await message.answer(
f"{row['title']} ({row['platform'].upper()})\nСтатус: {status}",
reply_markup=kb,
parse_mode="Markdown"
)
@dp.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_link(callback: types.CallbackQuery):
link_id = int(callback.data.split("_")[1])
res = supabase.table("parser_links").select("*").eq("id", link_id).eq("user_id", callback.from_user.id).execute()
if not res.data:
await callback.answer("Поиск не найден")
return
new_state = not res.data[0]["is_active"]
supabase.table("parser_links").update({"is_active": new_state}).eq("id", link_id).execute()
status = "🟢 Активен" if new_state else "⏸ На паузе"
toggle_text = "⏸ Пауза" if new_state else "▶️ Запустить"
kb = InlineKeyboardMarkup(inline_keyboard=[
[
InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_{link_id}"),
InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{link_id}")
]
])
await callback.message.edit_text(
f"{res.data[0]['title']} ({res.data[0]['platform'].upper()})\nСтатус: {status}",
reply_markup=kb,
parse_mode="Markdown"
)
await callback.answer("Статус обновлён")
@dp.callback_query(F.data.startswith("del_"))
async def cb_del_link(callback: types.CallbackQuery):
link_id = int(callback.data.split("_")[1])
supabase.table("parser_links").delete().eq("id", link_id).eq("user_id", callback.from_user.id).execute()
await callback.message.edit_text("🗑 Поиск удалён.")
await callback.answer("Удалено")
@dp.message(F.text == "💎 Тарифы и подписка")
@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
u = get_or_create_user(message.from_user)
until_text = ""
if u.get("subscription_until"):
dt = datetime.fromisoformat(u["subscription_until"].replace("Z", "+00:00"))
until_text = f"\n📅 Активна до: {dt.strftime('%d.%m.%Y %H:%M')} UTC"
text = (
f"💎 Твой тариф: {u['plan_type'].upper()} (лимит: {u['max_links']} ссылок){until_text}\n\n"
f"Доступные тарифы на 30 дней:\n"
f"🔹 PRO — 10$ / мес (до 10 ссылок)\n"
f"🔸 VIP — 25$ / мес (до 30 ссылок)\n\n"
f"Оплата в криптовалюте (USDT / TON) активируется автоматически за 10 секунд. "
f"Для оплаты картой (BYN/RUB/EU) напиши менеджеру: {MANAGER_USERNAME} (укажи свой ID: {message.from_user.id})."
)
kb = InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text="💳 Купить PRO (10$ USDT/TON)", callback_data="pay_pro")],
[InlineKeyboardButton(text="🔥 Купить VIP (25$ USDT/TON)", callback_data="pay_vip")],
])
await message.answer(text, reply_markup=kb, parse_mode="Markdown")
@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay_plan(callback: types.CallbackQuery):
plan_code = callback.data.split("_")[1]
name, price, _ = PLANS[plan_code]
invoice = await create_cryptobot_invoice(price, f"{callback.from_user.id}:{plan_code}")
if not invoice:
await callback.message.answer(
f"Для оформления тарифа {name} напиши в личку {MANAGER_USERNAME} и отправь свой ID: {callback.from_user.id}",
parse_mode="Markdown"
)
await callback.answer()
return
kb = InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text=f"🔗 Оплатить {price}$ в CryptoBot", url=invoice["pay_url"])],
[InlineKeyboardButton(text="✅ Я оплатил (проверить)", callback_data=f"chk_{invoice['invoice_id']}_{plan_code}")]
])
await callback.message.answer(
f"🧾 Счёт на тариф {name} ({price}$) создан!\n"
f"1. Нажми кнопку оплаты ниже.\n"
f"2. После перевода нажми «✅ Я оплатил».",
reply_markup=kb,
parse_mode="Markdown"
)
await callback.answer()
@dp.callback_query(F.data.startswith("chk_"))
async def cb_check_payment(callback: types.CallbackQuery):
, inv_id, plan_code = callback.data.split("")
status = await check_cryptobot_invoice(int(inv_id))
if status == "paid":
activate_subscription(callback.from_user.id, plan_code, days=30)
await callback.message.edit_text(
f"🎉 Оплата получена! Тариф {plan_code.upper()} активирован на 30 дней.",
parse_mode="Markdown"
)
elif status == "active":
await callback.answer("⏳ Оплата ещё не поступила. Попробуй через 10 секунд.", show_alert=True)
else:
await callback.message.edit_text("❌ Срок действия счёта истёк. Создай новый в разделе тарифов.")
@dp.message(Command("grant"))
async def cmd_admin_grant(message: types.Message):
if message.from_user.id != ADMIN_ID:
return
parts = message.text.split()
if len(parts) != 4 or parts[2] not in ("pro", "vip", "free"):
await message.answer("Формат: /grant USER_ID pro|vip|free ДНИ\nПример: /grant 6098417275 vip 30", parse_mode="Markdown")
return
target_id = int(parts[1])
plan = parts[2]
days = int(parts[3])
activate_subscription(target_id, plan, days)
await message.answer(f"✅ Пользователю {target_id} выдан тариф {plan.upper()} на {days} дн.", parse_mode="Markdown")
try:
await bot.send_message(target_id, f"🎁 Вам активирован тариф {plan.upper()} на {days} дней!", parse_mode="Markdown")
except Exception:
pass
@dp.message(Command("stats"))
async def cmd_admin_stats(message: types.Message):
if message.from_user.id != ADMIN_ID:
return
users = supabase.table("parser_users").select("plan_type").execute().data or []
links = supabase.table("parser_links").select("is_active").execute().data or []
pro_cnt = sum(1 for u in users if u["plan_type"] == "pro")
vip_cnt = sum(1 for u in users if u["plan_type"] == "vip")
active_links = sum(1 for l in links if l["is_active"])
await message.answer(
f"📊 Статистика бота:\n\n"
f"👥 Всего пользователей: {len(users)}\n"
f"🔹 PRO: {pro_cnt} | 🔸 VIP: {vip_cnt}\n"
f"🔗 Ссылок в базе: {len(links)} (активных: {active_links})",
parse_mode="Markdown"
)
async def sniper_worker():
logging.info("🚀 Воркер парсера запущен.")
async with AsyncSession(impersonate="chrome124") as session:
while True:
try:
res = supabase.table("parser_links").select(", parser_users()").eq("is_active", True).execute()
links = res.data or []
for link in links:
user = link.get("parser_users")
if not user or not user.get("is_active"):
continue
if user.get("plan_type") != "free" and user.get("subscription_until"):
sub_until = datetime.fromisoformat(user["subscription_until"].replace("Z", "+00:00"))
if sub_until < datetime.now(timezone.utc):
continue
platform = link["platform"]
url = link["original_url"]
last_id = link["last_item_id"]
items = await (fetch_kufar_items(session, url) if platform == "kufar" else fetch_vinted_items(session, url))
if not items:
await asyncio.sleep(random.uniform(1.0, 2.5))
continue
new_items = []
for item in items:
if item["id"] == last_id:
break
new_items.append(item)
if last_id is None and new_items:
new_items = [new_items[0]]
if new_items:
newest_id = new_items[0]["id"]
supabase.table("parser_links").update({"last_item_id": newest_id}).eq("id", link["id"]).execute()
for item in reversed(new_items):
caption = (
f"🔥 Новый лот | {link['title']}\n\n"
f"📌 {item['title']}\n"
f"💰 Цена: {item['price']}\n"
f"📏 Размер: {item['size']}\n"
f"🔗 Открыть объявление"
)
try:
if item["photo"]:
await bot.send_photo(
chat_id=link["user_id"],
photo=item["photo"],
caption=caption,
parse_mode="Markdown"
)
else:
await bot.send_message(
chat_id=link["user_id"],
text=caption,
parse_mode="Markdown"
)
except Exception as send_err:
logging.error(f"Send error to {link['user_id']}: {send_err}")
await asyncio.sleep(random.uniform(1.5, 3.0))
except Exception as loop_err:
logging.error(f"Worker loop error: {loop_err}")
await asyncio.sleep(random.uniform(25, 35))
async def main():
asyncio.create_task(sniper_worker())
await dp.start_polling(bot)
if name == "main":
asyncio.run(main())
