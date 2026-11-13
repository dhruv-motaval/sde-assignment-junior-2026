"""Regression tests for hostile / unusual input found by adversarial probing before submission.

Rule behind all of them: ONE bad record must never crash or block the rest of a file.
"""
import csv
import json
import time
from pathlib import Path

import pytest

from solution.cli import main
from solution.events import parse_event_line
from solution.leads import discover_lead_files, load_schemas
from solution.parsers import parse_timestamp
from solution.pii import Pii
from solution.report import build_report, write_report
from solution.store import ingest_event_drop, ingest_event_file, ingest_lead_drop, ingest_lead_file

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = load_schemas(ROOT / "assignment-a-data" / "schemas")
MOTOR = SCHEMAS["motor_renewal"]
PII = Pii(b"test-master-key")
HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


def line(**over):
    d = {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1",
         "occurred_at": "2026-10-05T04:30:00Z", "phone": "9876543210", "payload": {}}
    d.update(over)
    return json.dumps(d)


# --- event parsing: lines the database could not store are malformed, not crashes -----------------

@pytest.mark.parametrize("raw", [
    line(call_id="c\x001"),                                    # NUL in a text field
    line(payload={"k": "a\x00b"}),                             # NUL inside the payload
    '{"event_id":"e1","event_type":"x","occurred_at":"2026-10-05T04:30:00Z","payload":{"v":NaN}}',
    '{"event_id":"e1","event_type":"x","occurred_at":"2026-10-05T04:30:00Z","payload":{"v":Infinity}}',
    '{"event_id":"e1","event_type":"x","occurred_at":"2026-10-05T04:30:00Z","payload":{"v":"\\ud800"}}',  # lone surrogate
    line(occurred_at="0001-01-01 00:00:00"),                   # no UTC equivalent
    line(occurred_at="0001-01-01T00:00:00+05:30"),
    line(occurred_at="9999-12-31T23:59:59Z"),                  # no IST equivalent (reports convert to IST)
    line(occurred_at="9999-12-31T20:00:00-05:00"),             # rolls over to year 10000 in UTC
])
def test_unstorable_lines_are_malformed_not_crashes(raw):
    assert parse_event_line(raw) is None


@pytest.mark.parametrize("ts", ["2026-10-05T04:30:00z", "2026-10-05T10:00:00+0530", "2026-10-05T04:30:00.123456Z",
                                "2026-10-05 10:00:00"])
def test_odd_but_valid_timestamps_are_accepted(ts):
    assert parse_event_line(line(occurred_at=ts)) is not None


def test_unicode_payload_and_ids_are_fine():
    e = parse_event_line(line(call_id="call_😀_é", payload={"reason": "ஆ ₹ 😀"}))
    assert e.call_id == "call_😀_é" and e.payload["reason"] == "ஆ ₹ 😀"


def test_parse_timestamp_out_of_range_is_a_value_error():
    with pytest.raises(ValueError):
        parse_timestamp("0001-01-01 00:00:00")
    assert parse_timestamp("2026-10-05T04:30:00z") == parse_timestamp("2026-10-05T04:30:00Z")


# --- event files ----------------------------------------------------------------------------------

def write_events(tmp_path, data: bytes, drop="drop_x"):
    d = tmp_path / drop / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "events_0001.jsonl").write_bytes(data)
    return d / "events_0001.jsonl"


def count(db, t):
    return db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]


def test_bom_at_start_of_event_file_does_not_lose_the_first_event(db, tmp_path):
    p = write_events(tmp_path, b"\xef\xbb\xbf" + line(event_id="e1").encode() + b"\r\n" + line(event_id="e2").encode() + b"\r\n")
    r = ingest_event_file(db, PII, p, "drop_x", "events/events_0001.jsonl")
    assert (r.lines_read, r.malformed, r.inserted) == (2, 0, 2)


def test_one_bad_line_never_blocks_the_good_ones(db, tmp_path):
    good = [line(event_id=f"g{i}") for i in range(3)]
    bad = [line(event_id="b1", call_id="c\x00"), line(event_id="b2", payload={"a": "\x00"}),
           '{"event_id":"b3","event_type":"x","occurred_at":"2026-10-05T04:30:00Z","payload":{"v":NaN}}',
           line(event_id="b4", occurred_at="0001-01-01T00:00:00+05:30"),
           line(event_id="b5", occurred_at="9999-12-31T23:59:59Z")]
    data = "\n".join(good[:1] + bad + good[1:]).encode() + b"\n" + good[2].encode()[:30] + b"\xff\xfe\n"
    r = ingest_event_file(db, PII, write_events(tmp_path, data), "drop_x", "events/events_0001.jsonl")
    assert (r.malformed, r.inserted) == (6, 3)
    assert sorted(x[0] for x in db.execute("SELECT event_id FROM events")) == ["g0", "g1", "g2"]


def test_uppercase_file_extensions_are_found(tmp_path):
    (tmp_path / "drop_x" / "leads" / "motor_renewal").mkdir(parents=True)
    (tmp_path / "drop_x" / "leads" / "motor_renewal" / "batch_009.CSV").write_text(HEADER, encoding="utf-8")
    assert [(w, p.name) for w, p in discover_lead_files(tmp_path / "drop_x", SCHEMAS)] == [("motor_renewal", "batch_009.CSV")]


def test_uppercase_jsonl_extension_is_ingested(db, tmp_path):
    d = tmp_path / "drop_x" / "events"
    d.mkdir(parents=True)
    (d / "events_0001.JSONL").write_text(line() + "\n", encoding="utf-8")
    assert ingest_event_drop(db, PII, tmp_path / "drop_x")[0].inserted == 1


# --- lead files -------------------------------------------------------------------------------------

def ingest_csv(db, tmp_path, data: bytes, name="batch_001.csv", drop="drop_x"):
    d = tmp_path / drop / "leads" / "motor_renewal"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return ingest_lead_file(db, PII, MOTOR, d / name, drop, f"leads/motor_renewal/{name}")


def test_invalid_utf8_byte_does_not_crash_the_file(db, tmp_path):
    r = ingest_csv(db, tmp_path, HEADER.encode() + b"Bad\x80Name,9876543210,P1,,,\nOK,9876543211,P2,,,\n")
    assert (r.rows_total, r.new_leads, r.rows_rejected) == (2, 2, 0)


def test_nul_characters_are_stripped_not_fatal(db, tmp_path):
    r = ingest_csv(db, tmp_path, (HEADER + "A\x00B,9876543210,P\x001,,,\n").encode())
    assert r.new_leads == 1 and db.execute("SELECT external_ref FROM leads").fetchone()[0] == "P1"


def test_huge_cell_does_not_crash_the_file(db, tmp_path):
    r = ingest_csv(db, tmp_path, (HEADER + "A" * 1_000_000 + ",9876543210,P1,,,\nB,9876543211,P2,,,\n").encode())
    assert (r.new_leads, r.rows_rejected) == (2, 0)


def test_crlf_bom_quoted_multiline_and_commas(db, tmp_path):
    data = (b"\xef\xbb\xbf" + HEADER.replace("\n", "\r\n").encode() +
            b'"A\r\nB",9876543210,"P,1 ""x""",,,\r\n\r\nC,9876543211,P2,,,\r\n')
    r = ingest_csv(db, tmp_path, data)
    assert (r.rows_total, r.new_leads) == (2, 2)
    out = tmp_path / "out"
    write_report(build_report(db), out)
    rows = list(csv.reader((out / "lead_report.csv").open(encoding="utf-8", newline="")))
    assert rows[1][1] == 'P,1 "x"'                      # the CSV writer quoted it correctly


def test_unusual_phone_texts_are_rejected_per_spec_not_guessed(db, tmp_path):
    cases = ["9876543210.0", "९८७६५४३२१०", "N/A", "+1 415 555 0134", "98765"]
    rows = "".join(f"A{i},{p},P{i},,,\n" for i, p in enumerate(cases))
    r = ingest_csv(db, tmp_path, (HEADER + rows).encode())
    assert [x[1] for x in r.rejections] == ["invalid_phone"] * len(cases)


def test_same_batch_name_in_two_drops_does_not_collide_on_rejections(db, tmp_path):
    for drop, ref in (("drop_1", "P1"), ("drop_2", "P2")):                     # both have batch_001, row 1 rejected
        ingest_csv(db, tmp_path, (HEADER + f"A,123,{ref},,,\nB,{9876543210 + int(ref[1])},{ref},,,\n").encode(), drop=drop)
    assert count(db, "rejections") == 2 and count(db, "leads") == 2
    assert [r for r in build_report(db).rejections] == [["motor_renewal", "batch_001", 1, "invalid_phone"]] * 2


# --- empty / missing things ---------------------------------------------------------------------------

def test_report_on_an_empty_database_gives_header_only_files_and_zeros(db, tmp_path):
    out = tmp_path / "out"
    write_report(build_report(db), out)
    for name in ("batch_summary", "daily_overview", "lead_report", "rejections"):
        assert len((out / f"{name}.csv").read_text().splitlines()) == 1
    q = json.loads((out / "data_quality.json").read_text())
    assert q["events_total"] == 0 and q["rejections_by_reason"] == {} and q["orphan_events"] == 0


def test_cli_rejects_a_missing_or_wrong_drop_directory(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AU_PII_KEY", "k")
    base = ["--db-schema", db.execute("SELECT current_schema()").fetchone()[0]]
    assert main(["ingest", str(tmp_path / "nope"), *base]) == 2
    assert "not found" in capsys.readouterr().err

    (tmp_path / "data" / "drop_1" / "events").mkdir(parents=True)
    assert main(["ingest", str(tmp_path / "data"), *base]) == 2         # the parent folder, by mistake
    err = capsys.readouterr().err
    assert "not a drop" in err and "drop_1" in err                       # and it suggests the right one
    assert count(db, "ingested_files") == 0


def test_cli_unreachable_database_is_a_friendly_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AU_PII_KEY", "k")
    (tmp_path / "drop_1" / "events").mkdir(parents=True)
    t = time.monotonic()
    rc = main(["ingest", str(tmp_path / "drop_1"), "--db", "postgresql://au:x@127.0.0.1:5999/x", "--wait", "0"])
    err = capsys.readouterr().err
    assert rc == 2 and "cannot reach PostgreSQL" in err and "docker compose up" in err
    assert time.monotonic() - t < 15


def test_ingesting_twice_in_different_path_spellings_is_still_idempotent(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AU_PII_KEY", "test-master-key")   # the key the `db` fixture's database was built with
    base = ["--db-schema", db.execute("SELECT current_schema()").fetchone()[0]]
    d = tmp_path / "drop_1"
    (d / "events").mkdir(parents=True)
    (d / "events" / "events_0001.jsonl").write_text(line() + "\n", encoding="utf-8")
    assert main(["ingest", str(d), *base]) == 0
    assert main(["ingest", str(d) + "/", *base]) == 0                    # trailing slash
    assert capsys.readouterr().out.count("skipped") == 1 and count(db, "events") == 1
