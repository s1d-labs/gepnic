-- GePNIC's own tables and indexes in the scrape_hub database (taken out of the hub's schema, 2026-10-25). Applied at the app's
-- start, once per version of this file. Only CREATE ... IF NOT EXISTS: never drops or rewrites anything.
-- The hub's own tables (projects, queue, records, runs, batches, metrics, probes, adjustments, failures, sizes, upload_ledger,
-- bucket_usage) belong to the hub; this app reads them and writes records (patch-records) and batches/runs counters.

-- Lets a runner ask "how many tenders of this organisation and window are already stored?" without scanning every record.
CREATE INDEX IF NOT EXISTS records_org_window ON records (project, (data->>'org_id'), (data->'window'->>0), (data->'window'->>1));

-- How much load each portal server takes right now: scripts/ramp.py raises parallel workers (1, 4, 8, 16) for a few seconds each and
-- stops at the first step where the server slows down. One row per server per ramp; `steps` holds every step measured.
CREATE TABLE IF NOT EXISTS capacity (
    server_ip  TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    portals    TEXT[] NOT NULL DEFAULT '{}',
    safe       INT NOT NULL DEFAULT 0,          -- highest worker count that did not slow the server (0: not answering)
    tested_max INT NOT NULL DEFAULT 0,          -- highest worker count tried (safe = tested_max means the limit is above it)
    base_ms    INT,                             -- median answer time with one worker
    steps      JSONB NOT NULL DEFAULT '[]',
    note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS capacity_server_at ON capacity (server_ip, at);

-- Precomputed numbers for the data page (scripts/build_showcase.py), so the page never scans the records.
CREATE TABLE IF NOT EXISTS showcase (
    project  TEXT PRIMARY KEY,
    built_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    data     JSONB NOT NULL
);


-- records saved without their documents (being redone), counted per project by the overview. On a big live table, build it by hand
-- first with CREATE INDEX CONCURRENTLY (done 2026-10-20) so this line is a no-op.
CREATE INDEX IF NOT EXISTS records_missing_docs ON records (project) WHERE coalesce(data->'details'->'documents', '[]'::jsonb) @> '[{"status": "not_stored"}]'::jsonb;

-- tender bundles (scrape_hub/bundles.py): a zip of chosen tenders, built by scripts/bundler.py, downloadable at /bundle/<slug>.zip with its
-- own password, which opens that zip and nothing else
CREATE TABLE IF NOT EXISTS bundles (
    slug        TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',          -- private, for us
    public_note TEXT NOT NULL DEFAULT '',          -- shown on the download page and in the zip
    tenders     JSONB NOT NULL DEFAULT '[]',       -- ["2024_PWD_1_1", "gepnic-mahatenders:2024_PWD_2_1", ...]
    include     JSONB NOT NULL DEFAULT '{"pages": true, "documents": true, "record": true}',
    password    TEXT NOT NULL DEFAULT '',          -- scrypt$salt$hash; empty: the public link stays closed
    public      BOOLEAN NOT NULL DEFAULT true,     -- the public link can be switched off without losing the password
    status      TEXT NOT NULL DEFAULT 'queued',    -- queued, building, ready, failed
    progress    JSONB NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT '',
    object_key  TEXT,
    bytes       BIGINT,
    sha256      TEXT,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    built_at    TIMESTAMPTZ,
    downloads   INT NOT NULL DEFAULT 0,
    last_download_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS bundle_attempts (    -- password tries on the public page, for the lockout
    slug TEXT NOT NULL,
    ip   TEXT NOT NULL,
    ok   BOOLEAN NOT NULL,
    at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bundle_attempts_slug_at ON bundle_attempts (slug, at);

-- Records per project (and per project those still waiting for documents), refreshed by one hub worker in the background
-- (scrapes.refresh_counts). Counting 16M+ records takes 20 s or more, so requests read this table instead of counting.
CREATE TABLE IF NOT EXISTS record_counts (
    project text PRIMARY KEY,
    n bigint NOT NULL,
    missing_docs bigint,
    at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- team + runner pool (2026-10-25)
