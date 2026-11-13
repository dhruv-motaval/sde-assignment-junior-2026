from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from solution.leads import (
    LeadBook, discover_lead_files, evaluate_row, ingest_file, load_schemas, read_rows,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = load_schemas(ROOT / "assignment-a-data" / "schemas")
MOTOR = SCHEMAS["motor_renewal"]
FEEDBACK = SCHEMAS["policy_feedback"]

HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


def write(tmp_path, name, text, wf="motor_renewal"):
    d = tmp_path / "leads" / wf
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


# --- header mapping -----------------------------------------------------------

def test_header_aliases_case_underscores_reorder_and_extra_columns(tmp_path):
    p = write(tmp_path, "batch_003.csv",
              "Language,Insured Name,POLICY_NO,Mobile No,Expiry Date,Premium,agent_notes\n"
              "HI,Aarav,P1,98765 43210,05/11/2026,Rs. 100,note\n")
    [(n, raw)] = list(read_rows(p, MOTOR))
    assert n == 1
    assert raw == {"preferred_language": "HI", "customer_name": "Aarav", "policy_number": "P1",
                   "phone": "98765 43210", "policy_expiry": "05/11/2026", "premium_amount": "Rs. 100"}


def test_two_columns_for_same_field_first_wins():
    cmap = MOTOR.header_map(["Mobile", "phone", "name"])
    assert [f.name for f in cmap.values()] == ["phone", "customer_name"]
    assert list(cmap) == [0, 2]


def test_bom_does_not_break_first_header(tmp_path):
    p = tmp_path / "b.csv"
    p.write_bytes(b"\xef\xbb\xbf" + HEADER.encode() + b"A,9876543210,P1,,,\n")
    [(_, raw)] = list(read_rows(p, MOTOR))
    assert raw["customer_name"] == "A"


# --- blank rows and numbering --------------------------------------------------

def test_blank_rows_skipped_and_not_counted(tmp_path):
    p = write(tmp_path, "batch_001.csv",
              HEADER + "A,9876543210,P1,,,\n\n  , ,,,,  \nB,9876543211,P2,,,\n,,,,,\nC,9876543212,P3,,,\n")
    assert [n for n, _ in read_rows(p, MOTOR)] == [1, 2, 3]


def test_empty_file_and_header_only(tmp_path):
    assert list(read_rows(write(tmp_path, "a.csv", ""), MOTOR)) == []
    assert list(read_rows(write(tmp_path, "b.csv", HEADER), MOTOR)) == []


def test_short_row_means_missing_cells(tmp_path):
    p = write(tmp_path, "batch_001.csv", HEADER + "A,9876543210\n")
    [(_, raw)] = list(read_rows(p, MOTOR))
    reason, _, _ = evaluate_row(MOTOR, raw)
    assert reason == "missing_required:policy_number"


# --- validation ---------------------------------------------------------------

def good(**over):
    row = {"customer_name": "Aarav", "phone": "98765 43210", "policy_number": "P1",
           "policy_expiry": "01-Dec-2026", "premium_amount": "₹1,000", "preferred_language": " HI "}
    row.update(over)
    return row


def test_valid_row_is_normalised():
    reason, v, warn = evaluate_row(MOTOR, good())
    assert reason is None and warn == 0
    assert v == {"customer_name": "Aarav", "phone": "+919876543210", "policy_number": "P1",
                 "policy_expiry": date(2026, 12, 1), "premium_amount": Decimal("1000.00"),
                 "preferred_language": "hi"}


@pytest.mark.parametrize("over,reason", [
    ({"customer_name": ""}, "missing_required:customer_name"),
    ({"customer_name": "   "}, "missing_required:customer_name"),
    ({"phone": ""}, "missing_required:phone"),
    ({"phone": "12345"}, "invalid_phone"),
    ({"policy_number": ""}, "missing_required:policy_number"),
])
def test_rejection_reasons(over, reason):
    assert evaluate_row(MOTOR, good(**over))[0] == reason


def test_first_failure_in_schema_order_wins():
    # name (field 1) is missing AND phone (field 2) is invalid -> the name reason
    assert evaluate_row(MOTOR, good(customer_name="", phone="123"))[0] == "missing_required:customer_name"
    # phone invalid AND policy_number missing -> phone comes first in schema order
    assert evaluate_row(MOTOR, good(phone="123", policy_number=""))[0] == "invalid_phone"
    # feedback schema: phone, then order_id
    assert evaluate_row(FEEDBACK, {"customer_name": "A", "phone": "123", "order_id": ""})[0] == "invalid_phone"


def test_optional_invalid_becomes_null_with_one_warning_each():
    reason, v, warn = evaluate_row(MOTOR, good(policy_expiry="31/02/2026", premium_amount="abc",
                                               preferred_language="fr"))
    assert reason is None and warn == 3
    assert v["policy_expiry"] is None and v["premium_amount"] is None and v["preferred_language"] is None


def test_optional_missing_is_not_a_warning():
    reason, v, warn = evaluate_row(MOTOR, good(policy_expiry="", premium_amount=""))
    assert reason is None and warn == 0 and v["policy_expiry"] is None


def test_rejected_row_reports_no_warnings():
    reason, v, warn = evaluate_row(MOTOR, good(policy_expiry="bad", phone="1"))
    assert reason == "invalid_phone" and v is None and warn == 0


# --- merging -------------------------------------------------------------------

def test_first_row_creates_later_rows_merge_non_null_only_and_batch_is_permanent(tmp_path):
    book = LeadBook()
    f1 = write(tmp_path, "batch_001.csv", HEADER + "Aarav,98765 43210,P1,01-Dec-2026,500,hi\n"
                                                   "Aarav K,+91 9876543210,P1B,,,\n")
    f2 = write(tmp_path, "batch_002.csv", HEADER + "Aarav Z,09876543210,,15/01/2027,junk,\n")
    r1 = ingest_file(book, MOTOR, f1)
    r2 = ingest_file(book, MOTOR, f2)
    assert (r1.new_leads, r1.merged, r1.field_warnings) == (1, 1, 0)
    # f2 row has no policy_number -> required missing -> rejected, so it does NOT merge
    assert (r2.new_leads, r2.merged, r2.rows_rejected) == (0, 0, 1)
    f3 = write(tmp_path, "batch_003.csv", HEADER + "Aarav Z,09876543210,P9,15/01/2027,junk,\n")
    r3 = ingest_file(book, MOTOR, f3)
    assert (r3.merged, r3.field_warnings) == (1, 1)  # merged rows still count their warnings
    [lead] = book.leads.values()
    assert lead.batch_id == "batch_001"          # permanent
    assert lead.values["customer_name"] == "Aarav Z"      # last non-null wins
    assert lead.values["policy_number"] == "P9"
    assert lead.values["policy_expiry"] == date(2027, 1, 15)
    assert lead.values["premium_amount"] == Decimal("500.00")  # junk (null) did not erase it
    assert lead.values["preferred_language"] == "hi"


def test_identity_is_workflow_plus_phone():
    book = LeadBook()
    v = evaluate_row(MOTOR, good())[1]
    w = evaluate_row(FEEDBACK, {"customer_name": "A", "phone": "9876543210", "order_id": "O1"})[1]
    assert book.add(MOTOR, "batch_001", v) is True
    assert book.add(FEEDBACK, "batch_001", w) is True   # same phone, other workflow: new lead
    assert book.add(MOTOR, "batch_002", v) is False
    assert len(book.leads) == 2


def test_defaults_apply_only_to_still_null_fields():
    book = LeadBook()
    v = evaluate_row(MOTOR, good(preferred_language=""))[1]
    book.add(MOTOR, "batch_001", v)
    assert book.leads[("motor_renewal", "+919876543210")].values["preferred_language"] == "en"
    v2 = evaluate_row(MOTOR, good(preferred_language="TA"))[1]
    book.add(MOTOR, "batch_002", v2)      # a real value later overwrites the default
    assert book.leads[("motor_renewal", "+919876543210")].values["preferred_language"] == "ta"
    v3 = evaluate_row(MOTOR, good(preferred_language=""))[1]
    book.add(MOTOR, "batch_003", v3)      # a null later does not bring the default back
    assert book.leads[("motor_renewal", "+919876543210")].values["preferred_language"] == "ta"


def test_row_accounting_invariant(tmp_path):
    p = write(tmp_path, "batch_001.csv", HEADER +
              "A,9876543210,P1,,,\nB,9876543210,P2,,,\nC,123,P3,,,\n\n,9876543211,P4,,,\nD,9876543212,P5,,,\n")
    r = ingest_file(LeadBook(), MOTOR, p)
    assert r.rows_total == 5
    assert r.rows_total == r.rows_rejected + r.new_leads + r.merged
    assert r.rejections == [(3, "invalid_phone"), (4, "missing_required:customer_name")]


# --- discovery -----------------------------------------------------------------

def test_discover_is_sorted_and_skips_unknown_workflow(tmp_path):
    write(tmp_path, "batch_002.csv", HEADER)
    write(tmp_path, "batch_001.csv", HEADER)
    write(tmp_path, "batch_001.csv", HEADER, wf="policy_feedback")
    write(tmp_path, "batch_001.csv", HEADER, wf="mystery")
    with pytest.warns(UserWarning, match="mystery"):
        found = discover_lead_files(tmp_path, SCHEMAS)
    assert [(w, p.name) for w, p in found] == [
        ("motor_renewal", "batch_001.csv"), ("motor_renewal", "batch_002.csv"),
        ("policy_feedback", "batch_001.csv")]


# --- real generated data (skipped when data/ has not been generated) ------------

DATA = ROOT / "data"


@pytest.mark.skipif(not DATA.exists(), reason="run generate_data.py first")
def test_real_data_accounting_and_idempotent_order():
    book = LeadBook()
    total = rejected = new = merged = 0
    for drop in ("drop_1", "drop_2"):
        for wf, path in discover_lead_files(DATA / drop, SCHEMAS):
            r = ingest_file(book, SCHEMAS[wf], path)
            assert r.rows_total == r.rows_rejected + r.new_leads + r.merged
            total += r.rows_total; rejected += r.rows_rejected; new += r.new_leads; merged += r.merged
    assert new == len(book.leads) > 0
    assert total == rejected + new + merged
    # every stored lead has a valid canonical phone and its required fields
    for (wf, phone), lead in book.leads.items():
        assert phone.startswith("+91") and len(phone) == 13
        assert lead.values[SCHEMAS[wf].reference_field.name]
        assert lead.values["preferred_language"] is not None
