import os
import psycopg
from psycopg.rows import dict_row
from pathlib import Path

COUNTRY = "AU"

STORE_TABLES = {
    "Woolworths": "woolworths",
    "Coles": "coles",
    "ALDI": "aldi",
    "Harris Farm": "harris_farm",
    "IGA": "iga",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS match_decisions (
    canonical_id     TEXT NOT NULL,
    country_code     TEXT NOT NULL DEFAULT 'AU',
    store            TEXT NOT NULL,
    product_url      TEXT,
    product_name     TEXT,
    decision         TEXT NOT NULL,
    reason           TEXT,
    mode             TEXT NOT NULL DEFAULT 'confirm',
    decided_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code, store, product_url, mode)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    canonical_id     TEXT NOT NULL,
    country_code     TEXT NOT NULL DEFAULT 'AU',
    last_scraped_at  TIMESTAMPTZ DEFAULT now(),
    status           TEXT,
    error            TEXT,
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS pending_jobs (
    canonical_id     TEXT NOT NULL,
    country_code     TEXT NOT NULL DEFAULT 'AU',
    snapshot_id      TEXT,
    collection_id    TEXT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS job_history (
    canonical_id     TEXT NOT NULL,
    country_code     TEXT NOT NULL DEFAULT 'AU',
    snapshot_id      TEXT,
    collection_id    TEXT,
    status           TEXT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code, created_at)
);

CREATE TABLE IF NOT EXISTS stores (
    country_code     TEXT NOT NULL DEFAULT 'AU',
    store            TEXT NOT NULL,
    search_url       TEXT NOT NULL,
    updated_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (country_code, store)
);

CREATE TABLE IF NOT EXISTS canonical (
    canonical_id       TEXT NOT NULL,
    country_code       TEXT NOT NULL DEFAULT 'AU',
    canonical_name     TEXT NOT NULL,
    brand              TEXT,
    product_name       TEXT,
    category           TEXT,
    subcategory        TEXT,
    size_value         DOUBLE PRECISION,
    size_unit          TEXT,
    size_basis         TEXT,
    pack_count         INTEGER,
    variant            TEXT,
    barcode            TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS woolworths (
    canonical_id   TEXT NOT NULL,
    country_code   TEXT NOT NULL DEFAULT 'AU',
    product_name   TEXT,
    price          DOUBLE PRECISION,
    currency       TEXT DEFAULT 'AUD',
    product_url    TEXT,
    thumbnail_url  TEXT,
    match_source   TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS coles (
    canonical_id   TEXT NOT NULL,
    country_code   TEXT NOT NULL DEFAULT 'AU',
    product_name   TEXT,
    price          DOUBLE PRECISION,
    currency       TEXT DEFAULT 'AUD',
    product_url    TEXT,
    thumbnail_url  TEXT,
    match_source   TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS aldi (
    canonical_id   TEXT NOT NULL,
    country_code   TEXT NOT NULL DEFAULT 'AU',
    product_name   TEXT,
    price          DOUBLE PRECISION,
    currency       TEXT DEFAULT 'AUD',
    product_url    TEXT,
    thumbnail_url  TEXT,
    match_source   TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS harris_farm (
    canonical_id   TEXT NOT NULL,
    country_code   TEXT NOT NULL DEFAULT 'AU',
    product_name   TEXT,
    price          DOUBLE PRECISION,
    currency       TEXT DEFAULT 'AUD',
    product_url    TEXT,
    thumbnail_url  TEXT,
    match_source   TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);

CREATE TABLE IF NOT EXISTS iga (
    canonical_id   TEXT NOT NULL,
    country_code   TEXT NOT NULL DEFAULT 'AU',
    product_name   TEXT,
    price          DOUBLE PRECISION,
    currency       TEXT DEFAULT 'AUD',
    product_url    TEXT,
    thumbnail_url  TEXT,
    match_source   TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (canonical_id, country_code)
);
"""


def get_dsn():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env = {}
    for line in Path(__file__).parent.joinpath(".env").read_text().splitlines():
        if line.strip() and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    if env.get("DATABASE_URL"):
        return env["DATABASE_URL"]
    return "postgresql://postgres:postgres@localhost:5432/pricesearch"


def get_conn():
    return psycopg.connect(get_dsn(), row_factory=dict_row)


def init_schema():
    with get_conn() as conn:
        conn.execute(SCHEMA)
        for table in STORE_TABLES.values():
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS match_source TEXT")
        conn.execute("ALTER TABLE match_decisions ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'confirm'")
        conn.execute("ALTER TABLE match_decisions DROP CONSTRAINT IF EXISTS match_decisions_pkey")
        conn.execute(
            "ALTER TABLE match_decisions ADD PRIMARY KEY "
            "(canonical_id, country_code, store, product_url, mode)"
        )


def upsert_store_url(country, store, search_url):
    sql = (
        "INSERT INTO stores (country_code, store, search_url, updated_at) "
        "VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (country_code, store) DO UPDATE SET "
        "search_url = EXCLUDED.search_url, updated_at = now()"
    )
    with get_conn() as conn:
        conn.execute(sql, (country, store, search_url))


def upsert_canonical(canonical_id, name, country=COUNTRY, **attrs):
    cols = ["canonical_id", "country_code", "canonical_name"] + list(attrs.keys())
    values = [canonical_id, country, name] + list(attrs.values())
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ["updated_at = now()"]
    for k in attrs.keys():
        updates.append(f"{k} = EXCLUDED.{k}")
    sql = (
        f"INSERT INTO canonical ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (canonical_id, country_code) DO UPDATE SET {', '.join(updates)}"
    )
    with get_conn() as conn:
        conn.execute(sql, values)


def upsert_store(table, canonical_id, country, price, currency, product_url, thumbnail_url, product_name=None, match_source=None):
    sql = (
        f"INSERT INTO {table} (canonical_id, country_code, product_name, price, currency, product_url, thumbnail_url, match_source, updated_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
        f"ON CONFLICT (canonical_id, country_code) DO UPDATE SET "
        f"product_name = EXCLUDED.product_name, price = EXCLUDED.price, currency = EXCLUDED.currency, "
        f"product_url = EXCLUDED.product_url, thumbnail_url = EXCLUDED.thumbnail_url, "
        f"match_source = EXCLUDED.match_source, updated_at = now()"
    )
    with get_conn() as conn:
        conn.execute(sql, (canonical_id, country, product_name, price, currency, product_url, thumbnail_url, match_source))


def save_result(canonical_id, name, country, chain_name, result):
    table = STORE_TABLES.get(chain_name)
    if not table:
        return
    upsert_store(
        table,
        canonical_id,
        country,
        result.get("price"),
        result.get("currency") or "AUD",
        result.get("url"),
        result.get("thumbnail"),
        result.get("name"),
        result.get("match_source"),
    )


def get_match_decision(canonical_id, store, product_url, country=COUNTRY, mode="confirm"):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT decision FROM match_decisions "
            "WHERE canonical_id = %s AND country_code = %s AND store = %s AND product_url = %s AND mode = %s",
            (canonical_id, country, store, product_url, mode),
        ).fetchone()
    return row["decision"] if row else None


def save_match_decision(canonical_id, store, product_url, product_name, decision, reason, country=COUNTRY, mode="confirm"):
    sql = (
        "INSERT INTO match_decisions (canonical_id, country_code, store, product_url, product_name, decision, reason, mode, decided_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (canonical_id, country_code, store, product_url, mode) DO UPDATE SET "
        "product_name = EXCLUDED.product_name, decision = EXCLUDED.decision, "
        "reason = EXCLUDED.reason, decided_at = now()"
    )
    with get_conn() as conn:
        conn.execute(sql, (canonical_id, country, store, product_url, product_name, decision, reason, mode))


def clear_store_rows(canonical_ids, country=COUNTRY):
    """Delete existing per-store rows for the given canonical_ids (used before a re-run)."""
    if not canonical_ids:
        return
    with get_conn() as conn:
        for table in STORE_TABLES.values():
            conn.execute(
                f"DELETE FROM {table} WHERE country_code = %s AND canonical_id = ANY(%s)",
                (country, list(canonical_ids)),
            )


def mark_scraped(canonical_id, country=COUNTRY, status="ok", error=None):
    sql = (
        "INSERT INTO scrape_runs (canonical_id, country_code, last_scraped_at, status, error) "
        "VALUES (%s, %s, now(), %s, %s) "
        "ON CONFLICT (canonical_id, country_code) DO UPDATE SET "
        "last_scraped_at = now(), status = EXCLUDED.status, error = EXCLUDED.error"
    )
    with get_conn() as conn:
        conn.execute(sql, (canonical_id, country, status, error))


def save_pending_job(canonical_id, snapshot_id, collection_id, country=COUNTRY):
    sql = (
        "INSERT INTO pending_jobs (canonical_id, country_code, snapshot_id, collection_id) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (canonical_id, country_code) DO UPDATE SET "
        "snapshot_id = EXCLUDED.snapshot_id, collection_id = EXCLUDED.collection_id, created_at = now()"
    )
    with get_conn() as conn:
        conn.execute(sql, (canonical_id, country, snapshot_id, collection_id))


def get_pending_job(canonical_id, country=COUNTRY):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT snapshot_id, collection_id FROM pending_jobs "
            "WHERE canonical_id = %s AND country_code = %s",
            (canonical_id, country),
        ).fetchone()
    return dict(row) if row else None


def clear_pending_job(canonical_id, country=COUNTRY):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_jobs WHERE canonical_id = %s AND country_code = %s", (canonical_id, country))


def pending_job_ids(country=COUNTRY):
    """Return {canonical_id: {'snapshot_id':..,'collection_id':..}} for recorded jobs."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT canonical_id, snapshot_id, collection_id FROM pending_jobs WHERE country_code = %s",
            (country,),
        ).fetchall()
    return {r["canonical_id"]: {"snapshot_id": r["snapshot_id"], "collection_id": r["collection_id"]} for r in rows}


JOB_HISTORY_KEEP = 5


def save_job_history(canonical_id, snapshot_id, collection_id, status=None, country=COUNTRY):
    """Record a Bright Data job build for a product, keeping the newest JOB_HISTORY_KEEP rows."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO job_history (canonical_id, country_code, snapshot_id, collection_id, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (canonical_id, country, snapshot_id, collection_id, status),
        )
        conn.execute(
            "DELETE FROM job_history WHERE canonical_id = %s AND country_code = %s AND created_at NOT IN ("
            "SELECT created_at FROM job_history WHERE canonical_id = %s AND country_code = %s "
            "ORDER BY created_at DESC LIMIT %s)",
            (canonical_id, country, canonical_id, country, JOB_HISTORY_KEEP),
        )


def set_job_status(canonical_id, snapshot_id, status, country=COUNTRY):
    """Update the status of the most recent job build matching the given snapshot."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_history SET status = %s "
            "WHERE canonical_id = %s AND country_code = %s AND snapshot_id = %s "
            "AND created_at = (SELECT MAX(created_at) FROM job_history "
            "WHERE canonical_id = %s AND country_code = %s AND snapshot_id = %s)",
            (status, canonical_id, country, snapshot_id, canonical_id, country, snapshot_id),
        )


def get_job_history(canonical_ids=None, country=COUNTRY):
    """Return job_history rows (newest first) for the given ids, or all if ids is None."""
    with get_conn() as conn:
        if canonical_ids:
            rows = conn.execute(
                "SELECT canonical_id, country_code, snapshot_id, collection_id, status, created_at "
                "FROM job_history WHERE country_code = %s AND canonical_id = ANY(%s) "
                "ORDER BY canonical_id, created_at DESC",
                (country, list(canonical_ids)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT canonical_id, country_code, snapshot_id, collection_id, status, created_at "
                "FROM job_history WHERE country_code = %s ORDER BY canonical_id, created_at DESC",
                (country,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_eligible(batch_size=5, country=COUNTRY, force=False, order_by="next"):
    """Return up to batch_size canonical products that have not been scraped in the last 24h.

    order_by:
      'next'  -> never-run first, then oldest last_scraped_at (default for forward progress)
      'recent'-> most recently scraped first (used by --repeat)
    """
    if force:
        eligible_sql = "TRUE"
    else:
        eligible_sql = "(sr.last_scraped_at IS NULL OR sr.last_scraped_at < now() - interval '24 hours')"
    if order_by == "recent":
        order_sql = "sr.last_scraped_at DESC NULLS LAST, c.canonical_id ASC"
    else:
        order_sql = "sr.last_scraped_at IS NULL DESC, COALESCE(sr.last_scraped_at, c.created_at) ASC, c.canonical_id ASC"
    sql = (
        "SELECT c.canonical_id, c.country_code, c.canonical_name, c.brand, c.product_name, "
        "c.size_value, c.size_unit, c.size_basis, c.pack_count, c.variant "
        "FROM canonical c LEFT JOIN scrape_runs sr "
        "ON sr.canonical_id = c.canonical_id AND sr.country_code = c.country_code "
        f"WHERE c.country_code = %s AND {eligible_sql} "
        f"ORDER BY {order_sql} LIMIT %s"
    )
    with get_conn() as conn:
        return conn.execute(sql, (country, batch_size)).fetchall()
