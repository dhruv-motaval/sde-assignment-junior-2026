"""Database tests. Each test gets its own throw-away Postgres schema; skipped if Postgres is down
(start it with `docker compose up -d`)."""
import uuid
from pathlib import Path

import psycopg
import pytest

from solution.db import PiiKeyMismatch, connect, init_schema
from solution.leads import LeadBook, discover_lead_files, ingest_file, load_schemas
from solution.pii import Pii
from solution.store import (
    FileContentChanged, _split, _text, ingest_lead_drop, ingest_lead_file,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = load_schemas(ROOT / "assignment-a-data" / "schemas")
MOTOR = SCHEMAS["motor_renewal"]
PII = Pii(b"test-master-key")
HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


@pytest.fixture
def db():
    name = f"t_{uuid.uuid4().hex[:12]}"
    try:
        conn = connect(schema=name)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable ({e.__class__.__name__}); run `docker compose up -d`")
    init_schema(conn, PII)
    yield conn
    conn.execute(f'DROP SCHEMA "{name}" CASCADE')
    conn.close()


def csv_file(tmp_path, name, text, wf="motor_renewal"):
    d = tmp_path / "drop_x" / "leads" / wf
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def ingest(db, p, drop="drop_x"):
    rel = p.relative_to(p.parents[2]).as_posix()
    return ingest_lead_file(db, PII, MOTOR, p, drop, rel)


def count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# --- schema ------------------------------------------------------------------

def test_init_is_idempotent(db):
    init_schema(db, PII)
    init_schema(db, PII)
    assert count(db, "leads") == 0


def test_wrong_key_is_detected(db):
    with pytest.raises(PiiKeyMismatch):
        init_schema(db, Pii(b"another-key"))


def test_ledger_is_append_only(db):
    db.execute("INSERT INTO events (event_id, event_type, occurred_at) VALUES ('e1', 'call.initiated', now())")
    for sql in ("UPDATE events SET event_type = 'x'", "DELETE FROM events", "TRUNCATE events"):
        with pytest.raises(psycopg.Error, match="append-only"):
            db.execute(sql)
    assert count(db, "events") == 1
    db.execute("INSERT INTO events (event_id, event_type, occurred_at) VALUES ('e2', 'call.ended', now())")
    assert count(db, "events") == 2  # inserts still fine


# --- PII at rest ---------------------------------------------------------------

def test_no_plaintext_pii_anywhere_in_the_database(db, tmp_path):
    p = csv_file(tmp_path, "batch_001.csv", HEADER + "Zorblax Quimby,+91 98765 43210,POL-777,,,\n"
                                                      "Bad Row,12345,POL-1,,,\n")
    ingest(db, p)
    dump = ""
    for (t,) in db.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall():
        dump += "\n".join(r[0] for r in db.execute(f'SELECT x::text FROM "{t}" x').fetchall())
    for secret in ("Zorblax", "Quimby", "9876543210", "98765 43210", "98765", "Bad Row"):
        assert secret not in dump, secret
    # what we DO keep is enough to mask and to match
    last4, = db.execute("SELECT phone_last4 FROM leads").fetchone()
    assert last4 == "3210"
    enc, = db.execute("SELECT pii->>'customer_name' FROM leads").fetchone()
    assert PII.decrypt(enc) == "Zorblax Quimby"
    assert "12345" not in dump                           # rejected rows leave no phone behind
    assert db.execute("SELECT reason FROM rejections").fetchall() == [("invalid_phone",)]


# --- merge rules in SQL --------------------------------------------------------

def test_merge_semantics_match_spec(db, tmp_path):
    f1 = csv_file(tmp_path, "batch_001.csv", HEADER + "Aarav,98765 43210,P1,01-Dec-2026,500,hi\n"
                                                      "Aarav K,+91 9876543210,P1B,,,\n")
    f3 = csv_file(tmp_path, "batch_003.csv", HEADER + "Aarav Z,09876543210,P9,15/01/2027,junk,\n")
    r1, r3 = ingest(db, f1), ingest(db, f3)
    assert (r1.new_leads, r1.merged) == (1, 1) and (r3.merged, r3.field_warnings) == (1, 1)
    batch, ref, attrs, enc = db.execute("SELECT batch_id, external_ref, attributes, pii FROM leads").fetchone()
    assert batch == "batch_001"                          # permanent
    assert ref == "P9"
    assert attrs == {"policy_expiry": "2027-01-15", "premium_amount": "500.00", "preferred_language": "hi"}
    assert PII.decrypt(enc["customer_name"]) == "Aarav Z"


def test_default_applies_to_new_lead_only_when_null(db, tmp_path):
    p = csv_file(tmp_path, "batch_001.csv", HEADER + "A,9876543210,P1,,,\n")
    ingest(db, p)
    assert db.execute("SELECT attributes->>'preferred_language' FROM leads").fetchone()[0] == "en"


def test_same_phone_in_two_workflows_is_two_leads(db, tmp_path):
    ingest(db, csv_file(tmp_path, "batch_001.csv", HEADER + "A,9876543210,P1,,,\n"))
    fb = SCHEMAS["policy_feedback"]
    p = csv_file(tmp_path, "batch_001.csv", "customer_name,phone,order_id\nA,9876543210,O1\n", wf="policy_feedback")
    ingest_lead_file(db, PII, fb, p, "drop_x", "leads/policy_feedback/batch_001.csv")
    assert count(db, "leads") == 2


# --- idempotency / atomicity -----------------------------------------------------

def test_reingest_same_file_changes_nothing(db, tmp_path):
    p = csv_file(tmp_path, "batch_001.csv", HEADER + "A,9876543210,P1,,,\nB,111,P2,,,\nC,9876543210,P3,,,\n")
    first = ingest(db, p)
    snap = [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("leads", "rejections", "ingested_files")]
    assert ingest(db, p) is None
    assert [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("leads", "rejections", "ingested_files")] == snap
    assert snap == [1, 1, 1] and (first.new_leads, first.merged, first.rows_rejected) == (1, 1, 1)


def test_same_path_different_content_is_refused(db, tmp_path):
    p = csv_file(tmp_path, "batch_001.csv", HEADER + "A,9876543210,P1,,,\n")
    ingest(db, p)
    p.write_text(HEADER + "A,9876543210,P1,,,\nB,9876543211,P2,,,\n", encoding="utf-8")
    with pytest.raises(FileContentChanged):
        ingest(db, p)
    assert count(db, "leads") == 1


def test_failure_mid_file_rolls_back_the_whole_file(db, tmp_path, monkeypatch):
    p = csv_file(tmp_path, "batch_001.csv", HEADER + "A,9876543210,P1,,,\nB,9876543211,P2,,,\nC,9876543212,P3,,,\n")
    import solution.store as store
    real, calls = store.upsert_lead, []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("crash")
        return real(*a, **k)

    monkeypatch.setattr(store, "upsert_lead", boom)
    with pytest.raises(RuntimeError):
        ingest(db, p)
    assert (count(db, "leads"), count(db, "ingested_files")) == (0, 0)
    monkeypatch.setattr(store, "upsert_lead", real)
    assert ingest(db, p).new_leads == 3                   # a clean retry works


# --- oracle: DB must equal the in-memory LeadBook on the real data ----------------

DATA = ROOT / "data"


@pytest.mark.skipif(not DATA.exists(), reason="run generate_data.py first")
def test_database_matches_leadbook_on_real_data_and_reingest_is_noop(db):
    book, tot = LeadBook(), dict(rows=0, rej=0, merged=0, new=0, warn=0)
    for drop in ("drop_1", "drop_2"):
        for wf, p in discover_lead_files(DATA / drop, SCHEMAS):
            r = ingest_file(book, SCHEMAS[wf], p)
            tot["rows"] += r.rows_total; tot["rej"] += r.rows_rejected; tot["merged"] += r.merged
            tot["new"] += r.new_leads; tot["warn"] += r.field_warnings
        ingest_lead_drop(db, PII, SCHEMAS, DATA / drop)

    assert db.execute("SELECT sum(rows_total), sum(rows_rejected), sum(rows_merged), sum(new_leads), "
                      "sum(field_warnings) FROM ingested_files").fetchone() == tuple(tot.values())
    assert count(db, "leads") == len(book.leads) == tot["new"]
    assert count(db, "rejections") == tot["rej"]

    for (wf, phone), lead in book.leads.items():
        row = db.execute("SELECT batch_id, external_ref, attributes, pii FROM leads "
                         "WHERE workflow_id = %s AND phone_hash = %s", (wf, PII.phone_hash(phone))).fetchone()
        assert row is not None, phone
        enc, ref, attrs = _split(SCHEMAS[wf], lead.values, PII)
        assert (row[0], row[1], row[2]) == (lead.batch_id, ref, attrs)
        assert PII.decrypt(row[3]["customer_name"]) == lead.values["customer_name"]

    before = db.execute("SELECT count(*), md5(string_agg(x::text, ',' ORDER BY lead_id)) FROM leads x").fetchone()
    for drop in ("drop_1", "drop_2"):
        results = ingest_lead_drop(db, PII, SCHEMAS, DATA / drop)
        assert results and all(r is None for r in results)
    assert db.execute("SELECT count(*), md5(string_agg(x::text, ',' ORDER BY lead_id)) FROM leads x").fetchone() == before
