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
return f"vinted.pl?{urlencode(api_params)}"
async def fetch_kufar_items(session: AsyncSession, browser_url: str) -> list:
web_headers = {
"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
"Accept-Language": "ru-RU,ru;q=0.9",
}
try:
html_resp = await session.get(browser_url, headers=web_headers, timeout=15)
ads = []
match = re.search(r'(.*?)', html_resp.text)
if match:
next_data = json.loads(match.group(1))
listing = next_data.get("props", {}).get("initialState", {}).get("listing", {})
if isinstance(listing, dict):
ads = listing.get("ads", [])
