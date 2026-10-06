import sqlite3

DB_PATH = "twstock.db"


def get_conn(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table(conn: sqlite3.Connection, table: str, cols: list[str], key_cols: list[str]) -> None:
    col_defs = ",".join(f'"{c}" TEXT' for c in cols)
    pk = ",".join(f'"{k}"' for k in key_cols)
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs}, PRIMARY KEY ({pk}))')

    # A table can receive rows from more than one source with slightly
    # different fields (e.g. TWSE vs TPEx feeding the same stock_price
    # table) - add any column the incoming batch has that the table
    # doesn't yet, rather than erroring on INSERT.
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    for c in cols:
        if c not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" TEXT')


def upsert_records(conn: sqlite3.Connection, table: str, records: list[dict], key_cols: list[str]) -> int:
    """Insert or update rows keyed by key_cols. All columns stored as TEXT;
    cast with CAST(col AS REAL)/CAST(col AS INTEGER) when querying numerics."""
    if not records:
        return 0

    cols = list(records[0].keys())
    _ensure_table(conn, table, cols, key_cols)

    placeholders = ",".join(["?"] * len(cols))
    quoted_cols = ",".join(f'"{c}"' for c in cols)
    update_cols = [c for c in cols if c not in key_cols]
    conflict_keys = ",".join(f'"{k}"' for k in key_cols)

    if update_cols:
        update_clause = ",".join(f'"{c}"=excluded."{c}"' for c in update_cols)
        sql = (
            f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders}) '
            f'ON CONFLICT({conflict_keys}) DO UPDATE SET {update_clause}'
        )
    else:
        sql = f'INSERT OR REPLACE INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'

    rows = [tuple(str(r.get(c, "")) for c in cols) for r in records]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def ensure_views(conn: sqlite3.Connection) -> None:
    """(Re)create helper views. Safe to call every run."""
    conn.execute('DROP VIEW IF EXISTS industry_map')
    conn.execute(
        '''
        CREATE VIEW industry_map AS
        SELECT "公司代號", "產業別"
        FROM (
            SELECT "公司代號", "產業別", "資料年月",
                   ROW_NUMBER() OVER (
                       PARTITION BY "公司代號" ORDER BY "資料年月" DESC
                   ) AS rn
            FROM monthly_revenue
        )
        WHERE rn = 1
        '''
    )
    conn.commit()
