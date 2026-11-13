"""Parse one event line. Pure: no I/O, no database.

A line is MALFORMED (count it, skip it) when it is not valid JSON, is not a JSON object, has no
usable event_id (spec), or lacks a usable event_type / occurred_at (decision made with the user:
the ledger needs a real time and type to be meaningful). Everything else is a valid Event, even
when its phone is missing or invalid: such an event is simply unlinkable (an orphan, decided
later at report time).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from .parsers import parse_phone, parse_timestamp


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
    ts = d.get("occurred_at")
    if not isinstance(ts, str):
        return None
    try:
        occurred_at = parse_timestamp(ts)
    except ValueError:
        return None
    raw_phone = d.get("phone")
    phone = parse_phone(raw_phone.strip()) if isinstance(raw_phone, str) and raw_phone.strip() else None
    payload = d.get("payload")
    return Event(event_id, event_type, _str_or_none(d.get("workflow_id")), _str_or_none(d.get("call_id")),
                 occurred_at, phone, payload if isinstance(payload, dict) else {})
