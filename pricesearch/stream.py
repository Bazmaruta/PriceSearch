"""Progressive (drip-feed) search — yields results as they become available.

Each yielded value is a dict with a ``type`` key consumed by the SSE endpoint:

  - ``start``   query/stores/mode/category, sent immediately so the page paints fast
  - ``items``   the category's products (``products`` carry a stable ``pid``).
                In Basic mode one event per store streams in as each store's
                API responds; in Premium mode a single event arrives when the
                one Gemini call completes
  - ``enrich``  one product's resolved URL / thumbnail ({pid, url, image}),
                streamed as background enrichment finishes
  - ``finish``  the final canonical result (summary/interpretation/usage) once
                everything is done; also what a cached search returns
  - ``error``   a fatal failure

The client accumulates products into its own state by ``pid`` and computes the
cheapest-per-category + overall-cheapest locally, so there is no mid-stream
"authoritative snapshot" to reconcile. Cached searches short-circuit to
``start`` + ``finish`` immediately.

Two backends, chosen by ``mode``:
  - "premium" (default): one Gemini call (all stores × one category), grounded
    by Google Search.
  - "basic": each store's own frontend JSON API — free, best-effort.
"""

from __future__ import annotations

import queue
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Any, Iterator

from . import engine, enrich as enrich_module

VALID_CATEGORIES = ["Fresh", "Frozen", "Shelf"]


def _product_pid(product: dict[str, Any]) -> str:
    return f"{product['store']}|{product['name'].casefold()}|{product['price']}"


def _cached_usage(model: str) -> dict[str, Any]:
    return {
        "model": model, "prompt_tokens": 0, "thoughts_tokens": 0,
        "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
        "elapsed_sec": 0.0, "cached": True,
    }


def _stream_basic(query: str, stores: list[str], category: str, cache_path: Any) -> Iterator[dict[str, Any]]:
    """Basic mode: hit each store's frontend API, streaming per-store items."""
    from . import stores as stores_module

    errors: list[str] = []
    all_products: list[dict[str, Any]] = []
    start = time.monotonic()
    for store in stores:
        try:
            products = stores_module.search_store(store, query, category)
            for product in products:
                product["pid"] = _product_pid(product)
            all_products.extend(products)
            yield {"type": "items", "store": store, "category": category,
                   "products": products, "failed": False}
        except Exception as exc:  # noqa: BLE001 — best-effort per store
            errors.append(f"{store}: {exc}")
            yield {"type": "items", "store": store, "category": category,
                   "products": [], "failed": True}

    final = stores_module.build_result(query, stores, category, all_products, errors, time.monotonic() - start)
    if not final.get("errors"):
        engine._cache_save(cache_path, final)
    yield {"type": "finish", "result": final, "cached": False}


def _stream_advanced(query: str, stores: list[str], category: str, cache_path: Any) -> Iterator[dict[str, Any]]:
    """Advanced mode: one Pinch API call, streaming per-store items."""
    from . import stores as stores_module

    errors: list[str] = []
    start = time.monotonic()
    try:
        products = stores_module.search_pinch(query, category)
    except Exception as exc:  # noqa: BLE001 — best-effort
        errors.append(f"pinch: {exc}")
        products = []
    allowed = {s.casefold() for s in stores}
    filtered = [p for p in products if p["store"].casefold() in allowed] if allowed else products

    by_store: dict[str, list[dict[str, Any]]] = {}
    for product in filtered:
        product["pid"] = _product_pid(product)
        by_store.setdefault(product["store"], []).append(product)
    for store in stores:
        yield {"type": "items", "store": store, "category": category,
               "products": by_store.get(store, []), "failed": False}

    final = stores_module.build_result(query, stores, category, filtered, errors,
                                       time.monotonic() - start, mode="advanced")
    if not final.get("errors"):
        engine._cache_save(cache_path, final)
    yield {"type": "finish", "result": final, "cached": False}


def _stream_premium(query: str, stores: list[str], category: str, model: str, cache_path: Any) -> Iterator[dict[str, Any]]:
    """Premium mode: one Gemini call, then stream thumbnail enrichment."""
    raw, usage = engine.call_gemini(query, stores, model, category)
    final = engine.normalize_result(raw, query, stores)
    final["categories"] = [b for b in final["categories"] if b["category"] == category]
    products = [p for block in final["categories"] for p in block["products"]]
    for product in products:
        product["pid"] = _product_pid(product)

    yield {"type": "items", "store": "", "category": category, "products": products, "failed": False}

    def run_enrich(product: dict[str, Any]) -> None:
        try:
            enrich_module.enrich_product(product)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    if products:
        with ThreadPoolExecutor(max_workers=min(8, len(products))) as executor:
            futures = {executor.submit(run_enrich, p): p for p in products}
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.15, return_when=FIRST_COMPLETED)
                for future in done:
                    product = futures[future]
                    if product.get("url") or product.get("image_url"):
                        yield {"type": "enrich", "pid": product["pid"],
                               "url": product.get("url") or None,
                               "image_url": product.get("image_url") or None}

    final["usage"] = usage
    if not any(p["price"] is not None for block in final["categories"] for p in block["products"]):
        yield {"type": "error", "message": "Gemini returned no priced products. Grounding may have failed."}
        return
    engine._cache_save(cache_path, final)
    yield {"type": "finish", "result": final, "cached": False}


def search_stream(query: str, stores: list[str] | None = None, model: str | None = None, category: str | None = None, mode: str | None = None) -> Iterator[dict[str, Any]]:
    """Drip-feed a price search as a sequence of event dicts.

    ``mode`` selects the backend: "premium" (Gemini, default) or "basic"
    (store frontend JSON APIs). ``category`` narrows to a single category
    (Fresh/Frozen/Shelf); when omitted, the engine default (Fresh) is used.
    """
    engine.load_env()
    query = query.strip()
    if not query:
        yield {"type": "error", "message": "Query must not be empty."}
        return

    stores = engine.normalize_stores(stores)
    mode = (mode or "premium").strip().casefold()
    if mode not in ("premium", "basic", "advanced"):
        yield {"type": "error", "message": f"Unknown mode {mode!r}. Choose 'premium', 'basic' or 'advanced'."}
        return

    categories = [engine.DEFAULT_CATEGORY] if not category else [c for c in VALID_CATEGORIES if c.casefold() == category.strip().casefold()]
    if not categories:
        yield {"type": "error", "message": f"Unknown category {category!r}. Choose one of: Fresh, Frozen, Shelf."}
        return
    category = categories[0]
    model = engine.DEFAULT_MODEL

    cache_path = engine._cache_path(query, stores, model if mode == "premium" else "pinch-api" if mode == "advanced" else "store-api", category, mode)
    cached = engine._cache_load(cache_path)
    usage_model = model if mode == "premium" else "pinch-api" if mode == "advanced" else "store-api"
    if cached is not None:
        cached.setdefault("usage", _cached_usage(usage_model))
        yield {"type": "start", "query": query, "stores": stores, "model": usage_model, "cached": True,
               "mode": mode, "category": category, "tasks": len(stores)}
        yield {"type": "finish", "result": cached, "cached": True}
        return

    yield {"type": "start", "query": query, "stores": stores, "model": usage_model, "cached": False,
           "mode": mode, "category": category, "tasks": len(stores)}

    if mode == "basic":
        yield from _stream_basic(query, stores, category, cache_path)
    elif mode == "advanced":
        yield from _stream_advanced(query, stores, category, cache_path)
    else:
        yield from _stream_premium(query, stores, category, model, cache_path)
