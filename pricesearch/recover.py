"""URL + thumbnail recovery via each store's own product-search API.

Gemini sometimes returns placeholder URLs (filtered out) or products whose
scraped page has no og:image (JS-rendered SPA shells). This module queries the
stores' public search APIs directly — which return structured, authoritative
product URLs AND image URLs — and fills the gaps by matching the product name.

Endpoints (live-verified):
  - Woolworths: GET apis/ui/Search/products            -> SmallImageFile
  - Coles:      GET api/bff/products/search            -> imageUris[]
  - Aldi:       GET asl.api.aldi.com.au/v3/product-search -> assets[].url
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
_STORE_CACHE_PATH = CACHE_DIR / "stores.json"
_STORE_TTL_SECONDS = 24 * 60 * 60
_LOCK = threading.Lock()

_COLES_STORE_ID = "0584"
_COLES_SUB_KEY = "eae83861d1cd4de6bb9cd8a2cd6f041e"
_COLES_IMAGE_BASE = "https://shop.coles.com.au/wcsstore/Coles-CAS/images"
_ALDI_IMAGE_WIDTH = "400"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_STOP_WORDS = {"the", "and", "for", "with", "a", "an", "of", "to", "in", "pack", "bag", "each"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "application/json",
}
_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().casefold()).strip("-")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.casefold()) if t not in _STOP_WORDS}


def _overlap(query: str, title: str) -> float:
    a, b = _tokens(query), _tokens(title)
    if not a:
        return 0.0
    return len(a & b) / len(a)


# ---------------------------------------------------------------------------
# Store adapters
# ---------------------------------------------------------------------------

def _woolworths_search(query: str) -> list[dict[str, str | None]]:
    response = _SESSION.get(
        "https://www.woolworths.com.au/apis/ui/Search/products",
        params={"searchTerm": query, "pageNumber": "1", "pageSize": "24"},
        headers={"Origin": "https://www.woolworths.com.au/", "Referer": "https://www.woolworths.com.au/"},
        timeout=15,
    )
    response.raise_for_status()
    out: list[dict[str, str | None]] = []
    for group in response.json().get("Products") or []:
        for item in group.get("Products") or []:
            title = str(item.get("DisplayName") or item.get("Name") or "").strip()
            sku = str(item.get("Stockcode") or "").strip()
            if not title or not sku:
                continue
            out.append({
                "name": title,
                "url": f"https://www.woolworths.com.au/shop/productdetails/{sku}",
                "image": str(item.get("SmallImageFile") or "").strip() or None,
            })
    return out


def _coles_search(query: str) -> list[dict[str, str | None]]:
    response = _SESSION.get(
        "https://www.coles.com.au/api/bff/products/search",
        params={
            "storeId": _COLES_STORE_ID,
            "searchTerm": query,
            "start": "0",
            "sortBy": "salesDescending",
            "excludeAds": "true",
            "authenticated": "false",
            "subscription-key": _COLES_SUB_KEY,
        },
        headers={"Origin": "https://www.coles.com.au/", "Referer": "https://www.coles.com.au/"},
        timeout=15,
    )
    response.raise_for_status()
    out: list[dict[str, str | None]] = []
    for item in response.json().get("results") or []:
        title = str(item.get("name") or "").strip()
        sku = str(item.get("id") or item.get("sku") or "").strip()
        if not title or not sku:
            continue
        image = None
        if len(sku) >= 3:
            image = f"{_COLES_IMAGE_BASE}/{sku[0]}/{sku[1]}/{sku[2]}/{sku}.jpg"
        out.append({
            "name": title,
            "url": f"https://www.coles.com.au/product/{_slug(title)}-{sku}",
            "image": image,
        })
    return out


def _aldi_search(query: str) -> list[dict[str, str | None]]:
    response = _SESSION.get(
        "https://asl.api.aldi.com.au/commerce/v3/product-search",
        params={"q": query, "limit": "12", "offset": "0", "sort": "relevance"},
        headers={"Origin": "https://www.aldi.com.au/", "Referer": "https://www.aldi.com.au/"},
        timeout=15,
    )
    response.raise_for_status()
    out: list[dict[str, str | None]] = []
    for item in response.json().get("data") or []:
        title = str(item.get("name") or "").strip()
        slug = str(item.get("urlSlugText") or "").strip()
        if not title or not slug:
            continue
        image = None
        for asset in item.get("assets") or []:
            if isinstance(asset, dict) and asset.get("url"):
                url = str(asset["url"]).replace("{width}", _ALDI_IMAGE_WIDTH).replace("{slug}", slug)
                if url.startswith("http"):
                    image = url
                    break
        out.append({
            "name": title,
            "url": f"https://www.aldi.com.au/shop/en/products/{slug}",
            "image": image,
        })
    return out


_ADAPTERS = {
    "Woolworths": _woolworths_search,
    "Coles": _coles_search,
    "Aldi": _aldi_search,
}


# ---------------------------------------------------------------------------
# Cached store search
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, list[dict[str, str | None]]]:
    if not _STORE_CACHE_PATH.exists():
        return {}
    try:
        if time.time() - _STORE_CACHE_PATH.stat().st_mtime > _STORE_TTL_SECONDS:
            return {}
        return json.loads(_STORE_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _STORE_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def search_store(store: str, query: str) -> list[dict[str, str | None]]:
    """Top results for a product query at one store (cached 24h)."""
    adapter = _ADAPTERS.get(store)
    if not adapter:
        return []
    key = f"{store.lower()}|{query.strip().casefold()}"
    with _LOCK:
        cache = _load_cache()
        if key in cache:
            return cache[key]
    try:
        hits = adapter(query)
    except requests.RequestException:
        hits = []
    with _LOCK:
        cache = _load_cache()
        cache[key] = hits
        _save_cache(cache)
    return hits


def _best_match(product: dict[str, Any], hits: list[dict[str, str | None]]) -> dict[str, str | None] | None:
    """Best hit by token overlap with the product name; needs >= 0.5 overlap."""
    name = product.get("name") or ""
    if not name or not hits:
        return None
    best: dict[str, str | None] | None = None
    best_score = 0.5
    for hit in hits:
        score = _overlap(name, hit.get("name") or "")
        if score >= best_score:
            best, best_score = hit, score
    return best


def recover_product(product: dict[str, Any]) -> dict[str, str | None] | None:
    """Fill url/image for one product using its store's search API."""
    store = product.get("store") or ""
    if store not in _ADAPTERS:
        return None
    query = (product.get("name") or "").strip()
    if query.lower().startswith(store.lower()):
        query = query[len(store):].strip(" -")
    if not query:
        return None
    hits = search_store(store, query)
    match = _best_match(product, hits)
    if not match:
        return None
    return {
        "url": match.get("url") or None,
        "image": match.get("image") or None,
    }
