# PriceSearch

A state-of-the-art **grocery price search engine** powered by **Gemini**
(grounded by **Google Search**) that finds, categorizes, and compares prices
across Australian supermarkets — **Woolworths, Coles and Aldi** by default —
and renders results to a clickable HTML page.

Search "potatoes" and get *fresh* (washed, brushed, baby), *frozen* (fries,
wedges, mash) and *shelf* (canned, chips) products across all three stores,
each with a thumbnail, a clickable price that opens the product page, and the
cheapest option highlighted in every category plus overall.

## Architecture

```
User query ──► Streaming shell page (renders instantly, skeletons)
                    │
                    ▼
        SSE /api/search/stream (drip-feed)
        9 parallel Gemini calls (store × category) ──► products stream in
                    │        └── as each completes, ~2-4s for first results
                    ▼
          Normalize + cheapest detection (client + server)
                    │
                    ▼
          Enrich: resolve product URLs, fetch og:image thumbnails
          (streamed per product, overlaps remaining searches)
                    │
                    ▼
          Finish: render self-contained HTML (thumbnails, links, cheapest highlight)
```

Key modules:

| File | Purpose |
|---|---|
| `pricesearch/prompt.py` | The Gemini system prompt (tune it here) |
| `pricesearch/engine.py` | Grounded Gemini call, JSON validation, caching, mock mode |
| `pricesearch/stream.py` | Drip-feed generator → SSE events (start/items/enrich/finish) |
| `pricesearch/enrich.py` | Resolves URLs + extracts product thumbnails (cached, concurrent) |
| `pricesearch/render.py` | Static HTML + the streaming web shell (JS client) |
| `search.py` | CLI: query → HTML file |
| `app.py` | FastAPI web UI + `/api/search` JSON + `/api/search/stream` SSE |

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# ensure .env exists (GOOGLE_API_KEY + GEMINI_MODEL)
```

## Usage

CLI — writes an HTML file:

```powershell
python search.py "potatoes"                 # -> output/PriceSearch.html
python search.py "potatoes" --out output/potatoes.html
python search.py "milk" --stores "coles,aldi"
python search.py "bread" --mock              # demo data, no API call
```

Web UI:

```powershell
python app.py                               # http://localhost:8000
```

The web page is a shell that renders instantly; results **drip-feed** in over
Server-Sent Events (skeletons show first, then each store×category's products
appear as its parallel Gemini call completes, then thumbnails stream in).

API:

```powershell
curl -X POST http://localhost:8000/api/search -H "Content-Type: application/json" -d '{"query":"potatoes"}'   # full JSON (blocking)
curl -N "http://localhost:8000/api/search/stream?q=potatoes"                                                  # SSE drip-feed
```

## Behaviour notes

- **Model**: `GEMINI_MODEL` in `.env` (default `gemini-3.1-flash-lite`). The
  engine fires one grounded Gemini call per **store × category** (9 calls for
  3 stores × Fresh/Frozen/Shelf), all in parallel, and streams products as each
  completes — so first results appear in ~2-4s and the search finishes in
  ~8-12s. Thumbnails are fetched concurrently and streamed per product. Results
  are cached 24h so repeat searches are instant.
- **Stores**: defaults to Woolworths, Coles, Aldi; override per query.
- **Categories**: every product is tagged Fresh / Frozen / Shelf; cheapest in
  each category is highlighted, plus an overall-cheapest banner.
- **Anti-hallucination**: the prompt forbids invented prices/URLs; the engine
  also drops placeholder URLs and only trusts grounded results. Prices are
  indicative as surfaced by Google Search, not a guarantee of store pricing.
- **Thumbnails**: fetched from each product page's `og:image` and cached 7 days
  in `data/cache/urls.json`; missing images fall back to a brand initial.

## The system prompt

The heart of the engine is `pricesearch/prompt.py` — instructs Gemini to
interpret the query (singular/plural, synonyms), search every store × category
combo, extract name/brand/pack/price/URL, and return strict JSON.
