"""Search engine core — calls Gemini Flash grounded by Google Search.

Flow:
  1. Build the user prompt from the query (+ optional explicit stores).
  2. Call Gemini (model from GEMINI_MODEL, key from GOOGLE_API_KEY) with the
     system prompt and the native ``google_search`` grounding tool.
  3. Parse + validate the returned JSON into a canonical result shape.
  4. Compute cheapest-per-category and overall-cheapest in Python (authoritative).
  5. Cache results per query for 24h so repeat searches are instant and free.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .prompt import build_system_prompt

DEFAULT_STORES = ["Woolworths", "Coles", "Aldi"]
MAX_STORES = 3
DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_CATEGORY = "Fresh"
VALID_CATEGORIES = {"Fresh", "Frozen", "Shelf"}
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
CACHE_TTL_SECONDS = 24 * 60 * 60

_lock = threading.Lock()

MOCK_RESULTS: dict[str, list[dict[str, Any]]] = {}


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def load_env() -> None:
    """Make the repo-root .env authoritative.

    Overrides any pre-existing machine/user env values (e.g. a stale
    GEMINI_MODEL or GEMINI_API_KEY on the host) so the project's keys win.
    GEMINI_API_KEY is removed when the .env does not define it — the engine
    intentionally uses GOOGLE_API_KEY so the SDK never falls back elsewhere.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
    os.environ.update(values)
    if "GEMINI_API_KEY" not in values:
        os.environ.pop("GEMINI_API_KEY", None)


def normalize_stores(stores: list[str] | None) -> list[str]:
    """Normalize user-provided store names (max ``MAX_STORES``).

    Known aliases map to their canonical name; any other name is kept as-is
    (title-cased) so Premium mode can search custom stores like "Costco" or
    "Harris Farm" via Google Search grounding. Basic mode's storefront adapters
    only cover the known three, and gracefully skip anything else.
    """
    if not stores:
        return list(DEFAULT_STORES)
    aliases = {
        "ww": "Woolworths", "woolworth": "Woolworths", "woolworths": "Woolworths",
        "coles": "Coles", "coless": "Coles",
        "aldi": "Aldi", "aldis": "Aldi",
    }
    out: list[str] = []
    for s in stores:
        key = s.strip().casefold()
        canonical = aliases.get(key) or s.strip().title()
        if canonical and canonical not in out:
            out.append(canonical)
    return (out or list(DEFAULT_STORES))[:MAX_STORES]


# ---------------------------------------------------------------------------
# JSON parsing / validation
# ---------------------------------------------------------------------------

def _clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return num if 0 < num < 10000 else None


def _as_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return None


_PLACEHOLDER_URL_RE = re.compile(r"(?:123456|example\.com|placeholder|xxxxxxx|/product(?:details)?/12345\b)", re.I)
_REPEATED_DIGIT_RE = re.compile(r"/\d*(\d)\1{3,}\d*/")


def _real_url(value: Any) -> str | None:
    """Return the URL only if it looks like a real product page (rejects the
    placeholder URLs Gemini invents when grounding lacks an exact link)."""
    url = _as_url(value)
    if not url:
        return None
    if _PLACEHOLDER_URL_RE.search(url) or _REPEATED_DIGIT_RE.search(url):
        return None
    return url


def _category(value: Any) -> str:
    if isinstance(value, str):
        for cat in ("fresh", "frozen", "shelf"):
            if cat in value.casefold():
                return cat.title()
    return "Shelf"


def _normalize_product(raw: dict[str, Any]) -> dict[str, Any]:
    price = _as_float(raw.get("price"))
    url = _real_url(raw.get("url"))
    return {
        "name": str(raw.get("name") or "").strip(),
        "brand": str(raw.get("brand") or "").strip(),
        "pack_size": str(raw.get("pack_size") or "").strip(),
        "store": str(raw.get("store") or "").strip(),
        "category": _category(raw.get("category")),
        "price": price,
        "currency": "AUD",
        "url": url,
        "image_url": _as_url(raw.get("image_url")),
        "is_cheapest": False,
    }


def normalize_result(raw: Any, query: str, stores: list[str]) -> dict[str, Any]:
    """Validate/repair the Gemini output into a canonical, render-ready shape."""
    if not isinstance(raw, dict):
        raise ValueError("Gemini did not return a JSON object.")

    categories: dict[str, list[dict[str, Any]]] = {}
    seen_urls: set[str] = set()
    for cat_raw in raw.get("categories") or []:
        if not isinstance(cat_raw, dict):
            continue
        cat_name = _category(cat_raw.get("category"))
        products: list[dict[str, Any]] = []
        for prod_raw in cat_raw.get("products") or []:
            if not isinstance(prod_raw, dict):
                continue
            product = _normalize_product(prod_raw)
            if not (product["name"] and product["price"] is not None):
                continue
            canon = _match_store(product["store"], stores)
            if canon is None:
                continue
            if product["url"]:
                if product["url"] in seen_urls:
                    continue
                seen_urls.add(product["url"])
            product["store"] = canon
            products.append(product)
        categories.setdefault(cat_name, []).extend(products)

    category_blocks: list[dict[str, Any]] = []
    for cat_name, products in categories.items():
        cheapest = min((p for p in products if p["price"] is not None), key=lambda p: p["price"], default=None)
        for product in products:
            if cheapest and product is cheapest:
                product["is_cheapest"] = True
        category_blocks.append({"category": cat_name, "products": products})

    all_products = [p for block in category_blocks for p in block["products"]]
    overall = min((p for p in all_products if p["price"] is not None), key=lambda p: p["price"], default=None)
    if overall:
        overall = {k: overall[k] for k in ("name", "store", "category", "price", "currency", "url", "image_url")}

    summary = str(raw.get("summary") or "").strip()
    if not summary and overall:
        summary = f"Cheapest overall: {overall['name']} at {overall['store']} for ${overall['price']:.2f}."

    return {
        "query": query,
        "query_interpretation": str(raw.get("query_interpretation") or "").strip(),
        "stores": stores,
        "currency": "AUD",
        "summary": summary,
        "categories": category_blocks,
        "overall_cheapest": overall,
    }


def _canonical_store(store: str, allowed: list[str]) -> str:
    key = store.strip().casefold()
    for candidate in allowed:
        if key == candidate.casefold():
            return candidate
    if key in {"ww", "woolworth"}:
        return "Woolworths"
    if key.startswith("coles"):
        return "Coles"
    if key.startswith("aldi"):
        return "Aldi"
    return store.strip()


def _match_store(store: str, stores: list[str]) -> str | None:
    """Return the configured store a Gemini product store maps to.

    Uses exact, substring, then token-subset matching so real-world variations
    still count — e.g. a configured "Harris Farm" accepts "Harris Farm Markets"
    or "harris farm marketplace". Returns None when nothing matches.
    """
    ps = store.strip().casefold()
    for candidate in stores:
        ac = candidate.casefold()
        if ps == ac or ac in ps or ps in ac:
            return candidate
    pt = set(re.findall(r"[a-z0-9]+", ps))
    if pt:
        for candidate in stores:
            at = set(re.findall(r"[a-z0-9]+", candidate.casefold()))
            if at and (at <= pt or pt <= at):
                return candidate
    return None


# ---------------------------------------------------------------------------
# Gemini transport
# ---------------------------------------------------------------------------

def _gemini_client():
    from google import genai

    api_key = _env("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing from .env")
    return genai.Client(api_key=api_key)


def _extract_usage(response: Any, model: str, elapsed_sec: float) -> dict[str, Any]:
    """Pull token counts from the Gemini response.usage_metadata."""
    from . import pricing

    um = getattr(response, "usage_metadata", None)
    prompt = output = thoughts = total = 0
    if um is not None:
        prompt = int(getattr(um, "prompt_token_count", None) or 0)
        output = int(getattr(um, "candidates_token_count", None) or 0)
        thoughts = int(getattr(um, "thoughts_token_count", None) or 0)
        total = int(getattr(um, "total_token_count", None) or (prompt + output + thoughts))
    cost = pricing.estimate_cost(model, prompt + thoughts, output)
    return {
        "model": model,
        "prompt_tokens": prompt,
        "thoughts_tokens": thoughts,
        "output_tokens": output,
        "total_tokens": total,
        "cost_usd": round(cost, 6),
        "elapsed_sec": round(elapsed_sec, 1),
        "cached": False,
    }


def call_gemini(query: str, stores: list[str], model: str | None = None, category: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """One grounded Gemini call covering all stores × a single category.

    A single call means the system prompt is sent once instead of once per
    store, which saves the duplicated prompt tokens (input is cheaper than
    output but still worth trimming). Returns (JSON, usage).
    """
    from google.genai import types

    model = DEFAULT_MODEL
    category = category or DEFAULT_CATEGORY

    user_prompt = (
        f"Search prices for: {query!r}\n\n"
        f"Stores to search (use ONLY these): {', '.join(stores)}\n\n"
        f"Category to search (ONLY this): {category}\n\n"
        f"Remember: every price, URL and image must come from your Google "
        f"Search grounding. Return only the JSON."
    )

    client = _gemini_client()
    start = time.monotonic()
    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(stores, category),
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    elapsed = time.monotonic() - start
    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    return json.loads(_clean_json(response.text)), _extract_usage(response, model, elapsed)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(query: str, stores: list[str], model: str | None = None, category: str | None = None, mode: str | None = None) -> Path:
    key = f"{query.strip().casefold()}|{','.join(stores).casefold()}|{model or ''}|{mode or ''}|{category or ''}"
    safe = re.sub(r"[^a-z0-9_,.-]+", "_", key)
    return CACHE_DIR / f"search_{safe}.json"


def _cache_load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        import time

        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_save(path: Path, result: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(query: str, stores: list[str] | None = None, use_cache: bool = True, enrich: bool = True, model: str | None = None, category: str | None = None, mode: str | None = None) -> dict[str, Any]:
    """Run a full price search. Returns the canonical result dict (renderable).

    ``mode`` selects the backend: "premium" (Gemini + Google Search grounding,
    the default) or "basic" (each store's own frontend JSON API — free).
    """
    load_env()
    query = query.strip()
    if not query:
        raise ValueError("Query must not be empty.")

    stores = normalize_stores(stores)
    model = DEFAULT_MODEL
    category = category or DEFAULT_CATEGORY
    mode = (mode or "premium").strip().casefold()
    if mode not in ("premium", "basic", "advanced"):
        raise ValueError("Unknown mode {!r}. Choose 'premium', 'basic' or 'advanced'.".format(mode))
    cache_path = _cache_path(query, stores, model if mode == "premium" else "pinch-api" if mode == "advanced" else "store-api", category, mode)

    if use_cache:
        with _lock:
            cached = _cache_load(cache_path)
        if cached is not None:
            cached.setdefault("usage", {"model": model, "prompt_tokens": 0, "thoughts_tokens": 0,
                                        "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
                                        "elapsed_sec": 0.0, "cached": True})
            return cached

    if mode in ("basic", "advanced"):
        from . import stores as stores_module

        if mode == "advanced":
            result = stores_module.search_advanced(query, stores, category)
        else:
            result = stores_module.search(query, stores, category)
    else:
        raw, usage = call_gemini(query, stores, model, category)
        result = normalize_result(raw, query, stores)
        result["usage"] = usage

        if not any(p["price"] is not None for block in result["categories"] for p in block["products"]):
            raise RuntimeError(
                "Gemini returned no priced products. Grounding may have failed — "
                "check GEMINI_MODEL supports the google_search tool (e.g. gemini-3.1-flash-lite)."
            )

        if enrich:
            from . import enrich as enrich_module

            result = enrich_module.enrich_result(result)

    if use_cache and not result.get("errors"):
        with _lock:
            _cache_save(cache_path, result)
    return result


# ---------------------------------------------------------------------------
# Mock mode (for demos / HTML-only testing without the API)
# ---------------------------------------------------------------------------

def _mock_product(name: str, brand: str, pack: str, store: str, category: str, price: float, url: str, image: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "brand": brand,
        "pack_size": pack,
        "store": store,
        "category": category,
        "price": price,
        "currency": "AUD",
        "url": url,
        "image_url": image,
        "is_cheapest": False,
    }


def _build_mock(query: str, stores: list[str]) -> dict[str, Any]:
    blocks: dict[str, list[dict[str, Any]]] = {"Fresh": [], "Frozen": [], "Shelf": []}
    seed = {"potato": "Potato", "milk": "Milk", "bread": "Bread"}.get(query.strip().casefold(), query.strip().title())
    for store, price, img in [
        ("Woolworths", 3.50, "https://assets.woolworths.com.au/images/2010/600x600/123456.jpg"),
        ("Coles", 3.90, "https://cdn0.coles.com.au/media/productimage/123456.jpg"),
        ("Aldi", 2.99, "https://www.aldi.com.au/media/product/123456.jpg"),
    ]:
        blocks["Fresh"].append(_mock_product(f"{store} {seed} 2kg Bag", store, "2kg bag", store, "Fresh", price, f"https://www.example.com/{store.lower()}/product", img))
    blocks["Frozen"].append(_mock_product("Frozen French Fries 1kg", "FrozenChoice", "1kg", "Coles", "Frozen", 4.50, "https://www.example.com/coles/fries", "https://cdn0.coles.com.au/media/productimage/fries.jpg"))
    blocks["Shelf"].append(_mock_product("Canned Potatoes 425g", "BranWell", "425g", "Woolworths", "Shelf", 1.95, "https://www.example.com/ww/canned", "https://assets.woolworths.com.au/images/2010/600x600/canned.jpg"))
    return {"Fresh": blocks["Fresh"], "Frozen": blocks["Frozen"], "Shelf": blocks["Shelf"]}


def search_mock(query: str, stores: list[str] | None = None, model: str | None = None, category: str | None = None) -> dict[str, Any]:
    """Deterministic mock search — no API. Useful for testing the HTML renderer."""
    query = query.strip() or "potatoes"
    stores = normalize_stores(stores)
    category = category or DEFAULT_CATEGORY
    raw_blocks = _build_mock(query, stores)
    raw = {
        "query": query,
        "query_interpretation": f"Shopper wants {query} in any form, across stores.",
        "summary": "Mock demo results (no API call). Cheapest overall highlighted.",
        "categories": [
            {"category": cat, "products": products}
            for cat, products in raw_blocks.items()
            if cat == category
        ],
    }
    result = normalize_result(raw, query, stores)
    result["usage"] = {
        "model": DEFAULT_MODEL,
        "prompt_tokens": 0, "thoughts_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "cost_usd": 0.0, "elapsed_sec": 0.0, "cached": False,
    }
    return result
