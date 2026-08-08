"""Direct storefront price search — the free "Basic" mode.

Ported from the CartWise POC (price-engine/storefront.py) which live-verified
(2026-08) that Woolworths / Coles / Aldi public product-search APIs reply to
browser-origin GETs. These are unofficial APIs — they can change or rate-limit
at any time — so every store call is best-effort: a failing store is simply
skipped (the others still come back and the summary notes what was skipped).

Endpoints:
  - Woolworths: GET https://www.woolworths.com.au/apis/ui/Search/products
  - Coles:      GET https://www.coles.com.au/api/bff/products/search
  - Aldi:       GET https://asl.api.aldi.com.au/commerce/v3/product-search

Returned product dicts use the same canonical shape as the engine's
``_normalize_product`` so rendering/layout is identical to Premium mode.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from . import engine

_USER_AGENT = "PriceSearch/0.1 (grocery price comparison; respectful of retailer terms)"

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-AU,en;q=0.9",
})

_TIMEOUT = (15, 35)
MAX_PER_STORE = 12

# Coles search BFF params (from the POC).
_COLES_STORE_ID = "0584"
_COLES_SUBSCRIPTION_KEY = "eae83861d1cd4de6bb9cd8a2cd6f041e"

# Pinch product API (Advanced mode).
_PINCH_BASE = "https://pinch-app.com/api"
_PINCH_RETAILERS = {"woolworths": "Woolworths", "coles": "Coles", "aldi": "Aldi"}


def _get_json(url: str, params: dict[str, Any], referer: str = "", headers: dict[str, str] | None = None) -> Any:
    merged = dict(headers or {})
    if referer:
        merged["Origin"] = referer
        merged["Referer"] = referer
    response = _session.get(url, params=params, headers=merged or None, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _dollars_to_float(value: Any) -> float | None:
    """Dollar float/string (WW/Coles) -> float dollars."""
    return engine._as_float(value)


def _cents_to_float(value: Any) -> float | None:
    """Minor-units int/string (Aldi) -> float dollars."""
    if value is None:
        return None
    try:
        cents = int(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return round(cents / 100.0, 2) if 0 < cents < 1000000 else None


def _first_image(images: Any) -> str | None:
    if not isinstance(images, list):
        return engine._as_url(images)
    for entry in images:
        if isinstance(entry, dict):
            for key in ("Url", "url", "imageUrl", "src"):
                url = engine._as_url(entry.get(key))
                if url:
                    return url
    return None


# ---------------------------------------------------------------------------
# Per-retailer adapters (ported from the POC, incl. live-verified parsing)
# ---------------------------------------------------------------------------

def _search_woolworths(query: str) -> list[dict[str, Any]]:
    body = _get_json(
        "https://www.woolworths.com.au/apis/ui/Search/products",
        {"searchTerm": query, "pageNumber": "1", "pageSize": str(MAX_PER_STORE)},
        referer="https://www.woolworths.com.au/",
    )
    products: list[dict[str, Any]] = []
    for group in body.get("Products") or []:
        for item in group.get("Products") or []:
            title = str(item.get("DisplayName") or item.get("Name") or "").strip()
            sku = str(item.get("Stockcode") or "").strip()
            if not title or not sku:
                continue
            price = item.get("Price") if item.get("Price") is not None else item.get("InstorePrice")
            products.append({
                "name": title,
                "brand": "",
                "pack_size": str(item.get("CupMeasure") or item.get("PackageSize") or "").strip(),
                "price": _dollars_to_float(price),
                "currency": "AUD",
                "url": f"https://www.woolworths.com.au/shop/productdetails/{sku}",
                "image_url": _first_image(item.get("Images")),
                "is_cheapest": False,
            })
    return products


def _search_coles(query: str) -> list[dict[str, Any]]:
    body = _get_json(
        "https://www.coles.com.au/api/bff/products/search",
        {
            "storeId": _COLES_STORE_ID,
            "searchTerm": query,
            "start": "0",
            "sortBy": "salesDescending",
            "excludeAds": "true",
            "authenticated": "false",
            "subscription-key": _COLES_SUBSCRIPTION_KEY,
        },
        referer="https://www.coles.com.au/",
    )
    products: list[dict[str, Any]] = []
    for item in body.get("results") or []:
        title = str(item.get("name") or "").strip()
        sku = str(item.get("id") or item.get("sku") or "").strip()
        if not title or not sku:
            continue
        pricing = item.get("pricing") or {}
        price = pricing.get("now") if pricing.get("now") is not None else pricing.get("was")
        slug = _slug(title)
        products.append({
            "name": title,
            "brand": "",
            "pack_size": str(item.get("size") or "").strip(),
            "price": _dollars_to_float(price),
            "currency": "AUD",
            "url": f"https://www.coles.com.au/product/{slug}-{sku}",
            "image_url": _first_image(item.get("images") or item.get("image")),
            "is_cheapest": False,
        })
    return products


def _search_aldi(query: str) -> list[dict[str, Any]]:
    body = _get_json(
        "https://asl.api.aldi.com.au/commerce/v3/product-search",
        {"q": query, "limit": str(MAX_PER_STORE), "offset": "0", "sort": "relevance"},
        referer="https://www.aldi.com.au/",
    )
    products: list[dict[str, Any]] = []
    for item in body.get("data") or []:
        title = str(item.get("name") or item.get("productName") or "").strip()
        sku = str(item.get("sku") or "").strip()
        if not title or not sku:
            continue
        pricing = item.get("price") or {}
        price = pricing.get("amountRelevant") if pricing.get("amountRelevant") is not None else pricing.get("amount")
        slug = str(item.get("urlSlugText") or "").strip()
        products.append({
            "name": title,
            "brand": "",
            "pack_size": str(item.get("sellingSize") or "").strip(),
            "price": _cents_to_float(price),
            "currency": "AUD",
            "url": f"https://www.aldi.com.au/shop/en/products/{slug}" if slug else None,
            "image_url": _first_image(item.get("images") or item.get("image")),
            "is_cheapest": False,
        })
    return products


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.strip().casefold()).strip("-")


def _pinch_key() -> str:
    return engine._env("PINCH_Key") or engine._env("PINCH_API_KEY")


_FROZEN_HINTS = ("frozen", "ice cream", "ice-cream")
_FRESH_TOKENS = {
    "fruit", "fruits", "vegetable", "vegetables", "veg", "produce",
    "meat", "meats", "seafood", "poultry", "dairy", "egg", "eggs",
    "bakery", "deli", "salad", "herb", "herbs",
}


def _pinch_category(item: dict[str, Any]) -> str:
    """Map a Pinch item's category taxonomy to Fresh / Frozen / Shelf."""
    fields = " ".join(
        str(item.get(key) or "")
        for key in ("category", "subcategory", "derived_subcategory", "form", "canonical_type")
    ).casefold()
    product_categories = " ".join(str(item.get("product_categories") or "").lower().split())
    joined = fields + " " + product_categories
    if any(h in joined for h in _FROZEN_HINTS):
        return "Frozen"
    tokens = {tok for tok in joined.split() if tok.isalpha()}
    if tokens & _FRESH_TOKENS:
        return "Fresh"
    return "Shelf"


def _pinch_items(query: str, key: str) -> list[dict[str, Any]]:
    body = _get_json(
        f"{_PINCH_BASE}/products/search",
        {"q": query, "limit": "100"},
        headers={"Authorization": f"Bearer {key}"},
    )
    return body.get("data") or []


def _pinch_extract(items: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for item in items:
        title = str(item.get("name") or "").strip()
        retailer = _PINCH_RETAILERS.get(str(item.get("retailer") or "").casefold())
        if not title or not retailer:
            continue
        if _pinch_category(item) != category:
            continue
        price = _dollars_to_float(item.get("price"))
        if price is None:
            continue
        product_id = str(item.get("id") or "").strip()
        image = engine._as_url(str(item.get("image_url") or "").strip())
        if product_id:
            if retailer == "Woolworths":
                url = f"https://www.woolworths.com.au/shop/productdetails/{product_id}"
            elif retailer == "Coles":
                url = f"https://www.coles.com.au/product/{_slug(title)}-{product_id}"
            else:
                url = engine._as_url(str(item.get("product_url") or "").strip())
        else:
            url = engine._as_url(str(item.get("product_url") or "").strip())
        products.append({
            "name": title,
            "brand": str(item.get("brand") or "").strip(),
            "pack_size": str(item.get("size") or item.get("cup_measure") or "").strip(),
            "store": retailer,
            "category": category,
            "price": price,
            "currency": "AUD",
            "url": url,
            "image_url": image,
            "is_cheapest": False,
        })
    return products


def search_pinch(query: str, category: str = engine.DEFAULT_CATEGORY) -> list[dict[str, Any]]:
    """Search Pinch's grocery catalogue, focused on ``category`` (Fresh/Frozen/Shelf).

    Pinch's search API has no category param, so we map each item's own category
    taxonomy to Fresh/Frozen/Shelf and keep only matching items. The plain query
    is searched first; for Frozen/Shelf, if too few matches come back we retry
    with the category term appended (e.g. "potatoes frozen").
    """
    key = _pinch_key()
    if not key:
        raise RuntimeError("PINCH_Key is not configured in .env")
    products = _pinch_extract(_pinch_items(query, key), category)
    if len(products) < 5 and category.casefold() != "fresh":
        seen = {(p["url"] or p["name"]) for p in products}
        for product in _pinch_extract(_pinch_items(f"{query} {category.casefold()}", key), category):
            if (product["url"] or product["name"]) not in seen:
                seen.add(product["url"] or product["name"])
                products.append(product)
    return products


_HANDLERS = {
    "Woolworths": _search_woolworths,
    "Coles": _search_coles,
    "Aldi": _search_aldi,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_store(store: str, query: str, category: str = engine.DEFAULT_CATEGORY) -> list[dict[str, Any]]:
    """Search one store's frontend API, focused on ``category``.

    The storefront searches are plain keyword searches, so for Frozen we append
    "frozen" ("potatoes frozen") to surface that aisle; Fresh and Shelf search
    the query as-is (retailers don't keyword on "shelf").
    """
    handler = _HANDLERS.get(store)
    if not handler:
        return []
    search_q = f"{query} frozen" if category.casefold() == "frozen" else query
    raw_products = handler(search_q)
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for product in raw_products:
        if not (product["name"] and product["price"] is not None):
            continue
        product["store"] = store
        product["category"] = category
        key = f"{store}|{product['name'].casefold()}|{product['price']}"
        if key in seen:
            continue
        seen.add(key)
        products.append(product)
    return products


def build_result(query: str, stores: list[str], category: str, products: list[dict[str, Any]], errors: list[str], elapsed_sec: float, mode: str = "basic") -> dict[str, Any]:
    """Assemble a canonical result dict from already-fetched products."""
    if mode == "advanced":
        model = "pinch-api"
        summary = "Pinch API results (Advanced mode — free)."
    else:
        model = "store-api"
        summary = "Direct storefront API results (Basic mode — free)."
    if errors:
        summary += " Skipped: " + "; ".join(errors)
    elif not products:
        summary = "No products returned by the API for this query."

    raw = {
        "query": query,
        "query_interpretation": "",
        "stores": stores,
        "currency": "AUD",
        "summary": summary,
        "categories": [{"category": category, "products": products}],
    }
    result = engine.normalize_result(raw, query, stores)
    result["errors"] = errors
    result["usage"] = {
        "model": model,
        "prompt_tokens": 0, "thoughts_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "cost_usd": 0.0,
        "elapsed_sec": round(elapsed_sec, 1),
        "cached": False,
    }
    return result


def search(query: str, stores: list[str], category: str) -> dict[str, Any]:
    """Search all requested stores via their frontend APIs and return the
    canonical result dict (same shape as Premium mode)."""
    query = query.strip()
    errors: list[str] = []
    all_products: list[dict[str, Any]] = []
    start = time.monotonic()
    for store in stores:
        try:
            all_products.extend(search_store(store, query, category))
        except Exception as exc:  # noqa: BLE001 — best-effort per store
            errors.append(f"{store}: {exc}")

    return build_result(query, stores, category, all_products, errors, time.monotonic() - start)


def search_advanced(query: str, stores: list[str], category: str) -> dict[str, Any]:
    """Search Pinch's catalogue (one API call) and return the canonical result
    dict — same shape as Basic, different underlying API."""
    query = query.strip()
    errors: list[str] = []
    start = time.monotonic()
    try:
        products = search_pinch(query, category)
    except Exception as exc:  # noqa: BLE001 — best-effort
        errors.append(f"pinch: {exc}")
        products = []
    allowed = {s.casefold() for s in stores}
    filtered = [p for p in products if p["store"].casefold() in allowed] if allowed else products
    return build_result(query, stores, category, filtered, errors, time.monotonic() - start, mode="advanced")
