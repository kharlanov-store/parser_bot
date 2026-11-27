import asyncio
import json
import logging
import os
import random
import re
from urllib.parse import urlparse, parse_qs, urlencode

from aiogram import Bot
from curl_cffi.requests import AsyncSession
from supabase import create_client, Client

BOT_TOKEN = "".join((os.getenv("BOT_TOKEN") or "").split())
SUPABASE_URL = "".join((os.getenv("SUPABASE_URL") or "").split())
SUPABASE_KEY = "".join((os.getenv("SUPABASE_KEY") or "").split())

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot = Bot(token=BOT_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def build_kufar_api_url(browser_url: str) -> str:
    parsed = urlparse(browser_url)
    params = parse_qs(parsed.query)
    api_params = {"lang": "ru", "size": "15", "sort": "lst.d"}
    for k, v in params.items():
        if k not in ("sort", "size", "cursor", "r_recognize_suggester", "utm_suggestionType"):
            api_params[k] = v if len(v) == 1 else ",".join(v)
    return f"https://kufar.by?{urlencode(api_params)}"


async def fetch_kufar_items(session: AsyncSession, browser_url: str) -> list[dict]:
    api_url = build_kufar_api_url(browser_url)
    mobile_headers = {
        "User-Agent": "okhttp/4.11.0",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "X-App-Name": "Kufar",
        "X-Platform": "android",
    }
    try:
        async with AsyncSession() as mobile_session:
            resp = await mobile_session.get(api_url, headers=mobile_headers, timeout=15)

        ads = []
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            ads = resp.json().get("ads", [])
        else:
            web_headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
            html_resp = await session.get(browser_url, headers=web_headers, timeout=15)
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_resp.text)
            if match:
                next_data = json.loads(match.group(1))
                ads = next_data.get("props", {}).get("initialState", {}).get("listing", {}).get("ads", [])

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
            photo_url = None
            if images and isinstance(images, list) and len(images) > 0:
                img_path = images[0].get("path")
                if img_path:
                    photo_url = f"https://kufar.by{img_path}"

            results.append({"id": ad_id, "title": title, "price": price_str, "size": size, "url": link, "photo": photo_url})
        return results
    except Exception as e:
        logging.error(f"Kufar parse error: {e}")
        return []


async def run_once():
    me = await bot.get_me()
    logging.info(f"🤖 Авторизован бот: @{me.username} (id={me.id})")

    res = supabase.table("parser_links").select("*").eq("is_active", True).execute()
    links = res.data or []
    logging.info(f"Найдено активных ссылок: {len(links)}")

    async with AsyncSession(impersonate="chrome124") as session:
        for link in links:
            logging.info(f"Проверяю ссылку id={link['id']}, user_id={link['user_id']}, last_id={link['last_item_id']}")
            items = await fetch_kufar_items(session, link["original_url"])
            logging.info(f"Найдено лотов: {len(items)}")
            if not items:
                continue

            new_items = []
            for item in items:
                if item["id"] == link["last_item_id"]:
                    break
                new_items.append(item)

            if link["last_item_id"] is None and new_items:
                new_items = [new_items[0]]

            logging.info(f"Новых лотов к отправке: {len(new_items)}")
            if new_items:
                for item in reversed(new_items):
                    caption = (
                        f"🔥 Новый лот | {link['title']}\n\n"
                        f"📌 {item['title']}\n"
                        f"💰 Цена: {item['price']}\n"
                        f"📏 Размер: {item['size']}\n"
                        f"🔗 {item['url']}"
                    )
                    try:
                        if item["photo"]:
                            await bot.send_photo(chat_id=link["user_id"], photo=item["photo"], caption=caption)
                        else:
                            await bot.send_message(chat_id=link["user_id"], text=caption)
                        logging.info(f"✅ Успешно отправлено в chat_id={link['user_id']}!")
                    except Exception as e:
                        logging.error(f"❌ Ошибка отправки в chat_id={link['user_id']}: {e}")

                supabase.table("parser_links").update({"last_item_id": new_items[0]["id"]}).eq("id", link["id"]).execute()

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run_once())
