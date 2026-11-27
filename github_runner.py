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
    api_url = build_kufar_api_url(browser_url)
    # Заголовки мобильного клиента Kufar Android
    mobile_headers = {
        "User-Agent": "okhttp/4.11.0",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "X-App-Name": "Kufar",
        "X-Platform": "android",
    }
    try:
        # Делаем запрос без TLS-имперсонации браузера, чтобы имитировать чистый okhttp Android
        async with AsyncSession() as mobile_session:
            resp = await mobile_session.get(api_url, headers=mobile_headers, timeout=15)

        logging.info(f"Kufar API status={resp.status_code}, len={len(resp.text)}")
        ads = []

        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            ads = resp.json().get("ads", [])
        else:
            # Резервный парсинг веб-страницы с мобильным User-Agent
            web_headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
            html_resp = await session.get(browser_url, headers=web_headers, timeout=15)
            logging.info(f"Kufar HTML status={html_resp.status_code}, len={len(html_resp.text)}")
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_resp.text)
            if match:
                next_data = json.loads(match.group(1))
                ads = (
                    next_data.get("props", {})
                    .get("initialState", {})
                    .get("listing", {})
                    .get("ads", [])
                )

        if not ads:
            logging.warning("Kufar не вернул объявлений (IP заблокирован антиботом).")
            return []

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

            results.append({
                "id": ad_id, "title": title, "price": price_str,
                "size": size, "url": link, "photo": photo_url
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

        logging.info(f"Vinted API status={resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200 or not resp.text.strip().startswith("{"):
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
                "id": item_id, "title": title, "price": price_str,
                "size": size, "url": url, "photo": photo_url
            })
        return results
    except Exception as e:
        logging.error(f"Vinted parse error: {e}")
        return []


async def run_once():
    logging.info("🔍 Запуск проверки ссылок через GitHub Actions...")
    res = supabase.table("parser_links").select("*, parser_users(*)").eq("is_active", True).execute()
    links = res.data or []
    logging.info(f"Найдено активных ссылок: {len(links)}")

    async with AsyncSession(impersonate="chrome124") as session:
        for link in links:
            user = link.get("parser_users")
            if not user or not user.get("is_active"):
                continue

            platform = link["platform"]
            url = link["original_url"]
            last_id = link["last_item_id"]

            items = await (fetch_kufar_items(session, url) if platform == "kufar" else fetch_vinted_items(session, url))
            logging.info(f"Получено лотов для '{link['title']}': {len(items)}")
            if not items:
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
                        f"🔥 **Новый лот | {link['title']}**\n\n"
                        f"📌 **{item['title']}**\n"
                        f"💰 Цена: **{item['price']}**\n"
                        f"📏 Размер: **{item['size']}**\n"
                        f"🔗 [Открыть объявление]({item['url']})"
                    )
                    try:
                        if item["photo"]:
                            await bot.send_photo(chat_id=link["user_id"], photo=item["photo"], caption=caption, parse_mode="Markdown")
                        else:
                            await bot.send_message(chat_id=link["user_id"], text=caption, parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Ошибка отправки в Telegram: {e}")

            await asyncio.sleep(random.uniform(1.0, 2.0))

    await bot.session.close()
    logging.info("✅ Проверка завершена.")


if __name__ == "__main__":
    asyncio.run(run_once())
