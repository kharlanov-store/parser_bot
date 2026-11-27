import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse, parse_qs, urlencode

from aiogram import Bot
from aiogram.types import BufferedInputFile
from curl_cffi.requests import AsyncSession
from supabase import create_client, Client

BOT_TOKEN = "".join((os.getenv("BOT_TOKEN") or "").split())
SUPABASE_URL = "".join((os.getenv("SUPABASE_URL") or "").split())
SUPABASE_KEY = "".join((os.getenv("SUPABASE_KEY") or "").split())

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot = Bot(token=BOT_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def build_vinted_api_url(browser_url: str) -> str:
    parsed = urlparse(browser_url)
    params = parse_qs(parsed.query)
    api_params = {"page": "1", "per_page": "15", "order": "newest_first"}
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
    web_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    try:
        html_resp = await session.get(browser_url, headers=web_headers, timeout=15)
        ads = []

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_resp.text)
        if match:
            next_data = json.loads(match.group(1))
            listing = next_data.get("props", {}).get("initialState", {}).get("listing", {})
            ads = listing.get("ads", [])

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

            extra_param = ""
            for param in ad.get("ad_parameters", []):
                if param.get("p") in ("size", "clothing_size", "shoes_size", "mobile_phone_memory", "condition"):
                    val = param.get("vl")
                    if val:
                        extra_param = str(val)
                        break

            images = ad.get("images", [])
            photo_url = None
            if images and isinstance(images, list) and len(images) > 0:
                img_path = images.get("path")
                if img_path:
                    photo_url = f"https://kufar.by{img_path}"

            results.append({
                "id": ad_id,
                "title": title,
                "price": price_str,
                "extra": extra_param,
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

        headers = {"Accept": "application/json, text/plain, */*", "Referer": browser_url}
        resp = await session.get(api_url, headers=headers, timeout=15)
        if resp.status_code in (401, 403):
            await session.get("https://vinted.pl", timeout=15)
            await asyncio.sleep(1.5)
            resp = await session.get(api_url, headers=headers, timeout=15)

        if resp.status_code != 200 or not resp.text.strip().startswith("{"):
            return []

        items = resp.json().get("items", [])
        results = []
        for item in items:
            item_id = str(item.get("id"))
            title = item.get("title", "Без названия")
            price_obj = item.get("price", {})
            price_str = f"{price_obj.get('amount', '?')} {price_obj.get('currency_code', 'PLN')}"
            size = item.get("size_title") or ""
            url = item.get("url", f"https://vinted.plitems/{item_id}")
            photo_url = item.get("photo", {}).get("url") if item.get("photo") else None

            results.append({"id": item_id, "title": title, "price": price_str, "extra": size, "url": url, "photo": photo_url})
        return results
    except Exception as e:
        logging.error(f"Vinted parse error: {e}")
        return []


async def run_once():
    me = await bot.get_me()
    logging.info(f"🤖 Авторизован бот: @{me.username}")

    res = supabase.table("parser_links").select("*").eq("is_active", True).execute()
    links = res.data or []
    logging.info(f"Найдено активных ссылок: {len(links)}")

    async with AsyncSession(impersonate="chrome124") as session:
        for link in links:
            platform = link["platform"]
            items = await (fetch_kufar_items(session, link["original_url"]) if platform == "kufar" else fetch_vinted_items(session, link["original_url"]))
            logging.info(f"[{link['title']}] Найдено лотов: {len(items)}")
            if not items:
                continue

            seen_ids = set((link["last_item_id"] or "").split(",")) if link["last_item_id"] else set()

            if not seen_ids:
                new_items = [items]
            else:
                new_items = [it for it in items if it["id"] not in seen_ids]

            if new_items:
                for item in reversed(new_items):
                    extra_line = f"ℹ️ Параметр: {item['extra']}\n" if item.get("extra") else ""
                    caption = (
                        f"🔥 Новый лот | {link['title']}\n\n"
                        f"📌 {item['title']}\n"
                        f"💰 Цена: {item['price']}\n"
                        f"{extra_line}"
                        f"🔗 {item['url']}"
                    )
                    sent = False
                    if item["photo"]:
                        try:
                            img_resp = await session.get(item["photo"], timeout=10)
                            if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                                photo_file = BufferedInputFile(img_resp.content, filename="lot.jpg")
                                await bot.send_photo(chat_id=link["user_id"], photo=photo_file, caption=caption)
                                sent = True
                        except Exception:
                            pass

                    if not sent:
                        try:
                            await bot.send_message(chat_id=link["user_id"], text=caption)
                        except Exception as e:
                            logging.error(f"Ошибка отправки: {e}")

                latest_ids_str = ",".join(it["id"] for it in items[:15])
                supabase.table("parser_links").update({"last_item_id": latest_ids_str}).eq("id", link["id"]).execute()

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run_once())
