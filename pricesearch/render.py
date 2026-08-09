"""HTML renderer — turns a canonical result dict into a self-contained page.

Output is a single .html file with no external dependencies:
  - header + query interpretation + summary
  - overall-cheapest banner
  - store filter pills (JS toggling)
  - per-category product grids with thumbnails, store badges, pack sizes,
    clickable prices (open the product page in a new tab) and a highlighted
    "cheapest" chip on the lowest price in each category.
"""

from __future__ import annotations

import html as html_module
from typing import Any

from . import pricing

STORE_COLORS = {
    "Woolworths": ("#00a651", "#e6f7ef"),
    "Coles": ("#e4002b", "#fdecee"),
    "Aldi": ("#00a7e1", "#e6f7fd"),
}


def _esc(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""))


def _thousands(value: int) -> str:
    return f"{int(value):,}"


def _store_style(store: str) -> tuple[str, str]:
    return STORE_COLORS.get(store, ("#64748b", "#f1f5f9"))


def _money(price: float | None) -> str:
    return f"${price:,.2f}" if price is not None else "—"


def _product_card(product: dict[str, Any]) -> str:
    color, bg = _store_style(product.get("store", ""))
    image = product.get("image_url") or ""
    img_block = (
        f'<img src="{_esc(image)}" alt="{_esc(product["name"])}" '
        f'onerror="this.style.display=\'none\';this.parentNode.classList.add(\'no-img\')">'
        if image
        else f'<div class="thumb-placeholder">{_esc(product.get("brand") or product.get("name") or "?")[:1]}</div>'
    )
    cheapest_chip = (
        '<span class="chip-cheapest">Best price</span>' if product.get("is_cheapest") else ""
    )
    url = product.get("url")
    price_block = (
        f'<a class="price" href="{_esc(url)}" target="_blank" rel="noopener" '
        f'title="Open product page">Buy {_money(product.get("price"))} &rarr;</a>'
        if url
        else f'<span class="price price-unlinked">{_money(product.get("price"))}</span>'
    )
    pack = f'<span class="pack">{_esc(product.get("pack_size"))}</span>' if product.get("pack_size") else ""
    return f"""
    <div class="card" data-store="{_esc(product.get('store', '').lower())}">
      <div class="thumb">{img_block}</div>
      <div class="card-body">
        <span class="store-badge" style="background:{bg};color:{color}">{_esc(product.get("store", ""))}</span>
        {cheapest_chip}
        <h3 class="name">{_esc(product.get("name", ""))}</h3>
        <div class="meta">{_esc(product.get("brand", ""))}{pack}</div>
        <div class="card-foot">{price_block}</div>
      </div>
    </div>"""


def _category_block(cat: dict[str, Any], stores: list[str]) -> str:
    products = cat.get("products") or []
    if not products:
        return ""
    cards = "\n".join(_product_card(p) for p in products)
    return f"""
    <section class="category" data-category="{_esc(cat.get('category', '').lower())}">
      <h2 class="cat-title">{_esc(cat.get("category", ""))}
        <span class="count">{len(products)} products</span></h2>
      <div class="grid">{cards}</div>
    </section>"""


def _category_dropdown(current: str) -> str:
    options = "".join(
        f'<option value="{_esc(value)}"{(" selected" if value == current else "")}>{_esc(label)}</option>'
        for value, label in [
            ("Fresh", "Fresh Produce"),
            ("Frozen", "Frozen"),
            ("Shelf", "Shelf"),
        ]
    )
    return (
        f'<select id="category" name="category" class="category-select" title="Filter search to a category">'
        f'{options}</select>'
    )


def _format_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds >= 60:
        return f"{seconds / 60:.1f}m {seconds % 60:.0f}s"
    return f"{seconds:.1f}s"


def _usage_html(result: dict[str, Any]) -> str:
    usage = result.get("usage") or {}
    model = usage.get("model") or pricing.DEFAULT_MODEL
    prompt = usage.get("prompt_tokens", 0)
    thoughts = usage.get("thoughts_tokens", 0)
    output = usage.get("output_tokens", 0)
    total = usage.get("total_tokens", 0) or (prompt + thoughts + output)
    cost = usage.get("cost_usd", 0.0)
    elapsed = usage.get("elapsed_sec", 0.0)
    cached = bool(usage.get("cached"))
    has_tokens = total > 0
    if has_tokens:
        tokens_line = (
            f"{_thousands(prompt)} in / {_thousands(thoughts)} thinking / "
            f"{_thousands(output)} out / {_thousands(total)} total"
        )
        cost_line = f"est. cost ${cost:.4f} USD"
    else:
        tokens_line = "tokens n/a"
        cost_line = "est. cost n/a"
    badge = '<span class="cached-badge">cached</span>' if cached else ""
    return (
        f'<div class="usage-line"><strong>Model:</strong> {_esc(model)} {badge}'
        f'<span class="usage-sep">&middot;</span>{tokens_line}'
        f'<span class="usage-sep">&middot;</span>{cost_line}'
        f'<span class="usage-sep">&middot;</span>took {_format_elapsed(elapsed)}</div>'
    )


def render_html(result: dict[str, Any], title: str | None = None) -> str:
    """Return the full standalone HTML document for a search result."""
    query = result.get("query", "")
    interpretation = result.get("query_interpretation") or ""
    summary = result.get("summary") or ""
    stores = result.get("stores") or []
    overall = result.get("overall_cheapest")
    usage = result.get("usage") or {}

    overall_banner = ""
    if overall:
        overall_banner = f"""
      <div class="overall">
        <div class="overall-badge">Overall cheapest</div>
        <div class="overall-body">
          <span class="overall-name">{_esc(overall.get("name", ""))}</span>
          <span class="overall-meta">{_esc(overall.get("store", ""))} &middot; {_money(overall.get("price"))}</span>
        </div>
        {f'<a class="overall-link" href="{_esc(overall.get("url"))}" target="_blank" rel="noopener">Open &rarr;</a>' if overall.get("url") else ""}
      </div>"""

    store_pills = "".join(
        f'<button class="pill active" data-store="{_esc(s.lower())}">{_esc(s)}</button>' for s in stores
    )

    categories = "\n".join(_category_block(c, stores) for c in result.get("categories") or [])
    total_products = sum(len(c.get("products") or []) for c in result.get("categories") or [])

    page_title = _esc(title or f"PriceSearch — {query}")
    head = _esc(query)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
  :root {{ --bg:#f4f6fb; --card:#fff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
           --accent:#0ea5e9; --good:#059669; --good-bg:#ecfdf5; --bad:#b91c1c; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         "Helvetica Neue",Arial,sans-serif; background:var(--bg); color:var(--ink); }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:24px 20px 64px; }}
  header.top {{ background:linear-gradient(135deg,#0ea5e9,#6366f1); color:#fff;
         border-radius:0 0 22px 22px; padding:30px 20px; }}
  header.top h1 {{ margin:0; font-size:28px; letter-spacing:-.5px; }}
  header.top .sub {{ opacity:.9; margin-top:6px; font-size:15px; }}
  .searchbox {{ margin-top:18px; display:flex; gap:8px; }}
  .searchbox input {{ flex:1; padding:12px 16px; border:none; border-radius:12px;
         font-size:16px; outline:none; }}
  .searchbox button {{ padding:12px 22px; border:none; border-radius:12px;
         background:#fff; color:#0ea5e9; font-weight:700; font-size:15px;
         cursor:pointer; }}
  .searchbox button:hover {{ background:#f1f5f9; }}
  .usage-line {{ margin-top:12px; font-size:13px; color:#dbeafe; opacity:.95; }}
  .usage-line strong {{ color:#fff; }}
  .usage-sep {{ margin:0 6px; opacity:.6; }}
  .cached-badge {{ background:rgba(255,255,255,.22); padding:2px 8px; border-radius:999px;
         font-size:11px; font-weight:600; margin-left:8px; }}
  .meta-line {{ color:var(--muted); font-size:13px; margin:14px 0 4px; }}
  .interpret {{ color:var(--muted); font-size:15px; margin:6px 0 16px; font-style:italic; }}
  .summary {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
         padding:14px 18px; font-size:15px; margin-bottom:16px; }}
  .overall {{ display:flex; align-items:center; gap:14px; background:var(--good-bg);
         border:1px solid #a7f3d0; border-radius:14px; padding:14px 18px;
         margin-bottom:20px; }}
  .overall-badge {{ background:var(--good); color:#fff; font-weight:700; font-size:12px;
         padding:5px 10px; border-radius:999px; white-space:nowrap; }}
  .overall-name {{ font-weight:700; font-size:16px; }}
  .overall-meta {{ color:var(--muted); font-size:14px; }}
  .overall-link {{ margin-left:auto; color:var(--good); font-weight:600;
         text-decoration:none; white-space:nowrap; }}
  .pills {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 18px; }}
  .pill {{ border:1px solid var(--line); background:var(--card); padding:8px 16px;
         border-radius:999px; font-size:14px; cursor:pointer; color:var(--muted); }}
  .pill.active {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
  .category {{ margin-bottom:34px; }}
  .cat-title {{ font-size:21px; margin:0 0 14px; letter-spacing:-.3px; }}
  .cat-title .count {{ color:var(--muted); font-size:13px; font-weight:500; margin-left:8px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
         gap:16px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
         overflow:hidden; transition:transform .12s ease, box-shadow .12s ease;
         display:flex; flex-direction:column; }}
  .card:hover {{ transform:translateY(-3px); box-shadow:0 10px 24px rgba(15,23,42,.10); }}
  .thumb {{ height:150px; background:#f8fafc; display:flex; align-items:center;
         justify-content:center; padding:12px; }}
  .thumb img {{ max-width:100%; max-height:100%; object-fit:contain; }}
  .thumb.no-img {{ background:var(--good-bg); }}
  .thumb-placeholder {{ font-size:44px; font-weight:800; color:#cbd5e1; }}
  .card-body {{ padding:14px 16px 16px; display:flex; flex-direction:column; flex:1; }}
  .store-badge {{ align-self:flex-start; font-size:12px; font-weight:700;
         padding:3px 9px; border-radius:999px; margin-bottom:8px; }}
  .chip-cheapest {{ align-self:flex-start; background:var(--good); color:#fff;
         font-size:11px; font-weight:700; padding:3px 9px; border-radius:999px;
         margin:0 0 8px; }}
  .name {{ font-size:15px; line-height:1.35; margin:0 0 6px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:12px; min-height:18px; }}
  .card-foot {{ margin-top:auto; }}
  .price {{ display:inline-block; background:var(--ink); color:#fff; font-weight:700;
         text-decoration:none; padding:9px 14px; border-radius:10px; font-size:15px; }}
  .price:hover {{ background:var(--accent); }}
  .price-unlinked {{ background:#e2e8f0; color:var(--ink); cursor:default; }}
  .empty {{ color:var(--muted); padding:24px 0; }}
  .foot {{ color:var(--muted); font-size:12px; margin-top:40px; text-align:center; }}

  /* ------------------------------------------------------------------
     Mobile layout — stacked, touch-friendly. Active below 640px.
     ------------------------------------------------------------------ */
  @media (max-width: 640px) {{
    header.top {{ padding:20px 16px; border-radius:0 0 16px 16px; }}
    header.top h1 {{ font-size:22px; }}
    .wrap {{ padding:16px 12px 48px; }}
    .searchbox {{ flex-wrap:wrap; gap:8px; margin-top:14px; }}
    .searchbox input {{ flex:1 1 100%; padding:14px 16px; }}
    .searchbox button {{ flex:1 1 auto; padding:13px 16px; }}
    .category-select {{ flex:1 1 auto; min-width:0; max-width:none;
         padding:12px 10px; font-size:14px; }}
    .meta-line {{ font-size:12px; margin:10px 0 2px; }}
    .interpret {{ font-size:14px; margin:4px 0 12px; }}
    .summary {{ font-size:14px; padding:12px 14px; }}
    .overall {{ flex-wrap:wrap; gap:10px; padding:12px 14px; }}
    .overall-link {{ margin-left:0; }}
    .pills {{ flex-wrap:nowrap; overflow-x:auto; -webkit-overflow-scrolling:touch;
         margin:0 -12px 14px; padding:0 12px; scrollbar-width:none; }}
    .pills::-webkit-scrollbar {{ display:none; }}
    .pill {{ flex:0 0 auto; padding:9px 16px; }}
    .grid {{ grid-template-columns:1fr; gap:12px; }}
    .card {{ flex-direction:row; border-radius:14px; }}
    .card:hover {{ transform:none; }}
    .thumb {{ height:88px; width:88px; flex:0 0 88px; padding:8px; }}
    .card-body {{ padding:10px 12px; }}
    .store-badge, .chip-cheapest {{ font-size:11px; margin-bottom:6px; }}
    .name {{ font-size:15px; margin-bottom:4px; }}
    .meta {{ font-size:12px; margin-bottom:8px; min-height:0; }}
    .price {{ display:block; text-align:center; padding:10px 14px; font-size:15px; }}
    .cat-title {{ font-size:18px; margin:0 0 10px; }}
    .foot {{ margin-top:28px; }}
  }}
</style>
</head>
<body>
  <header class="top">
    <div class="wrap" style="padding-top:0;padding-bottom:0;max-width:1180px;">
      <h1>&#129360; PriceSearch</h1>
      <div class="sub">Grocery price engine &middot; powered by Gemini &amp; Google Search</div>
      <form class="searchbox" action="/" method="get">
        <input name="q" placeholder="Search a product, e.g. potatoes" value="{_esc(query)}"
               autocomplete="off">
        <button type="submit">Search</button>
      </form>
      {_usage_html(result)}
    </div>
  </header>
  <main class="wrap">
    <div class="meta-line">Results for <strong>"{head}"</strong> &middot; {total_products} products &middot; {", ".join(_esc(s) for s in stores)}</div>
    {f'<div class="interpret">"{_esc(interpretation)}"</div>' if interpretation else ""}
    {f'<div class="summary">{_esc(summary)}</div>' if summary else ""}
    {overall_banner}
    <div class="pills">{store_pills}</div>
    {categories or '<div class="empty">No priced products found. Try a different search.</div>'}
  </main>
  <div class="foot">Prices are indicative as surfaced by Google Search and are not a
    guarantee of current store pricing. Generated by PriceSearch.</div>
<script>
  const pills = document.querySelectorAll('.pill');
  const cards = document.querySelectorAll('.card');
  let active = new Set();
  pills.forEach(function (p) {{ p.dataset.store.split(' ').forEach(s => active.add(s)); }});
  function apply() {{
    pills.forEach(function (p) {{
      const on = active.has(p.dataset.store);
      p.classList.toggle('active', on);
    }});
    cards.forEach(function (c) {{
      c.style.display = active.has(c.dataset.store) ? '' : 'none';
    }});
    document.querySelectorAll('.category').forEach(function (sec) {{
      const visible = Array.from(sec.querySelectorAll('.card'))
        .filter(c => c.style.display !== 'none').length;
      sec.style.display = visible ? '' : 'none';
    }});
  }}
  pills.forEach(function (p) {{
    p.addEventListener('click', function () {{
      const s = p.dataset.store;
      if (active.has(s) && active.size > 1) {{ active.delete(s); }} else {{ active.add(s); }}
      apply();
    }});
  }});
  apply();
</script>
</body>
</html>"""


def render_to_file(result: dict[str, Any], path: str | None = None) -> str:
    """Render and write the HTML. Returns the written path (default output/PriceSearch.html)."""
    from pathlib import Path

    out = Path(path) if path else (Path(__file__).resolve().parent.parent / "output" / "PriceSearch.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    html_doc = render_html(result, title=f"PriceSearch — {result.get('query', '')}")
    out.write_text(html_doc, encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------------------
# Streaming shell (web app) — renders instantly, results drip-feed over SSE
# ---------------------------------------------------------------------------

_SHELL_CSS = """
  :root { --bg:#f4f6fb; --card:#fff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
           --accent:#0ea5e9; --good:#059669; --good-bg:#ecfdf5; --bad:#b91c1c; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         "Helvetica Neue",Arial,sans-serif; background:var(--bg); color:var(--ink); }
  .wrap { max-width:1180px; margin:0 auto; padding:24px 20px 64px; }
  header.top { background:linear-gradient(135deg,#0ea5e9,#6366f1); color:#fff;
         border-radius:0 0 22px 22px; padding:30px 20px; }
  header.top h1 { margin:0; font-size:28px; letter-spacing:-.5px; }
  header.top .sub { opacity:.9; margin-top:6px; font-size:15px; }
  .searchbox { margin-top:18px; display:flex; gap:8px; }
  .searchbox .q-wrap { position:relative; flex:1; display:flex; min-width:0; }
  .searchbox .q-wrap input { flex:1; width:100%; padding:12px 40px 12px 16px; border:none; border-radius:12px;
         font-size:16px; outline:none; }
  .q-clear { position:absolute; right:8px; top:50%; transform:translateY(-50%); border:none;
         background:#e2e8f0; color:#475569; width:22px; height:22px; border-radius:50%;
         font-size:15px; line-height:1; cursor:pointer; display:none; align-items:center;
         justify-content:center; padding:0; }
  .q-clear.visible { display:flex; }
  .q-clear:hover { background:#cbd5e1; color:#0f172a; }
  .searchbox button { padding:12px 22px; border:none; border-radius:12px;
         background:#fff; color:#0ea5e9; font-weight:700; font-size:15px;
         cursor:pointer; }
  .searchbox button:hover { background:#f1f5f9; }
  .category-select { padding:0 12px; border:none; border-radius:12px; font-size:14px;
         background:#fef3c7; color:#92400e; font-weight:600; cursor:pointer; outline:none;
         max-width:190px; }
  .mode-select { padding:0 12px; border:none; border-radius:12px; font-size:14px;
         background:#ede9fe; color:#5b21b6; font-weight:600; cursor:pointer; outline:none;
         max-width:160px; }
  .store-picker { display:flex; align-items:center; gap:6px; background:rgba(255,255,255,.18);
         border-radius:12px; padding:6px 10px; }
  .store-picker.hidden { display:none; }
  .store-picker-label { font-size:12px; font-weight:700; color:#dbeafe; margin-right:2px; }
  .store-chips { display:inline-flex; flex-wrap:wrap; gap:4px; }
  .store-picker .chip { display:inline-flex; align-items:center; gap:4px; background:#fff;
         color:#0f172a; border-radius:999px; padding:2px 6px 2px 10px; font-size:12px; font-weight:600; }
  .store-picker .chip-x { border:none; background:transparent; color:#64748b; font-size:15px;
         line-height:1; cursor:pointer; padding:0 2px; }
  #store-add { border:none; border-radius:8px; padding:5px 8px; font-size:12px; width:96px; outline:none; }
  .store-add-btn { border:none; border-radius:8px; padding:5px 9px; font-size:12px; background:#fff;
         color:#0ea5e9; font-weight:700; cursor:pointer; }
  .usage-line { margin-top:12px; font-size:13px; color:#dbeafe; opacity:.95; }
  .usage-line strong { color:#fff; }
  .status-line { margin-top:6px; font-size:13px; color:#dbeafe; opacity:.95; min-height:16px; }
  .usage-sep { margin:0 6px; opacity:.6; }
  .cached-badge { background:rgba(255,255,255,.22); padding:2px 8px; border-radius:999px;
         font-size:11px; font-weight:600; margin-left:8px; }
  .meta-line { color:var(--muted); font-size:13px; margin:14px 0 4px; }
  .interpret { color:var(--muted); font-size:15px; margin:6px 0 16px; font-style:italic; }
  .summary { background:var(--card); border:1px solid var(--line); border-radius:14px;
         padding:14px 18px; font-size:15px; margin-bottom:16px; }
  .overall { display:flex; align-items:center; gap:14px; background:var(--good-bg);
         border:1px solid #a7f3d0; border-radius:14px; padding:14px 18px;
         margin-bottom:20px; }
  .overall-badge { background:var(--good); color:#fff; font-weight:700; font-size:12px;
         padding:5px 10px; border-radius:999px; white-space:nowrap; }
  .overall-name { font-weight:700; font-size:16px; }
  .overall-meta { color:var(--muted); font-size:14px; }
  .overall-link { margin-left:auto; color:var(--good); font-weight:600;
         text-decoration:none; white-space:nowrap; }
  .pills { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 18px; }
  .pill { border:1px solid var(--line); background:var(--card); padding:8px 16px;
         border-radius:999px; font-size:14px; cursor:pointer; color:var(--muted); }
  .pill.active { background:var(--ink); color:#fff; border-color:var(--ink); }
  .category { margin-bottom:34px; }
  .cat-title { font-size:21px; margin:0 0 14px; letter-spacing:-.3px; }
  .cat-title .count { color:var(--muted); font-size:13px; font-weight:500; margin-left:8px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
         gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
         overflow:hidden; transition:transform .12s ease, box-shadow .12s ease;
         display:flex; flex-direction:column; }
  .card:hover { transform:translateY(-3px); box-shadow:0 10px 24px rgba(15,23,42,.10); }
  .skel { height:260px; border:1px solid var(--line); border-radius:16px;
         background:linear-gradient(100deg,#f1f5f9 40%,#e2e8f0 50%,#f1f5f9 60%);
         background-size:200% 100%; animation:shimmer 1.4s infinite; }
  @keyframes shimmer { 0% { background-position:200% 0; } 100% { background-position:-200% 0; } }
  .thumb { height:150px; background:#f8fafc; display:flex; align-items:center;
         justify-content:center; padding:12px; }
  .thumb img { max-width:100%; max-height:100%; object-fit:contain; }
  .thumb.no-img { background:var(--good-bg); }
  .thumb-placeholder { font-size:44px; font-weight:800; color:#cbd5e1; }
  .card-body { padding:14px 16px 16px; display:flex; flex-direction:column; flex:1; }
  .store-badge { align-self:flex-start; font-size:12px; font-weight:700;
         padding:3px 9px; border-radius:999px; margin-bottom:8px; }
  .chip-cheapest { align-self:flex-start; background:var(--good); color:#fff;
         font-size:11px; font-weight:700; padding:3px 9px; border-radius:999px;
         margin:0 0 8px; }
  .name { font-size:15px; line-height:1.35; margin:0 0 6px; }
  .meta { color:var(--muted); font-size:13px; margin-bottom:12px; min-height:18px; }
  .card-foot { margin-top:auto; }
  .price { display:inline-block; background:var(--ink); color:#fff; font-weight:700;
         text-decoration:none; padding:9px 14px; border-radius:10px; font-size:15px; }
  .price:hover { background:var(--accent); }
  .price-unlinked { background:#e2e8f0; color:var(--ink); cursor:default; }
  .btn-add { display:block; width:100%; margin-top:8px; border:1px solid var(--accent);
         background:#fff; color:var(--accent); font-weight:700; font-size:14px;
         padding:9px 14px; border-radius:10px; cursor:pointer; }
  .btn-add:hover { background:var(--accent); color:#fff; }
  .btn-add:disabled { opacity:.7; cursor:default; }
  .btn-add.added { background:var(--good); border-color:var(--good); color:#fff; }
  .empty { color:var(--muted); padding:24px 0; }
  .foot { color:var(--muted); font-size:12px; margin-top:40px; text-align:center; }

  /* ------------------------------------------------------------------
     Mobile layout — stacked, touch-friendly. Active below 640px.
     ------------------------------------------------------------------ */
  @media (max-width: 640px) {
    header.top { padding:20px 16px; border-radius:0 0 16px 16px; }
    header.top h1 { font-size:22px; }
    .wrap { padding:16px 12px 48px; }
    .searchbox { flex-wrap:wrap; gap:8px; margin-top:14px; }
    .searchbox .q-wrap { flex:1 1 100%; }
    .searchbox .q-wrap input { flex:1 1 100%; padding:14px 40px 14px 16px; }
    .searchbox button { flex:1 1 auto; padding:13px 16px; }
    .category-select { flex:1 1 auto; min-width:0; max-width:none;
         padding:12px 10px; font-size:14px; }
    .mode-select { flex:1 1 auto; min-width:0; max-width:none;
         padding:12px 10px; font-size:14px; }
    .store-picker { flex:1 1 100%; flex-wrap:wrap; padding:8px 10px; }
    .meta-line { font-size:12px; margin:10px 0 2px; }
    .interpret { font-size:14px; margin:4px 0 12px; }
    .summary { font-size:14px; padding:12px 14px; }
    .overall { flex-wrap:wrap; gap:10px; padding:12px 14px; }
    .overall-link { margin-left:0; }
    .pills { flex-wrap:nowrap; overflow-x:auto; -webkit-overflow-scrolling:touch;
         margin:0 -12px 14px; padding:0 12px; scrollbar-width:none; }
    .pills::-webkit-scrollbar { display:none; }
    .pill { flex:0 0 auto; padding:9px 16px; }
    .grid { grid-template-columns:1fr; gap:12px; }
    .skel { height:120px; }
    .card { flex-direction:row; border-radius:14px; }
    .card:hover { transform:none; }
    .thumb { height:88px; width:88px; flex:0 0 88px; padding:8px; }
    .card-body { padding:10px 12px; }
    .store-badge, .chip-cheapest { font-size:11px; margin-bottom:6px; }
    .name { font-size:15px; margin-bottom:4px; }
    .meta { font-size:12px; margin-bottom:8px; min-height:0; }
    .price { display:block; text-align:center; padding:10px 14px; font-size:15px; }
    .btn-add { font-size:13px; padding:8px 12px; }
    .cat-title { font-size:18px; margin:0 0 10px; }
    .foot { margin-top:28px; }
  }
"""

_SHELL_JS = """
(function () {
  var state = {
    query: '', model: '', mode: 'premium', category: '', stores: [], categories: [], products: {},
    overall: null, interpretation: '', summary: '', usage: null,
    status: 'idle', error: '', cached: false,
    active: null, totalTasks: 0, completedTasks: 0
  };
  var t0 = 0, timer = null, es = null;
  var inApp = !!(window.ReactNativeWebView && window.ReactNativeWebView.postMessage);
  var pendingAdds = {};

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (m) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
    });
  }
  function money(p) { return (p == null) ? '&mdash;' : '$' + Number(p).toFixed(2); }
  function style(store) {
    var c = { Woolworths: ['#00a651', '#e6f7ef'], Coles: ['#e4002b', '#fdecee'], Aldi: ['#00a7e1', '#e6f7fd'] }[store];
    return c || ['#64748b', '#f1f5f9'];
  }
  function th(n) { return Number(n || 0).toLocaleString(); }

  function card(p) {
    var s = style(p.store || '');
    var img = p.image_url
      ? '<img src="' + esc(p.image_url) + '" alt="' + esc(p.name) + '" onerror="this.style.display=\\'none\\';this.parentNode.classList.add(\\'no-img\\')">'
      : '<div class="thumb-placeholder">' + esc((p.brand || p.name || '?').charAt(0)) + '</div>';
    var chip = p.is_cheapest ? '<span class="chip-cheapest">Best price</span>' : '';
    var price = p.url
      ? '<a class="price" href="' + esc(p.url) + '" target="_blank" rel="noopener" title="Open product page">Buy ' + money(p.price) + ' &rarr;</a>'
      : '<span class="price price-unlinked">' + money(p.price) + '</span>';
    var pack = p.pack_size ? '<span class="pack">' + esc(p.pack_size) + '</span>' : '';
    var addBtn = inApp
      ? '<button type="button" class="btn-add">Add to shopping list</button>'
      : '';
    return '<div class="card" data-store="' + esc((p.store || '').toLowerCase()) + '" data-pid="' + esc(p.pid || '') + '">' +
      '<div class="thumb">' + img + '</div>' +
      '<div class="card-body">' +
        '<span class="store-badge" style="background:' + s[1] + ';color:' + s[0] + '">' + esc(p.store || '') + '</span>' +
        chip +
        '<h3 class="name">' + esc(p.name || '') + '</h3>' +
        '<div class="meta">' + esc(p.brand || '') + pack + '</div>' +
        '<div class="card-foot">' + price + addBtn + '</div>' +
      '</div></div>';
  }

  function addToShoppingList(pid, btn) {
    var p = state.products[pid];
    if (!p || !window.ReactNativeWebView) return;
    var requestId = (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
    pendingAdds[requestId] = { pid: pid, el: btn };
    btn.disabled = true;
    btn.textContent = 'Adding\u2026';
    window.ReactNativeWebView.postMessage(JSON.stringify({
      type: 'ADD_ITEM',
      name: p.name || '',
      productId: pid,
      storeId: p.store || '',
      priceCents: (p.price != null) ? Math.round(Number(p.price) * 100) : null,
      price: (p.price != null) ? Number(p.price) : null,
      unitLabel: p.pack_size || p.unit_label || '',
      imageUrl: p.image_url || '',
      productUrl: p.url || '',
      requestId: requestId
    }));
  }

  window.__cartwiseOnAddResult = function (res) {
    if (!res || !res.requestId) return;
    var rec = pendingAdds[res.requestId];
    if (!rec) return;
    delete pendingAdds[res.requestId];
    var btn = (rec.el && rec.el.isConnected) ? rec.el : null;
    if (!btn) {
      var target = null;
      document.querySelectorAll('.card[data-pid]').forEach(function (c) {
        if (c.getAttribute('data-pid') === rec.pid) target = c;
      });
      if (target) btn = target.querySelector('.btn-add');
    }
    if (!btn) return;
    btn.disabled = true;
    if (res.ok) {
      btn.classList.add('added');
      btn.textContent = 'Added to ' + (res.listName || 'list') + ' \u2713';
    } else if (res.error === 'already exists') {
      btn.textContent = 'Already in list';
    } else {
      btn.disabled = false;
      btn.textContent = 'Add to shopping list';
    }
  };

  function computeCheapest() {
    Object.keys(state.products).forEach(function (pid) {
      state.products[pid].is_cheapest = false;
    });
    var all = [];
    state.categories.forEach(function (c) {
      var ps = c.pids.map(function (pid) { return state.products[pid]; }).filter(Boolean)
        .filter(function (p) { return state.active && state.active.has((p.store || '').toLowerCase()); });
      var min = null;
      ps.forEach(function (p) { if (p.price != null && (min === null || p.price < min.price)) min = p; });
      if (min) min.is_cheapest = true;
      all = all.concat(ps);
    });
    var overall = null;
    all.forEach(function (p) {
      if (p.price != null && (overall === null || p.price < overall.price)) overall = p;
    });
    state.overall = overall;
  }

  function render() {
    computeCheapest();
    var u = state.usage || {};
    var usageHtml = '<strong>Model:</strong> ' + esc(state.model || u.model || '');
    if (state.cached) usageHtml += '<span class="cached-badge">cached</span>';
    if (u.total_tokens) usageHtml += '<span class="usage-sep">&middot;</span>' + th(u.prompt_tokens) +
      ' in / ' + th(u.thoughts_tokens) + ' thinking / ' + th(u.output_tokens) + ' out';
    if (u.cost_usd) usageHtml += '<span class="usage-sep">&middot;</span>est. cost $' + Number(u.cost_usd).toFixed(4) + ' USD';
    if (u.elapsed_sec) usageHtml += '<span class="usage-sep">&middot;</span>took ' + Number(u.elapsed_sec).toFixed(1) + 's';
    document.getElementById('usage').innerHTML = usageHtml;

    var count = Object.keys(state.products).length;
    var meta = 'Results for <strong>"' + esc(state.query) + '"</strong>';
    if (count) meta += ' &middot; ' + count + ' products';
    if (state.stores.length) meta += ' &middot; ' + state.stores.map(esc).join(', ');
    document.getElementById('meta').innerHTML = meta;

    document.getElementById('interpret').innerHTML = state.interpretation
      ? '<div class="interpret">"' + esc(state.interpretation) + '"</div>' : '';
    document.getElementById('summary').innerHTML = state.summary
      ? '<div class="summary">' + esc(state.summary) + '</div>' : '';

    var ov = state.overall;
    document.getElementById('overall').innerHTML = ov
      ? '<div class="overall"><div class="overall-badge">Overall cheapest</div>' +
        '<div class="overall-body"><span class="overall-name">' + esc(ov.name) + '</span>' +
        '<span class="overall-meta">' + esc(ov.store) + ' &middot; ' + money(ov.price) + '</span></div>' +
        (ov.url ? '<a class="overall-link" href="' + esc(ov.url) + '" target="_blank" rel="noopener">Open &rarr;</a>' : '') +
        '</div>' : '';

    document.getElementById('pills').innerHTML = state.stores.map(function (s) {
      return '<button class="pill' + (state.active && state.active.has(s.toLowerCase()) ? ' active' : '') +
        '" data-store="' + esc(s.toLowerCase()) + '">' + esc(s) + '</button>';
    }).join('');

    var cats = state.categories.filter(function (c) {
      return c.pids.some(function (pid) { return !!state.products[pid]; });
    });
    var html = cats.map(function (c) {
      var ps = c.pids.map(function (pid) { return state.products[pid]; }).filter(Boolean);
      return '<section class="category"><h2 class="cat-title">' + esc(c.category) +
        '<span class="count">' + ps.length + ' products</span></h2>' +
        '<div class="grid">' + ps.map(card).join('') + '</div></section>';
    }).join('');
    var resultsEl = document.getElementById('results');
    if (html) {
      resultsEl.innerHTML = html;
    } else if (state.status === 'searching' || state.status === 'enriching') {
      resultsEl.innerHTML = '<div class="grid">' +
        Array.apply(null, { length: 6 }).map(function () { return '<div class="skel"></div>'; }).join('') +
        '</div>';
    } else {
      resultsEl.innerHTML = '<div class="empty">No priced products found. Try a different search.</div>';
    }

    document.querySelectorAll('.card').forEach(function (c) {
      c.style.display = (state.active && state.active.has(c.dataset.store)) ? '' : 'none';
    });
    document.querySelectorAll('.category').forEach(function (sec) {
      var vis = Array.prototype.slice.call(sec.querySelectorAll('.card'))
        .filter(function (c) { return c.style.display !== 'none'; }).length;
      sec.style.display = vis ? '' : 'none';
    });
    document.querySelectorAll('.pill').forEach(function (p) {
      p.onclick = function () {
        var s = p.dataset.store;
        if (state.active.has(s) && state.active.size > 1) state.active.delete(s); else state.active.add(s);
        render();
      };
    });
  }

  function statusText() {
    if (state.status === 'error') return 'Search failed: ' + state.error;
    if (state.status === 'done') return state.cached ? 'Completed (cached)' : 'Completed';
    var secs = Math.max(0, (performance.now() - t0) / 1000).toFixed(0);
    if (state.status === 'enriching') return 'Gathering thumbnails&hellip; ' + secs + 's';
    return 'Searching ' + state.completedTasks + '/' + state.totalTasks + ' categories &mdash; ' + secs + 's';
  }
  function updateStatus() {
    var el = document.getElementById('status');
    if (!el) return;
    el.innerHTML = statusText();
    el.style.color = (state.status === 'error') ? '#fda4af' : '';
  }

  function loadResult(r) {
    state.categories = [];
    state.products = {};
    (r.categories || []).forEach(function (b) {
      var block = { category: b.category, pids: [] };
      (b.products || []).forEach(function (p, i) {
        var pid = p.pid || (b.category + '-' + i + '-' + (p.store || '') + '-' + (p.name || '') + '-' + (p.price != null ? p.price : ''));
        p.pid = pid;
        state.products[pid] = p;
        block.pids.push(pid);
      });
      if (block.pids.length) state.categories.push(block);
    });
  }

  function handle(d) {
    if (d.type === 'start') {
      state.query = d.query; state.model = d.model; state.stores = d.stores;
      state.mode = d.mode || state.mode;
      state.category = d.category || '';
      state.status = 'searching'; state.cached = !!d.cached;
      state.active = new Set(d.stores.map(function (s) { return s.toLowerCase(); }));
      state.categories = []; state.products = {}; state.overall = null;
      state.totalTasks = d.tasks || (d.stores.length * 3); state.completedTasks = 0;
      t0 = performance.now();
      if (timer) clearInterval(timer);
      timer = setInterval(updateStatus, 400);
      render(); updateStatus();
    } else if (d.type === 'items') {
      state.completedTasks += 1;
      if (state.completedTasks >= state.totalTasks) state.status = 'enriching';
      if (!d.failed) {
        var block = null;
        for (var i = 0; i < state.categories.length; i++) {
          if (state.categories[i].category === d.category) { block = state.categories[i]; break; }
        }
        if (!block) { block = { category: d.category, pids: [] }; state.categories.push(block); }
        (d.products || []).forEach(function (p) {
          if (!state.products[p.pid]) {
            state.products[p.pid] = p;
            block.pids.push(p.pid);
          }
        });
      }
      render(); updateStatus();
    } else if (d.type === 'enrich') {
      var p = state.products[d.pid];
      if (p) { if (d.url) p.url = d.url; if (d.image_url) p.image_url = d.image_url; }
      render();
    } else if (d.type === 'finish') {
      if (d.result) {
        if (Object.keys(state.products).length === 0) loadResult(d.result);
        state.interpretation = d.result.query_interpretation || state.interpretation;
        state.summary = d.result.summary || state.summary;
        state.usage = d.result.usage || state.usage;
      }
      state.cached = !!d.cached;
      state.status = 'done';
      if (timer) clearInterval(timer);
      if (es) es.close();
      render(); updateStatus();
    } else if (d.type === 'error') {
      state.status = 'error'; state.error = d.message || 'Search failed';
      if (timer) clearInterval(timer);
      if (es) es.close();
      render(); updateStatus();
    }
  }

  var pickerStores = [];
  function selectedStores() { return pickerStores.slice(); }

  function renderChips() {
    var box = document.getElementById('store-chips');
    if (!box) return;
    box.innerHTML = '';
    pickerStores.forEach(function (s) {
      var label = document.createElement('label');
      label.className = 'chip';
      label.appendChild(document.createTextNode(esc(s)));
      var x = document.createElement('button');
      x.type = 'button'; x.className = 'chip-x'; x.setAttribute('data-store', s); x.innerHTML = '&times;';
      label.appendChild(x);
      var hidden = document.createElement('input');
      hidden.type = 'hidden'; hidden.name = 'stores'; hidden.value = s;
      label.appendChild(hidden);
      box.appendChild(label);
    });
  }

  function addPickerStore() {
    var input = document.getElementById('store-add');
    var name = (input.value || '').trim();
    if (!name) return;
    if (pickerStores.some(function (s) { return s.toLowerCase() === name.toLowerCase(); })) {
      alert('Store already added: ' + name); input.value = ''; return;
    }
    if (pickerStores.length >= 3) { alert('Maximum 3 stores.'); return; }
    pickerStores.push(name);
    input.value = '';
    renderChips();
  }

  function toggleStores() {
    var picker = document.getElementById('store-picker');
    if (picker) picker.classList.toggle('hidden', state.mode !== 'premium');
  }

  function connect(q, category, mode, stores) {
    var url = '/api/search/stream?q=' + encodeURIComponent(q) +
              '&mode=' + encodeURIComponent(mode || 'premium') +
              '&category=' + encodeURIComponent(category || '');
    if (stores && stores.length) url += '&stores=' + encodeURIComponent(stores.join(','));
    es = new EventSource(url);
    es.onmessage = function (ev) { try { handle(JSON.parse(ev.data)); } catch (e) {} };
    es.onerror = function () {};
  }

  var params = new URLSearchParams(location.search);
  var q = (params.get('q') || '').trim();
  var qInput = document.getElementById('q');
  var qClear = document.getElementById('q-clear');
  function refreshClear() {
    if (qClear) qClear.classList.toggle('visible', !!(qInput.value || '').trim());
  }
  qInput.addEventListener('input', refreshClear);
  if (qClear) qClear.addEventListener('click', function () {
    qInput.value = '';
    qInput.focus();
    refreshClear();
  });
  refreshClear();
  state.mode = (params.get('mode') || 'premium').toLowerCase();
  document.getElementById('mode').value = state.mode;
  var st = params.getAll('stores');
  pickerStores = (st.length ? st : ['Woolworths', 'Aldi', 'Coles']).slice(0, 3);
  toggleStores();
  renderChips();
  document.getElementById('mode').addEventListener('change', function () {
    state.mode = this.value;
    toggleStores();
  });
  document.getElementById('store-add-btn').addEventListener('click', addPickerStore);
  document.getElementById('store-add').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); addPickerStore(); }
  });
  document.getElementById('store-chips').addEventListener('click', function (ev) {
    if (ev.target.classList.contains('chip-x')) {
      var s = ev.target.getAttribute('data-store');
      pickerStores = pickerStores.filter(function (x) { return x !== s; });
      renderChips();
    }
  });
  document.querySelector('form.searchbox').addEventListener('submit', function (ev) {
    if (state.mode === 'premium' && selectedStores().length === 0) {
      ev.preventDefault();
      alert('Add at least one store.');
    }
  });
  (function () {
    var box = document.getElementById('results');
    if (!box) return;
    box.addEventListener('click', function (ev) {
      var t = ev.target;
      while (t && t !== box && !(t.classList && t.classList.contains('btn-add'))) t = t.parentNode;
      if (!t || t === box || !t.classList || !t.classList.contains('btn-add')) return;
      var cardEl = t.parentNode;
      while (cardEl && cardEl !== box && !(cardEl.classList && cardEl.classList.contains('card'))) cardEl = cardEl.parentNode;
      if (cardEl) addToShoppingList(cardEl.getAttribute('data-pid') || '', t);
    });
  })();
  if (q) {
    state.query = q;
    state.model = params.get('model') || '';
    state.category = params.get('category') || 'Fresh';
    document.getElementById('q').value = q;
    document.getElementById('category').value = state.category;
    connect(q, state.category, state.mode, selectedStores());
    render(); updateStatus();
  }
})();
"""


def render_shell(query: str = "", model: str | None = None, category: str | None = None, mode: str | None = None, stores: str | None = None) -> str:
    """Return the streaming web page shell (instant render; results via SSE)."""
    current = model or pricing.DEFAULT_MODEL
    current_category = category or "Fresh"
    current_mode = (mode or "premium").strip().casefold()
    if current_mode not in ("premium", "basic", "advanced"):
        current_mode = "premium"
    mode_options = "".join(
        f'<option value="{_esc(m)}"{(" selected" if m == current_mode else "")}>{_esc(label)}</option>'
        for m, label in [
            ("premium", "Premium (AI)"),
            ("basic", "Basic (store APIs)"),
            ("advanced", "Advanced (Pinch)"),
        ]
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PriceSearch</title>
<style>{_SHELL_CSS}</style>
</head>
<body>
  <header class="top">
    <div class="wrap" style="padding-top:0;padding-bottom:0;max-width:1180px;">
      <h1>&#129360; PriceSearch</h1>
      <div class="sub">Grocery price engine &middot; powered by Gemini &amp; Google Search</div>
      <form class="searchbox" action="/" method="get">
        <div class="q-wrap">
          <input id="q" name="q" placeholder="Search a product, e.g. potatoes" value="{_esc(query)}" autocomplete="off">
          <button type="button" id="q-clear" class="q-clear" aria-label="Clear search" title="Clear">&times;</button>
        </div>
        <select id="mode" name="mode" class="mode-select" title="Search mode">{mode_options}</select>
        <div id="store-picker" class="store-picker{'' if current_mode == 'premium' else ' hidden'}" title="Stores (max 3)">
          <span class="store-picker-label">Stores</span>
          <span id="store-chips" class="store-chips"></span>
          <input id="store-add" type="text" placeholder="+ store" autocomplete="off" maxlength="40">
          <button type="button" id="store-add-btn" class="store-add-btn">Add</button>
        </div>
        {_category_dropdown(current_category)}
        <button type="submit">Search</button>
      </form>
      <div class="usage-line" id="usage"><strong>Mode:</strong> {_esc(current_mode)} &middot; <strong>Model:</strong> {_esc(current)}</div>
      <div class="status-line" id="status"></div>
    </div>
  </header>
  <main class="wrap">
    <div class="meta-line" id="meta">Enter a product above &mdash; try 'potatoes'.</div>
    <div id="interpret"></div>
    <div id="summary"></div>
    <div id="overall"></div>
    <div class="pills" id="pills"></div>
    <div id="results"><div class="empty">Search results will appear here as they stream in.</div></div>
  </main>
  <div class="foot">Prices are indicative as surfaced by Google Search and are not a
    guarantee of current store pricing. Generated by PriceSearch.</div>
<script>{_SHELL_JS}</script>
</body>
</html>"""
