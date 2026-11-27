import asyncio, json, logging, os, re
from urllib.parse import urlparse, parse_qs, urlencode
from aiogram import Bot
from aiogram.types import BufferedInputFile
from curl_cffi.requests import AsyncSession
from supabase import create_client

BOT_TOKEN = "".join((os.getenv("BOT_TOKEN") or "").split())
SUPABASE_URL = "".join((os.getenv("SUPABASE_URL") or "").split())
SUPABASE_KEY = "".join((os.getenv("SUPABASE_KEY") or "").split())
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot = Bot(token=BOT_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

async def fetch_kufar(session, url):
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9"}
    r = await session.get(url, headers=headers, timeout=15)
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
    if not m:
        return []
    data = json.loads(m.group(1))
    listing = data.get("props", {}).get("initialState", {}).get("listing", {})
    ads = listing.get("ads", []) if isinstance(listing, dict) else []
    res = []
    for ad in ads:
        if not isinstance(ad, dict) or ad.get("company_ad"):
            continue
        ad_id = str(ad.get("ad_id"))
        price = ad.get("price_byn", "0")
        p_str = f"{int(price)/100:.2f} BYN" if str(price).isdigit() and int(price) > 0 else "Договорная"
        imgs = ad.get("images") or []
        photo = f"https://kufar.by{imgs[0]['path']}" if imgs and isinstance(imgs[0], dict) and imgs[0].get("path") else None
        res.append({"id": ad_id, "title": ad.get("subject", "Без названия"), "price": p_str, "url": ad.get("ad_link", f"https://kufar.by{ad_id}"), "photo": photo})
    return res

async def run():
    links = supabase.table("parser_links").select("*").eq("is_active", True).execute().data or []
    async with AsyncSession(impersonate="chrome124") as session:
        for link in links:
            items = await fetch_kufar(session, link["original_url"])
            logging.info(f"[{link['title']}] Найдено: {len(items)}")
            if not items:
                continue
            seen = set((link["last_item_id"] or "").split(",")) if link["last_item_id"] else set()
            new_items = items[:1] if not seen else [x for x in items if x["id"] not in seen]
            for it in reversed(new_items):
                msg = f"🔥 Новый лот | {link['title']}\n\n📌 {it['title']}\n💰 Цена: {it['price']}\n🔗 {it['url']}"
                sent = False
                if it["photo"]:
                    try:
                        ir = await session.get(it["photo"], timeout=10)
                        if ir.status_code == 200:
                            await bot.send_photo(link["user_id"], BufferedInputFile(ir.content, "lot.jpg"), caption=msg)
                            sent = True
                    except Exception:
                        pass
                if not sent:
                    await bot.send_message(link["user_id"], msg)
            if new_items:
                supabase.table("parser_links").update({"last_item_id": ",".join(x["id"] for x in items[:15])}).eq("id", link["id"]).execute()
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(run())
