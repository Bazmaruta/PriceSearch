"""Reuse already-paid Bright Data output to finish batch 9 without new triggers.

black/white seedless grapes were stuck polling the 10:52 dataset snapshots.
We pull from existing ready snapshot/collection IDs and re-run the matcher.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import ai_confirm
import bd_store_search as b
import db
import run_canonical_batch as rcb

LOG_FILE = Path(__file__).parent / "logs" / "run_canonical_batch.log"
logger = logging.getLogger("rcb")


def setup():
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if not logger.handlers:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)


def fetch_product(cid):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT canonical_id, canonical_name, brand, product_name, size_value, size_unit, size_basis, pack_count, variant "
            "FROM canonical WHERE canonical_id = %s AND country_code = 'AU'",
            (cid,),
        ).fetchone()
    return dict(row)


def run_from_existing(canonical, api_key, snapshot_id, collection_id, confirm_fn):
    cid = canonical["canonical_id"]
    items = b.build_urls(canonical["canonical_name"])
    ds_items = [it for it in items if "base" not in it]
    dca_items = [it for it in items if "base" in it]

    store_products = {}
    if snapshot_id:
        records = rcb.with_retry(b.download, api_key, snapshot_id)
        url_to_store = {it["url"]: it["store"] for it in ds_items}
        for rec in records:
            url = (rec.get("input") or {}).get("url") or (rec.get("input") or {}).get("url")
            store = url_to_store.get(url) or url
            store_products[store] = b.extract_products(store, rec.get("markdown") or "")
        logger.info("reuse %s: downloaded dataset %s (%d records)", cid, snapshot_id, len(records))
    if collection_id:
        rows = rcb.with_retry(b.download_dca, api_key, collection_id)
        store = dca_items[0]["store"] if dca_items else "Harris Farm"
        store_products[store] = [b.dca_record_to_product(r) for r in rows]
        logger.info("reuse %s: downloaded DCA %s (%d rows)", cid, collection_id, len(rows))

    counts = {}
    matched = {}
    for it in items:
        prods = store_products.get(it["store"]) or []
        counts[it["store"]] = len(prods)
        best, best_score, decision = rcb.pick_best(canonical, prods, it["store"], rcb.MIN_MATCH_SCORE, confirm_fn)
        matched[it["store"]] = best is not None
        db.save_result(
            cid,
            canonical["canonical_name"],
            rcb.COUNTRY,
            it["store"],
            {
                "price": best.get("price") if best and best.get("price") is not None else None,
                "currency": "AUD",
                "url": best.get("url") if best else None,
                "thumbnail": best.get("image_url") if best else None,
                "name": best.get("name") if best else None,
                "match_source": decision,
            },
        )
        if best and best.get("price") is not None:
            logger.debug("reuse %s: saved %s best=%s price=%s score=%.1f decision=%s", cid, it["store"], best.get("name"), best.get("price"), best_score, decision)
        else:
            logger.debug("reuse %s: %s no match (%s) -> empty row (best_score=%s, candidates=%d)", cid, it["store"], decision, best_score, len(prods))

    n_with = sum(1 for s in rcb.STORE_ORDER if matched.get(s))
    status = "ok" if n_with == len(rcb.STORE_ORDER) else ("partial" if n_with else "error")
    db.mark_scraped(cid, rcb.COUNTRY, status)
    logger.info("reuse %s: done status=%s counts=%s", cid, status, ", ".join(f"{s}={counts.get(s, 0)}" for s in rcb.STORE_ORDER))
    return status


def main():
    setup()
    api_key = rcb.load_api_key()
    if not api_key:
        raise SystemExit("no api key")
    db.init_schema()
    confirm_fn = ai_confirm.confirm_match
    logger.info("=== reuse_batch9 start (no new Bright Data triggers) ===")

    jobs = [
        ("black seedless grapes", "sd_mswirp931t9r4odzq9", "j_mswirq29deyvlivxz"),
        ("white seedless grapes", "sd_mswic7nr1vaqwntvji", "j_mswirq2s197snsksbo"),
    ]
    for cid, sid, cid_ in jobs:
        product = fetch_product(cid)
        run_from_existing(product, api_key, sid, cid_, confirm_fn)
    logger.info("=== reuse_batch9 done ===")


if __name__ == "__main__":
    main()
