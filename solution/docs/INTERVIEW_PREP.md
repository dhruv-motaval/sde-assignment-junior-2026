# Interview prep: read this before the 45-minute conversation

The interview is 15 min walking through the design, 20 min changing a requirement **live** (AI tools
allowed), 10 min your questions. The code is about 1,200 lines in 9 files plus 1,450 lines of tests, which
is readable in an evening. Be honest in the interview about which parts the AI wrote (see `AI_USAGE.md`).

## 1. Reading order (about 3 hours)

| # | File | Lines | What to be able to say about it |
|---|---|---|---|
| 1 | `parsers.py` | 102 | One small pure function per spec rule. Regex is the gate (not `Decimal()`, which accepts `NaN`). `parse_timestamp` turns all three forms into UTC. |
| 2 | `leads.py` | 228 | `read_rows` (header map, blank rows, row numbers) -> `evaluate_row` (schema order, first failure) -> `LeadBook` (the in-memory merge rule; also the test oracle). |
| 3 | `pii.py` | 70 | HMAC for phones (matchable, not reversible without the key), Fernet for names, one master key -> two sub-keys, key fingerprint. |
| 4 | `db.py` | 142 | The schema, the append-only triggers, `init_schema` key check, `reset_schema`. |
| 5 | `store.py` | 261 | `ingest_lead_file` (parse in memory, stage, UPDATE then INSERT, one transaction) and `ingest_event_file` (malformed -> duplicate -> store, `COPY`, one transaction). `_already_ingested` is the idempotency. |
| 6 | `events.py` | 55 | What counts as a malformed line. |
| 7 | `report.py` | 256 | `derive` (orphans, unknown, calls, effective disposition) -> `build_rows` (buckets, daily, lead rows, sorting) -> `build_report` / `write_report`. |
| 8 | `cli.py` | 75 | `ingest`, `report`, `reset`. |
| 9 | tests | 1,450 | Skim `test_report_derive.py` (hand-built cases) and the oracle at the bottom of `test_report_db.py`. |

Run any file's tests while reading: `pytest solution/tests/test_parsers.py -q`.

## 2. Thirty-second answers

- **Why derive reports on read instead of storing calls?** The ledger is the single source of truth. Late
  leads, re-runs and any load order then give the same files; nothing can drift.
- **How is a re-run safe?** Each file is one transaction that also writes its `ingested_files` row (with the
  file's SHA-256). Already ingested -> skipped; changed content -> refused.
- **What happens if the process dies mid-file?** The transaction rolls back; the file is not listed; a retry
  starts clean. (There is a test that crashes after the rows were inserted.)
- **How do events match leads without storing phones?** `HMAC(secret key, canonical phone)`; equal numbers give
  equal hashes. All six phone formats normalise to one number first.
- **Why not a plain SHA-256?** There are only ~4 billion Indian mobile numbers; anyone could hash them all.
- **What about an event that arrives before its lead?** It is stored; orphan status is computed at report time,
  so when the lead arrives the next report counts it.
- **How is an override handled?** It is a new event; the ledger is never edited. The effective disposition is the
  latest override, else the latest set. Overrides have no phone, so they link through `call_id`.
- **Why Postgres?** Closer to production and a real constraint/trigger model. Trade-off: a reviewer needs Docker.
- **What would you change at 100k leads/day?** Reports in SQL, incremental by date; partition `events`; KMS for the
  key; a lock for parallel ingest. (`solution/docs/07-...`)
- **What are you least sure about?** The guesses about the grader: rounding ties, `rejections_by_reason`
  contents, empty batches, tie-breaks (all in the `SUBMISSION.md` decisions table).

## 3. Likely live changes: where to edit

Practise a few of these. Each is small if you know where it lives.

| Requirement change | Edit | Then |
|---|---|---|
| Accept a new date format, e.g. `DD.MM.YYYY` | `parsers.py`: add a regex like `_DMY` and a branch in `parse_date` | add a case to `test_date_formats` |
| Accept a new currency prefix (`USD`) | `parsers.py`: the `_MONEY` regex | add a test row |
| Mobile numbers may start with 5 | `parsers.py`: `_MOBILE = [6-9]...` | adjust the invalid-phone test |
| Round `connectivity_rate` half-up | `report.py` `build_rows` daily rate: use `Decimal(...).quantize(..., ROUND_HALF_UP)` | update `test_connectivity_rate_...` |
| New bucket, e.g. `callback_pending` when the latest disposition is `callback_requested` | `report.py`: constants, `lead_bucket`, header names, `batch_summary` columns | tests in `test_report_derive.py` |
| `call.voicemail` counts as connected | `report.py` `derive` (the `CONNECTED` branch) | add a derive test |
| On duplicate `event_id`, the **last** copy wins | `store.py` `ingest_event_file` / `_flush_events` (currently first wins) | revisit `test_duplicates_across_files...` |
| Ties on `occurred_at`: later-ingested wins | `report.py`: the `(occurred_at, event_id)` key; would need ingest order from `events.ingested_at` | tests for ties |
| A new lead field (e.g. `city`) | only `schemas/*.json`: non-PII fields flow into `attributes` automatically | one lead test |
| Mark a field as PII (encrypt it) | the schema JSON: `"pii": true`; `store._split` already encrypts PII fields | check the PII-at-rest test |
| Show the phone masked differently | `report.py` `build_rows` (`"+91******" + last4`) | update the file-format test |
| Add a column to `lead_report` | `report.py`: `HEADERS`, `build_rows` row list | update tests that index rows |
| Reject rows whose premium is 0 | `leads.py` `evaluate_row` | add a rejection reason test |
| Also count warnings only for new leads | `leads.py` / `store.py`: where `field_warnings` is summed | decision table + test |

## 4. Things you can demo in 2 minutes

```bash
python -m solution ingest data/drop_1 && python -m solution ingest data/drop_2
python -m solution report --out reports/
python -m solution ingest data/drop_1          # everything skipped: idempotent
python -m solution reset --yes                 # empty database for a new seed
```

Then show the PII check in a SQL shell (`docker compose exec postgres psql -U au au_assignment`):
`SELECT * FROM leads LIMIT 3;` (hash, last 4, encrypted name) and
`UPDATE events SET event_type='x';` (rejected by the append-only trigger).
