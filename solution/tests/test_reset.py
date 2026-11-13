import json

from solution.cli import main

KEY = "test-master-key"
HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


def make_drop(root, rows, event_id):
    d = root / "drop_1"                                   # same drop NAME every time, like a new seed
    (d / "leads" / "motor_renewal").mkdir(parents=True, exist_ok=True)
    (d / "events").mkdir(parents=True, exist_ok=True)
    (d / "leads" / "motor_renewal" / "batch_001.csv").write_text(HEADER + rows, encoding="utf-8")
    (d / "events" / "events_0001.jsonl").write_text(json.dumps(
        {"event_id": event_id, "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1",
         "occurred_at": "2026-10-05T04:30:00Z", "phone": "9876543210", "payload": {}}) + "\n", encoding="utf-8")
    return d


def tables(db):
    return {r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}


def test_reset_needs_yes_and_then_allows_a_fresh_seed(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AU_PII_KEY", KEY)
    base = ["--db-schema", db.execute("SELECT current_schema()").fetchone()[0]]
    main(["ingest", str(make_drop(tmp_path / "seed1", "A,9876543210,P1,,,\n", "e1")), *base])
    assert db.execute("SELECT count(*) FROM leads").fetchone()[0] == 1

    assert main(["reset", *base]) == 2                    # no --yes: refuses, deletes nothing
    assert "--yes" in capsys.readouterr().out
    assert db.execute("SELECT count(*) FROM leads").fetchone()[0] == 1

    # without a reset, the "new seed" (same drop name, different content) is refused, not double-counted
    assert main(["ingest", str(make_drop(tmp_path / "seed2", "B,9876543211,P2,,,\n", "e2")), *base]) == 1
    assert "different content" in capsys.readouterr().err
    assert db.execute("SELECT count(*) FROM leads").fetchone()[0] == 1

    assert main(["reset", "--yes", *base]) == 0
    assert tables(db) == set()                            # everything of ours is gone

    main(["ingest", str(make_drop(tmp_path / "seed2", "B,9876543211,P2,,,\n", "e2")), *base])
    assert db.execute("SELECT external_ref FROM leads").fetchall() == [("P2",)]
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_reset_also_clears_the_key_fingerprint_so_a_new_key_works(db, tmp_path, monkeypatch, capsys):
    base = ["--db-schema", db.execute("SELECT current_schema()").fetchone()[0]]
    monkeypatch.setenv("AU_PII_KEY", KEY)
    d = make_drop(tmp_path / "s", "A,9876543210,P1,,,\n", "e1")
    main(["ingest", str(d), *base])
    monkeypatch.setenv("AU_PII_KEY", "a-different-key")
    assert main(["ingest", str(d), *base]) == 1                 # friendly error, not a traceback
    assert "different PII key" in capsys.readouterr().err
    main(["reset", "--yes", *base])
    main(["ingest", str(d), *base])                       # new key accepted on an empty database
    assert db.execute("SELECT count(*) FROM leads").fetchone()[0] == 1
