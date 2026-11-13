"""Lead CSV ingestion, in memory. Persistence to the database comes in step 4.

Layers (each testable on its own):
  load_schemas   JSON -> Schema
  read_rows      CSV file -> (row_number, {field_name: raw cell})   header mapping + blank-row rules
  evaluate_row   raw cells -> (rejection reason | normalised values, warnings)
  LeadBook       merge rule: first valid row creates the lead, later ones fill non-null fields
  ingest_file    glue: one CSV through all of the above -> FileResult counters
"""
from __future__ import annotations

import csv
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from .parsers import (
    normalize_header, parse_date, parse_decimal, parse_enum, parse_phone,
)


NUL = chr(0)  # PostgreSQL text cannot hold NUL
csv.field_size_limit(1 << 24)  # default 128 KB makes one huge cell crash the whole file

# --- schema ------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    name: str
    type: str  # string | phone | date | decimal | enum
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class Schema:
    workflow_id: str
    fields: tuple[Field, ...]

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    def header_map(self, headers: list[str]) -> dict[int, Field]:
        """column index -> field. Unmatched columns are ignored. If two columns match the same
        field, the first one wins (spec is silent; recorded in the logbook)."""
        lookup: dict[str, Field] = {}
        for f in self.fields:
            for n in (f.name, *f.aliases):
                lookup.setdefault(normalize_header(n), f)
        mapped: dict[int, Field] = {}
        used: set[str] = set()
        for i, h in enumerate(headers):
            f = lookup.get(normalize_header(h))
            if f and f.name not in used:
                mapped[i] = f
                used.add(f.name)
        return mapped


def load_schemas(schema_dir: str | Path) -> dict[str, Schema]:
    out = {}
    for p in sorted(Path(schema_dir).glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[d["workflow_id"]] = Schema(d["workflow_id"], tuple(
            Field(name=f["name"], type=f["type"], required=f.get("required", False),
                  pii=f.get("pii", False), reference=f.get("reference", False),
                  aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
                  default=f.get("default"))
            for f in d["fields"]))
    return out


# --- rows --------------------------------------------------------------------

def read_rows(path: str | Path, schema: Schema):
    """Yield (row_number, {field_name: raw cell}) for each NON-blank data row.

    row_number counts non-blank data rows from 1 (header and blank rows are not counted).
    utf-8-sig strips an Excel BOM that would otherwise corrupt the first header name.
    """
    # errors="replace": one bad byte must not crash the file; NUL cannot be stored by PostgreSQL, so drop it
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(line.replace(NUL, "") for line in fh)
        header = next(reader, None)
        if header is None:
            return
        cmap = schema.header_map(header)
        n = 0
        for cells in reader:
            if all(not c.strip() for c in cells):  # also true for [] (empty line)
                continue
            n += 1
            # a short row simply has missing cells; cells beyond the header are ignored
            yield n, {f.name: (cells[i] if i < len(cells) else "") for i, f in cmap.items()}


def _parse_cell(f: Field, cell: str):
    if f.type == "string":
        return cell
    if f.type == "phone":
        return parse_phone(cell)
    if f.type == "date":
        return parse_date(cell)
    if f.type == "decimal":
        return parse_decimal(cell)
    if f.type == "enum":
        return parse_enum(cell, list(f.values))
    raise ValueError(f"unknown field type {f.type!r}")


def evaluate_row(schema: Schema, raw: dict[str, str]):
    """Walk fields in schema order. Returns (reason, values, warnings).

    First failure rejects the row with a single reason and no values.
    Optional field present but invalid -> None + one warning (warnings only count if the row
    survives, which is why a rejected row returns 0).
    """
    values: dict[str, object] = {}
    warn = 0
    for f in schema.fields:
        cell = raw.get(f.name, "").strip()
        if not cell:
            if f.required:
                return f"missing_required:{f.name}", None, 0
            values[f.name] = None
            continue
        v = _parse_cell(f, cell)
        if v is None:
            if f.type == "phone":
                return "invalid_phone", None, 0
            if f.required:  # spec defines no reason for this; no current schema triggers it
                return f"invalid_{f.name}", None, 0
            warn += 1
        values[f.name] = v
    return None, values, warn


# --- merge -------------------------------------------------------------------

@dataclass
class Lead:
    workflow_id: str
    phone: str
    batch_id: str  # permanent: the batch that first created the lead
    values: dict[str, object]


def apply_defaults(schema: Schema, values: dict[str, object]) -> dict[str, object]:
    """Defaults apply after merging, to fields that are still null."""
    defaults = {f.name: f.default for f in schema.fields if f.default is not None}
    return {k: (defaults.get(k) if v is None else v) for k, v in values.items()}


class LeadBook:
    """In-memory reference implementation of the merge rule.

    Step 4 re-implements this against the database; this class stays as the test oracle.
    """

    def __init__(self):
        self.leads: dict[tuple[str, str], Lead] = {}

    def add(self, schema: Schema, batch_id: str, values: dict[str, object]) -> bool:
        """True if this row created a new lead, False if it merged into an existing one."""
        key = (schema.workflow_id, values[schema.phone_field.name])
        lead = self.leads.get(key)
        created = lead is None
        if created:
            lead = self.leads[key] = Lead(schema.workflow_id, key[1], batch_id, dict(values))
        else:
            for k, v in values.items():
                if v is not None:  # last write wins, but only where the new value is non-null
                    lead.values[k] = v
        # Defaults are applied on save (decision: logbook). Same result as applying at the very
        # end, because a default only ever fills a field that is still null.
        lead.values = apply_defaults(schema, lead.values)
        return created


# --- one file ----------------------------------------------------------------

@dataclass
class FileResult:
    workflow_id: str
    batch_id: str
    rows_total: int = 0
    new_leads: int = 0
    merged: int = 0
    field_warnings: int = 0
    rejections: list[tuple[int, str]] = field(default_factory=list)  # (row_number, reason)

    @property
    def rows_rejected(self) -> int:
        return len(self.rejections)


def ingest_file(book: LeadBook, schema: Schema, path: str | Path) -> FileResult:
    path = Path(path)
    res = FileResult(schema.workflow_id, path.stem)
    for n, raw in read_rows(path, schema):
        res.rows_total += 1
        reason, values, warn = evaluate_row(schema, raw)
        if reason:
            res.rejections.append((n, reason))
            continue
        res.field_warnings += warn
        if book.add(schema, res.batch_id, values):
            res.new_leads += 1
        else:
            res.merged += 1
    return res


def discover_lead_files(drop_dir: str | Path, schemas: dict[str, Schema]) -> list[tuple[str, Path]]:
    """(workflow_id, csv path) for one drop, in deterministic order: workflow, then filename."""
    out = []
    for wf_dir in sorted(p for p in (Path(drop_dir) / "leads").glob("*") if p.is_dir()):
        if wf_dir.name not in schemas:
            warnings.warn(f"no schema for workflow {wf_dir.name!r}; skipping {wf_dir}")
            continue
        out += [(wf_dir.name, f) for f in sorted((p for p in wf_dir.glob("*") if p.suffix.lower() == ".csv"),
                                             key=lambda p: p.name)]
    return out
