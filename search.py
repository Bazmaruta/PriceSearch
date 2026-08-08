"""PriceSearch CLI — search a product and emit an HTML file.

Examples:
    python search.py "potatoes"
    python search.py "potatoes" --stores "coles,aldi" --out output/potatoes.html
    python search.py "potatoes" --mock            # demo data, no API call
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search grocery prices and render an HTML file.")
    parser.add_argument("query", help="product to search, e.g. potatoes")
    parser.add_argument("--stores", help="comma-separated stores (default: Woolworths,Coles,Aldi)")
    parser.add_argument("--model", help="Gemini model (default: GEMINI_MODEL from .env)")
    parser.add_argument("--category", help="category to search: Fresh, Frozen or Shelf (default: Fresh)")
    parser.add_argument("--mode", choices=["premium", "basic", "advanced"], default=None,
                        help="backend: premium=Gemini (default), basic=storefront APIs, advanced=Pinch API")
    parser.add_argument("--out", help="output HTML path (default: output/PriceSearch.html)")
    parser.add_argument("--mock", action="store_true", help="use deterministic demo data (no API)")
    parser.add_argument("--no-cache", action="store_true", help="skip the 24h result cache")
    parser.add_argument("--no-enrich", action="store_true", help="skip URL resolution / thumbnail fetch")
    args = parser.parse_args(argv)

    from pricesearch import engine, render

    stores = [s.strip() for s in args.stores.split(",") if s.strip()] if args.stores else None

    try:
        if args.mock:
            result = engine.search_mock(args.query, stores, model=args.model, category=args.category)
        else:
            result = engine.search(args.query, stores=stores, use_cache=not args.no_cache,
                                   enrich=not args.no_enrich, model=args.model, category=args.category,
                                   mode=args.mode)
    except Exception as exc:  # noqa: BLE001
        print(f"[PriceSearch] error: {exc}", file=sys.stderr)
        return 1

    path = render.render_to_file(result, args.out)
    total = sum(len(c["products"]) for c in result["categories"])
    print(f"[PriceSearch] {total} products for {args.query!r} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
