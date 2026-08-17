import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from canonical import canonicalize

CANONICAL_FILE = Path(__file__).parent.parent / "data" / "canonical_regression_203.json"

# fields the price matcher needs populated so it can gate on brand/variant/size
ENRICH_FIELDS = ("brand", "product_name", "variant", "size_value", "size_unit", "size_basis", "pack_count")


def seed():
    data = json.loads(CANONICAL_FILE.read_text(encoding="utf-8"))
    db.init_schema()
    n = 0
    for item in data:
        cid = item["canonical_name"].lower()
        db.upsert_canonical(
            cid,
            item["canonical_name"],
            country=db.COUNTRY,
            brand=item.get("brand"),
            product_name=item.get("product_name"),
            category=item.get("category"),
            subcategory=item.get("subcategory"),
            size_value=item.get("size_value"),
            size_unit=item.get("size_unit"),
            size_basis=item.get("size_basis"),
            pack_count=item.get("pack_count"),
            variant=item.get("variant"),
            barcode=item.get("barcode"),
        )
        n += 1
    print(f"seeded {n} canonical products")


def enrich(dry_run=False):
    """Backfill attributes the price matcher needs (brand/product_name/size/variant)
    for canonical rows that are missing them.

    Uses the canonical engine (AI, Gemini via ``canonical.canonicalize``) on the
    product's canonical_name, so this is systematic and re-runnable (the engine
    caches results for 24h). Only missing fields are written — existing good
    attributes are never overwritten. Products with no meaningful brand (loose
    produce) keep a null brand because the engine reports none.
    """
    db.init_schema()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT canonical_id, canonical_name, brand, product_name, "
            "size_value, size_unit, size_basis, variant FROM canonical "
            "WHERE country_code = %s AND "
            "(brand IS NULL OR product_name IS NULL OR size_value IS NULL) "
            "ORDER BY canonical_id",
            (db.COUNTRY,),
        ).fetchall()

    if not rows:
        print("no products missing attributes")
        return

    print(f"{len(rows)} product(s) missing attributes; canonicalising...")
    updated = 0
    for r in rows:
        name = (r["canonical_name"] or "").strip()
        if not name:
            continue
        res, _ = canonicalize(name)
        changes = {}
        if not r["brand"] and res.get("brand"):
            changes["brand"] = res["brand"]
        if not r["product_name"] and res.get("product_name"):
            changes["product_name"] = res["product_name"]
        if not r["variant"] and res.get("variant"):
            changes["variant"] = res["variant"]
        if r["size_value"] is None and res.get("size_value") is not None:
            changes["size_value"] = res["size_value"]
            changes["size_unit"] = res.get("size_unit")
            changes["size_basis"] = res.get("size_basis")
        if changes:
            print(f"  {r['canonical_id'][:45]:<47} {changes}")
            if not dry_run:
                db.upsert_canonical(r["canonical_id"], name, country=db.COUNTRY, **changes)
            updated += 1
    print(f"{'would update' if dry_run else 'enriched'} {updated} product(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed / enrich the canonical product table")
    ap.add_argument("--seed", action="store_true", help="seed from the regression JSON (default: seed)")
    ap.add_argument("--enrich", action="store_true", help="backfill missing brand/product_name/size via the AI engine")
    ap.add_argument("--dry-run", action="store_true", help="with --enrich: show what would change without writing")
    args = ap.parse_args()

    if args.enrich:
        enrich(dry_run=args.dry_run)
    else:
        seed()
        if not args.dry_run:
            print("run with --enrich to backfill missing brand/product_name/size")
