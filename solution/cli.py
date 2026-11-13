"""Command line:  python -m solution ingest <drop_dir>  |  report --out <dir>  |  reset --yes

All commands accept --db (default: $DATABASE_URL or the docker-compose Postgres), --wait (seconds to
wait for Postgres to come up, default 30) and --schemas (default: assignment-a-data/schemas).
Output contains counts only, never PII. Expected problems end with a one-line `error: ...` and a
non-zero exit code (2 = wrong usage, 1 = anything else), never a traceback.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg

from .db import PiiKeyMismatch, connect, init_schema, reset_schema
from .leads import load_schemas
from .pii import Pii, load_master_key
from .report import build_report, write_report
from .store import FileContentChanged, ingest_drop, load_dispositions

DEFAULT_SCHEMAS = Path(__file__).resolve().parents[1] / "assignment-a-data" / "schemas"


class CliError(Exception):
    """A usage problem the user can fix; reported as one line, exit code 2."""


def _connect(args) -> psycopg.Connection:
    """Connect, waiting up to --wait seconds: `docker compose up -d` returns before Postgres accepts connections."""
    deadline = time.monotonic() + args.wait
    told = False
    while True:
        try:
            return connect(args.db, schema=args.db_schema)
        except psycopg.OperationalError as e:
            if time.monotonic() >= deadline:
                reason = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
                raise CliError(f"cannot reach PostgreSQL ({reason}). Start it with `docker compose up -d`, "
                               "or set --db / $DATABASE_URL.") from e
            if not told:
                print("waiting for PostgreSQL ...", file=sys.stderr)
                told = True
            time.sleep(1)


def _open(args):
    pii = Pii(load_master_key())
    conn = _connect(args)
    init_schema(conn, pii)
    load_dispositions(conn, Path(args.schemas) / "dispositions.csv")
    return conn, pii


def _check_drop(path: str) -> Path:
    p = Path(path)
    if not p.is_dir():
        raise CliError(f"drop directory not found: {path}")
    if not (p / "leads").is_dir() and not (p / "events").is_dir():
        drops = sorted(c.name for c in p.iterdir() if c.is_dir() and ((c / "leads").is_dir() or (c / "events").is_dir()))
        hint = f" Did you mean one of: {', '.join(str(p / d) for d in drops)}?" if drops else ""
        raise CliError(f"{path} is not a drop (it has no leads/ or events/ folder).{hint}")
    return p


def cmd_ingest(args) -> int:
    drop = _check_drop(args.drop_dir)           # before touching the database
    conn, pii = _open(args)
    res = ingest_drop(conn, pii, load_schemas(args.schemas), drop)
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


def cmd_reset(args) -> int:
    if not args.yes:
        print("reset deletes ALL ingested data (leads, events, rejections, file records) from the database. "
              "Re-run with --yes to confirm. The PII key file is not touched.")
        return 2
    reset_schema(_connect(args))
    print("reset: all pipeline tables dropped; the next ingest starts from an empty database")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m solution", description=__doc__.split("\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="Postgres URL (default: $DATABASE_URL or docker-compose Postgres)")
    common.add_argument("--db-schema", help="use a separate Postgres schema (for tests)")
    common.add_argument("--schemas", default=str(DEFAULT_SCHEMAS), help="directory with workflow schemas + dispositions.csv")
    common.add_argument("--wait", type=float, default=30, help="seconds to wait for Postgres to accept connections")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", parents=[common], help="ingest one drop (leads + events)")
    i.add_argument("drop_dir")
    i.set_defaults(fn=cmd_ingest)
    r = sub.add_parser("report", parents=[common], help="write the five report files")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_report)
    x = sub.add_parser("reset", parents=[common], help="delete all ingested data (needs --yes)")
    x.add_argument("--yes", action="store_true", help="confirm the deletion")
    x.set_defaults(fn=cmd_reset)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (PiiKeyMismatch, FileContentChanged) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
