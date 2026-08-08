"""Ad-hoc latency baseline harness — measures each phase of a cold search.

Usage:
    python bench.py "butter" [--model gemini-3.1-flash-lite] [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-enrich", action="store_true")
    args = parser.parse_args()

    from pricesearch import engine, enrich

    engine.load_env()
    model = args.model or engine._env("GEMINI_MODEL") or engine.DEFAULT_MODEL
    stores = engine.normalize_stores(None)

    print(f"query={args.query!r} model={model} stores={stores} enrich={not args.no_enrich}")
    print(f"search cache dir: {engine.CACHE_DIR}")

    phases: dict[str, float] = {}

    t0 = time.monotonic()
    raw, usage = engine.call_gemini(args.query, stores, model)
    phases["model_call_sec"] = round(time.monotonic() - t0, 1)
    print(f"model call:       {phases['model_call_sec']:>8.1f}s  (usage: {usage['total_tokens']} tok, {usage['thoughts_tokens']} thought)")

    t0 = time.monotonic()
    result = engine.normalize_result(raw, args.query, stores)
    phases["normalize_sec"] = round(time.monotonic() - t0, 3)
    n_products = sum(len(b["products"]) for b in result["categories"])
    print(f"normalize:        {phases['normalize_sec']:>8.3f}s  ({n_products} products)")

    if not args.no_enrich:
        t0 = time.monotonic()
        result = enrich.enrich_result(result)
        phases["enrich_sec"] = round(time.monotonic() - t0, 1)
        print(f"enrich:           {phases['enrich_sec']:>8.1f}s")

    phases["total_sec"] = round(sum(phases.values()), 1)
    print(f"total:            {phases['total_sec']:>8.1f}s")

    report = {
        "query": args.query,
        "model": model,
        "stores": stores,
        "phases": phases,
        "n_products": n_products,
        "usage": usage,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
