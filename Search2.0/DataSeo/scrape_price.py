import asyncio
import json
import re
from playwright.async_api import async_playwright

URL = "https://www.woolworths.com.au/shop/productdetails/208064/a2-milk-full-cream-milk"

PRICE_RE = re.compile(r'"price"\s*:\s*"?\$?([0-9]+\.[0-9]{2})"?')


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        html = await page.content()
        title = await page.title()

        price = None
        m = re.search(r'"price"\s*:\s*([0-9.]+)', html)
        if m:
            price = float(m.group(1))
        else:
            m = PRICE_RE.search(html)
            if m:
                price = float(m.group(1))

        selectors = [
            "span.price",
            "div.price",
            "[class*=price]",
            "[data-testid*=price]",
        ]
        text_prices = set()
        for sel in selectors:
            for el in await page.query_selector_all(sel):
                txt = (await el.inner_text() or "").strip()
                if "$" in txt:
                    text_prices.add(txt[:40])
                    if price is None:
                        mm = re.search(r"\$([0-9]+\.[0-9]{2})", txt)
                        if mm:
                            price = float(mm.group(1))

        print("URL:", URL)
        print("TITLE:", title)
        print("PRICE from JSON:", price)
        print("text price elements:", text_prices)
        await browser.close()


asyncio.run(main())
