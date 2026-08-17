import re
import json
import time
import threading
import requests
from pathlib import Path

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

QUERY = "a2 full cream milk 2l"
LOCATION_CODE = 2036


def extract_ww_price(html):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        pd = data["props"]["pageProps"]["pdDetails"]
        prod = pd["Product"]
        return {
            "name": prod["Name"],
            "price": float(prod["Price"]),
            "currency": "AUD",
            "thumbnail": prod.get("LargeImageFile") or prod.get("MediumImageFile"),
        }
    except Exception:
        return None


def extract_coles_price(html):
    m = re.search(r'"price"\s*:\s*([0-9.]+)', html)
    if not m:
        return None
    name_m = re.search(r'"name"\s*:\s*"([^"]{5,80})"', html)
    img_m = re.search(r'<meta property="og:image" content="([^"]+)"', html) or re.search(r'"image"\s*:\s*"([^"]+\.jpg)"', html)
    return {
        "name": name_m.group(1) if name_m else None,
        "price": float(m.group(1)),
        "currency": "AUD",
        "thumbnail": img_m.group(1) if img_m else None,
    }


def extract_aldi_price(html):
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        entries = d if isinstance(d, list) else [d]
        for item in entries:
            if isinstance(item, dict) and item.get("@type") == "Product":
                offers = item.get("offers") or {}
                price = offers.get("price") if isinstance(offers, dict) else None
                img = item.get("image")
                img = img[0] if isinstance(img, list) and img else img
                return {
                    "name": item.get("name"),
                    "price": float(price),
                    "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
                    "thumbnail": img,
                }
    return None


def extract_hf_price(html):
    m = re.search(r'"handle":"[^"]+","variants":\[\{"[^}]*"price":(\d+)', html)
    if not m:
        return None
    name_m = re.search(r'"name":"([^"]{5,80})","public_title"', html)
    img_m = re.search(r'"image":\s*\{"src":\s*"([^"]+)"', html)
    img = img_m.group(1) if img_m else None
    if img:
        img = img.replace("\\/", "/")
        if img.startswith("//"):
            img = "https:" + img
    return {
        "name": name_m.group(1) if name_m else None,
        "price": float(m.group(1)) / 100,
        "currency": "AUD",
        "thumbnail": img,
    }


def extract_iga_price(html):
    m = re.search(r'"price"\s*:\s*([0-9.]+)', html)
    if not m:
        return None
    name_m = re.search(r'"name"\s*:\s*"([^"]{5,80})"', html)
    img_m = re.search(r'(https://cdn\.metcash\.media/[^"\']+\.jpg)', html)
    return {
        "name": name_m.group(1) if name_m else None,
        "price": float(m.group(1)),
        "currency": "AUD",
        "thumbnail": img_m.group(1) if img_m else None,
    }


CHAINS = [
    {"name": "Woolworths", "domain": "woolworths.com.au", "product_path": "productdetails/", "extract": extract_ww_price},
    {"name": "Coles", "domain": "coles.com.au", "product_path": "/product/", "extract": extract_coles_price},
    {"name": "ALDI", "domain": "aldi.com.au", "product_path": "/product/", "extract": extract_aldi_price},
    {"name": "Harris Farm", "domain": "harrisfarm.com.au", "product_path": "/products/", "extract": extract_hf_price},
    {"name": "IGA", "domain": "igashop.com.au", "product_path": "/product/", "extract": extract_iga_price},
]


class DataSeoClient:
    def __init__(self, env_path=".env"):
        env = {}
        for line in Path(env_path).read_text().splitlines():
            if line.strip() and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        self.session = requests.Session()
        self.session.auth = (env["DATAFORSEO_LOGIN"], env["DATAFORSEO_PASSWORD"])
        self.web = requests.Session()
        self.web.headers.update(HEADERS)
        self.serp_base = "https://api.dataforseo.com/v3/serp/google/organic"
        self.merchant_base = "https://api.dataforseo.com/v3/merchant/google/products"
        self.cache_file = Path(__file__).parent / "url_cache.json"
        self.cache = self._load_cache()

    def _load_cache(self):
        if self.cache_file.exists():
            try:
                return json.loads(self.cache_file.read_text())
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        self.cache_file.write_text(json.dumps(self.cache, indent=2))

    def find_product_url(self, domain, query, product_path, skip_serp=False):
        key = f"{domain}|{query}"
        if key in self.cache:
            return self.cache[key]
        if skip_serp:
            return None
        payload = [{
            "keyword": f"site:{domain} {query}",
            "location_code": LOCATION_CODE,
            "language_code": "en",
            "se_domain": "google.com.au",
            "depth": 20,
        }]
        try:
            resp = self.session.post(self.serp_base + "/live/regular", json=payload, timeout=90)
            tb = resp.json().get("tasks") or []
            tb = tb[0] if tb else {}
        except Exception:
            return None
        if tb.get("status_code") == 40200:
            print(f"  [DataForSEO] credits exhausted (402) for {domain}")
            return None
        url = None
        if tb.get("result"):
            urls = [it.get("url") for it in tb["result"][0].get("items") or [] if it.get("type") == "organic"]
            for u in urls:
                if u and product_path in u:
                    url = u
                    break
        self.cache[key] = url
        self._save_cache()
        return url

    def probe(self, chain):
        """Detect per-site fetch strategy with a single gentle request."""
        key = f"strategy|{chain['domain']}"
        if key in self.cache:
            return self.cache[key]

        probe_url = f"https://www.{chain['domain']}/"
        strategy = "requests"
        try:
            r = requests.get(probe_url, headers=HEADERS, timeout=20)
            m = re.search(r"<title>(.*?)</title>", r.text, re.S)
            title = m.group(1) if m else ""
            if "pardon" in title.lower() or "access denied" in title.lower() or "captcha" in title.lower():
                strategy = "cdp"
        except Exception:
            strategy = "cdp"

        self.cache[key] = strategy
        self._save_cache()
        print(f"  probe [{chain['name']}]: strategy = {strategy}")
        return strategy

    def fetch(self, url, chain=None):
        strategy = None
        if chain:
            strategy = self.probe(chain)
        if strategy == "cdp":
            return self._fetch_cdp(url)
        if strategy == "playwright":
            return self._fetch_playwright(url)

        import random
        for attempt in range(3):
            with fetch_lock:
                time.sleep(random.uniform(2, 5))
                try:
                    r = requests.get(url, headers=HEADERS, timeout=30)
                except Exception:
                    time.sleep(20)
                    continue
                m = re.search(r"<title>(.*?)</title>", r.text, re.S)
                title = m.group(1) if m else ""
                if "pardon" in title.lower() or "access denied" in title.lower():
                    break
                return r.text
        return self._fetch_cdp(url)

    def _fetch_cdp(self, url, cdp_url="http://localhost:9222"):
        try:
            import asyncio
            from playwright.async_api import async_playwright
        except ImportError:
            return None

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(cdp_url)
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(5000)
                    return await page.content()
                finally:
                    await page.close()

        return asyncio.run(run())

    def _fetch_playwright(self, url):
        try:
            import asyncio
            from playwright.async_api import async_playwright
        except ImportError:
            return None

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    ctx = await browser.new_context(
                        user_agent=HEADERS["User-Agent"],
                        viewport={"width": 1366, "height": 900},
                        locale="en-AU",
                    )
                    page = await ctx.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(6000)
                    return await page.content()
                finally:
                    await browser.close()

        return asyncio.run(run())

    def coles_merchant_fallback(self, query):
        """DataForSEO Google Shopping fallback for Coles (Railway-safe, no browser)."""
        payload = [{
            "keyword": query,
            "location_code": LOCATION_CODE,
            "language_code": "en",
            "se_domain": "google.com.au",
            "depth": 40,
        }]
        try:
            resp = self.session.post(self.merchant_base + "/task_post", json=payload, timeout=60)
            tb = resp.json().get("tasks") or []
            tb = tb[0] if tb else {}
        except Exception:
            return None
        if tb.get("status_code") == 40200:
            return None
        tid = tb.get("id")
        if not tid:
            return None
        for _ in range(15):
            time.sleep(5)
            try:
                gt = self.session.get(f"{self.merchant_base}/task_get/advanced/{tid}", timeout=60)
                gb = gt.json().get("tasks") or []
                gb = gb[0] if gb else {}
            except Exception:
                continue
            if gb.get("status_code") == 40200:
                return None
            if gb.get("result"):
                items = [it for res in gb["result"] for it in (res.get("items") or []) if isinstance(it, dict)]
                for it in items:
                    seller = (it.get("seller") or "").lower()
                    if "coles" in seller:
                        return {
                            "chain": "Coles",
                            "url": it.get("shopping_url") or it.get("url"),
                            "name": it.get("title"),
                            "price": it.get("price"),
                            "currency": it.get("currency", "AUD"),
                            "note": "via Google Shopping merchant API",
                        }
                return None
            if gb.get("status_code") == 40102:
                return None
        return None


fetch_lock = threading.Lock()


def run_chain(client, chain, query, refresh=False):
    url = client.find_product_url(chain["domain"], query, chain["product_path"], skip_serp=refresh)
    if not url:
        return {"chain": chain["name"], "url": None, "price": None, "note": "no product url found (refresh: not cached)" if refresh else "no product url found"}
    html = client.fetch(url, chain=chain)
    if html is None:
        if chain["name"] == "Coles":
            fb = client.coles_merchant_fallback(query)
            if fb:
                return fb
        return {"chain": chain["name"], "url": url, "price": None, "note": "blocked by anti-bot"}
    result = chain["extract"](html)
    if result is None:
        # page fetched but no price found -> maybe delisted/404; invalidate cache
        client.cache.pop(f"{chain['domain']}|{query}", None)
        client._save_cache()
        return {"chain": chain["name"], "url": url, "price": None, "note": "price not found (cache invalidated)"}
    result.setdefault("url", url)
    result.setdefault("chain", chain["name"])
    return result


def main():
    import sys
    args = sys.argv[1:]
    refresh = "--refresh" in args
    no_db = "--no-db" in args
    queries = [a for a in args if a not in ("--refresh", "--no-db")] or [QUERY]
    client = DataSeoClient()
    for chain in CHAINS:
        client.probe(chain)

    db = None
    if not no_db:
        try:
            import db as dbmod
            dbmod.init_schema()
            db = dbmod
        except Exception as e:
            print(f"  [db] disabled: {e}")

    for query in queries:
        canonical_id = query
        results = {}
        threads = [threading.Thread(target=lambda c=c: results.__setitem__(c["name"], run_chain(client, c, query, refresh))) for c in CHAINS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"\n=== PRICE COMPARISON: {query} (AU){' [refresh]' if refresh else ''} ===\n")
        for chain in CHAINS:
            r = results.get(chain["name"]) or {"chain": chain["name"], "url": None, "price": None, "note": "error"}
            price = r["price"]
            if price is not None:
                print(f"{chain['name']:<14} ${price:.2f}  {r.get('name')}")
            else:
                print(f"{chain['name']:<14} N/A  {r.get('note') or 'price not found'}")
            if r.get("url"):
                print(f"   {r['url']}")
            if r.get("thumbnail"):
                print(f"   img: {r['thumbnail']}")
            if db:
                try:
                    db.upsert_canonical(canonical_id, query, country=db.COUNTRY)
                    db.save_result(canonical_id, query, db.COUNTRY, chain["name"], r)
                except Exception as e:
                    print(f"   [db error] {e}")
        if db:
            print(f"  -> saved to postgres (canonical_id='{canonical_id}')")


if __name__ == "__main__":
    main()
