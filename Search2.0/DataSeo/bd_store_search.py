import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

import db

DATASET_ID = "gd_m6gjtfmeh43we6cqc"
API_BASE = "https://api.brightdata.com/datasets/v3"
DCA_BASE = "https://api.brightdata.com/dca"
DCA_COLLECTORS = {"Harris Farm": "c_msvmt1yx1scomxxc3o"}

PATH_PATTERNS = {
    "Woolworths": "/shop/productdetails/",
    "Coles": "/product/",
    "ALDI": "/product/",
    "Harris Farm": "/products/",
    "IGA": "/product/",
}

STORE_DOMAINS = {
    "Woolworths": "https://www.woolworths.com.au",
    "Coles": "https://www.coles.com.au",
    "ALDI": "https://www.aldi.com.au",
    "Harris Farm": "https://www.harrisfarm.com.au",
    "IGA": "https://www.igashop.com.au",
}


def load_api_key():
    env = {}
    for line in Path(__file__).parent.joinpath(".env.brightdata").read_text().splitlines():
        if line.strip() and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env.get("BRIGHTDATA_API_KEY")


def build_urls(canonical):
    q = quote(canonical)
    items = []
    with db.get_conn() as conn:
        rows = conn.execute("SELECT store, search_url FROM stores ORDER BY store").fetchall()
    for r in rows:
        if r["store"] in DCA_COLLECTORS:
            items.append({"store": r["store"], "url": r["search_url"], "base": r["search_url"]})
        else:
            items.append({"store": r["store"], "url": r["search_url"].format(query=q)})
    return items


def trigger(api_key, items):
    payload = {"input": [{"url": it["url"]} for it in items], "limit_per_input": 5}
    r = requests.post(
        f"{API_BASE}/trigger",
        params={"dataset_id": DATASET_ID, "notify": "false", "include_errors": "true"},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def poll(api_key, snapshot_id, timeout=900, log=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API_BASE}/progress/{snapshot_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        body = r.json()
        status = body.get("status")
        message = f"  poll: {status} ({body.get('records', '?')} records)"
        if log:
            log(f"dataset {snapshot_id} {status} ({body.get('records', '?')} records)")
        print(message)
        if status == "ready":
            return body
        if status in ("failed", "error"):
            raise SystemExit(f"snapshot failed: {body}")
        time.sleep(15)
    raise TimeoutError("snapshot not ready in time")


def download(api_key, snapshot_id):
    r = requests.get(
        f"{API_BASE}/snapshot/{snapshot_id}",
        params={"format": "json"},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def trigger_dca(api_key, collector_id, canonical, base_url):
    payload = [{"search_query": canonical, "url": base_url}]
    r = requests.post(
        f"{DCA_BASE}/trigger",
        params={"collector": collector_id, "queue_next": "1"},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    collection_id = body.get("collection_id")
    if not collection_id:
        print("dca trigger response:", body)
        raise SystemExit("no collection_id returned")
    return collection_id


def poll_dca(api_key, collection_id, timeout=900, log=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{DCA_BASE}/dataset?id={collection_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        text = r.text.lstrip()
        if text.startswith("{"):
            first = text.splitlines()[0].strip()
            if '"status"' not in first:
                return
            status = json.loads(first).get("status")
            if log:
                log(f"dca {collection_id} {status}")
            print(f"  poll [dca]: {status}")
        time.sleep(15)
    raise TimeoutError("dca dataset not ready in time")


def download_dca(api_key, collection_id):
    r = requests.get(f"{DCA_BASE}/dataset?id={collection_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rows.append(json.loads(line))
    return rows


def dca_record_to_product(rec):
    price = rec.get("price") or {}
    value = price.get("value") if isinstance(price, dict) else price
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = None
    return {
        "name": rec.get("product_name"),
        "price": value,
        "url": rec.get("product_url"),
        "image_url": rec.get("image_url"),
    }


def find_link_bracket(pre):
    idx = len(pre)
    while True:
        idx = pre.rfind("[", 0, idx)
        if idx == -1:
            return -1
        if idx == 0 or pre[idx - 1] != "!":
            return idx


def extract_name(link_text):
    m = re.search(r'!\[\]\([^)]*"([^"]+)"\)', link_text)
    if m:
        return m.group(1)
    m = re.search(r"!\[([^\]]+)\]\([^)]*\)", link_text)
    if m:
        return m.group(1).strip()
    m = re.search(r"\[([^\]]+)\]\($", link_text)
    if m:
        return m.group(1).strip()
    return link_text[1:].rsplit("]", 1)[0].strip()


def first_actual_price(chunk):
    """First $ price in chunk, skipping 'SAVE $x.xx' and 'was $x.xx' tags
    (Woolworths roundels, IGA was-price strikethrough)."""
    for m in re.finditer(r"\$(\d[\d,]*(?:\.\d{1,2})?)", chunk):
        seg = chunk[max(0, m.start() - 10) : m.start()].upper()
        if re.search(r"(SAVE|WAS)\s*$", seg):
            continue
        return m.group(1)
    return None


def extract_products(store, markdown):
    price_re = re.compile(r"\$(\d[\d,]*(?:\.\d{1,2})?)")
    img_re = re.compile(r"!\[[^\]]*\]\(([^)]+?)(?:\s+\"[^\"]*\")?\)")
    needle = PATH_PATTERNS.get(store, "/product/")
    domain = STORE_DOMAINS.get(store, "")
    results = {}
    paths = [
        m
        for m in re.finditer(re.escape(needle) + r"[^)\s]+", markdown)
        if not re.search(r"\.(?:jpg|jpeg|png|webp|svg|gif)\b", m.group(0))
        and "/cdn/" not in m.group(0)
        and not re.search(r"(?:is/image|scaleWidth|\.(?:jpg|jpeg|png|webp)\/)", m.group(0))
    ]
    for i, m in enumerate(paths):
        pre = markdown[: m.start()]
        open_bracket = find_link_bracket(pre)
        if open_bracket == -1:
            continue
        link_text = pre[open_bracket:]
        name = extract_name(link_text).strip()
        if not name:
            name = m.group(0).strip("/").rsplit("/", 1)[-1]
        img_m = img_re.search(link_text) or img_re.search(pre[max(0, m.start() - 400) : m.start()])
        image_url = img_m.group(1).strip() if img_m else None
        product_url = m.group(0)
        if domain and product_url.startswith("/"):
            product_url = domain + product_url
        end = m.end()
        nxt = paths[i + 1].start() if i + 1 < len(paths) else len(markdown)
        price = None
        if store == "ALDI":
            lt_prices = price_re.findall(link_text)
            if lt_prices:
                price = float(lt_prices[-1].replace(",", ""))
        if price is None:
            price_str = first_actual_price(markdown[end : min(nxt, end + 300)])
            if price_str is None:
                continue
            price = float(price_str.replace(",", ""))
        if price <= 0 or price > 1000:
            continue
        key = (name, price)
        if key not in results:
            results[key] = {"name": name, "price": price, "url": product_url, "image_url": image_url}
    return list(results.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("canonical", help="canonical product name, e.g. 'a2 Milk Full Cream 2L'")
    ap.add_argument("--api-key", help="Bright Data API key (defaults to .env.brightdata)")
    args = ap.parse_args()

    api_key = args.api_key or load_api_key()
    if not api_key:
        raise SystemExit("BRIGHTDATA_API_KEY not set in .env.brightdata")

    items = build_urls(args.canonical)
    print(f"=== Search for '{args.canonical}' ===")
    for it in items:
        if "base" in it:
            print(f"  {it['store']:<13} collector={DCA_COLLECTORS[it['store']]} base={it['url']}")
        else:
            print(f"  {it['store']:<13} {it['url']}")

    ds_items = [it for it in items if "base" not in it]
    dca_items = [it for it in items if "base" in it]

    snapshot_id = None
    if ds_items:
        print("\nTriggering Bright Data dataset...")
        body = trigger(api_key, ds_items)
        snapshot_id = body.get("snapshot_id")
        if not snapshot_id:
            print("trigger response:", body)
            raise SystemExit("no snapshot_id returned")
        print(f"  snapshot_id: {snapshot_id}")
        poll(api_key, snapshot_id)

    dca_jobs = {}
    for it in dca_items:
        print(f"\nTriggering DCA collector for {it['store']}...")
        collection_id = trigger_dca(api_key, DCA_COLLECTORS[it["store"]], args.canonical, it["url"])
        print(f"  collection_id: {collection_id}")
        dca_jobs[it["store"]] = collection_id

    for store, collection_id in dca_jobs.items():
        poll_dca(api_key, collection_id)

    store_products = {}
    if snapshot_id:
        records = download(api_key, snapshot_id)
        url_to_store = {it["url"]: it["store"] for it in ds_items}
        for rec in records:
            store = url_to_store.get(rec.get("input", {}).get("url")) or rec.get("input", {}).get("url")
            markdown = rec.get("markdown") or ""
            store_products[store] = extract_products(store, markdown)
    for store, collection_id in dca_jobs.items():
        rows = download_dca(api_key, collection_id)
        store_products[store] = [dca_record_to_product(r) for r in rows]

    print(f"\n=== PRICE COMPARISON: {args.canonical} (AU) ===\n")
    for it in items:
        store = it["store"]
        prods = store_products.get(store) or []
        print(f"[{store}]")
        if not prods:
            print("  (no products extracted)")
        for p in prods[:5]:
            price = p["price"]
            if price is None:
                print(f"  {'N/A':>10}  {p['name']}")
            else:
                print(f"  ${price:>8.2f}  {p['name']}")
            if p.get("url"):
                print(f"    url:  {p['url']}")
            if p.get("image_url"):
                print(f"    img:  {p['image_url']}")
        print()


if __name__ == "__main__":
    main()
