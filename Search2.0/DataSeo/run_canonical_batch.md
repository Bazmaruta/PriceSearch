# run_canonical_batch — Strategy & Requirements

## Purpose

A batch scheduler that takes canonical products from the `canonical` table and runs a
5-store price search for each, saving results into the per-store tables
(`woolworths`, `coles`, `aldi`, `harris_farm`, `iga`).

This program **does not** modify `bd_store_search.py`. That file is the single-product
debugging tool and is imported as a library (trigger/poll/download/extract helpers).

## How a single product is searched (per store)

For a given canonical name, 5 store searches are fired:

| Store        | Mechanism                                                    |
|--------------|--------------------------------------------------------------|
| Woolworths   | Bright Data dataset `gd_m6gjtfmeh43we6cqc` (markdown output) |
| Coles        | Bright Data dataset `gd_m6gjtfmeh43we6cqc` (markdown output) |
| ALDI         | Bright Data dataset `gd_m6gjtfmeh43we6cqc` (markdown output) |
| IGA          | Bright Data dataset `gd_m6gjtfmeh43we6cqc` (markdown output) |
| Harris Farm  | DCA collector `c_msvmt1yx1scomxxc3o` (structured JSON)       |

- The 4 dataset stores are submitted in a single trigger call (`input` array, `limit_per_input=5`).
- Harris Farm is a separate DCA trigger (`{"search_query": <name>, "url": "https://harrisfarm.com.au"}`).
- Each search URL comes from the `stores` table; Harris Farm's base URL is stored as `https://harrisfarm.com.au`.

## Batching strategy

- **Batch size:** 5 products, run **in parallel** (5 worker threads, one per product).
- The program **waits for all 5 threads** to complete before starting the next batch.
- Per-store API results are saved to the per-store tables with `ON CONFLICT DO UPDATE`
  (overwrites the row for that canonical + country), then the product is marked in `scrape_runs`.

## Modes / CLI

```
python run_canonical_batch.py                    # test: next 5 eligible, then STOP
python run_canonical_batch.py --limit N          # test: next N eligible (default 5), then STOP
python run_canonical_batch.py --all              # full: loop ALL eligible in batches of 5
python run_canonical_batch.py --force            # ignore the 24h gate for the eligibility pick
python run_canonical_batch.py --repeat           # re-run the last batch (most recent scrape_runs)
python run_canonical_batch.py --ids <id1,id2>    # re-run specific canonical_id(s)
python run_canonical_batch.py --resume           # reuse already-triggered jobs instead of re-triggering
python run_canonical_batch.py --no-save          # dry-run: extract/print but do NOT write DB rows
```

## Daily deduplication & crash safety

- A product is **eligible** when `scrape_runs.last_scraped_at` is `NULL` OR older than
  **24 hours**.
- Eligibility is ordered by `COALESCE(last_scraped_at, created_at) ASC` — never-run
  products first, then oldest-run first. This makes "the next batch" deterministic:
  re-running the command picks the next 5 not-done-in-24h products.
- **Crash safety:** only products marked in `scrape_runs` are considered done. If the
  process dies mid-batch, unmarked products are retried on the next run and marked
  products are skipped for 24h. No restart-from-scratch.
- `--force` bypasses the 24h gate (all products become eligible, still ordered by the
  eligibility ordering).
- `--repeat` re-runs the 5 most recently scraped products regardless of the 24h gate.
- `--ids` re-runs the exact listed products regardless of the 24h gate.
- `--repeat` and `--ids` **clear the existing per-store rows** for the selected
  products before re-scraping, so stale data (e.g. an old wrong price) can never
  survive a re-run. This is skipped when `--no-save` is set.

## Resilience

- **Network retries:** every Bright Data call (trigger / poll / download, dataset
  and DCA) is wrapped in `with_retry` — up to 4 attempts with linear backoff for
  transient DNS / connection / timeout failures, so a blip does not fail a whole batch.
- **Per-product deadline (`PRODUCT_TIMEOUT`, 10 min):** each product thread's
  snapshot / DCA polls are capped so one slow Bright Data job cannot stall the whole
  batch. On deadline, the poll `with_retry` raises `TimeoutError` and the product is
  marked `error` (not gated, retried next run). Failed products are **not** gated:
- **Poll progress:** dataset / DCA poll status is now logged at INFO per product
  (e.g. `product: X | dataset sd_... running (0 records)`), so a slow snapshot is
  visible in the log instead of a silent freeze.
- **Resume from paid jobs (`--resume`):** every triggered `snapshot_id` /
  `collection_id` is persisted to `pending_jobs` (written after the trigger, so an
  interrupted run can continue). Re-running with `--resume` downloads that
  already-paid output instead of triggering Bright Data again — avoiding wasted
  spend when a batch is interrupted mid-run.
- **Failed products are NOT gated:** a product that ends in `error` is **not**
  recorded in `scrape_runs` (its row is skipped), so it is automatically retried on
  the next run. Only `ok` / `partial` results advance `last_scraped_at`.
- **Empty rows on no match:** when a store has no acceptable match, a row is still
  written for that canonical + store with `price`/`product_name`/`product_url`
  left `NULL`, so you can see that the product was searched but not found.

## Best-match selection

Instead of blindly saving the first search result, each store's candidates are
ranked by `score_match(canonical, product)` and the top one is saved.

Scoring factors (higher = better):

| Factor | Bonus / penalty |
|--------|-----------------|
| Token overlap with canonical (name + `product_name` + `brand`), near-match aware (`banana`~`bananas`) | `+2` per shared token |
| Distractor tokens in the result not in canonical | `-0.5` each |
| All canonical tokens present in the result | `+1` (extra words can't sink a full match, e.g. "Fresh Chokoes each") |
| Brand agreement when `canonical.brand` is set | `+3` match, `-2` different brand |
| Exact size match (`2L` == 2 × L) | `+5` |
| Same unit but wrong size (`1L` vs `2L`) | `-4` |
| Different unit | `-2` |
| Canonical size known but result has no parseable size | `-1` |

### Closest-weight fallback

When a product matches the canonical identity (brand + all `product_name`/`variant`
descriptor tokens) but the **exact size is not stocked**, the **closest weight** at
that store is saved instead of rejecting the product (`decision = "closest size"`,
recorded in the store row's `match_source`):

- Identity candidates are scored size-agnostically (`_size_agnostic_score` drops the
  canonical size token and the size penalty, so `Blackberries 125g` still clears the
  bar for canonical `Blackberries 170g`).
- Exact size always wins if present; otherwise the same-unit candidate with the
  smallest `|size - canonical_size|` is chosen (ties broken by match score).
- The closest-size candidate is **confirmed by AI with a size-tolerant prompt**
  (`ai_confirm.confirm_match(..., size_tolerant=True)`): a different weight of the
  same product is accepted (e.g. 125g for 170g blackberries), but a genuinely
  different product is rejected — including a different STATE such as **Fresh vs
  Frozen**. Decisions are cached under `mode='size'`, separate from the strict
  `mode='confirm'` cache, so the two never collide.
- Candidates that only share the size but not the product identity (e.g. "Dried
  Cranberries 170g" for a blackberry canonical) are **not** identity matches and go
  through the existing AI doubt path → rejected.
- **Head-noun guard:** the candidate's last meaningful noun (`head_token`, strips
  brand/store/size/filler) must near-match a canonical descriptor. If it signals a
  different product KIND (e.g. "Custard & Pink Lady Apple **Scrolls**" — a pastry —
  for canonical "Custard Apple"), the candidate is sent through strict AI confirm →
  rejected. This catches false positives where all descriptor tokens appear but the
  product type differs.

- Size is parsed from text (`2L`, `2 L`, `500g`, `1kg`, `6 pack`) or taken from
  structured `size_value`/`size_unit` when populated. If structured fields are
  missing, the size is parsed from `canonical_name`.
- `pick_best(canonical, prods)` returns `(best_product, best_score)`. If nothing
  scores at least `MIN_MATCH_SCORE` (default `2.0`, CLI: `--min-score`), the store
  is treated as **no match** and nothing is saved. This rejects search fallbacks
  (e.g. Harris Farm returning "Strawberries 250g" for a "3M Command Hooks" query,
  which scores about -5) instead of storing a wrong price.
- **Brand + variant gate with AI confirmation (doubt zone):** when
  `canonical.brand` is populated (139/203 products), a candidate is saved directly
  only if **more than half of the brand tokens** appear in its name
  (`brand_matches` majority rule) — a single generic shared token (e.g. "dairy"
  between "Coach House **Dairy**" and "Bethune Lane **Dairy** Milk") can never
  satisfy the gate. AI is consulted in the **doubt zone** when either:
  1. the highest-scoring candidate **fails the brand gate** (e.g. Coles "Command"
     for the 3M canonical), or
  2. the brand matches but **descriptor tokens from `product_name`/`variant` are
     missing** in the candidate (e.g. canonical "Full Cream **UHT** Milk" vs "Full
     Cream Milk", or "Sourdough **White** Bread" vs "Sourdough **Rye** Loaf").

  The LLM (`ai_confirm.confirm_match`, Gemini `gemini-3.1-flash-lite` via the
  OpenAI-compatible endpoint, using `GEMINI_API_KEY`) decides yes/no:
  - LLM says **yes** → saved (`decision=ai confirmed`) — e.g. Coles "Command Clear
    Mini Hooks" accepted as the 3M product.
  - LLM says **no** → empty row (`decision=ai rejected`) — e.g. "Black & Gold" for
    an `a2` canonical, or "A2 Light Milk" for a Full Cream canonical.
  - No AI configured / `--no-ai` → strict reject (`decision=wrong brand`).
- **Cached:** each decision is stored in `match_decisions`
  (canonical × store × product_url), so a given product is only asked once.
- **Audit trail:** every store row now has a `match_source` column recording how the
  match was decided (`brand ok` / `ai confirmed` / `ai rejected` / `low score` /
  `wrong brand` / `no results`). AI calls are also logged at INFO level in
  `run_canonical_batch.log` (`AI confirmed/rejected <store>: <product>`), and every
  AI decision + reason is stored in `match_decisions` with a timestamp.

## Canonical attribute enrichment (`seed_canonical.py`)

The matcher relies on `canonical.brand`, `product_name`, `variant` and `size_*` to
gate matches. Products missing those (e.g. `so good almond milk 1l`) let wrong
variants/brands through. `seed_canonical.py` now backfills them **systematically**
through the canonical AI engine (`canonical.canonicalize`, the same Gemini
`gemini-3.1-flash-lite` extractor used for receipt lines):

```
python seed_canonical.py --seed            # seed from data/canonical_regression_203.json
python seed_canonical.py --enrich          # backfill missing brand/product_name/size/variant
python seed_canonical.py --enrich --dry-run  # preview without writing
```

- Enrichment runs the product's `canonical_name` through the engine and writes only
  the **missing** fields (existing good attributes are never overwritten).
- The engine caches results 24h, so re-runs are instant and cheap.
- Products with no meaningful brand (loose produce) keep `brand = NULL` — the
  engine reports none, so no spurious gate is added.
- Run it once after seeding and again whenever new products are added.

## How the next batch is triggered

Test mode always runs **exactly one batch** (5 products) and exits. To run the next
batch, re-run the same command; the `scrape_runs` table is the cursor and the program
will pick the next 5 eligible products. The operator explicitly triggers each batch
until the run is verified.

## Logging

- Uses Python's `logging` module (logger `rcb`).
- **Console output** is on by default (INFO level) so progress is visible when
  triggering from CLI / PowerShell. Each line includes a timestamp and the
  worker thread name (`prod_0`..`prod_4`), so you can follow the 5 parallel
  product threads.
- **File logging** always writes DEBUG-level detail to
  `logs/run_canonical_batch.log` (rotating: 5 MB per file, 3 backups).
- Key events logged per product: building URLs, dataset trigger + `snapshot_id`,
  DCA trigger + `collection_id`, polling start/finish, download, per-store
  product counts, saved best match (DEBUG), and final status
  (`ok` / `partial` / `error`) with per-store counts.
- Errors are logged with the exception type/message (and a stack trace at
  DEBUG level via `--verbose`).

### CLI flags

| Flag            | Effect                                                        |
|-----------------|---------------------------------------------------------------|
| `--verbose`     | Print DEBUG-level detail on screen (incl. stack traces).      |
| `--quiet`       | Only warnings/errors on screen; full log still goes to file.  |
| `--log-file`    | Override log file path (default `logs/run_canonical_batch.log`). |
| `--min-score`   | Minimum match score to save a store result (default `2.0`).   |
| `--no-ai`       | Disable AI confirmation of doubtful brand matches (strict mode). |

## Testing strategy

Logic is validated with a mocked test harness (no real Bright Data jobs fired):

1. **Eligibility** — never-run first, <24h skipped, >24h eligible, `--force` bypasses.
2. **Batch selection** — test mode picks exactly `--limit` (default 5) and stops.
3. **Repeat** — `--repeat` picks the last 5 scraped; `--ids` picks the given ids.
4. **Parallel wait** — a batch's 5 tasks all complete before the next batch starts.
5. **Persistence** — results written to per-store tables; `scrape_runs` marked with status.
6. **Crash recovery** — products marked done are skipped; unmarked are retried.
7. **Dry run** — `--no-save` performs extraction without writing DB rows.

Real-job integration tests are added later and run manually before extending to `--all`.
