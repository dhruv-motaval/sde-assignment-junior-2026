"""Parse one event line. Pure: no I/O, no database.

A line is MALFORMED (count it, skip it) when it is not valid JSON, is not a JSON object, has no
usable event_id (spec), or lacks a usable event_type / occurred_at (decision made with the user:
the ledger needs a real time and type to be meaningful). Also malformed, because the database
could not store them and one bad line must never block a whole file: strings containing NUL,
payloads containing NaN/Infinity or invalid Unicode, and timestamps so extreme that they cannot be
converted to UTC/IST. Everything else is a valid Event, even when its phone is missing or invalid:
such an event is simply unlinkable (an orphan, decided later at report time).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from .parsers import IST, parse_phone, parse_timestamp


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    workflow_id: str | None
    call_id: str | None
    occurred_at: datetime        # timezone-aware UTC (naive input was IST)
    phone: str | None            # canonical +91XXXXXXXXXX, or None (missing / invalid / override)
    payload: dict = field(default_factory=dict)


def _str_or_none(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _has_nul(v) -> bool:
    """True if any string in a (nested) JSON value contains NUL, which PostgreSQL cannot store."""
    if isinstance(v, str):
        return "\x00" in v
    if isinstance(v, dict):
        return any(_has_nul(k) or _has_nul(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_has_nul(x) for x in v)
    return False


def payload_text(payload: dict) -> str:
    """JSON text for the jsonb column. Raises ValueError for NaN/Infinity and for text that is not valid Unicode."""
    text = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    try:
        text.encode("utf-8")  # lone surrogates (from "\ud800"-style escapes) cannot be encoded
    except UnicodeEncodeError as e:
        raise ValueError("payload is not valid Unicode") from e
    return text


def parse_event_line(line: str) -> Event | None:
    """Return the Event, or None if the line is malformed."""
    try:
        d = json.loads(line)
    except ValueError:  # includes JSONDecodeError (truncated lines)
        return None
    if not isinstance(d, dict):
        return None
    event_id, event_type = _str_or_none(d.get("event_id")), _str_or_none(d.get("event_type"))
    if event_id is None or event_type is None:
        return None
    workflow_id, call_id = _str_or_none(d.get("workflow_id")), _str_or_none(d.get("call_id"))
    payload = d.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    ts = d.get("occurred_at")
    if not isinstance(ts, str):
        return None
    try:
        occurred_at = parse_timestamp(ts)
        occurred_at.astimezone(IST)   # reports convert to IST: reject what cannot be converted (year 9999 ...)
        payload_text(payload)
    except (ValueError, OverflowError):
        return None
    if any(_has_nul(x) for x in (event_id, event_type, workflow_id, call_id, payload)):
        return None
    raw_phone = d.get("phone")
    phone = parse_phone(raw_phone.strip()) if isinstance(raw_phone, str) and raw_phone.strip() else None
    return Event(event_id, event_type, workflow_id, call_id, occurred_at, phone, payload)
