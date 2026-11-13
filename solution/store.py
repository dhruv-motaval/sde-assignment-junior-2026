"""Persist leads (the database version of LeadBook) and events (the append-only ledger)."""
from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg

from .events import parse_event_line, payload_text
from .leads import FileResult, Schema, apply_defaults, discover_lead_files, evaluate_row, read_rows
from .pii import Pii

CHUNK = 5000  # events per dedupe query / COPY batch


class FileContentChanged(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_ingested(conn: psycopg.Connection, drop_name: str, rel_path: str, digest: str) -> bool:
    """True if this exact file was ingested before. Same path with different content is refused."""
    row = conn.execute("SELECT sha256 FROM ingested_files WHERE drop_name = %s AND path = %s",
                       (drop_name, rel_path)).fetchone()
    if row and row[0] != digest:
        raise FileContentChanged(f"{drop_name}/{rel_path} was ingested with different content; "
                                 "start from an empty database to re-ingest changed data")
    return row is not None


def _text(v: object) -> str:
    return v.isoformat() if isinstance(v, date) else str(v)  # Decimal -> '1000.00'


@functools.lru_cache(maxsize=None)
def _field_map(schema: Schema) -> dict:
    return {f.name: f for f in schema.fields}


def _split(schema: Schema, values: dict[str, object], pii: Pii):
    """values -> (encrypted PII dict, external_ref, plain attributes dict); None values are dropped."""
    phone, ref = schema.phone_field.name, schema.reference_field.name
    fields = _field_map(schema)
    enc, attrs = {}, {}
    for k, v in values.items():
        if v is None or k == phone:
            continue
        if fields[k].pii:
            enc[k] = pii.encrypt(str(v))
        elif k != ref:
            attrs[k] = _text(v)
    ref_val = values.get(ref)
    return enc, (None if ref_val is None else str(ref_val)), attrs


_STAGE_DDL = """CREATE TEMP TABLE _stage (
    seq int, workflow_id text, phone_hash bytea, phone_last4 char(4), batch_id text, external_ref text,
    pii_new jsonb, attrs_new jsonb, pii_upd jsonb, attrs_upd jsonb) ON COMMIT DROP"""


def _collapse(schema: Schema, pii: Pii, rows: dict[bytes, dict], batch_id: str) -> list[tuple]:
    """One staged row per distinct phone in the file, merging repeated phones in file order
    (first row creates, later rows overwrite only non-null values), like LeadBook does.

    `*_new` is what a brand-new lead gets (defaults fill still-null fields); `*_upd` is what an
    EXISTING lead receives (only the non-null incoming values, never defaults, never nulls).
    """
    out = []
    for seq, (h, raw) in enumerate(rows.items()):
        enc, ref, attrs = _split(schema, apply_defaults(schema, raw), pii)   # PII is encrypted once per lead
        enc_upd = {k: v for k, v in enc.items() if raw.get(k) is not None}
        attrs_upd = {k: v for k, v in attrs.items() if raw.get(k) is not None}
        phone = raw[schema.phone_field.name]
        out.append((seq, schema.workflow_id, h, phone[-4:], batch_id, ref,
                    json.dumps(enc), json.dumps(attrs), json.dumps(enc_upd), json.dumps(attrs_upd)))
    return out


def _merge_stage(conn: psycopg.Connection) -> int:
    """Apply the staged rows; returns how many NEW leads were created.

    Existing leads: update with the non-null incoming values (jsonb `||` replaces matching keys).
    Missing leads: insert in file order. Update-then-insert means each lead is touched once.
    """
    conn.execute(
        """UPDATE leads l SET external_ref = COALESCE(s.external_ref, l.external_ref),
                              pii = l.pii || s.pii_upd, attributes = l.attributes || s.attrs_upd
           FROM _stage s WHERE l.workflow_id = s.workflow_id AND l.phone_hash = s.phone_hash""")
    return conn.execute(
        """INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref, pii, attributes)
           SELECT s.workflow_id, s.phone_hash, s.phone_last4, s.batch_id, s.external_ref, s.pii_new, s.attrs_new
           FROM _stage s
           WHERE NOT EXISTS (SELECT 1 FROM leads l WHERE l.workflow_id = s.workflow_id AND l.phone_hash = s.phone_hash)
           ORDER BY s.seq""").rowcount


def ingest_lead_file(conn: psycopg.Connection, pii: Pii, schema: Schema, path: Path,
                     drop_name: str, rel_path: str) -> FileResult | None:
    """Ingest one CSV atomically. Returns None if this exact file was already ingested.

    Raises FileContentChanged if the same (drop, path) is seen again with different content.
    Parsing and merging repeated phones happen in memory first (no database traffic per row);
    then ONE transaction bulk-loads a staging table and merges it into `leads`.
    """
    path = Path(path)
    digest = sha256_file(path)
    if _already_ingested(conn, drop_name, rel_path, digest):
        return None
    res = FileResult(schema.workflow_id, path.stem)
    phone_name = schema.phone_field.name
    rows: dict[bytes, dict] = {}
    valid = 0
    for n, raw in read_rows(path, schema):
        res.rows_total += 1
        reason, values, warn = evaluate_row(schema, raw)
        if reason:
            res.rejections.append((n, reason))
            continue
        res.field_warnings += warn
        valid += 1
        h = pii.phone_hash(values[phone_name])
        if h not in rows:
            rows[h] = dict(values)
        else:
            rows[h].update({k: v for k, v in values.items() if v is not None})
    staged = _collapse(schema, pii, rows, res.batch_id)

    with conn.transaction():  # rejections + leads + the ingested_files row commit together, or none do
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO rejections (workflow_id, batch_id, row_number, reason, drop_name, path) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            [(res.workflow_id, res.batch_id, n, r, drop_name, rel_path) for n, r in res.rejections])
            cur.execute(_STAGE_DDL)
            with cur.copy("COPY _stage FROM STDIN") as cp:
                for row in staged:
                    cp.write_row(row)
        res.new_leads = _merge_stage(conn)
        res.merged = valid - res.new_leads   # every valid row that did not create a lead merged into one
        conn.execute(
            """INSERT INTO ingested_files (drop_name, path, kind, sha256, workflow_id, batch_id,
                   rows_total, rows_rejected, rows_merged, new_leads, field_warnings)
               VALUES (%s, %s, 'leads', %s, %s, %s, %s, %s, %s, %s, %s)""",
            (drop_name, rel_path, digest, res.workflow_id, res.batch_id, res.rows_total,
             res.rows_rejected, res.merged, res.new_leads, res.field_warnings))
    return res


def ingest_lead_drop(conn: psycopg.Connection, pii: Pii, schemas: dict[str, Schema],
                     drop_dir: str | Path) -> list[FileResult | None]:
    drop_dir = Path(drop_dir)
    return [ingest_lead_file(conn, pii, schemas[wf], p, drop_dir.name, p.relative_to(drop_dir).as_posix())
            for wf, p in discover_lead_files(drop_dir, schemas)]


# --- events ------------------------------------------------------------------

@dataclass
class EventFileResult:
    lines_read: int = 0   # non-empty lines, incl. malformed and duplicate ones
    malformed: int = 0
    dup_events: int = 0   # event_id already seen: earlier in this file, or in any earlier file/drop
    inserted: int = 0


_EVENT_COLS = ("event_id, event_type, workflow_id, call_id, occurred_at, phone_hash, payload, "
               "source_file, line_no")


def _flush_events(conn: psycopg.Connection, pii: Pii, chunk: list, source: str, res: EventFileResult) -> None:
    """Drop ids the ledger already has, then COPY the rest. `chunk` has no ids repeated within itself."""
    if not chunk:
        return
    known = {r[0] for r in conn.execute("SELECT event_id FROM events WHERE event_id = ANY(%s)",
                                        ([ev.event_id for ev, _ in chunk],))}
    res.dup_events += len(known)
    new = [(ev, n) for ev, n in chunk if ev.event_id not in known]
    with conn.cursor() as cur, cur.copy(f"COPY events ({_EVENT_COLS}) FROM STDIN") as cp:
        for ev, n in new:
            cp.write_row((ev.event_id, ev.event_type, ev.workflow_id, ev.call_id, ev.occurred_at,
                          pii.phone_hash(ev.phone) if ev.phone else None,
                          payload_text(ev.payload), source, n))
    res.inserted += len(new)
    chunk.clear()


def ingest_event_file(conn: psycopg.Connection, pii: Pii, path: Path, drop_name: str,
                      rel_path: str) -> EventFileResult | None:
    """Ingest one JSONL file atomically into the ledger. None if already ingested.

    Per line, in spec order: malformed -> skip+count; duplicate event_id -> skip+count; otherwise
    store. Orphan / unknown-disposition are NOT decided here (a lead may arrive in a later drop);
    they are derived at report time from what is in the ledger.
    """
    path = Path(path)
    digest = sha256_file(path)
    if _already_ingested(conn, drop_name, rel_path, digest):
        return None
    res, seen, chunk = EventFileResult(), set(), []
    source = f"{drop_name}/{rel_path}"
    with conn.transaction():
        # utf-8-sig drops a BOM (else the first event would be lost); errors="replace": a stray bad byte
        # makes that one line malformed instead of crashing the whole file
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                res.lines_read += 1
                ev = parse_event_line(line)
                if ev is None:
                    res.malformed += 1
                    conn.execute("INSERT INTO malformed_lines VALUES (%s, %s, %s)", (drop_name, rel_path, line_no))
                    continue
                if ev.event_id in seen:
                    res.dup_events += 1
                    continue
                seen.add(ev.event_id)
                chunk.append((ev, line_no))
                if len(chunk) >= CHUNK:
                    _flush_events(conn, pii, chunk, source, res)
        _flush_events(conn, pii, chunk, source, res)
        conn.execute(
            """INSERT INTO ingested_files (drop_name, path, kind, sha256, lines_read, malformed, dup_events)
               VALUES (%s, %s, 'events', %s, %s, %s, %s)""",
            (drop_name, rel_path, digest, res.lines_read, res.malformed, res.dup_events))
    return res


def ingest_event_drop(conn: psycopg.Connection, pii: Pii, drop_dir: str | Path) -> list[EventFileResult | None]:
    drop_dir = Path(drop_dir)
    files = sorted((p for p in (drop_dir / "events").glob("*") if p.suffix.lower() == ".jsonl"), key=lambda p: p.name)
    return [ingest_event_file(conn, pii, p, drop_dir.name, p.relative_to(drop_dir).as_posix()) for p in files]


def ingest_drop(conn: psycopg.Connection, pii: Pii, schemas: dict[str, Schema], drop_dir: str | Path) -> dict:
    """Leads first, then events (order does not affect results: linking happens at report time)."""
    return {"leads": ingest_lead_drop(conn, pii, schemas, drop_dir),
            "events": ingest_event_drop(conn, pii, drop_dir)}


def load_dispositions(conn: psycopg.Connection, csv_path: str | Path) -> int:
    """Upsert the disposition catalogue. Safe to run every time (idempotent)."""
    import csv
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = [(r["workflow_id"].strip(), r["code"].strip(),
                 r["is_terminal"].strip().lower() == "true", r["is_success"].strip().lower() == "true")
                for r in csv.DictReader(fh)]
    with conn.transaction():
        for r in rows:
            conn.execute("""INSERT INTO dispositions VALUES (%s, %s, %s, %s)
                            ON CONFLICT (workflow_id, code) DO UPDATE
                            SET is_terminal = EXCLUDED.is_terminal, is_success = EXCLUDED.is_success""", r)
    return len(rows)
