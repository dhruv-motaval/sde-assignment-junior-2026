"""Pure value parsers. No I/O, no database: every rule from the spec lives here.

Every parser takes an already-trimmed, non-empty string and returns the normalised
value, or None when the value is invalid. (Empty -> "missing" is handled by the caller.)
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))

# --- headers ---------------------------------------------------------------

_SEP = re.compile(r"[_\s]+")


def normalize_header(h: str) -> str:
    """lower-case, trim, then collapse every run of '_' / whitespace to one space (spec order)."""
    return _SEP.sub(" ", h.lower().strip())


# --- phone -----------------------------------------------------------------

_MOBILE = re.compile(r"[6-9][0-9]{9}")


def parse_phone(raw: str) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    d = re.sub(r"[^0-9]", "", raw)  # ASCII digits only
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return "+91" + d if _MOBILE.fullmatch(d) else None


# --- date ------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ISO = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
_DMY = re.compile(r"([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{4})")  # DD/MM/YYYY and DD-MM-YYYY
_DMON = re.compile(r"([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})")


def parse_date(raw: str) -> date | None:
    """YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (day-first). Impossible dates -> None."""
    try:
        if m := _ISO.fullmatch(raw):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif m := _DMY.fullmatch(raw):
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        elif m := _DMON.fullmatch(raw):
            d, y = int(m[1]), int(m[3])
            mo = _MONTHS[m[2].lower()]
        else:
            return None
        return date(y, mo, d)
    except (ValueError, KeyError):  # 31/02, month 13, unknown month name
        return None


# --- decimal ---------------------------------------------------------------

# Optional currency prefix, optional space, then digits with optional commas / fraction.
# The regex (not Decimal()) is the gate: Decimal() would happily accept 'NaN', 'Infinity', '1e5'.
_MONEY = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
_CENTS = Decimal("0.01")


def parse_decimal(raw: str) -> Decimal | None:
    """Money amount >= 0, rounded half-up to 2 places. Negative or junk -> None."""
    m = _MONEY.fullmatch(raw)
    if not m:
        return None
    return Decimal(m[1].replace(",", "")).quantize(_CENTS, rounding=ROUND_HALF_UP)


# --- enum ------------------------------------------------------------------


def parse_enum(raw: str, values: list[str]) -> str | None:
    """Case-insensitive match against `values`; stored lower-case."""
    v = raw.lower()
    return v if v in {x.lower() for x in values} else None


# --- event timestamps --------------------------------------------------------


def parse_timestamp(s: str) -> datetime:
    """'...Z', '...+05:30' or naive ('2026-10-03 10:01:00', naive means IST) -> aware UTC datetime.

    Raises ValueError on garbage or out-of-range values. Always compare/sort the returned datetimes, never the raw
    strings: mixed Z / offset / naive strings do not sort chronologically.
    """
    s = s.strip()
    if s[-1:] == "z":  # accept a lower-case 'z' too
        s = s[:-1] + "Z"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    try:
        return d.astimezone(timezone.utc)
    except OverflowError as e:  # e.g. year 0001 in IST has no UTC equivalent
        raise ValueError(f"timestamp out of range: {s}") from e
