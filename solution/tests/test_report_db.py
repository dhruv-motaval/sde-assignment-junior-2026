"""Reports end to end against Postgres: late leads, re-runs, load-order independence, CLI, and an
independent re-implementation over the raw files as an oracle."""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from solution.cli import main
from solution.leads import LeadBook, discover_lead_files, ingest_file, load_schemas
from solution.parsers import IST, parse_phone, parse_timestamp
from solution.pii import Pii
from solution.report import build_report, write_report
from solution.store import ingest_drop, ingest_event_drop, ingest_lead_drop, load_dispositions

TEST_KEY = b"test-master-key"  # same as conftest.py
PII = Pii(TEST_KEY)
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "assignment-a-data" / "schemas"
SCHEMAS = load_schemas(SCHEMA_DIR)
DATA = ROOT / "data"
FILES = ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json")
HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


def ev(id_, type_, call, phone="98765 43210", ts="2026-10-05T04:30:00Z", **payload):
    d = {"event_id": id_, "event_type": type_, "workflow_id": "motor_renewal", "call_id": call,
         "occurred_at": ts, "payload": payload}
    if phone:
        d["phone"] = phone
    return json.dumps(d)


def drop(tmp_path, name, events=None, leads=None):
    d = tmp_path / name
    if events is not None:
        (d / "events").mkdir(parents=True)
        (d / "events" / "events_0001.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    if leads is not None:
        (d / "leads" / "motor_renewal").mkdir(parents=True)
        (d / "leads" / "motor_renewal" / "batch_001.csv").write_text(HEADER + "\n".join(leads) + "\n", encoding="utf-8")
    return d


def prime(db):
    load_dispositions(db, SCHEMA_DIR / "dispositions.csv")


# --- late-arriving leads ------------------------------------------------------------------

def test_orphans_become_calls_when_the_lead_arrives_in_a_later_drop(db, tmp_path):
    prime(db)
    events = [ev("e1", "call.initiated", "c1"), ev("e2", "call.connected", "c1", ts="2026-10-05T04:31:00Z"),
              ev("e3", "disposition.set", "c1", ts="2026-10-05T04:40:00Z", disposition_code="callback_requested"),
              ev("e4", "disposition.override", "c1", phone=None, ts="2026-10-05T05:00:00Z",
                 disposition_code="renewed", overridden_by="sup", reason="qa")]
    ingest_event_drop(db, PII, drop(tmp_path, "drop_1", events=events))
    r1 = build_report(db)
    assert r1.data_quality["orphan_events"] == 4 and r1.lead_report == [] and r1.daily_overview == []

    ingest_lead_drop(db, PII, SCHEMAS, drop(tmp_path, "drop_2", leads=["Aarav,+91 98765 43210,P1,,,"]))
    r2 = build_report(db)
    assert r2.data_quality["orphan_events"] == 0
    assert r2.lead_report == [["motor_renewal", "P1", "batch_001", "+91******3210", "completed", 1, 1,
                               "2026-10-05T10:00:00+05:30", "renewed", "true"]]
    assert r2.daily_overview == [["motor_renewal", "2026-10-05", 1, 1, "1.0000", 1]]
    assert r2.batch_summary == [["motor_renewal", "batch_001", 1, 0, 0, 0, 1]]


def test_unknown_disposition_and_rejection_counts(db, tmp_path):
    prime(db)
    ingest_lead_drop(db, PII, SCHEMAS, drop(tmp_path, "drop_1", leads=["Aarav,9876543210,P1,,,", "Bad,123,P2,,,"]))
    ingest_event_drop(db, PII, drop(tmp_path, "drop_1", events=[
        ev("e1", "call.initiated", "c1"), ev("e2", "disposition.set", "c1", disposition_code="bogus"), "{broken"]))
    q = build_report(db).data_quality
    assert (q["unknown_disposition_events"], q["malformed_events"], q["events_total"]) == (1, 1, 3)
    assert q["rejections_by_reason"] == {"invalid_phone": 1} and q["lead_rows_total"] == 2


# --- files --------------------------------------------------------------------------------------

def test_file_format(db, tmp_path):
    prime(db)
    ingest_lead_drop(db, PII, SCHEMAS, drop(tmp_path, "drop_1", leads=["Aarav,9876543210,P1,,,", "Bad,123,P2,,,"]))
    ingest_event_drop(db, PII, drop(tmp_path, "drop_1", events=[ev("e1", "call.initiated", "c1")]))
    out = tmp_path / "out"
    paths = write_report(build_report(db), out)
    assert [p.name for p in paths] == list(FILES)
    raw = (out / "lead_report.csv").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    rows = list(csv.reader(raw.decode().splitlines()))
    assert rows[0] == ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                       "connected_calls", "last_call_at", "current_disposition", "ever_success"]
    assert rows[1] == ["motor_renewal", "P1", "batch_001", "+91******3210", "in_progress_dialed", "1", "0",
                       "2026-10-05T10:00:00+05:30", "", "false"]
    assert (out / "rejections.csv").read_text().splitlines() == [
        "workflow_id,batch_id,row_number,reason", "motor_renewal,batch_001,2,invalid_phone"]
    q = json.loads((out / "data_quality.json").read_text())
    assert list(q) == ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason", "duplicate_lead_rows_merged",
                       "field_warnings", "events_total", "malformed_events", "duplicate_events_dropped",
                       "orphan_events", "unknown_disposition_events"]
    for p in paths:  # no raw phone number or name anywhere in the outputs
        text = p.read_text(encoding="utf-8")
        assert "9876543210" not in text and "Aarav" not in text


# --- CLI ------------------------------------------------------------------------------------------

@pytest.mark.skipif(not DATA.exists(), reason="run generate_data.py first")
def test_cli_ingest_twice_then_report_twice_gives_identical_files(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AU_PII_KEY", TEST_KEY.decode())
    schema = db.execute("SELECT current_schema()").fetchone()[0]
    base = ["--db-schema", schema]
    for d in ("drop_1", "drop_2"):
        assert main(["ingest", str(DATA / d), *base]) == 0
    first = capsys.readouterr().out
    assert "skipped" not in first and "stored=" in first
    for d in ("drop_1", "drop_2"):
        main(["ingest", str(DATA / d), *base])
    again = capsys.readouterr().out
    n_files = len(list(DATA.glob("drop_*/leads/*/*.csv"))) + len(list(DATA.glob("drop_*/events/*.jsonl")))
    assert again.count("skipped") == n_files and "new=" not in again   # works for any seed / scale
    main(["report", "--out", str(tmp_path / "r1"), *base])
    main(["report", "--out", str(tmp_path / "r2"), *base])
    for f in FILES:
        assert (tmp_path / "r1" / f).read_bytes() == (tmp_path / "r2" / f).read_bytes()


# --- order independence: drop_1 -> drop_2 == events first (reversed), leads last ----------------------

@pytest.mark.skipif(not DATA.exists(), reason="run generate_data.py first")
def test_report_is_independent_of_load_order(make_db, tmp_path):
    a, b = make_db(), make_db()
    prime(a), prime(b)
    for d in ("drop_1", "drop_2"):
        ingest_drop(a, PII, SCHEMAS, DATA / d)
    for d in ("drop_2", "drop_1"):  # events of both drops, newest first ...
        ingest_event_drop(b, PII, DATA / d)
    for d in ("drop_1", "drop_2"):  # ... and only then the leads
        ingest_lead_drop(b, PII, SCHEMAS, DATA / d)
    write_report(build_report(a), tmp_path / "a")
    write_report(build_report(b), tmp_path / "b")
    for f in FILES:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f


# --- independent oracle over the raw files (no database, no shared report code) --------------------------

def oracle():
    book = LeadBook()
    for d in ("drop_1", "drop_2"):
        for wf, p in discover_lead_files(DATA / d, SCHEMAS):
            ingest_file(book, SCHEMAS[wf], p)
    cat = {}
    with open(SCHEMA_DIR / "dispositions.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            cat[(r["workflow_id"], r["code"])] = (r["is_terminal"] == "true", r["is_success"] == "true")

    seen, events = set(), []
    for d in ("drop_1", "drop_2"):
        for f in sorted((DATA / d / "events").glob("*.jsonl")):
            for raw in f.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if e["event_id"] in seen:
                    continue
                seen.add(e["event_id"])
                events.append(e)

    def lead_of(e):
        ph = parse_phone(e["phone"]) if e.get("phone") else None
        return book.leads.get((e["workflow_id"], ph)) if ph else None

    call_lead = {e["call_id"]: lead_of(e) for e in events if e["event_type"] != "disposition.override" and lead_of(e)}
    orphans = unknown = 0
    per_call = defaultdict(list)
    for e in events:
        is_ov = e["event_type"] == "disposition.override"
        lead = call_lead.get(e["call_id"]) if is_ov else lead_of(e)
        if lead is None:
            orphans += 1
            continue
        code = e["payload"].get("disposition_code")
        if e["event_type"].startswith("disposition.") and (e["workflow_id"], code) not in cat:
            unknown += 1
            continue
        per_call[e["call_id"]].append((parse_timestamp(e["occurred_at"]), e["event_id"], e, lead))

    calls = []  # (lead, initiated_at, connected, telfailed, effective code, call_id)
    for cid, items in per_call.items():
        inits = [i for i in items if i[2]["event_type"] == "call.initiated"]
        if not inits:
            continue
        lead = min(inits, key=lambda i: (i[0], i[1]))[3]
        mine = [i for i in items if i[3] is lead]
        t0 = min(i[0] for i in inits)
        ov = sorted(i[:2] + (i[2],) for i in mine if i[2]["event_type"] == "disposition.override")
        st = sorted(i[:2] + (i[2],) for i in mine if i[2]["event_type"] == "disposition.set")
        pick = ov or st
        code = pick[-1][2]["payload"]["disposition_code"] if pick else None
        calls.append((lead, t0, any(i[2]["event_type"] == "call.connected" for i in mine),
                      any(i[2]["event_type"] == "call.telephony_failed" for i in mine), code, cid))
    return book, cat, calls, orphans, unknown


@pytest.mark.skipif(not DATA.exists(), reason="run generate_data.py first")
def test_pipeline_matches_independent_oracle_on_real_data(db):
    prime(db)
    for d in ("drop_1", "drop_2"):
        ingest_drop(db, PII, SCHEMAS, DATA / d)
    rep = build_report(db)
    book, cat, calls, orphans, unknown = oracle()

    assert (rep.data_quality["orphan_events"], rep.data_quality["unknown_disposition_events"]) == (orphans, unknown)

    # lead_report
    want = []
    by_lead = defaultdict(list)
    for c in calls:
        by_lead[id(c[0])].append(c)
    for (wf, phone), lead in book.leads.items():
        cs = sorted(by_lead[id(lead)], key=lambda c: (c[1], c[5]))
        coded = [c for c in cs if c[4]]
        if not cs:
            bucket = "yet_to_reach_out"
        elif any(cat[(wf, c[4])][0] for c in coded):
            bucket = "completed"
        elif all(c[3] for c in cs):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        want.append([wf, lead.values[SCHEMAS[wf].reference_field.name], lead.batch_id, "+91******" + phone[-4:],
                     bucket, len(cs), sum(c[2] for c in cs),
                     cs[-1][1].astimezone(IST).isoformat(timespec="seconds") if cs else "",
                     coded[-1][4] if coded else "", "true" if any(cat[(wf, c[4])][1] for c in coded) else "false"])
    key = lambda r: (r[0], r[1], r[2], r[3])
    assert sorted(rep.lead_report, key=key) == sorted(want, key=key)
    assert len(rep.lead_report) == len(book.leads)

    # daily_overview
    days = defaultdict(lambda: [0, 0, set()])
    for c in calls:
        wf = c[0].workflow_id
        g = days[(wf, c[1].astimezone(IST).date().isoformat())]
        g[0] += 1
        g[1] += c[2]
        if c[4] and cat[(wf, c[4])][1]:
            g[2].add(id(c[0]))
    want_daily = sorted([[w, d, g[0], g[1], f"{g[1] / g[0]:.4f}", len(g[2])] for (w, d), g in days.items()])
    assert rep.daily_overview == want_daily

    # batch_summary
    tot = Counter()
    for r in want:
        tot[(r[0], r[2], r[4])] += 1
    batches = {(k[0], k[1]) for k in tot}
    want_batch = sorted([[w, b, sum(v for (w2, b2, _), v in tot.items() if (w2, b2) == (w, b)),
                          tot[(w, b, "yet_to_reach_out")], tot[(w, b, "in_progress_telephony_failed")],
                          tot[(w, b, "in_progress_dialed")], tot[(w, b, "completed")]] for (w, b) in batches])
    assert rep.batch_summary == want_batch

    # cross-checks that must hold for any seed
    assert sum(r[2] for r in rep.batch_summary) == len(book.leads)
    assert sum(r[2] for r in rep.daily_overview) == sum(r[5] for r in rep.lead_report) == len(calls)
    q = rep.data_quality
    stored = db.execute("SELECT count(*) FROM events").fetchone()[0]
    assert q["events_total"] == q["malformed_events"] + q["duplicate_events_dropped"] + stored
    assert q["lead_rows_total"] == q["lead_rows_rejected"] + q["duplicate_lead_rows_merged"] + len(book.leads)
    assert sum(q["rejections_by_reason"].values()) == q["lead_rows_rejected"] == len(rep.rejections)
