import argparse
import concurrent.futures
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import ai_confirm
import bd_store_search as b
import db

COUNTRY = "AU"
DEFAULT_BATCH_SIZE = 5
MIN_MATCH_SCORE = 2.0
PRODUCT_TIMEOUT = 600  # per-product cap: snaphot/DCA polls + downloads must finish in this many seconds
STORE_ORDER = ["Woolworths", "Coles", "ALDI", "Harris Farm", "IGA"]

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "run_canonical_batch.log"
logger = logging.getLogger("rcb")


def setup_logging(console_level=logging.INFO, log_file=None):
    log_file = Path(log_file) if log_file else LOG_FILE
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not logger.handlers:
        fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setLevel(console_level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    else:
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler):
                h.setLevel(console_level)
    return logger


def load_api_key():
    return b.load_api_key()


def tokenize_name(text):
    return [
        t
        for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
        if len(t) > 1
    ]


# Common brand/variant abbreviations that should match their full forms.
_ABBREVIATIONS = {
    "antibac": "antibacterial",
    "antibact": "antibacterial",
    "sanit": "sanitiser",
}


def tokens_near(a, b):
    """True when two tokens match, including common plural/inflected forms
    (e.g. banana/bananas, tomato/tomatoes, cookie/cookies)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if min(la, lb) < 4:
        return False
    # abbreviation map (antibac ~ antibacterial)
    if _ABBREVIATIONS.get(a) == b or _ABBREVIATIONS.get(b) == a:
        return True
    if abs(la - lb) > 3:
        return False
    return a.startswith(b) or b.startswith(a)


def token_overlap(a_toks, b_toks):
    """Count of a_toks tokens with a near-match in b_toks (each used once)."""
    b_unused = list(b_toks)
    count = 0
    for ta in a_toks:
        for i, tb in enumerate(b_unused):
            if tokens_near(ta, tb):
                del b_unused[i]
                count += 1
                break
    return count


def parse_size(text):
    """Extract (value, unit) from a product name e.g. '2L' -> (2.0,'l'), '500g' -> (500.0,'g')."""
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ml|l|kg|g|pack|ea)\b", t)
    if m:
        return float(m.group(1)), m.group(2)
    return None


def canonical_size(canonical):
    if canonical.get("size_value") is not None and canonical.get("size_unit"):
        return float(canonical["size_value"]), str(canonical["size_unit"]).lower()
    return parse_size(canonical.get("canonical_name"))


def score_match(canonical, product):
    """Rank a store result against the canonical product. Higher = better match.

    Factors: overlap with canonical tokens (name + product_name + brand),
    brand agreement, and size agreement (strongest signal).
    """
    name = product.get("name") or ""
    toks = tokenize_name(name)
    if not toks:
        return -1e9

    canon_toks = tokenize_name(canonical.get("canonical_name"))
    if canonical.get("product_name"):
        canon_toks += tokenize_name(canonical["product_name"])
    if canonical.get("brand"):
        canon_toks += tokenize_name(canonical["brand"])
    if not canon_toks:
        return 0.0

    overlap = token_overlap(toks, canon_toks)
    unique_canon = set(canon_toks)
    capped = min(overlap, len(unique_canon))
    if overlap > 0 and overlap >= len(unique_canon):
        # every canonical token present -> strong match; extra words shouldn't sink it
        # (e.g. "Fresh Chokoes each" for canonical "Chokoes"), but duplicate tokens
        # in the candidate must not inflate the score past the true token set.
        score = 2.0 * capped + 1.0
    else:
        score = 2.0 * overlap - 0.5 * (len(toks) - overlap)

    brand = canonical.get("brand")
    if brand:
        btoks = set(tokenize_name(brand))
        if btoks & set(toks):
            score += 3.0
        else:
            score -= 2.0

    cs = canonical_size(canonical)
    rs = parse_size(name)
    if cs and rs:
        if abs(cs[0] - rs[0]) < 0.01 and cs[1] == rs[1]:
            score += 5.0
        elif cs[1] == rs[1]:
            score -= 4.0
        else:
            score -= 2.0
    elif cs and not rs:
        score -= 1.0
    return score


def brand_matches(canonical, name):
    """True when the candidate name contains the canonical brand (or brand is unknown).

    Requires MORE THAN HALF of the brand tokens to appear in the candidate, so a
    single generic shared token (e.g. 'dairy' in 'Coach House Dairy' vs
    'Bethune Lane Dairy Milk') can never satisfy the gate. Single-token brands
    (e.g. 'a2', 'Bega') simply require that token.
    """
    brand = canonical.get("brand")
    if not brand:
        return True
    btoks = set(tokenize_name(brand))
    if not btoks:
        return True
    ntoks = set(tokenize_name(name))
    overlap = len(btoks & ntoks)
    if overlap > len(btoks) // 2:
        return True
    # compound-word tolerance: "MyEcoBag" == "My Eco Bag" (token-level overlap is
    # zero because one side is a single concatenated token). Compare the
    # de-spaced lowercase strings for a substring match in either direction.
    def _joined(s):
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    bj, nj = _joined(brand), _joined(name)
    if len(bj) >= 4 and (bj in nj or nj in bj):
        return True
    return False


def missing_descriptor_tokens(canonical, name):
    """Canonical product_name/variant tokens absent from the candidate name
    (near-match aware, so 'banana' matches 'bananas'). Non-empty means the
    candidate may be a different variant (e.g. UHT vs fresh, rye vs white) and
    warrants AI confirmation."""
    must = []
    for field in ("product_name", "variant"):
        if canonical.get(field):
            must += tokenize_name(canonical[field])
    if not must:
        return set()
    must = set(must)  # each descriptor token is required once, regardless of how
    # many fields mention it (e.g. "Compostable" in both product_name and variant)
    cand = tokenize_name(name)
    missing = set()
    unused = list(cand)
    for tok in must:
        matched = False
        for i, ct in enumerate(unused):
            if tokens_near(tok, ct):
                del unused[i]
                matched = True
                break
        if not matched:
            missing.add(tok)
    return missing


def _identity_candidates(canonical, prods):
    """Candidates that are the same product family: brand matches AND every
    product_name/variant descriptor token is present (near-match aware)."""
    out = []
    for p in prods:
        name = p.get("name") or ""
        if brand_matches(canonical, name) and not missing_descriptor_tokens(canonical, name):
            out.append(p)
    return out


def _size_agnostic_score(canonical, product):
    """Score ignoring size: the canonical's size token is dropped from the
    required set and no size penalty is applied, so a same-product candidate at a
    different weight can still clear the threshold."""
    name = product.get("name") or ""
    toks = tokenize_name(name)
    canon_toks = tokenize_name(canonical.get("canonical_name"))
    if canonical.get("product_name"):
        canon_toks += tokenize_name(canonical["product_name"])
    if canonical.get("brand"):
        canon_toks += tokenize_name(canonical["brand"])
    cs = canonical_size(canonical)
    if cs:
        size_tok = f"{cs[0]:g}{cs[1]}".lower()
        canon_toks = [t for t in canon_toks if t != size_tok]
    if not canon_toks:
        return 0.0
    overlap = token_overlap(toks, canon_toks)
    unique_canon = set(canon_toks)
    capped = min(overlap, len(unique_canon))
    if overlap > 0 and overlap >= len(unique_canon):
        return 2.0 * capped + 1.0
    return 2.0 * overlap - 0.5 * (len(toks) - overlap)


_SIZE_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?[a-z]*$")
_GLUED_SIZE_RE = re.compile(r"^(?P<word>[a-z]+)(?P<size>\d+(?:\.\d+)?(?:ml|l|kg|g|pack|pk|ea)?)$")
# Packaging / container / form words are NOT product-kind signals: "Lindt ...
# Chocolate Block" and "Liquid-Plumr ... Gel" are the same products as their
# canonical entries, so the head noun must skip these and find the real noun.
_HEAD_FILLER = {
    "pack", "packs", "pk", "each", "ea", "x", "approx", "loose", "prepack", "pkt", "bunch",
    "block", "bar", "box", "bag", "bottle", "tin", "jar", "tub", "loaf", "roll", "can",
    "packet", "pouch", "carton", "sachet", "gift", "gel", "cream", "loose", "squeeze",
    "pair", "grinder", "refill", "bundle", "multi", "refills", "pairs",
}


def head_token(name, brand_tokens):
    """Last meaningful noun of a product name (strip brand/store, size and filler
    tokens), e.g. 'Woolworths Custard & Pink Lady Apple Scrolls 2 pack' -> 'scrolls'."""
    toks = tokenize_name(name)
    bt = set(brand_tokens or [])
    while toks:
        t = toks[-1]
        if t in bt or t in _HEAD_FILLER or _SIZE_TOKEN_RE.match(t):
            toks.pop()
        elif _GLUED_SIZE_RE.match(t):
            toks[-1] = _GLUED_SIZE_RE.match(t).group("word")
            if toks[-1] in _HEAD_FILLER or not toks[-1]:
                toks.pop()
        else:
            break
    return toks[-1] if toks else None


def head_compatible(canonical, name):
    """True when the candidate's head noun is compatible with the canonical
    product identity (any product_name/variant/canonical descriptor). Catches
    cases where all descriptor tokens appear but the product KIND differs,
    e.g. 'Custard & Pink Lady Apple Scrolls' (pastry) vs canonical 'Custard Apple'."""
    head = head_token(name, tokenize_name(canonical.get("brand")))
    if not head:
        return True
    desc = tokenize_name(canonical.get("canonical_name"))
    if canonical.get("product_name"):
        desc += tokenize_name(canonical["product_name"])
    if canonical.get("variant"):
        desc += tokenize_name(canonical["variant"])
    if canonical.get("brand"):
        desc += tokenize_name(canonical["brand"])
    cs = canonical_size(canonical)
    if cs:
        size_tok = f"{cs[0]:g}{cs[1]}".lower()
        desc = [t for t in desc if t != size_tok]
    if not desc:
        return True
    if any(tokens_near(head, d) for d in desc):
        return True
    # compound-word tolerance: "Chickpeas" head vs "chick"+"peas" descriptors
    # ("Macro Organic Chickpeas 425g" is the same product as "Chick Peas 425g").
    return any(d in head or head in d for d in desc)


def _cached_confirm(canonical, product, store):
    """Return a cached strict-confirm AI decision ('yes'/'no') for this candidate."""
    cid = canonical.get("canonical_id")
    purl = product.get("url")
    if not cid or not purl or purl.startswith("__"):
        return None
    return db.get_match_decision(cid, store, purl, country=COUNTRY, mode="confirm")


def _maybe_ai_head(canonical, best, store, score, decision, confirm_fn):
    """Respect a cached AI verdict, then if the candidate's head noun signals a
    different product kind, ask the AI (strict confirm) before saving.
    Returns (best, score, decision)."""
    cached = _cached_confirm(canonical, best, store)
    if cached == "no":
        return None, score, "ai rejected"
    if not head_compatible(canonical, best.get("name") or ""):
        if confirm_fn is not None:
            confirmed, _ = confirm_fn(canonical, best, store)
            if not confirmed:
                return None, score, "ai rejected"
            return best, score, "ai confirmed"
        return None, score, "ai rejected"
    return best, score, decision


def pick_best(canonical, prods, store, min_score=MIN_MATCH_SCORE, confirm_fn=None):
    """Return (best_product, best_score, decision).

    decision explains the outcome:
      'brand ok'     - saved directly (same product identity, exact size)
      'closest size' - same product but exact size not stocked; closest weight saved
      'ai confirmed' - brand gate failed but AI said it's the same product
      'ai rejected'  - brand gate failed and AI said different product
      'wrong brand'  - brand gate failed and no AI available to confirm
      'low score'    - nothing scored >= min_score
      'no results'   - no candidates at all
    """
    if not prods:
        return None, None, "no results"

    identity = _identity_candidates(canonical, prods)
    cs = canonical_size(canonical)

    if identity and cs:
        same_unit = []
        for p in identity:
            s = parse_size(p.get("name") or "")
            if s and s[1] == cs[1]:
                same_unit.append((p, s))
        eligible = [(p, s, _size_agnostic_score(canonical, p)) for p, s in same_unit
                    if _size_agnostic_score(canonical, p) >= min_score]
        if eligible:
            exact = [(p, s, sc) for p, s, sc in eligible if abs(s[0] - cs[0]) < 0.01]
            if exact:
                best = max(exact, key=lambda x: score_match(canonical, x[0]))[0]
                return _maybe_ai_head(canonical, best, store, score_match(canonical, best), "brand ok", confirm_fn)
            best = min(eligible, key=lambda x: (abs(x[1][0] - cs[0]), -x[2]))[0]
            if confirm_fn is not None:
                confirmed, _ = confirm_fn(canonical, best, store, size_tolerant=True)
                if not confirmed:
                    return None, _size_agnostic_score(canonical, best), "ai rejected"
            best, score, decision = _maybe_ai_head(
                canonical, best, store, _size_agnostic_score(canonical, best), "closest size", confirm_fn
            )
            return best, score, decision

    if identity:
        best = max(identity, key=lambda p: score_match(canonical, p))
        score = score_match(canonical, best)
        if score >= min_score:
            res = _maybe_ai_head(canonical, best, store, score, "brand ok", confirm_fn)
            if res[0] is not None:
                return res
            # identity best rejected by head-guard / cached AI; fall through so a
            # better-scoring non-identity candidate (e.g. the true-size product)
            # can still be found in the doubt loop below.

    # ---- fall back to existing score + AI doubt logic ----
    best = max(prods, key=lambda p: score_match(canonical, p))
    score = score_match(canonical, best)
    if score < min_score:
        return None, score, "low score"
    if confirm_fn is not None:
        # Judge plausible same-product candidates with the AI in score order.
        # A low score here only means the name lacks a canonical token (e.g. a
        # store's "Mint Bunch" for "fresh mint"), so we let the AI decide instead
        # of rejecting the true product on score alone.
        canon_toks = tokenize_name(canonical.get("canonical_name"))
        if canonical.get("product_name"):
            canon_toks += tokenize_name(canonical["product_name"])
        if canonical.get("brand"):
            canon_toks += tokenize_name(canonical["brand"])
        cands = [
            p for p in prods
            if token_overlap(tokenize_name(p.get("name") or ""), canon_toks) > 0
            and (brand_matches(canonical, p.get("name") or "") or p is best)
            and head_compatible(canonical, p.get("name") or "")
        ]
        ranked = sorted(cands, key=lambda p: score_match(canonical, p), reverse=True)
        for p in ranked[:10]:
            confirmed, _ = confirm_fn(canonical, p, store)
            if confirmed:
                return p, score_match(canonical, p), "ai confirmed"
        return None, score, "ai rejected"
    best_name = best.get("name") or ""
    if brand_matches(canonical, best_name):
        return best, score, "brand ok"
    return None, score, "wrong brand"


def with_retry(fn, *args, retries=4, base_delay=3, deadline=None, **kwargs):
    """Retry a network call on transient failures (DNS/connection/timeout).

    When a `deadline` (epoch seconds) is given and exceeded before an attempt,
    raise TimeoutError so the caller can fail fast instead of blocking the batch.
    """
    log = logging.getLogger("rcb")
    last = None
    for attempt in range(1, retries + 1):
        if deadline is not None and time.time() >= deadline:
            raise TimeoutError(f"product deadline reached (deadline exhausted after {attempt-1} attempt(s))")
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            delay = base_delay * attempt
            log.warning(
                "attempt %d/%d failed (%s): %s -- retrying in %ds",
                attempt,
                retries,
                type(e).__name__,
                e,
                delay,
            )
            time.sleep(delay)
    log.error("giving up after %d attempts: %s", retries, last)
    raise last


def run_single_product(product, api_key, save=True, min_score=MIN_MATCH_SCORE, confirm_fn=None, resume=False):
    """Fire the 5-store search for one canonical product and return a result dict.

    Never raises: failures are captured in the returned dict so the batch can
    continue and mark the product accordingly.
    """
    log = logging.getLogger("rcb")
    canonical_id = product["canonical_id"]
    canonical_name = product["canonical_name"]
    canonical = {
        "canonical_id": product.get("canonical_id"),
        "canonical_name": product.get("canonical_name"),
        "brand": product.get("brand"),
        "product_name": product.get("product_name"),
        "size_value": product.get("size_value"),
        "size_unit": product.get("size_unit"),
        "size_basis": product.get("size_basis"),
        "pack_count": product.get("pack_count"),
        "variant": product.get("variant"),
    }
    try:
        log.info("product: %s | building store search urls", canonical_id)
        items = b.build_urls(canonical_name)
        ds_items = [it for it in items if "base" not in it]
        dca_items = [it for it in items if "base" in it]

        snapshot_id = None
        dca_jobs = {}

        # --resume: reuse already-paid jobs for this product instead of re-triggering
        if resume:
            prev = db.get_pending_job(canonical_id)
            if prev and (prev.get("snapshot_id") or prev.get("collection_id")):
                log.info("product: %s | resuming from existing jobs snapshot_id=%s collection_id=%s",
                         canonical_id, prev.get("snapshot_id"), prev.get("collection_id"))
                snapshot_id = prev.get("snapshot_id")
                if prev.get("collection_id"):
                    dca_jobs["Harris Farm"] = prev["collection_id"]

        if snapshot_id is None and ds_items:
            log.info("product: %s | triggering dataset (%d stores)", canonical_id, len(ds_items))
            body = with_retry(b.trigger, api_key, ds_items)
            snapshot_id = body.get("snapshot_id")
            if not snapshot_id:
                raise RuntimeError(f"dataset trigger returned no snapshot_id: {body}")
            log.info("product: %s | dataset snapshot_id=%s", canonical_id, snapshot_id)

        if not dca_jobs:
            for it in dca_items:
                log.info("product: %s | triggering DCA collector for %s", canonical_id, it["store"])
                cid = with_retry(b.trigger_dca, api_key, b.DCA_COLLECTORS[it["store"]], canonical_name, it["url"])
                dca_jobs[it["store"]] = cid
                log.info("product: %s | DCA %s collection_id=%s", canonical_id, it["store"], cid)

        if save:
            db.save_pending_job(canonical_id, snapshot_id, dca_jobs.get("Harris Farm"))
            db.save_job_history(canonical_id, snapshot_id, dca_jobs.get("Harris Farm"))

        poll_log = lambda msg: log.info("product: %s | %s", canonical_id, msg)
        deadline = time.time() + PRODUCT_TIMEOUT
        # poll deadline bounded by PRODUCT_TIMEOUT (via with_retry) so a slow snapshot can't stall the batch
        if snapshot_id:
            log.info("product: %s | waiting for dataset snapshot...", canonical_id)
            with_retry(b.poll, api_key, snapshot_id, log=poll_log, deadline=deadline)
        for store, cid in dca_jobs.items():
            log.info("product: %s | waiting for DCA %s job...", canonical_id, store)
            with_retry(b.poll_dca, api_key, cid, log=poll_log, deadline=deadline)

        store_products = {}
        if snapshot_id:
            log.info("product: %s | downloading dataset snapshot", canonical_id)
            records = with_retry(b.download, api_key, snapshot_id)
            url_to_store = {it["url"]: it["store"] for it in ds_items}
            for rec in records:
                inp = rec.get("input")
                url = inp.get("url") if isinstance(inp, dict) else inp
                store = url_to_store.get(url) or url
                store_products[store] = b.extract_products(store, rec.get("markdown") or "")
        for store, cid in dca_jobs.items():
            log.info("product: %s | downloading DCA %s job", canonical_id, store)
            rows = with_retry(b.download_dca, api_key, cid)
            store_products[store] = [b.dca_record_to_product(r) for r in rows]

        counts = {}
        matched = {}
        for it in items:
            prods = store_products.get(it["store"]) or []
            counts[it["store"]] = len(prods)
            best, best_score, decision = pick_best(canonical, prods, it["store"], min_score, confirm_fn)
            matched[it["store"]] = best is not None
            if save:
                db.save_result(
                    canonical_id,
                    canonical_name,
                    COUNTRY,
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
                    log.debug(
                        "product: %s | saved %s best=%s price=%s score=%.1f decision=%s",
                        canonical_id,
                        it["store"],
                        best.get("name"),
                        best.get("price"),
                        best_score,
                        decision,
                    )
                    if decision == "ai confirmed":
                        log.info("product: %s | AI confirmed %s: %s ($%s)", canonical_id, it["store"], best.get("name"), best.get("price"))
                else:
                    log.debug(
                        "product: %s | %s no match (%s) -> empty row saved (best_score=%s, candidates=%d)",
                        canonical_id,
                        it["store"],
                        decision,
                        best_score,
                        len(prods),
                    )
                    if decision == "ai rejected":
                        log.info("product: %s | AI rejected %s: %s", canonical_id, it["store"], best.get("name") if best else "(no match)")

        n_with_products = sum(1 for s in STORE_ORDER if matched.get(s))
        status = "ok" if n_with_products == len(STORE_ORDER) else ("partial" if n_with_products else "error")
        log.info(
            "product: %s | done status=%s counts=%s",
            canonical_id,
            status,
            ", ".join(f"{s}={counts.get(s, 0)}" for s in STORE_ORDER),
        )
        if save and snapshot_id:
            db.set_job_status(canonical_id, snapshot_id, status)
        return {
            "canonical_id": canonical_id,
            "name": canonical_name,
            "status": status,
            "counts": counts,
            "error": None,
        }
    except Exception as e:
        log.error("product: %s | FAILED: %s: %s", canonical_id, type(e).__name__, e, exc_info=log.isEnabledFor(logging.DEBUG))
        return {
            "canonical_id": canonical_id,
            "name": canonical_name,
            "status": "error",
            "counts": {},
            "error": f"{type(e).__name__}: {e}",
        }


def process_batch(api_key, batch, save=True, min_score=MIN_MATCH_SCORE, confirm_fn=None, resume=False):
    """Run one batch of products in parallel (one thread each). Waits for all threads."""
    log = logging.getLogger("rcb")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(batch)), thread_name_prefix="prod") as ex:
        futures = [ex.submit(run_single_product, p, api_key, save, min_score, confirm_fn, resume) for p in batch]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    if save:
        for r in results:
            if r["status"] == "error":
                log.warning("status=error -> not marking scraped (will be retried next run): %s", r["canonical_id"])
                continue
            db.mark_scraped(r["canonical_id"], COUNTRY, r["status"], r.get("error"))
            db.clear_pending_job(r["canonical_id"], COUNTRY)
            log.debug("marked scraped: %s status=%s", r["canonical_id"], r["status"])
    return results


def fetch_by_ids(ids, country=COUNTRY):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT canonical_id, country_code, canonical_name, brand, product_name, "
            "size_value, size_unit, size_basis, pack_count, variant FROM canonical "
            "WHERE country_code = %s AND canonical_id = ANY(%s)",
            (country, list(ids)),
        ).fetchall()
    return [dict(r) for r in rows]


def print_results(results):
    log = logging.getLogger("rcb")
    log.info("")
    for r in results:
        line = f"[{r['status']:<8}] {r['canonical_id']}"
        if r.get("counts"):
            line += "  " + ", ".join(f"{s}={r['counts'].get(s, 0)}" for s in STORE_ORDER)
        if r.get("error"):
            log.error("%s  ERROR: %s", line, r["error"])
        elif r["status"] == "ok":
            log.info("%s", line)
        else:
            log.warning("%s", line)


def main():
    ap = argparse.ArgumentParser(description="Batch 5-store price search over canonical products")
    ap.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE, help="products per batch (default 5)")
    ap.add_argument("--all", action="store_true", help="process ALL eligible products (loop batches)")
    ap.add_argument("--force", action="store_true", help="ignore the 24h gate when picking eligible")
    ap.add_argument("--repeat", action="store_true", help="re-run the last batch (most recently scraped)")
    ap.add_argument("--ids", help="comma-separated canonical_ids to re-run")
    ap.add_argument("--resume", action="store_true", help="resume from already-triggered jobs instead of triggering new ones")
    ap.add_argument("--no-save", action="store_true", help="dry run: extract/print but do not write DB rows")
    ap.add_argument("--no-ai", action="store_true", help="disable AI confirmation of doubtful brand matches (strict mode)")
    ap.add_argument("--min-score", type=float, default=MIN_MATCH_SCORE,
                    help=f"min match score to save a store result (default {MIN_MATCH_SCORE})")
    ap.add_argument("--api-key", help="Bright Data API key (defaults to .env.brightdata)")
    ap.add_argument("--log-file", help="log file path (default: logs/run_canonical_batch.log)")
    ap.add_argument("--quiet", action="store_true", help="only show warnings/errors on screen (full log still written to file)")
    ap.add_argument("--verbose", action="store_true", help="print DEBUG-level detail on screen")
    args = ap.parse_args()

    console_level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    setup_logging(console_level=console_level, log_file=args.log_file)
    logger.info("=== run_canonical_batch start (quiet=%s verbose=%s) ===", args.quiet, args.verbose)

    api_key = args.api_key or load_api_key()
    if not api_key:
        logger.error("BRIGHTDATA_API_KEY not set in .env.brightdata")
        raise SystemExit("BRIGHTDATA_API_KEY not set in .env.brightdata")

    db.init_schema()

    confirm_fn = None if args.no_ai else ai_confirm.confirm_match
    logger.info("ai_confirm=%s", "disabled" if confirm_fn is None else "enabled")

    if args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        logger.info("mode=ids ids=%s", ids)
        eligible = fetch_by_ids(ids)
        if not eligible:
            logger.warning("no canonical rows found for ids: %s", ids)
            return
    elif args.repeat:
        logger.info("mode=repeat")
        eligible = db.get_eligible(batch_size=args.limit, order_by="recent", force=True)
        if not eligible:
            logger.warning("nothing scraped yet -- nothing to repeat")
            return
    else:
        logger.info("mode=eligible all=%s force=%s", args.all, args.force)
        eligible = db.get_eligible(batch_size=args.limit if not args.all else 10**6, force=args.force)
        if not eligible:
            logger.warning("no eligible products (all scraped within 24h). Use --force, --repeat or --ids.")
            return

    total = len(eligible)
    logger.info("Eligible products: %d", total)
    for p in eligible:
        logger.info("  - %s", p["canonical_id"])

    if (args.repeat or args.ids) and not args.no_save:
        db.clear_store_rows([p["canonical_id"] for p in eligible])
        logger.info("cleared existing store rows for %d product(s) before re-run", len(eligible))

    all_results = []
    batch_size = args.limit
    for start in range(0, total, batch_size):
        batch = eligible[start : start + batch_size]
        logger.info("=== BATCH %d: %d product(s) ===", start // batch_size + 1, len(batch))
        results = process_batch(api_key, batch, save=not args.no_save, min_score=args.min_score,
                                confirm_fn=confirm_fn, resume=args.resume)
        all_results.extend(results)
        print_results(results)
        if not args.all:
            logger.info("[test mode] stopped after first batch. Re-run to pick the next batch.")
            break

    if args.all:
        logger.info("Done. Processed %d product(s).", len(all_results))


if __name__ == "__main__":
    main()
