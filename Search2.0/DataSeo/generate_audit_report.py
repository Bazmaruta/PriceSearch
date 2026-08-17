"""Generate a lean audit report (markdown + PDF) from the DB.

Only products whose last run was `partial` or `error` are included, with their
Bright Data job ids (snapshot_id / collection_id) so the run can be re-pulled.

Pure Python, no AI/judgment baked in: dumps the current DB state into
audit_reports/ for analysis in the AI session.

Usage:
  python generate_audit_report.py                         # latest run's partial/error products
  python generate_audit_report.py --ids "a,b"             # specific canonical ids
  python generate_audit_report.py --since "2026-08-17 13:00" --until "2026-08-17 14:00"
  python generate_audit_report.py --pdf-only              # skip the .md file
  python generate_audit_report.py --out DIR               # output directory (default audit_reports/)
"""
import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import db

COUNTRY = "AU"
STORE_ORDER = ["Woolworths", "Coles", "ALDI", "Harris Farm", "IGA"]
STORE_TABLES = db.STORE_TABLES


def fmt_price(p):
    return f"${p:.2f}" if p is not None else "—"


def load_products(ids=None, since=None, until=None):
    """Return canonical products to report on, restricted to partial/error runs
    within an optional time window (by last_scraped_at)."""
    where = ["c.country_code = %s", "sr.status IN ('partial', 'error')"]
    params = [COUNTRY]
    if ids:
        where.append("c.canonical_id = ANY(%s)")
        params.append(list(ids))
    if since:
        where.append("sr.last_scraped_at >= %s")
        params.append(since)
    if until:
        where.append("sr.last_scraped_at <= %s")
        params.append(until)
    sql = (
        "SELECT c.canonical_id, c.canonical_name, c.brand, c.product_name, c.variant, "
        "       c.size_value, c.size_unit, "
        "       sr.last_scraped_at, sr.status AS run_status "
        "FROM canonical c JOIN scrape_runs sr "
        "  ON sr.canonical_id = c.canonical_id AND sr.country_code = c.country_code "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY sr.last_scraped_at DESC NULLS LAST, c.canonical_id"
    )
    with db.get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def load_store_rows(canonical_ids):
    rows = {}
    with db.get_conn() as conn:
        for tbl in STORE_TABLES.values():
            q = (
                f"SELECT canonical_id, product_name, price, product_url, match_source, "
                f"to_char(updated_at, 'YYYY-MM-DD HH24:MI') AS updated "
                f"FROM {tbl} WHERE country_code = %s AND canonical_id = ANY(%s)"
            )
            for r in conn.execute(q, (COUNTRY, list(canonical_ids))).fetchall():
                rows.setdefault(r["canonical_id"], {})[tbl] = dict(r)
    return rows


def load_decisions(canonical_ids):
    out = {}
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT canonical_id, store, product_name, decision, reason, mode, "
            "to_char(decided_at, 'YYYY-MM-DD HH24:MI') AS decided "
            "FROM match_decisions WHERE country_code = %s AND canonical_id = ANY(%s "
            ") ORDER BY canonical_id, decided_at DESC",
            (COUNTRY, list(canonical_ids)),
        ).fetchall()
    for r in rows:
        out.setdefault(r["canonical_id"], []).append(dict(r))
    return out


def download_snippets(snapshot_ids):
    """Download raw markdown per snapshot (dataset) - free, only reads."""
    import bd_store_search as b

    api = b.load_api_key()
    if not api:
        return {}
    out = {}
    for sid in set(filter(None, snapshot_ids)):
        try:
            recs = b.download(api, sid)
        except Exception:
            continue
        for rec in recs:
            inp = rec.get("input")
            url = inp.get("url") if isinstance(inp, dict) else inp
            store = "Woolworths" if "woolworths" in str(url) else ("Coles" if "coles" in str(url) else ("ALDI" if "aldi" in str(url) else ("IGA" if "iga" in str(url) else "?")))
            md = rec.get("markdown") or ""
            if md:
                out.setdefault(sid, {})[store] = md
    return out


def build_markdown(products, store_rows, decisions, history, snippets, with_snippets):
    lines = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"# Audit Report (partials & errors) — {now}")
    lines.append("")
    lines.append(f"- Products with issues: {len(products)}")
    lines.append("")
    # summary table first: one row per product, status + job ids
    lines.append("## Summary")
    lines.append("")
    lines.append("| Product | Status | snapshot_id | collection_id | job status |")
    lines.append("|---------|--------|-------------|---------------|------------|")
    for p in products:
        cid = p["canonical_id"]
        hist = [h for h in history if h["canonical_id"] == cid]
        h = hist[0] if hist else {}
        lines.append(
            f"| {p['canonical_name'][:45]} | {p['run_status'] or '—'} "
            f"| `{h.get('snapshot_id') or '—'}` | `{h.get('collection_id') or '—'}` | {h.get('status') or '—'} |"
        )
    lines.append("")
    lines.append("## Detail")
    lines.append("")
    for p in products:
        cid = p["canonical_id"]
        lines.append("---")
        lines.append("")
        lines.append(f"### {p['canonical_name']}")
        lines.append("")
        lines.append(f"- **canonical_id:** `{cid}` | **status:** {p['run_status'] or '—'}")
        hist = [h for h in history if h["canonical_id"] == cid]
        if hist:
            lines.append(f"- **snapshot_id:** `{hist[0].get('snapshot_id') or '—'}` | **collection_id:** `{hist[0].get('collection_id') or '—'}`")
        lines.append("")
        lines.append("| Store | Name | Price | Match |")
        lines.append("|-------|------|-------|-------|")
        rows = store_rows.get(cid, {})
        for tbl in STORE_TABLES.values():
            r = rows.get(tbl)
            if r:
                name = (r["product_name"] or "").replace("|", "/")[:55]
                lines.append(f"| {tbl} | {name} | {fmt_price(r['price'])} | {r['match_source'] or '—'} |")
            else:
                lines.append(f"| {tbl} | — | — | — |")
        lines.append("")
    lines.append("---")
    lines.append("_Generated by generate_audit_report.py — raw DB snapshot, no judgment applied._")
    return "\n".join(lines)


def build_pdf(md_text, out_pdf):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceAfter=4)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9, leading=12)
    mono = ParagraphStyle("Mono", parent=body, fontName="Courier", fontSize=7.5, leading=9)

    doc = SimpleDocTemplate(str(out_pdf), pagesize=A4,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm)
    story = []

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # naive markdown -> platypus: split on sections and tables
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("# "):
            story.append(Paragraph(esc(ln[2:]), h1))
        elif ln.startswith("## "):
            story.append(Spacer(1, 4))
            story.append(Paragraph(esc(ln[3:]), h2))
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.grey))
        elif ln.startswith("|") and i + 1 < len(lines) and lines[i + 1].startswith("|--"):
            # table block
            header = [c.strip() for c in ln.strip("|").split("|")]
            data = []
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                data.append([c.strip().strip("`") for c in lines[j].strip("|").split("|")])
                j += 1
            tbl = Table([header] + data, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(tbl)
            story.append(Spacer(1, 6))
            i = j
            continue
        elif ln.startswith("```text") or ln.startswith("```"):
            j = i + 1
            buf = []
            while j < len(lines) and not lines[j].startswith("```"):
                buf.append(lines[j])
                j += 1
            story.append(Paragraph(esc("\n".join(buf[:80])) + ("…" if len(buf) > 80 else ""), mono))
            i = j + 1
            continue
        elif ln.strip():
            story.append(Paragraph(esc(ln), body))
        i += 1
    doc.build(story)


def latest_run_window():
    """Return (since, until) bounding the most recent batch's scrape window."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT max(last_scraped_at) AS m FROM scrape_runs WHERE country_code = %s",
            (COUNTRY,),
        ).fetchone()
        if not row or not row["m"]:
            return None, None
        # products in the same batch share the same last_scraped_at (batch loop marks together)
        since = conn.execute(
            "SELECT min(last_scraped_at) AS m FROM scrape_runs WHERE country_code = %s "
            "AND last_scraped_at >= (SELECT max(last_scraped_at) - interval '15 minutes' "
            "                         FROM scrape_runs WHERE country_code = %s)",
            (COUNTRY, COUNTRY),
        ).fetchone()["m"]
        return since, row["m"]


def main():
    ap = argparse.ArgumentParser(description="Generate audit report (md + pdf) from DB")
    ap.add_argument("--ids", help="comma-separated canonical ids (default: latest run's partial/error)")
    ap.add_argument("--since", help="only products scraped since this time (e.g. '2026-08-17 12:00')")
    ap.add_argument("--until", help="only products scraped until this time")
    ap.add_argument("--pdf-only", action="store_true", help="skip the .md file")
    ap.add_argument("--out", default="audit_reports", help="output directory (default: audit_reports)")
    args = ap.parse_args()

    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    db.init_schema()

    since, until = args.since, args.until
    if not ids and not since and not until:
        since, until = latest_run_window()
        if since:
            print(f"auto-scoped to latest run: {since} .. {until}")
    products = load_products(ids=ids, since=since, until=until)
    if not products:
        print("no partial/error products matched", file=sys.stderr)
        return 1

    cids = [p["canonical_id"] for p in products]
    store_rows = load_store_rows(cids)
    history = db.get_job_history(cids)

    md_text = build_markdown(products, store_rows, {}, history, {}, False)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"report_{ts}.md"
    pdf_path = out_dir / f"report_{ts}.pdf"

    if not args.pdf_only:
        md_path.write_text(md_text, encoding="utf-8")
        print(f"wrote {md_path}")
    try:
        build_pdf(md_text, pdf_path)
        print(f"wrote {pdf_path}")
    except Exception as e:
        print(f"PDF failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
