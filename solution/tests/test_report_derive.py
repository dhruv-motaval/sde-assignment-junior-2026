"""Report rules on hand-built cases: pure functions, no database."""
from datetime import datetime, timedelta, timezone

from solution.report import (
    COMPLETED, DIALED, TEL_FAIL, YET, EvRow, LeadRow, build_rows, derive, lead_bucket,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)  # 10:00 IST
WF = "motor_renewal"
CAT = {
    (WF, "renewed"): (True, True),
    (WF, "not_interested"): (True, False),
    (WF, "callback_requested"): (False, False),
    (WF, "payment_link_sent"): (False, False),
}
LEADS = {1: LeadRow(1, WF, "P1", "batch_001", "1111"), 2: LeadRow(2, WF, "P2", "batch_001", "2222")}
_n = [0]


def ev(type_, call, lead=1, mins=0, code=None, eid=None, wf=WF, when=None):
    _n[0] += 1
    return EvRow(eid or f"e{_n[0]:04d}", type_, wf, call, when or T0 + timedelta(minutes=mins), code,
                 None if type_ == "disposition.override" else lead)


def init(call, lead=1, mins=0, **kw):
    return ev("call.initiated", call, lead, mins, **kw)


def disp(call, code, mins, lead=1, override=False, **kw):
    return ev("disposition.override" if override else "disposition.set", call, lead, mins, code, **kw)


def run(events, leads=LEADS):
    d = derive(leads, events, CAT)
    return d, build_rows(leads, d, CAT)


def eff(d, call):
    return d.calls[call].effective


# --- calls ------------------------------------------------------------------------

def test_call_exists_only_if_initiated_is_linked():
    d, _ = run([ev("call.connected", "c1"), disp("c1", "renewed", 1), init("c2")])
    assert set(d.calls) == {"c2"}
    assert d.orphan_events == 0   # c1's events are linked (not orphans), the call just does not count


def test_connected_and_telephony_failed_flags():
    d, _ = run([init("c1"), ev("call.connected", "c1", mins=1), init("c2"), ev("call.telephony_failed", "c2", mins=1),
                init("c3")])
    assert [(c.connected, c.telephony_failed) for c in (d.calls[k] for k in ("c1", "c2", "c3"))] == [
        (True, False), (False, True), (False, False)]


def test_events_of_another_lead_with_same_call_id_are_not_part_of_the_call():
    d, _ = run([init("c1", lead=1), ev("call.connected", "c1", lead=2, mins=1)])
    assert not d.calls["c1"].connected


# --- effective disposition ---------------------------------------------------------

def test_latest_set_wins_when_no_override():
    d, _ = run([init("c1"), disp("c1", "callback_requested", 1), disp("c1", "renewed", 5), disp("c1", "not_interested", 3)])
    assert eff(d, "c1") == "renewed"


def test_override_beats_a_later_set_and_latest_override_wins():
    d, _ = run([init("c1"), disp("c1", "renewed", 1),
                disp("c1", "not_interested", 2, override=True), disp("c1", "callback_requested", 9),
                disp("c1", "payment_link_sent", 3, override=True)])
    assert eff(d, "c1") == "payment_link_sent"


def test_no_disposition_means_none():
    d, _ = run([init("c1")])
    assert eff(d, "c1") is None


def test_unknown_codes_are_counted_and_ignored_not_orphans():
    d, _ = run([init("c1"), disp("c1", "renewed", 1), disp("c1", "bogus", 5),
                disp("c1", "also_bogus", 6, override=True)])
    assert d.unknown_disposition_events == 2 and d.orphan_events == 0
    assert eff(d, "c1") == "renewed"          # unknown override ignored -> falls back to the set


def test_unknown_code_depends_on_workflow():
    d, _ = run([init("c1"), disp("c1", "feedback_captured", 1)])   # exists only in policy_feedback
    assert d.unknown_disposition_events == 1 and eff(d, "c1") is None


def test_ties_on_occurred_at_resolved_by_higher_event_id():
    d, _ = run([init("c1"), disp("c1", "renewed", 4, eid="e_a"), disp("c1", "not_interested", 4, eid="e_b")])
    assert eff(d, "c1") == "not_interested"
    d, _ = run([init("c1"), disp("c1", "not_interested", 4, eid="e_b"), disp("c1", "renewed", 4, eid="e_a")])
    assert eff(d, "c1") == "not_interested"   # independent of list order


# --- orphans ------------------------------------------------------------------------

def test_orphan_rules():
    unlinked = EvRow("o1", "call.initiated", WF, "cX", T0, None, None)
    orphan_override = EvRow("o2", "disposition.override", WF, "cX", T0, "renewed", None)
    no_call_override = EvRow("o3", "disposition.override", WF, None, T0, "renewed", None)
    orphan_with_unknown_code = EvRow("o4", "disposition.set", WF, "cX", T0, "bogus", None)
    linked_override = disp("c1", "renewed", 2, override=True)
    d, _ = run([init("c1"), linked_override, unlinked, orphan_override, no_call_override, orphan_with_unknown_code])
    assert d.orphan_events == 4                 # step 3 (orphan) comes before step 4 (unknown)
    assert d.unknown_disposition_events == 0
    assert eff(d, "c1") == "renewed"


# --- buckets --------------------------------------------------------------------------

def bucket_of(events):
    d, (_, _, lead_rows) = run(events, {1: LEADS[1]})
    return lead_rows[0][4]


def test_bucket_yet_to_reach_out():
    assert bucket_of([]) == YET


def test_bucket_completed_on_terminal_even_if_telephony_failed():
    assert bucket_of([init("c1"), ev("call.telephony_failed", "c1", mins=1), disp("c1", "renewed", 2)]) == COMPLETED


def test_bucket_telephony_failed_requires_every_call_failed():
    f = lambda c, m: [init(c, mins=m), ev("call.telephony_failed", c, mins=m + 1)]
    assert bucket_of(f("c1", 0)) == TEL_FAIL
    assert bucket_of(f("c1", 0) + f("c2", 10)) == TEL_FAIL
    assert bucket_of(f("c1", 0) + [init("c2", mins=10)]) == DIALED


def test_bucket_dialed_for_everything_else():
    assert bucket_of([init("c1"), disp("c1", "callback_requested", 1)]) == DIALED
    assert bucket_of([init("c1"), ev("call.connected", "c1", mins=1)]) == DIALED


def test_override_can_make_a_terminal_call_non_terminal():
    evs = [init("c1"), disp("c1", "renewed", 1), disp("c1", "callback_requested", 2, override=True)]
    d, (_, _, rows) = run(evs, {1: LEADS[1]})
    assert rows[0][4] == DIALED and rows[0][9] == "false" and rows[0][8] == "callback_requested"


def test_override_can_make_a_non_terminal_call_terminal():
    assert bucket_of([init("c1"), disp("c1", "callback_requested", 1), disp("c1", "not_interested", 2, override=True)]) == COMPLETED


def test_lead_bucket_function_directly():
    assert lead_bucket([], lambda c: True) == YET


# --- lead_report ------------------------------------------------------------------------

def test_lead_row_fields():
    evs = [init("c1", mins=0), ev("call.connected", "c1", mins=1), disp("c1", "renewed", 2),
           init("c2", mins=60), init("c3", mins=120), disp("c3", "callback_requested", 121)]
    _, (_, _, rows) = run(evs, {1: LEADS[1]})
    # IST = UTC+5:30; c3 starts 04:30+120min = 06:30 UTC = 12:00 IST
    assert rows == [[WF, "P1", "batch_001", "+91******1111", "completed", 3, 1,
                     "2026-10-05T12:00:00+05:30", "callback_requested", "true"]]


def test_current_disposition_skips_latest_call_without_one():
    evs = [init("c1", mins=0), disp("c1", "not_interested", 1), init("c2", mins=60)]
    _, (_, _, rows) = run(evs, {1: LEADS[1]})
    assert rows[0][7].endswith("+05:30") and rows[0][8] == "not_interested"


def test_lead_with_no_calls_has_blank_fields_and_false():
    _, (_, _, rows) = run([], {1: LEADS[1]})
    assert rows == [[WF, "P1", "batch_001", "+91******1111", YET, 0, 0, "", "", "false"]]


def test_lead_report_sorted_by_workflow_then_external_ref_codepoint():
    leads = {1: LeadRow(1, WF, "b", "batch_001", "1"), 2: LeadRow(2, WF, "B", "batch_001", "2"),
             3: LeadRow(3, "policy_feedback", "A", "batch_001", "3"), 4: LeadRow(4, WF, "a", "batch_001", "4"),
             5: LeadRow(5, WF, "MOT/2026/99", "batch_001", "5"), 6: LeadRow(6, WF, "MOT/2026/100", "batch_001", "6")}
    _, (_, _, rows) = run([], leads)
    assert [(r[0], r[1]) for r in rows] == [(WF, "B"), (WF, "MOT/2026/100"), (WF, "MOT/2026/99"),
                                           (WF, "a"), (WF, "b"), ("policy_feedback", "A")]


# --- daily_overview --------------------------------------------------------------------------

def test_date_is_the_ist_date_of_call_initiated():
    late_utc = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)          # 00:30 IST on the 6th
    early_utc = datetime(2026, 10, 5, 18, 29, tzinfo=UTC)        # 23:59 IST on the 5th
    _, (_, daily, _) = run([init("c1", when=late_utc), init("c2", when=early_utc)])
    assert [(r[1], r[2]) for r in daily] == [("2026-10-05", 1), ("2026-10-06", 1)]


def test_daily_counts_rate_and_distinct_success_leads():
    evs = []
    for i in range(16):                                      # 16 calls, 9 connected -> 0.5625
        evs.append(init(f"c{i}", lead=1 if i < 4 else 2, mins=i))
        if i < 9:
            evs.append(ev("call.connected", f"c{i}", lead=1 if i < 4 else 2, mins=i + 100))
    evs += [disp("c0", "renewed", 200), disp("c1", "renewed", 201),   # lead 1 twice -> counts once
            disp("c5", "renewed", 202, lead=2), disp("c6", "not_interested", 203, lead=2)]
    _, (_, daily, _) = run(evs)
    assert daily == [[WF, "2026-10-05", 16, 9, "0.5625", 2]]


def test_connectivity_rate_uses_python_four_decimal_formatting():
    evs = [init(f"c{i}", mins=i) for i in range(32)] + [ev("call.connected", "c0", mins=99)]
    _, (_, daily, _) = run(evs)
    assert daily[0][4] == "0.0312"        # 1/32 = 0.03125: exact tie -> round-half-even (decision, see logbook)


def test_success_leads_counts_effective_disposition_on_that_calls_date():
    d2 = datetime(2026, 10, 6, 4, 30, tzinfo=UTC)
    evs = [init("c1", when=T0), disp("c1", "not_interested", 1), init("c2", when=d2), disp("c2", "renewed", 2)]
    _, (_, daily, _) = run(evs)
    assert [(r[1], r[5]) for r in daily] == [("2026-10-05", 0), ("2026-10-06", 1)]


# --- batch_summary --------------------------------------------------------------------------------

def test_batch_summary_counts_every_lead_by_permanent_batch():
    leads = {1: LeadRow(1, WF, "P1", "batch_001", "1"), 2: LeadRow(2, WF, "P2", "batch_001", "2"),
             3: LeadRow(3, WF, "P3", "batch_002", "3"), 4: LeadRow(4, "policy_feedback", "O1", "batch_001", "4")}
    evs = [init("c1", lead=1), disp("c1", "renewed", 1, lead=1),
           init("c2", lead=2), ev("call.telephony_failed", "c2", lead=2, mins=1),
           init("c3", lead=3)]
    _, (batch, _, _) = run(evs, leads)
    assert batch == [
        [WF, "batch_001", 2, 0, 1, 0, 1],
        [WF, "batch_002", 1, 0, 0, 1, 0],
        ["policy_feedback", "batch_001", 1, 1, 0, 0, 0],
    ]
