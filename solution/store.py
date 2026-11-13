"""Persist leads: the database version of LeadBook (same rules, now in SQL)."""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .leads import FileResult, Schema, apply_defaults, discover_lead_files, evaluate_row, read_rows
from .pii import Pii


class FileContentChanged(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _text(v: object) -> str:
    return v.isoformat() if isinstance(v, date) else str(v)  # Decimal -> '1000.00'


def _split(schema: Schema, values: dict[str, object], pii: Pii):
    """values -> (encrypted PII dict, external_ref, plain attributes dict); None values are dropped."""
    phone, ref = schema.phone_field.name, schema.reference_field.name
    enc, attrs = {}, {}
    for k, v in values.items():
        if v is None or k == phone:
            continue
        f = next(x for x in schema.fields if x.name == k)
        if f.pii:
            enc[k] = pii.encrypt(str(v))
        elif k != ref:
            attrs[k] = _text(v)
    ref_val = values.get(ref)
    return enc, (None if ref_val is None else str(ref_val)), attrs


def upsert_lead(conn: psycopg.Connection, pii: Pii, schema: Schema, batch_id: str,
                values: dict[str, object]) -> bool:
    """True if a new lead was created, False if merged into an existing one.

    New lead: defaults fill still-null fields. Existing lead: only non-null incoming values
    overwrite (jsonb `||` replaces matching keys; None values were dropped by _split), so a
    later null never erases data.
    """
    phone = values[schema.phone_field.name]
    h = pii.phone_hash(phone)
    enc, ref, attrs = _split(schema, apply_defaults(schema, values), pii)
    created = conn.execute(
        """INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref, pii, attributes)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (workflow_id, phone_hash) DO NOTHING RETURNING lead_id""",
        (schema.workflow_id, h, pii.last4(phone), batch_id, ref, Jsonb(enc), Jsonb(attrs))).fetchone()
    if created:
        return True
    enc, ref, attrs = _split(schema, values, pii)  # raw values: no defaults on a merge
    conn.execute(
        """UPDATE leads SET external_ref = COALESCE(%s, external_ref),
                            pii = pii || %s, attributes = attributes || %s
           WHERE workflow_id = %s AND phone_hash = %s""",
        (ref, Jsonb(enc), Jsonb(attrs), schema.workflow_id, h))
    return False


def ingest_lead_file(conn: psycopg.Connection, pii: Pii, schema: Schema, path: Path,
                     drop_name: str, rel_path: str) -> FileResult | None:
    """Ingest one CSV atomically. Returns None if this exact file was already ingested.

    Raises FileContentChanged if the same (drop, path) is seen again with different content.
    """
    path = Path(path)
    digest = sha256_file(path)
    row = conn.execute("SELECT sha256 FROM ingested_files WHERE drop_name = %s AND path = %s",
                       (drop_name, rel_path)).fetchone()
    if row:
        if row[0] != digest:
            raise FileContentChanged(f"{drop_name}/{rel_path} was ingested with different content; "
                                     "start from an empty database to re-ingest changed data")
        return None
    res = FileResult(schema.workflow_id, path.stem)
    with conn.transaction():  # all rows + the ingested_files row commit together, or none do
        for n, raw in read_rows(path, schema):
            res.rows_total += 1
            reason, values, warn = evaluate_row(schema, raw)
            if reason:
                res.rejections.append((n, reason))
                conn.execute("INSERT INTO rejections VALUES (%s, %s, %s, %s)",
                             (res.workflow_id, res.batch_id, n, reason))
                continue
            res.field_warnings += warn
            if upsert_lead(conn, pii, schema, res.batch_id, values):
                res.new_leads += 1
            else:
                res.merged += 1
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
