"""PostgreSQL connection and schema. Everything is created with IF NOT EXISTS, so init is idempotent."""
from __future__ import annotations

import os

import psycopg

from .pii import Pii

DEFAULT_URL = "postgresql://au:au_local_only@localhost:5432/au_assignment"  # docker-compose.yml


class PiiKeyMismatch(RuntimeError):
    pass


DDL = """
-- which input files were fully ingested, with their counters. A file is processed in ONE
-- transaction together with its row in this table, so "listed here" == "fully applied".
CREATE TABLE IF NOT EXISTS ingested_files (
    drop_name     text   NOT NULL,
    path          text   NOT NULL,               -- relative to the drop, e.g. leads/motor_renewal/batch_001.csv
    kind          text   NOT NULL CHECK (kind IN ('leads', 'events')),
    sha256        text   NOT NULL,
    ingest_seq    bigserial,                     -- order files were ingested in
    workflow_id   text,
    batch_id      text,
    rows_total    int    NOT NULL DEFAULT 0,     -- leads: non-blank data rows
    rows_rejected int    NOT NULL DEFAULT 0,
    rows_merged   int    NOT NULL DEFAULT 0,
    new_leads     int    NOT NULL DEFAULT 0,
    field_warnings int   NOT NULL DEFAULT 0,
    lines_read    int    NOT NULL DEFAULT 0,     -- events: non-empty lines
    malformed     int    NOT NULL DEFAULT 0,
    dup_events    int    NOT NULL DEFAULT 0,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (drop_name, path)
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id     bigserial PRIMARY KEY,
    workflow_id text   NOT NULL,
    phone_hash  bytea  NOT NULL,                 -- HMAC-SHA256 of the canonical phone
    phone_last4 char(4) NOT NULL CHECK (phone_last4 ~ '^[0-9]{4}$'),
    batch_id    text   NOT NULL,                 -- batch that first created the lead; never changes
    external_ref text,                           -- the schema field marked "reference"
    pii         jsonb  NOT NULL DEFAULT '{}',    -- {field: Fernet token}; no plaintext
    attributes  jsonb  NOT NULL DEFAULT '{}',    -- other non-PII fields (dates / amounts as text)
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS rejections (          -- no PII: reason + position only
    workflow_id text NOT NULL,
    batch_id    text NOT NULL,
    row_number  int  NOT NULL,
    reason      text NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- the interaction ledger: insert-only (enforced by triggers below)
CREATE TABLE IF NOT EXISTS events (
    event_id    text PRIMARY KEY,
    event_type  text NOT NULL,
    workflow_id text,
    call_id     text,
    occurred_at timestamptz NOT NULL,            -- normalised to UTC; naive input was IST
    phone_hash  bytea,                           -- NULL for disposition.override (no phone)
    payload     jsonb NOT NULL DEFAULT '{}',
    source_file text,
    line_no     int,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

-- malformed event lines: WHERE they are, never WHAT they say (a truncated line may hold a phone number)
CREATE TABLE IF NOT EXISTS malformed_lines (
    drop_name text NOT NULL,
    path      text NOT NULL,
    line_no   int  NOT NULL,
    PRIMARY KEY (drop_name, path, line_no)
);

-- disposition catalogue (from schemas/dispositions.csv); reloaded idempotently on every ingest/report
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id text NOT NULL,
    code        text NOT NULL,
    is_terminal boolean NOT NULL,
    is_success  boolean NOT NULL,
    PRIMARY KEY (workflow_id, code)
);

CREATE TABLE IF NOT EXISTS meta (key text PRIMARY KEY, value text NOT NULL);

CREATE OR REPLACE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME;
END $$;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
DROP TRIGGER IF EXISTS events_no_truncate ON events;
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
"""


def connect(url: str | None = None, schema: str | None = None) -> psycopg.Connection:
    """Autocommit connection; use `with conn.transaction():` for atomic blocks.

    `schema` (used by tests) puts all tables in their own Postgres schema so tests never touch real data.
    """
    conn = psycopg.connect(url or os.environ.get("DATABASE_URL", DEFAULT_URL), autocommit=True)
    if schema:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
    return conn


def init_schema(conn: psycopg.Connection, pii: Pii) -> None:
    """Create tables (idempotent) and verify the DB was built with this PII key."""
    with conn.transaction():
        conn.execute(DDL)
        row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_check'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta VALUES ('pii_key_check', %s)", (pii.key_fingerprint,))
        elif row[0] != pii.key_fingerprint:
            raise PiiKeyMismatch(
                "this database was created with a different PII key; phone hashes would not match. "
                "Use the original key (AU_PII_KEY or solution/.pii_key) or start from an empty database.")


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop every table this pipeline owns (in the connection's schema) so a new seed starts clean.

    Dropping is allowed on the append-only ledger: the triggers only guard UPDATE / DELETE /
    TRUNCATE of rows. The next `ingest` recreates everything via init_schema.
    """
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS events, leads, rejections, ingested_files, malformed_lines, "
                     "dispositions, meta CASCADE")
        conn.execute("DROP FUNCTION IF EXISTS forbid_change()")
