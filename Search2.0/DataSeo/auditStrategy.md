# PriceSearch Batch Run + Audit — Handoff / Operating Guide

Use this as the prompt/context for the next session. It documents how to run the
batch scraper and how to audit the results (product match + price verification).

Working dir: `C:\Users\vprad\Agents\PriceSearch\Search2.0\DataSeo`
Postgres (local): `postgresql://postgres@localhost:5432/pricesearch`

---

## 1. What this project does

For each canonical product in the `canonical` table, it fires a **5-store search**
(Woolworths, Coles, ALDI, IGA via the Bright Data dataset `gd_m6gjtfmeh43we6cqc`;
Harris Farm via DCA collector `c_msvmt1yx1scomxxc3o`), picks the best-matching
product per store (with AI confirmation for doubts), and saves name/price/url/image
into the per-store tables. A `scrape_runs` table gates each product to once per 24h.

## 2. Key files

| File | Role |
|------|------|
| `bd_store_search.py` | Bright Data transport + markdown extraction (DEBUG TOOL — do not refactor) |
| `run_canonical_batch.py` | Batch scheduler + matching engine (`pick_best`, scoring, closest-size, head-noun) |
| `ai_confirm.py` | Gemini `gemini-3.1-flash-lite` confirmation (strict `confirm` + size-tolerant `size` modes), cached in `match_decisions` |
| `db.py` | Schema + helpers (`canonical`, `scrape_runs`, per-store tables, `match_decisions`, `stores`) |
| `seed_canonical.py` | Seed canonical table from `data/canonical_regression_203.json`; `--enrich` backfills missing brand/product/size via the AI engine |
| `canonical/` | Receipt-line → canonical product AI engine (used by seed enrich) |
| `tests/test_run_canonical_batch.py` | Unit tests (currently 37, all pass) |

## 3. Running batches

Test mode = **one batch of 5 products, then stop** (per earlier requirement).

```
python run_canonical_batch.py --verbose            # next 5 eligible products, then stop
python run_canonical_batch.py --repeat --verbose   # re-run the 5 most recently scraped (clears store rows first)
python run_canonical_batch.py --ids "<id1>,<id2>"  # re-run specific products (clears store rows first)
python run_canonical_batch.py --all                # process ALL eligible in batches of 5
python run_canonical_batch.py --no-ai              # strict mode (no AI confirmation)
python run_canonical_batch.py --no-save            # dry run (persists nothing)
```

Flags: `--limit N`, `--force` (ignore 24h), `--min-score` (default 2.0),
`--log-file`, `--quiet`, `--verbose`.

Logs: console (INFO by default) + rotating file `logs/run_canonical_batch.log`.

### Batch loop procedure (the process we've been following)

1. Run `python run_canonical_batch.py --verbose`. Record the 5 products + start/stop time + statuses.
2. If any product reports `status=error` (no acceptable match at any store), investigate briefly:
   - Sometimes it is genuinely "product not stocked" (mark it done so it stops re-queueing):
     `python -c "import db;db.mark_scraped('<id>','AU','partial','no acceptable match')"`
   - Sometimes it is a matcher bug (see section 5) — fix before continuing.
3. Repeat until the required number of batches is done.
4. Run the audit (section 4) and report findings.

## 4. The audit process (run after each set of batches)

### 4a. Dump what was saved

List the products just scraped and their per-store rows:

```python
import db
with db.get_conn() as c:
    rows = c.execute(
        "SELECT canonical_id, status FROM scrape_runs "
        "WHERE last_scraped_at >= now() - interval '<X minutes>' ORDER BY last_scraped_at").fetchall()
    for cid in rows:
        for tbl in ["woolworths","coles","aldi","harris_farm","iga"]:
            r = c.execute(f"SELECT product_name, price, match_source FROM {tbl} WHERE canonical_id=%s",(cid,)).fetchone()
            print(tbl, r)
```

`match_source` values: `brand ok` (identity+exact size), `closest size`
(same product, nearest weight), `ai confirmed`, `ai rejected`, `low score`,
`wrong brand`, `no results`.

### 4a2. Job-history correlation (which Bright Data jobs produced these rows)

Every triggered run records a row in `job_history` (snapshot_id + collection_id +
run status), keeping the **newest 5 builds per product** (oldest overwritten).
This lets you re-pull the exact captured output from Bright Data if the report
needs deeper digging:

```python
import db
db.get_job_history(["lindt orange intense excellence chocolate 100g"])
# -> [{'snapshot_id': 'sd_...', 'collection_id': 'j_...', 'status': 'partial', ...}, ...]
```

Re-downloading a snapshot is FREE (only triggers cost money), so you can re-run
extraction/matching on historical output without re-scraping.

### 4b. Match audit (manual review)

For every saved price, sanity-check the product name matches the canonical.
Common issues found so far and their fixes (see section 5):
- wrong brand (e.g. "Just Milk" for "so good") → brand gate (majority tokens)
- wrong variant (UHT vs fresh, rye vs white, Light vs Full Cream) → descriptor tokens → AI
- wrong product kind (pastry "Apple Scrolls" vs custard apple fruit) → head-noun guard → AI
- different-size same product → closest-size + size-tolerant AI

Suspicious = saved price that is much cheaper/more expensive than other stores,
or a product name that clearly isn't the canonical.

### 4c. Price audit (live verification)

Fetch each saved `product_url` and compare the live price to the saved one.

- **WW, IGA, ALDI, Harris Farm**: plain `requests` works. Extract patterns:
  - WW: `"Price":12.34` in page
  - IGA/HF/ALDI: quoted string `"price":"12.34"` (prefer over int cents `"price":469`)
- **Coles**: blocks direct requests AND headless playwright. Use the **open Chrome via CDP**:
  1. Launch: `& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\opencode\chrome-profile"`
  2. Connect with `p.chromium.connect_over_cdp("http://localhost:9222")`, reuse an existing context/page.
  3. Extract Coles price from JSON-LD: `"priceSpecification":{...,"price":12}`.

Reusable audit script pattern (HTTP + Coles-CDP) was built as a temp script during
the session; rebuild from section 4c patterns if needed.

**Interpreting mismatches**: `saved < current` is usually a **special that ended**
(verify by re-checking the snapshot markdown for `Save $X / Was $Y`). That is NOT an
extraction bug. So far every price mismatch found was a special-then-ended.

### 4d. Re-scrape to correct a bad row

For any product whose stored row is wrong: `python run_canonical_batch.py --ids "<id>"`
(clears that product's store rows first, then re-scrapes with current logic).

### 4e. Generate the audit report (md + pdf, 100% Python)

A pure-DB snapshot report (no judgment baked in — analyse it afterwards):

```
python generate_audit_report.py                          # all scraped products
python generate_audit_report.py --ids "a,b"              # specific products
python generate_audit_report.py --since "2026-08-17 13:30"  # recent batch(es)
python generate_audit_report.py --with-snippets          # + raw markdown snippets
```

Writes `audit_reports/report_<timestamp>.md` and `.pdf` (reportlab) with:
per-store rows per product, `job_history` ids + status (for re-pulling), cached AI
decisions + reasons, and (with `--with-snippets`) the raw Bright Data markdown.

### 4f. Step-by-step batch audit workflow

1. **Record the run** — start/stop time, the 5 canonical_ids, each product's `status`.
   Job ids are auto-saved to `job_history` by the runner.
2. **Triage errors** — `status=error` means no acceptable match at any store:
   - genuinely not stocked (verify in snapshot candidates) → `db.mark_scraped('<id>','AU','partial','no acceptable match')`
   - matcher/extraction bug → fix before continuing.
3. **Dump rows** (§4a) — per-store `product_name`, `price`, `match_source`.
4. **Match audit** (§4b) — name vs canonical sanity check (brand/variant/kind/size).
5. **Price audit** (§4c) — live-verify every saved URL (HTTP + Coles CDP).
6. **Correct bad rows** (§4d) — `--ids` re-scrape.
7. **Generate report** (§4e) — md + pdf; analyse the report, pull `job_history`
   snapshots if deeper digging is needed.

## 5. Matching rules (current) & bugs fixed this session

Scoring (higher = better): `+2` per near-matching token (banana↔bananas),
`-0.5` per distractor, `+1` if ALL canonical tokens present, size `+5` exact /
`-4` same-unit wrong / `-2` different unit, brand `+3`/`-2`.

- **Brand gate**: `brand_matches` requires > half of brand tokens (single generic
  token can never pass, e.g. "dairy" in "Coach House Dairy").
- **Descriptor tokens**: `missing_descriptor_tokens` (near-match aware) — missing
  product_name/variant tokens → AI.
- **Head-noun guard**: `head_compatible` — candidate's last meaningful noun must
  near-match a canonical descriptor; else strict AI (catches pastry-for-fruit).
  Packaging/form words (`block`, `gel`, `box`, `bag`, `bottle`, `tub`, `jar`, ...)
  are treated as fillers and skipped, so "Lindt ... Chocolate Block" and
  "Liquid-Plumr ... Gel" still resolve to the real head noun (`chocolate`, `clear`).
  Glued sizes (`Orange100g`) are stripped to the word.
- **Duplicate-token cap**: in the "all canonical tokens present" bonus, overlap is
  capped at `len(unique_canon)` so a candidate repeating canonical words
  (`mint` + `mints`) cannot outscore the genuine product.
- **Doubt-zone AI iteration**: when the top-scoring candidate is doubtful, the AI is
  consulted over *all* plausible candidates (token overlap > 0, brand-compatible,
  head-compatible) in score order — a rejected decoy no longer hides the real
  product (fixes "fresh mint" where mouthwash/candy decoys outscored the herb).
- **Identity fall-through**: if the identity best is AI-rejected / head-incompatible,
  pick_best falls through to the doubt loop instead of returning immediately
  (fixes `hercules small 30 pack` where a 12L decoy blocked the true 30-pack).
- **Closest size**: identity match + no exact size → closest same-unit weight saved
  as `closest size`, confirmed by **size-tolerant AI** (Fresh vs Frozen rejected).
- **AI caching**: `match_decisions` PK includes `mode` (`confirm` vs `size`) so the
  two prompts never share an answer.
- **tokenize**: drops single-char tokens (fixes "Abbott's" vs "Jesse's" on "s").

Known remaining gap: `dove triple hydrate body wash 1l` @ Coles saved a **400mL**
as `brand ok` (not `closest size`) because L vs mL units differ — closest-size logic
only handles same-unit. Low priority.

## 6. Current DB state (as of this session)

- `canonical`: 202 rows AU (after merging the duplicate Balnea `5 x 100g` → `5 pack`).
- ~14 batches scraped so far; 24h gate active on done products.
- `job_history` records every Bright Data job build (snapshot/collection id + status),
  keeping the newest 5 per product.
- `fresh mint` (WW/Coles/ALDI/IGA herb now matched via doubt-zone iteration),
  `ginger` (variant=Fresh), `hercules small 30 pack`, `lindt orange`,
  `liquid-plumr` re-scraped with matcher fixes.
- `bega tasty cheese block 500g` ALDI correctly empty (private-label rejected);
  `custard apple` WW empty (pastry rejected), HF = Apple Custard $6.99.

## 7. Tests

```
python -m unittest discover -s tests -p "test_run_canonical_batch.py"
```
Currently 42/42 pass. The `TestParallelism` timing test (`elapsed < 1.0`) is flaky
under system load — if it fails, re-run.

## 8. Suggested next steps

1. Run the next set of 5 batches (section 3) with per-batch start/stop reporting.
2. Audit matches + prices (section 4) and generate the report (§4e).
3. Fix the L-vs-mL closest-size gap if it shows up again.
4. If AI decisions look wrong, tighten `ai_confirm.py` prompts (they are cached —
   invalidate by `DELETE FROM match_decisions WHERE ...` before re-testing).
