# Submission

## Candidate
- **Name:** Dhruv Motaval
- **Assignment chosen:** A (Data): Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** about 1.5 h of my own time directing the work and answering design
  questions. Nearly all code, tests and docs were written by an AI assistant (Claude Code); see
  [`AI_USAGE.md`](AI_USAGE.md). I keep one file to understand the code whenever needed:
  [`solution/docs/CODE_GUIDE.md`](solution/docs/CODE_GUIDE.md).

## How to run
Needs Python 3.11+ (developed and tested on 3.14.4) and Docker (for PostgreSQL 16). From a clean clone:

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
docker compose up -d                                   # Postgres on localhost:5432; wait ~5 s

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/               # writes the five files

pytest                                                 # 183 tests, ~20 s, needs the Postgres container

python -m solution reset --yes                         # empty the database BEFORE ingesting a different seed
```

- Database: `$DATABASE_URL`, default `postgresql://au:au_local_only@localhost:5432/au_assignment`
  (the `docker-compose.yml` credentials).
- PII key: `$AU_PII_KEY`, otherwise `solution/.pii_key` is created on first use (git-ignored). **Keep it with
  the database**: a database built with one key refuses to run with another (`PiiKeyMismatch`).
- Tests use throw-away Postgres schemas and never touch real data. If Postgres is down, database tests
  skip with a hint. Real-data tests use `data/` if it exists.
- Verified from a fresh clone with a different seed (7): setup, ingest, report, re-ingest, `pytest`
  (183 passed), `reset`.

## Design
**Idea:** store raw facts, derive everything else. Leads go into a table; every event goes into an
**append-only ledger**; the five reports are computed from those at `report` time and nothing derived
is stored. Because "orphan", "call", "bucket" are never stored, a lead arriving in `drop_2` automatically
turns `drop_1`'s orphan events into calls, and the result does not depend on load order (tested).

```
 CSV leads ──► header map ► validate ► reject/warn ► merge ──► leads  (phone = HMAC, name = encrypted)
 JSONL events ► parse ► malformed? ► duplicate? ──────────────► events (append-only, phone = HMAC)
                                                                   │
                      SQL join events ⟕ leads on (workflow, phone hash)
                                                                   ▼
                  Python derive: orphans, unknown dispositions, calls, effective disposition, buckets
                                                                   ▼
                       batch_summary · daily_overview · lead_report · rejections · data_quality
```

**Schema (PostgreSQL)**

| Table | Purpose |
|---|---|
| `leads` | one row per `(workflow_id, phone_hash)` (UNIQUE = lead identity); `phone_last4`; permanent `batch_id`; `external_ref`; `pii` jsonb (Fernet tokens); `attributes` jsonb (other non-PII fields) |
| `rejections` | `workflow_id, batch_id, row_number, reason`: no PII |
| `events` | the ledger. `event_id` PK; UTC `occurred_at`; `phone_hash` (NULL for overrides and invalid phones); `payload` jsonb. **Triggers block UPDATE / DELETE / TRUNCATE** |
| `ingested_files` | `(drop, path)` PK + SHA-256 + per-file counters; written in the same transaction as the file's rows |
| `malformed_lines` | file + line number only (a truncated line can contain a phone) |
| `dispositions` | the catalogue from `dispositions.csv` |
| `meta` | fingerprint of the PII key |

**PII at rest.** Phone = HMAC-SHA256 with a secret key (a plain hash of a 10-digit mobile number could be
reversed by hashing all ~4 billion numbers); still deterministic so events match leads by equality. Only the
hash and last 4 digits are kept; the full number is never stored. Name = Fernet encryption. Two sub-keys are
derived from one master key. Reports contain only masked phones; a test scans every table and output file for
plaintext.

**Idempotent and incremental.** Each file is ingested in one transaction together with its `ingested_files`
row, so "listed" means "fully applied". Re-running a drop skips its files (0.9 s on the large set). The same
path with different content is refused instead of silently double-counted.

**Reports.** One SQL query links events to leads; pure Python functions (`solution/report.py`, `derive`)
apply the spec's rules. Sorting is done in Python (code-point order) so a database collation cannot
disagree with the grader.

More detail, and why each step was built this way with the alternatives, is in
[`solution/docs/`](solution/docs/) (one file per step) and [`solution/LOGBOOK.md`](solution/LOGBOOK.md).

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| PostgreSQL | closer to production; my choice | SQLite (zero setup for a reviewer; was the AI's recommendation) |
| Reports derived at `report` time, nothing stored | late leads and any load order just work | store calls / buckets at ingest and relink |
| Events stored even if orphaned; orphan decided at report time | an event can precede its lead by a drop | drop orphans at ingest |
| Phone: HMAC with secret key; keep only hash + last 4 | no plaintext PII, still matchable | store encrypted phone; plain SHA-256 |
| Name: Fernet encryption | reversible, record stays complete | keyed hash; do not store |
| Key: env var, else auto-created git-ignored file; DB remembers key fingerprint | works from a clean clone; wrong key fails fast | env var required |
| Workflow-specific fields in `attributes jsonb` | one table, merge in SQL with `||` | table per workflow; EAV |
| Header normalisation: lower, trim, then collapse `_`/whitespace (spec order) | literal reading | collapse then trim |
| Two columns match one field: first column wins | deterministic | first non-empty; reject file |
| Required non-phone field present but invalid: reject `invalid_<field>` | consistent with `invalid_phone` (no schema triggers it) | treat as missing |
| Folder under `leads/` without a schema: skip with a warning | one bad folder must not stop a drop | fail ingest |
| Defaults applied on save, to still-null fields | same result as at the end | apply at read time |
| Field warnings also counted for merged duplicate rows | merged rows are valid, not rejected | count only new leads |
| Decimal rounding half-up; 1-digit day/month accepted | what people expect for money; `strptime` also accepts it | half-even; strict 2-digit |
| Event with bad/missing `occurred_at` or `event_type`, non-object JSON, empty/non-string `event_id`: counted malformed | ledger needs a real time and type | placeholder time; fail the file |
| Malformed lines: keep file + line number only | truncated lines can contain a phone | raw text; count only |
| Event with invalid or missing phone: stored with NULL hash, an orphan (the generator emits ~150 of these) | spec: events link by normalised phone | treat as linked via `call_id` |
| Same `event_id`, different content: first copy wins | spec: skip later duplicates | last wins |
| Load events with bulk `COPY` per file | 600k events in the 2-minute goal | row-by-row insert |
| "Latest" = `(occurred_at, event_id)`; tie goes to higher `event_id` | deterministic, independent of load order | later-ingested wins |
| A call with several `call.initiated`: earliest | simplest deterministic rule | latest |
| Linked event whose call has no linked initiated: not an orphan, creates no call | it did link by phone | count as orphan |
| `connectivity_rate` = Python `f"{x:.4f}"` (exact ties round half-even, e.g. 1/32 -> `0.0312`) | most likely what a reference does | Decimal half-up (`0.0313`) |
| `batch_summary`: only batches that have leads | `total_leads` counts leads | list empty batches with zeros |
| `lead_report` ties: batch_id, masked phone, lead id | spec only gives workflow, external_ref | none |
| `rejections_by_reason`: only reasons that occurred | natural Counter output | all possible reasons with 0 |
| `last_call_at` to the second, `+05:30` | spec example | include fractions |
| Lead loading: staging table + one UPDATE + one INSERT per file | 101 s to 13 s on large data | `executemany`; leave as is |
| `reset --yes` drops our tables, keeps the key file | TRUNCATE is blocked by the append-only trigger | truncate; docs only |

## Failure modes considered
- **Bad lead rows:** missing/invalid fields are rejected with one reason (schema order) or become null + warning.
  Blank rows are skipped and not counted; short rows, BOM, reordered/renamed/extra columns are handled.
- **Bad event lines:** malformed lines are counted and located (file + line); duplicates, including across
  drops and within a file, are counted and skipped (first copy wins).
- **Late data:** events before their lead are kept and link later; overrides link through `call_id`.
- **Re-runs:** a file already ingested is skipped; counters do not move. A changed file under the same name is
  refused (`FileContentChanged`).
- **Crash mid-file:** the file's transaction rolls back completely (tested for both leads and events); a retry
  starts clean.
- **Lost or wrong PII key:** detected through a stored fingerprint; ingest stops with a clear message.
- **Append-only ledger:** the database itself rejects UPDATE / DELETE / TRUNCATE on `events`.
- **Not handled / known limits:**
  - One writer at a time. Two parallel ingests could race on the lead merge.
  - A non-UTF-8 lead CSV raises an error (event files tolerate bad bytes per line).
  - `report` loads every event into memory (fine for ~600k, not for months of 100k leads/day).
  - No key rotation. The key lives next to the code; production would use a KMS.
  - Docker/Postgres must be running; there is no SQLite fallback.
  - If the same `call_id` appears with two different leads' phones, only the initiated event's lead owns the call.

## Testing
**183 tests**, ~20 s, pass on a fresh clone with a different seed.
- **Parsers** (72): all six phone formats, invalid phones, dates incl. impossible and day-first, money prefixes
  and `NaN`/`Infinity`, enum, header normalisation, timestamps in all three forms (including a case where string
  order is wrong but datetime order is right).
- **Lead ingestion** (22): header aliases/reorder/extra columns, BOM, blank/short rows, every rejection reason,
  schema-order precedence, warnings, merge semantics, defaults, row-accounting invariant.
- **Database** (about 20): no plaintext PII in any table, wrong-key detection, append-only triggers, idempotent
  re-ingest, changed-file refusal, crash rollback, bulk-merge edge cases (phone repeated in a file, no defaults
  or nulls over an existing lead), `reset`.
- **Events** (34): malformed variants, three timestamp forms, duplicates across chunks/files/drops, line numbers,
  rollback.
- **Reports** (about 32): hand-built cases for effective disposition (override vs later set, unknown codes,
  ties), orphans, bucket order, IST midnight boundary, rounding, sorting; end to end late-arriving lead, file
  format, CLI, load-order independence, and an **independent oracle** (a from-scratch implementation over the raw
  files with no database) that matches `lead_report`, `daily_overview`, `batch_summary` and the orphan/unknown
  counts exactly.
- **Not tested:** parallel ingest, non-UTF-8 CSVs, other Postgres versions, Python 3.11 specifically (only
  3.14.4 was run), and any hidden-grader expectations. The oracle was written by the same author as the pipeline,
  so a shared misreading of the spec would not be caught.

## Results
Laptop-class Windows machine, Postgres 16 in Docker on the same machine, one run each; expect a few seconds of
variation.

| Dataset | ingest drop_1 | ingest drop_2 | report | total |
|---|---|---|---|---|
| **large** (seed 42): 132,903 leads, 613,481 events | 31.5 s | 26.9 s | 8.5 s | **66.8 s** (goal: under 2 min) |

Small set (seed 42: 1,892 leads, 8,500 events): both drops ingest in about 1.4 s and the whole ingest + report
run takes about 1.7 s.

- Before the staging-table change the large run took 152 s (leads 101 s, events 39 s, report 11 s).
  Re-ingesting a drop is 0.9 s because every file is skipped.
- Row accounting holds at both scales, e.g. large: 143,990 lead rows = 7,097 rejected + 3,990 merged + 132,903
  new; 632,602 event lines = 631 malformed + 18,490 duplicates + 613,481 stored.
- **Surprising in the data:** (1) about 150 stored events have unusable phones (`N/A`, landlines, 9-digit, a US
  number). They are orphans, even when the same call has other linked events. (2) The same call's events use
  different phone formats, so phones must be normalised per event before hashing. (3) `batch_001` exists in both
  workflows, so a batch is only unique together with its workflow. (4) All overrides have a `workflow_id` but
  no phone. (5) The sample has no ties and no calls with several initiated events, so those rules are untested
  by data.
- Seed 42 small: 1,892 leads = 816 completed, 526 in_progress_dialed, 512 yet_to_reach_out, 38
  in_progress_telephony_failed; 2,536 calls, 1,467 connected; 462 orphan events; 18 unknown dispositions.

**Production note (100k leads/day), short version.** Scheduler triggers `ingest` per drop and `report` on a
cadence; retries are safe (idempotent per file). Alert on rates (malformed, rejected, orphan %) and missing drops;
the row-accounting invariants double as health checks. Backfills and late data need only a re-run of `report`;
corrections arrive as new events. At that scale, move the aggregation into SQL and make it incremental by IST
date, partition `events` by month, move the PII key to a KMS with key ids for rotation, and add a lock for
parallel ingest. Full note: [`solution/docs/07-speed-reset-and-production.md`](solution/docs/07-speed-reset-and-production.md).

## If I had another day
1. Work through every module with [`solution/docs/CODE_GUIDE.md`](solution/docs/CODE_GUIDE.md) (the one file that
   explains the code and where to change it) and try its "where to edit" changes.
2. Re-run the load-order and idempotency checks at large scale, and on Python 3.11.
3. Move report aggregation into SQL (incremental by date) so it scales past memory.
4. Speed up event loading (parallel workers per file), now the slowest stage.
5. A SQLite fallback so a reviewer without Docker can run it; a lock for parallel ingest; PII key rotation.
6. Re-check the guesses about the grader (rounding, `rejections_by_reason`, empty batches, tie-breaks) if the
   expected files turn out to differ.
