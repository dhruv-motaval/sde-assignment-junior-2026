"""Command line:  python -m solution ingest <drop_dir>   |   python -m solution report --out <dir>

Both commands accept --db (default: $DATABASE_URL or the docker-compose Postgres) and --schemas
(default: assignment-a-data/schemas). Output contains counts only, never PII.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .db import connect, init_schema
from .leads import load_schemas
from .pii import Pii, load_master_key
from .report import build_report, write_report
from .store import ingest_drop, load_dispositions

DEFAULT_SCHEMAS = Path(__file__).resolve().parents[1] / "assignment-a-data" / "schemas"


def _open(args):
    pii = Pii(load_master_key())
    conn = connect(args.db, schema=args.db_schema)
    init_schema(conn, pii)
    load_dispositions(conn, Path(args.schemas) / "dispositions.csv")
    return conn, pii


def cmd_ingest(args) -> int:
    conn, pii = _open(args)
    res = ingest_drop(conn, pii, load_schemas(args.schemas), args.drop_dir)
    for f in res["leads"]:
        print("leads : skipped (already ingested)" if f is None else
              f"leads : {f.workflow_id}/{f.batch_id}: rows={f.rows_total} new={f.new_leads} "
              f"merged={f.merged} rejected={f.rows_rejected} warnings={f.field_warnings}")
    for f in res["events"]:
        print("events: skipped (already ingested)" if f is None else
              f"events: lines={f.lines_read} stored={f.inserted} malformed={f.malformed} duplicates={f.dup_events}")
    return 0


def cmd_report(args) -> int:
    conn, _ = _open(args)
    for p in write_report(build_report(conn), args.out):
        print("wrote", p)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m solution", description=__doc__.split("\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="Postgres URL (default: $DATABASE_URL or docker-compose Postgres)")
    common.add_argument("--db-schema", help="use a separate Postgres schema (for tests)")
    common.add_argument("--schemas", default=str(DEFAULT_SCHEMAS), help="directory with workflow schemas + dispositions.csv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", parents=[common], help="ingest one drop (leads + events)")
    i.add_argument("drop_dir")
    i.set_defaults(fn=cmd_ingest)
    r = sub.add_parser("report", parents=[common], help="write the five report files")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)
