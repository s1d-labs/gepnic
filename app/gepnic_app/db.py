from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool = None
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=config.DB_POOL_MAX, kwargs={"row_factory": dict_row}, open=True)
    return _pool


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


def init_schema():
    """Apply schema.sql once per version (serialised across workers; a deadlock with live traffic is retried)."""
    import hashlib
    import time
    import psycopg
    sql = SCHEMA.read_text()
    digest = hashlib.sha256(sql.encode()).hexdigest()
    with conn() as c:     # already applied (e.g. by the owner at deploy time): no DDL, so a role without CREATE rights starts fine
        if c.execute("SELECT to_regclass('gepnic_schema_applied') AS t").fetchone()["t"] and \
                c.execute("SELECT 1 FROM gepnic_schema_applied WHERE digest=%s", (digest,)).fetchone():
            return
    for attempt in range(8):
        try:
            with conn() as c:
                c.execute("SELECT pg_advisory_xact_lock(748220)")
                c.execute("CREATE TABLE IF NOT EXISTS gepnic_schema_applied (digest text PRIMARY KEY, at timestamptz NOT NULL DEFAULT now())")
                if c.execute("SELECT 1 FROM gepnic_schema_applied WHERE digest=%s", (digest,)).fetchone():
                    return
                c.execute("SET LOCAL lock_timeout = '10s'")
                c.execute(sql)
                c.execute("INSERT INTO gepnic_schema_applied (digest) VALUES (%s) ON CONFLICT DO NOTHING", (digest,))
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable):
            time.sleep(1 + attempt)
    raise RuntimeError("could not apply schema.sql")
