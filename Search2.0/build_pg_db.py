"""Build the canonical product database in local PostgreSQL.

Recursively finds receipt files (text, PDF e-receipts, and photo images) across
the configured roots, canonicalises every receipt line with the canonical
engine (hard-coded ``gemini-3.1-flash-lite``; images are OCR'd first with the
vision model), then loads the results into two tables:

  receipt_lines       one row per receipt line (provenance + full result)
  canonical_products  one row per distinct canonical product (aggregated)

Receipt roots (env ``RECEIPT_DIRS``, comma-separated; defaults below):
  - ``Search2.0/data``          text receipt files (*.txt)
  - ``C:\\WR_Amp``              Woolworths e-receipt PDFs + photo JPGs

Connection config (env vars, sane defaults for the portable local PG):
  PG_HOST (127.0.0.1)  PG_PORT (5432)  PG_DB (canonical_products)  PG_USER (postgres)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE

DEFAULT_PG_BIN = r"C:\Users\vprad\AppData\Local\Temp\opencode\pgsql\bin"

pg_bin = os.environ.get("PG_BIN", DEFAULT_PG_BIN)
if pg_bin:
    os.environ["PATH"] = pg_bin + os.pathsep + os.environ.get("PATH", "")

import psycopg  # noqa: E402

sys.path.insert(0, str(HERE))
from canonical import DEFAULT_MODEL, canonicalize_many  # noqa: E402
from canonical.sources import find_receipt_files, read_receipt  # noqa: E402

DEFAULT_RECEIPT_DIRS = [ROOT / "data", Path(r"C:\WR_Amp")]

PG_CONF = {
    "host": os.environ.get("PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PG_PORT", "5432")),
    "dbname": os.environ.get("PG_DB", "canonical_products"),
    "user": os.environ.get("PG_USER", "postgres"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt_lines (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    source_type TEXT NOT NULL,
    line_no INTEGER,
    raw_name TEXT NOT NULL,
    source_text TEXT,
    brand TEXT,
    product_name TEXT,
    variant TEXT,
    category TEXT,
    subcategory TEXT,
    size_value DOUBLE PRECISION,
    size_unit TEXT,
    size_basis TEXT,
    raw_size TEXT,
    pack_count INTEGER,
    barcode TEXT,
    canonical_name TEXT,
    confidence DOUBLE PRECISION,
    attribute_confidence JSONB,
    ambiguities JSONB,
    model TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_receipt_lines_source ON receipt_lines(source_file);
CREATE INDEX IF NOT EXISTS idx_receipt_lines_canonical ON receipt_lines(canonical_name);
CREATE INDEX IF NOT EXISTS idx_receipt_lines_raw_lower ON receipt_lines(lower(raw_name));

CREATE TABLE IF NOT EXISTS canonical_products (
    id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    brand TEXT,
    product_name TEXT,
    variant TEXT,
    category TEXT,
    subcategory TEXT,
    size_value DOUBLE PRECISION,
    size_unit TEXT,
    size_basis TEXT,
    raw_size TEXT,
    pack_count INTEGER,
    barcode TEXT,
    confidence DOUBLE PRECISION,
    attribute_confidence JSONB,
    receipt_count INTEGER NOT NULL DEFAULT 0,
    raw_names JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def main() -> None:
    dirs_raw = os.environ.get("RECEIPT_DIRS", "").strip()
    if dirs_raw:
        roots = [Path(p.strip()) for p in dirs_raw.split(",") if p.strip()]
    else:
        roots = DEFAULT_RECEIPT_DIRS

    files = find_receipt_files(roots)
    if not files:
        raise SystemExit(f"No receipt files found under: {', '.join(str(r) for r in roots)}")

    entries: list[dict] = []
    failed_reads: list[str] = []
    for path in files:
        try:
            entries.extend(read_receipt(path))
        except Exception as exc:  # noqa: BLE001
            failed_reads.append(f"{path}: {exc}")

    unique: list[str] = []
    seen: set[str] = set()
    for e in entries:
        key = e["raw_name"].casefold()
        if key not in seen:
            seen.add(key)
            unique.append(e["raw_name"])

    print(f"{len(files)} receipt file(s), {len(entries)} line(s), "
          f"{len(unique)} unique raw line(s).")
    for f in failed_reads:
        print(f"  [failed] {f}")
    print(f"Canonicalising with {DEFAULT_MODEL}...")
    results = canonicalize_many(unique, workers=8)

    by_raw: dict[str, dict] = {}
    for res in results:
        by_raw[res.get("raw_name", "").casefold()] = res

    conn = psycopg.connect(**PG_CONF)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS canonical_products")
            cur.execute("DROP TABLE IF EXISTS receipt_lines")
            cur.execute(SCHEMA)

            insert_sql = """
                INSERT INTO receipt_lines (
                    source_file, source_type, line_no, raw_name, source_text,
                    brand, product_name, variant, category, subcategory,
                    size_value, size_unit, size_basis, raw_size, pack_count,
                    barcode, canonical_name, confidence, attribute_confidence,
                    ambiguities, model
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            loaded = 0
            for e in entries:
                res = by_raw.get(e["raw_name"].casefold())
                if res is None:
                    continue
                cur.execute(insert_sql, (
                    str(e["path"]),
                    e["source_type"],
                    e["line_no"],
                    res.get("raw_name"),
                    e["source_text"],
                    res.get("brand"),
                    res.get("product_name"),
                    res.get("variant"),
                    res.get("category"),
                    res.get("subcategory"),
                    res.get("size_value"),
                    res.get("size_unit"),
                    res.get("size_basis"),
                    res.get("raw_size"),
                    res.get("pack_count"),
                    res.get("barcode"),
                    res.get("canonical_name"),
                    res.get("confidence"),
                    json.dumps(res.get("attribute_confidence") or {}),
                    json.dumps(res.get("ambiguities") or []),
                    DEFAULT_MODEL,
                ))
                loaded += 1

            agg_sql = """
                SELECT canonical_name,
                       (ARRAY_AGG(id ORDER BY confidence DESC NULLS LAST))[1],
                       COUNT(*),
                       ARRAY_AGG(DISTINCT raw_name)
                FROM receipt_lines
                WHERE canonical_name IS NOT NULL
                GROUP BY canonical_name
            """
            cur.execute(agg_sql)
            agg = cur.fetchall()

            upsert_sql = """
                INSERT INTO canonical_products (
                    canonical_name, brand, product_name, variant, category,
                    subcategory, size_value, size_unit, size_basis, raw_size,
                    pack_count, barcode, confidence, attribute_confidence,
                    receipt_count, raw_names, first_seen, last_seen
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())
            """
            for canonical_name, rep_id, cnt, raws in agg:
                cur.execute(
                    "SELECT brand, product_name, variant, category, subcategory, "
                    "size_value, size_unit, size_basis, raw_size, pack_count, barcode, "
                    "confidence, attribute_confidence FROM receipt_lines WHERE id = %s",
                    (rep_id,),
                )
                rep = cur.fetchone()
                cur.execute(upsert_sql, (
                    canonical_name,
                    rep[0], rep[1], rep[2], rep[3], rep[4],
                    rep[5], rep[6], rep[7], rep[8], rep[9], rep[10], rep[11],
                    json.dumps(rep[12] or {}),
                    cnt,
                    json.dumps(list(dict.fromkeys(raws)), ensure_ascii=False),
                ))

            conn.commit()
            cur.execute("SELECT COUNT(*) FROM receipt_lines")
            line_cnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM canonical_products")
            prod_cnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM receipt_lines WHERE canonical_name IS NULL")
            null_cnt = cur.fetchone()[0]
            cur.execute("SELECT source_type, COUNT(*) FROM receipt_lines GROUP BY source_type ORDER BY source_type")
            by_type = cur.fetchall()
    finally:
        conn.close()

    print(f"Loaded {line_cnt} receipt lines, {prod_cnt} canonical products "
          f"({null_cnt} lines without a canonical name).")
    for t, c in by_type:
        print(f"  {t}: {c}")


if __name__ == "__main__":
    main()
