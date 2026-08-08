"""Product enrichment — resolve links and fetch thumbnails.

After Gemini returns the product list, each product URL is a Google
grounding-api-redirect link (or already a store URL). We:
  1. Resolve it to the final store product page (follow redirects).
  2. Extract a thumbnail from the page's og:image / JSON-LD / meta tags.
  3. Cache every lookup in data/cache/urls.json (7-day TTL) so re-renders are
     free and we never re-fetch the same page.

This is best-effort: if a page can't be fetched or has no image, the product
keeps its original URL and image stays None (the renderer shows a placeholder).
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
_URL_CACHE_PATH = CACHE_DIR / "urls.json"
_URL_TTL_SECONDS = 7 * 24 * 60 * 60
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}
_LOCK = threading.Lock()

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image|thumbnail)[^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_JSONLD_IMAGE_RE = re.compile(r'"(?:image|thumbnailUrl)"\s*:\s*"([^"]+)"', re.I)
_GOOGLE_IMG_RE = re.compile(r'^https?://(?:[a-z]+\.)?googleusercontent\.com/')

_session = requests.Session()
_session.headers.update(_HEADERS)


def _load_cache() -> dict[str, dict[str, Any]]:
    if not _URL_CACHE_PATH.exists():
        return {}
    try:
        if time.time() - _URL_CACHE_PATH.stat().st_mtime > _URL_TTL_SECONDS:
            return {}
        return json.loads(_URL_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _URL_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _pick_image(html: str) -> str | None:
    for pattern in (_OG_IMAGE_RE, _JSONLD_IMAGE_RE):
        match = pattern.search(html)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def enrich_url(url: str) -> dict[str, str | None]:
    """Resolve a URL and find its page thumbnail. Returns {final, image}."""
    cache = _load_cache()
    with _LOCK:
        if url in cache:
            entry = cache[url]
            return {"final": entry.get("final"), "image": entry.get("image")}

    final_url = url
    image: str | None = None
    try:
        response = _session.get(url, timeout=12, allow_redirects=True, stream=True)
        if response.ok:
            response.close()
            final_url = response.url or url
            if final_url.startswith("http"):
                html = _session.get(final_url, timeout=12).text[:500_000]
                image = _pick_image(html)
                if image and _GOOGLE_IMG_RE.match(image):
                    image = None
    except requests.RequestException:
        pass

    entry = {"final": final_url, "image": image}
    with _LOCK:
        cache = _load_cache()
        cache[url] = entry
        _save_cache(cache)
    return entry


def enrich_product(product: dict[str, Any], fetch_images: bool = True) -> dict[str, Any]:
    """Enrich a single product in place: resolve URL + recover missing image."""
    from . import recover as recover_module

    url = product.get("url")
    if url:
        entry = enrich_url(url)
        if entry["final"]:
            product["url"] = entry["final"]
        if fetch_images and entry["image"] and not product.get("image_url"):
            product["image_url"] = entry["image"]

    if not product.get("url") or (fetch_images and not product.get("image_url")):
        recovered = recover_module.recover_product(product)
        if recovered:
            if not product.get("url") and recovered["url"]:
                product["url"] = recovered["url"]
            if fetch_images and not product.get("image_url") and recovered["image"]:
                product["image_url"] = recovered["image"]
    return product


def enrich_result(result: dict[str, Any], fetch_images: bool = True) -> dict[str, Any]:
    """In-place enrichment of every product: clean URL + thumbnail.

    Pass 1: resolve grounding-redirect URLs and scrape og:image for any product
            that already has a valid URL.
    Pass 2: recover url/image from the store's own product-search API for any
            product still missing a URL or a thumbnail (this fixes JS-rendered
            pages that expose no og:image, and Gemini placeholder URLs).

    Products are enriched concurrently — each product's network fetches are
    independent, and the URL/store caches are lock-guarded — which turns the
    per-product serial fan-out (~2 HTTP calls each) into one parallel round.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    products = [p for block in result.get("categories") or [] for p in block.get("products") or []]
    if not products:
        return result

    max_workers = min(8, len(products))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(enrich_product, product, fetch_images) for product in products]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:  # noqa: BLE001 — enrichment is best-effort
                pass
    return result
