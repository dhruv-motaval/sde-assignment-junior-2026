import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import solution.store as store
from solution.events import parse_event_line
from solution.parsers import parse_phone
from solution.pii import Pii
from solution.store import FileContentChanged, ingest_event_drop, ingest_event_file

TEST_KEY = b"test-master-key"  # same as conftest.py
PII = Pii(TEST_KEY)
ROOT = Path(__file__).resolve().parents[2]


def line(**over):
    d = {"event_id": "evt_1", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "call_1", "occurred_at": "2026-10-05T07:50:21Z", "phone": "91-77187-50036", "payload": {}}
    d.update(over)
    return json.dumps({k: v for k, v in d.items() if v is not ...})


# --- pure parsing ---------------------------------------------------------------

def test_valid_line():
    e = parse_event_line(line())
    assert (e.event_id, e.event_type, e.workflow_id, e.call_id) == ("evt_1", "call.initiated", "motor_renewal", "call_1")
    assert e.phone == "+917718750036"
    assert e.occurred_at == datetime(2026, 10, 5, 7, 50, 21, tzinfo=timezone.utc)


def test_three_timestamp_forms_agree():
    forms = ["2026-10-05T07:50:21Z", "2026-10-05T13:20:21+05:30", "2026-10-05 13:20:21"]
    assert len({parse_event_line(line(occurred_at=f)).occurred_at for f in forms}) == 1


def test_override_has_no_phone_and_keeps_payload():
    e = parse_event_line(line(event_type="disposition.override", phone=...,
                              payload={"disposition_code": "dnd", "overridden_by": "sup_1", "reason": "qa"}))
    assert e.phone is None and e.payload["disposition_code"] == "dnd"


@pytest.mark.parametrize("bad_phone", ["N/A", "12345", "", None, 42])
def test_missing_or_invalid_phone_is_still_a_valid_event(bad_phone):
    e = parse_event_line(line(phone=bad_phone))
    assert e is not None and e.phone is None


@pytest.mark.parametrize("raw", [
    '{"event_id": "evt_00002099"',            # truncated, as in the generated data
    "not json at all",
    "[1, 2, 3]", '"just a string"', "42", "null",
    json.dumps({"event_type": "call.initiated", "occurred_at": "2026-10-05T07:50:21Z"}),   # no event_id
    line(event_id=""), line(event_id=None), line(event_id=123),
    line(occurred_at=...), line(occurred_at="yesterday"), line(occurred_at=12345),         # bad time
    line(event_type=...), line(event_type=""),
])
def test_malformed(raw):
    assert parse_event_line(raw) is None


def test_odd_but_usable_fields_are_tolerated():
    e = parse_event_line(line(payload="oops", workflow_id=None, call_id=...))
    assert e.payload == {} and e.workflow_id is None and e.call_id is None


# --- ingestion into the ledger -----------------------------------------------------

def write(tmp_path, name, *lines):
    d = tmp_path / "drop_x" / "events"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def ingest(db, p, drop="drop_x"):
    return ingest_event_file(db, PII, p, drop, p.relative_to(p.parents[1]).as_posix())


def count(db, t):
    return db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]


def test_counters_dedupe_and_blank_lines(db, tmp_path):
    p = write(tmp_path, "events_0001.jsonl",
              line(event_id="e1"), "", "   ", line(event_id="e2", event_type="call.connected"),
              '{"event_id": "e3", "ev', line(event_id="e1"), "garbage", line(event_id="e4"))
    r = ingest(db, p)
    assert (r.lines_read, r.malformed, r.dup_events, r.inserted) == (6, 2, 1, 3)  # blanks not counted
    assert count(db, "events") == 3
    assert db.execute("SELECT line_no FROM malformed_lines ORDER BY 1").fetchall() == [(5,), (7,)]
    assert db.execute("SELECT lines_read, malformed, dup_events FROM ingested_files").fetchone() == (6, 2, 1)


def test_duplicates_across_files_and_drops_first_copy_wins(db, tmp_path):
    p1 = write(tmp_path, "events_0001.jsonl", line(event_id="e1", payload={"v": 1}), line(event_id="e2"))
    d2 = tmp_path / "drop_y" / "events"
    d2.mkdir(parents=True)
    p2 = d2 / "events_1001.jsonl"
    p2.write_text(line(event_id="e1", payload={"v": 2}) + "\n" + line(event_id="e3") + "\n", encoding="utf-8")
    ingest(db, p1)
    r2 = ingest_event_file(db, PII, p2, "drop_y", "events/events_1001.jsonl")
    assert (r2.dup_events, r2.inserted) == (1, 1)
    assert db.execute("SELECT payload FROM events WHERE event_id = 'e1'").fetchone()[0] == {"v": 1}


def test_dedupe_works_across_chunk_boundaries(db, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CHUNK", 2)
    ids = ["a", "b", "c", "a", "d", "c", "e", "a"]
    p = write(tmp_path, "events_0001.jsonl", *[line(event_id=i) for i in ids])
    r = ingest(db, p)
    assert (r.inserted, r.dup_events) == (5, 3) and count(db, "events") == 5


def test_ledger_row_content(db, tmp_path):
    p = write(tmp_path, "events_0001.jsonl",
              line(event_id="e1", occurred_at="2026-10-03 10:01:00"),
              line(event_id="e2", event_type="disposition.override", phone=..., payload={"disposition_code": "dnd"}),
              line(event_id="e3", phone="N/A"))
    ingest(db, p)
    rows = {r[0]: r for r in db.execute(
        "SELECT event_id, occurred_at, phone_hash, payload, source_file, line_no FROM events").fetchall()}
    assert rows["e1"][1] == datetime(2026, 10, 3, 4, 31, tzinfo=timezone.utc)      # naive = IST -> UTC
    assert bytes(rows["e1"][2]) == PII.phone_hash("+917718750036")
    assert rows["e2"][2] is None and rows["e2"][3] == {"disposition_code": "dnd"}
    assert rows["e3"][2] is None                                                    # invalid phone: unlinkable
    assert rows["e1"][4:] == ("drop_x/events/events_0001.jsonl", 1)


def test_same_call_different_phone_formats_get_the_same_hash(db, tmp_path):
    p = write(tmp_path, "events_0001.jsonl",
              line(event_id="e1", phone="9876543210"), line(event_id="e2", phone="+91 98765 43210"),
              line(event_id="e3", phone="(+91) 9876543210"))
    ingest(db, p)
    assert db.execute("SELECT count(DISTINCT phone_hash) FROM events").fetchone()[0] == 1


def test_no_plaintext_phone_in_events_or_malformed_lines(db, tmp_path):
    p = write(tmp_path, "events_0001.jsonl", line(event_id="e1", phone="+91 98765 43210"),
              '{"event_id": "e2", "phone": "98765 43211"')   # truncated line containing a phone
    ingest(db, p)
    dump = ""
    for (t,) in db.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall():
        dump += "\n".join(r[0] for r in db.execute(f'SELECT x::text FROM "{t}" x').fetchall())
    for secret in ("9876543210", "98765 43210", "98765 43211", "9876543211"):
        assert secret not in dump


def test_reingest_is_noop_and_changed_content_refused(db, tmp_path):
    p = write(tmp_path, "events_0001.jsonl", line(event_id="e1"), "junk", line(event_id="e1"))
    ingest(db, p)
    snap = (count(db, "events"), count(db, "malformed_lines"), count(db, "ingested_files"))
    assert ingest(db, p) is None
    assert (count(db, "events"), count(db, "malformed_lines"), count(db, "ingested_files")) == snap == (1, 1, 1)
    p.write_text(line(event_id="e9") + "\n", encoding="utf-8")
    with pytest.raises(FileContentChanged):
        ingest(db, p)


def test_crash_mid_file_rolls_back_everything(db, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CHUNK", 2)          # first chunk is COPYed before the crash
    p = write(tmp_path, "events_0001.jsonl", *[line(event_id=f"e{i}") for i in range(5)], "junk")
    real, calls = PII.phone_hash, []

    class Boom(Pii):
        def phone_hash(self, ph):
            calls.append(1)
            if len(calls) == 4:
                raise RuntimeError("crash")
            return real(ph)

    boom = Boom(TEST_KEY)
    with pytest.raises(RuntimeError):
        ingest_event_file(db, boom, p, "drop_x", "events/events_0001.jsonl")
    assert (count(db, "events"), count(db, "malformed_lines"), count(db, "ingested_files")) == (0, 0, 0)
    assert ingest(db, p).inserted == 5                # clean retry


def test_drop_ingests_files_in_filename_order(db, tmp_path):
    write(tmp_path, "events_0002.jsonl", line(event_id="x", payload={"f": 2}))
    write(tmp_path, "events_0001.jsonl", line(event_id="x", payload={"f": 1}))
    ingest_event_drop(db, PII, tmp_path / "drop_x")
    assert db.execute("SELECT payload FROM events").fetchone()[0] == {"f": 1}   # file 0001 came first


# --- real generated data -----------------------------------------------------------

DATA = ROOT / "data"


@pytest.mark.skipif(not DATA.exists(), reason="run generate_data.py first")
def test_real_data_matches_independent_count_and_reingest_is_noop(db):
    # independent oracle: plain Python over the raw files, no shared code with the loader
    lines = malformed = unlinkable = 0
    seen = set()
    for f in sorted(DATA.glob("drop_*/events/*.jsonl")):
        for raw in f.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            lines += 1
            try:
                d = json.loads(raw)
            except ValueError:
                malformed += 1
                continue
            if d["event_id"] not in seen and d["event_type"] != "disposition.override"                     and parse_phone(d.get("phone") or "") is None:
                unlinkable += 1          # the generator emits bad phones ('N/A', landlines, 9 digits)
            seen.add(d["event_id"])
    dups = lines - malformed - len(seen)

    for drop in ("drop_1", "drop_2"):
        ingest_event_drop(db, PII, DATA / drop)
    assert db.execute("SELECT sum(lines_read), sum(malformed), sum(dup_events) FROM ingested_files "
                      "WHERE kind = 'events'").fetchone() == (lines, malformed, dups)
    assert count(db, "events") == len(seen)
    assert lines == malformed + dups + count(db, "events")                      # accounting invariant
    # overrides never have a phone; other events have a hash unless their phone is invalid (-> orphans later)
    assert db.execute("SELECT count(*) FROM events WHERE event_type = 'disposition.override' "
                      "AND phone_hash IS NOT NULL").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM events WHERE event_type <> 'disposition.override' "
                      "AND phone_hash IS NULL").fetchone()[0] == unlinkable > 0

    before = db.execute("SELECT md5(string_agg(event_id, ',' ORDER BY event_id)) FROM events").fetchone()
    for drop in ("drop_1", "drop_2"):
        assert all(r is None for r in ingest_event_drop(db, PII, DATA / drop))
    assert db.execute("SELECT md5(string_agg(event_id, ',' ORDER BY event_id)) FROM events").fetchone() == before
