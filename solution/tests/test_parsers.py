from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from solution.parsers import (
    normalize_header, parse_date, parse_decimal, parse_enum, parse_phone, parse_timestamp,
)


# --- headers ---
@pytest.mark.parametrize("raw,want", [
    ("Customer_Name", "customer name"),
    ("  customer   name ", "customer name"),
    ("MOBILE__NO", "mobile no"),
    ("Policy\tNo", "policy no"),
    ("policy_number", "policy number"),
])
def test_normalize_header(raw, want):
    assert normalize_header(raw) == want


# --- phone ---
@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210",
    "91-98765-43210", "(+91) 9876543210", "+919876543210",  # the six generator formats
])
def test_phone_valid_formats(raw):
    assert parse_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",            # 9 digits
    "5876543210",           # bad prefix
    "022-2345-6789",        # landline: 0 stripped -> 2234567... starts with 2
    "N/A", "", "+1 415 555 0134",
    "91 5876543210",        # 12 digits, 91 stripped, remainder starts with 5
    "9198765432101",        # 13 digits
    "0 0987654321",         # only ONE leading 0 may be dropped
])
def test_phone_invalid(raw):
    assert parse_phone(raw) is None


def test_ten_digit_number_starting_91_is_not_stripped():
    assert parse_phone("9187654321") == "+919187654321"


# --- date ---
@pytest.mark.parametrize("raw", ["2026-12-01", "01/12/2026", "01-12-2026", "01-Dec-2026",
                                 "01-dec-2026", "01-DEC-2026"])
def test_date_formats(raw):
    assert parse_date(raw) == date(2026, 12, 1)


def test_date_is_day_first():
    assert parse_date("01/02/2026") == date(2026, 2, 1)
    assert parse_date("13/02/2026") == date(2026, 2, 13)
    assert parse_date("02/13/2026") is None  # would be month 13


@pytest.mark.parametrize("raw", [
    "31/02/2026", "2026-02-30", "2026-13-01", "29/02/2027", "00/01/2026",
    "2026/12/01",           # not an accepted format
    "01-December-2026",     # only 3-letter months
    "01-Xyz-2026", "garbage", "",
])
def test_date_invalid(raw):
    assert parse_date(raw) is None


def test_date_leap_day():
    assert parse_date("29/02/2028") == date(2028, 2, 29)


# --- decimal ---
@pytest.mark.parametrize("raw,want", [
    ("27554", "27554.00"), ("₹27,554", "27554.00"), ("Rs. 26505", "26505.00"),
    ("Rs.26505", "26505.00"), ("rs 100", "100.00"), ("INR 33473.7", "33473.70"),
    ("inr5", "5.00"), ("₹ 5,000", "5000.00"), ("1,25,000", "125000.00"), ("0", "0.00"),
    ("12.345", "12.35"),    # half-up, not banker's rounding
    ("12.344", "12.34"), ("0.005", "0.01"),
])
def test_decimal_valid(raw, want):
    assert parse_decimal(raw) == Decimal(want)


@pytest.mark.parametrize("raw", ["-5", "-₹5", "abc", "NaN", "Infinity", "1e5", "", "₹", "Rs.", "5.",
                                 "$100", "1 000"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


# --- enum ---
def test_enum():
    langs = ["en", "hi", "ta"]
    assert parse_enum("HI", langs) == "hi"
    assert parse_enum("ta", langs) == "ta"
    assert parse_enum("fr", langs) is None


# --- timestamps ---
UTC = timezone.utc


@pytest.mark.parametrize("raw", [
    "2026-10-05T07:50:21Z",
    "2026-10-05T13:20:21+05:30",
    "2026-10-05 13:20:21",          # naive == IST
    "2026-10-05T13:20:21",
])
def test_timestamp_forms_agree(raw):
    assert parse_timestamp(raw) == datetime(2026, 10, 5, 7, 50, 21, tzinfo=UTC)


def test_timestamp_result_is_utc_aware():
    assert parse_timestamp("2026-10-05 13:20:21").utcoffset() == timedelta(0)


def test_timestamp_chronological_where_strings_are_not():
    a, b = "2026-10-05T10:00:00Z", "2026-10-05T15:00:00+05:30"  # b is 09:30Z, earlier than a
    assert a < b                                           # string order lies
    assert parse_timestamp(b) < parse_timestamp(a)         # datetime order is right


def test_timestamp_garbage_raises():
    with pytest.raises(ValueError):
        parse_timestamp("yesterday")
