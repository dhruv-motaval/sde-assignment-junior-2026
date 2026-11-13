# Step 8: Edge-case hardening before submitting

Code touched: [`../events.py`](../events.py), [`../parsers.py`](../parsers.py), [`../leads.py`](../leads.py),
[`../store.py`](../store.py), [`../db.py`](../db.py), [`../cli.py`](../cli.py).
New tests: [`test_edge_cases.py`](../tests/test_edge_cases.py) (29). Whole suite: **212 passed, 0 skipped**.

## What we did

Before submitting, we tried to break the pipeline on purpose, in three ways:

1. **More data seeds.** The full suite (including the independent oracle) on seeds 1, 2, 3, 99 and 2026: all 183
   tests passed on every one.
2. **Adversarial probes.** A throw-away script (about 50 hostile inputs) fed odd files to the ingest functions and
   the command line, and printed OK or CRASH for each. It found real problems (below).
3. **Large-scale checks we had skipped.** Load-order independence and idempotency on the 133k-lead / 613k-event
   dataset (reports byte-identical; every file skipped on re-ingest).

## What the probes found, and the fix

The rule we enforced: **one bad record must never crash or block the rest of a file** (same spirit as the
spec's malformed-line rule).

| Input | What happened before | Now |
|---|---|---|
| Event string containing a NUL character (`\u0000`) | **crash**: PostgreSQL text cannot hold NUL, so the whole file's transaction failed | line counted as malformed |
| Payload with `NaN` / `Infinity` (JSON allows it in Python) | **crash**: jsonb rejects it | malformed |
| Payload with an invalid Unicode escape (`"\ud800"`) | would crash on the way to the database | malformed |
| Timestamp in year 0001, or one that rolls to year 10000 (`9999-12-31T23:59:59Z`) | **crash** (`OverflowError`) while loading, and it would also have crashed `report` when converting to IST | malformed |
| UTF-8 BOM at the start of an event file | **silent data loss**: the first event was counted malformed | BOM ignored, event kept |
| Lower-case `z` timestamp (`...00z`) | counted malformed | accepted |
| Lead CSV with an invalid UTF-8 byte | **crash** (`UnicodeDecodeError`) for the whole file | bad byte replaced, row still processed |
| Lead CSV cell with NUL | crashed when it landed in the reference column | NULs stripped while reading |
| Lead CSV with a 1 MB cell | **crash** (`csv` field limit 128 KB) | limit raised to 16 MB |
| `batch_009.CSV` / `.JSONL` (upper-case extension) | skipped on case-sensitive file systems (Linux/macOS), found on Windows | found everywhere |
| Two drops that both contain `batch_001` | would have **crashed** on the `rejections` primary key (a rejection at row 1 in both) | `rejections` now remembers its source file |
| `ingest data/drop_99` (typo) or `ingest data` (the parent) | exit code 0, no output, nothing ingested | `error: ...` with exit code 2; the parent case suggests `data/drop_1`, `data/drop_2` |
| Postgres not up yet (`docker compose up -d` returns before it accepts connections) | a long hang, then a traceback | waits up to 30 s (`--wait`), then a one-line error with the fix |
| Wrong key / changed file | traceback | one-line `error: ...`, exit code 1 |

Things that were already fine (verified, now covered by tests): CRLF line endings, quoted multi-line cells,
commas and quotes in a reference, emoji and Indian-script names, a 100,000-character event id, empty files,
header-only files, report on an empty database (header-only files, zero counters), ingesting the same drop
with a trailing slash or as an absolute path.

## Why we did it this way

- **Malformed rather than fatal.** The database cannot store these values, so the only choices are crash,
  silently fix, or skip-and-count. Counting is consistent with the spec's treatment of bad lines and keeps good
  data flowing; the line stays locatable (file + line number).
- **Strip NUL / replace bad bytes in leads, but skip in events.** A lead row costs a customer if it is dropped,
  and a replacement character in a name is harmless; an event with a NUL in its id has no sensible repair.
- **Reject extreme timestamps at ingest.** Reports convert every time to IST, so a value that cannot be converted
  would otherwise pass ingest and crash `report` hours later, which is the worst time to find out.
- **Fail fast with a clear message at the CLI.** A reviewer who mistypes a path or starts the tool a few seconds
  before Postgres is ready should see what to do, not a stack trace or silent success.

## Alternatives we could have used

| Alternative | Why not |
|---|---|
| Let the transaction fail and tell the user to fix the file | One bad line would block thousands of good ones; the spec expects bad data to be counted. |
| Sanitise events (strip NUL, clamp dates) | Invents data. A repaired timestamp could change a call's date or a disposition. |
| Guess Excel-damaged phones (`9.87654321E9`, `9876543210.0`) | The spec's rule is exact (strip non-digits, then validate). `9876543210.0` is rejected and `9.87654321E9` becomes `+919876543219`, as the rule says. Guessing would diverge from the grader. |
| Auto-detect the CSV delimiter (`;`) | Not in the spec; such a file is rejected row by row (`missing_required`), which is visible. |
| Retry forever on connection failure | A mistyped URL would hang. A bounded wait (30 s) covers container start-up. |
| Keep the old `rejections` key and fail on duplicate batch names | The spec does not promise unique batch names across drops; a crash is a worse answer than a slightly wider key. |

## Findings

- **212 tests pass** (183 + 29 edge-case tests). All 183 earlier tests also pass on five other data seeds.
- **Large dataset, order independence:** drop_1 then drop_2 (72 s) versus events of both drops (newest first) and
  only then the leads (77 s) produce **byte-identical** reports. Re-ingesting all 133 files skips every one and
  the report is unchanged. This closes a gap listed in an earlier step.
- Probe results are one-time exploration; the regression tests are the lasting record.

## Known limits that remain (also in `SUBMISSION.md`)

- Everything was run on **Windows only**. Linux and macOS are untested (case-sensitive paths are now handled,
  but nothing has been executed there).
- Python 3.14.4 only; 3.11 is untested.
- One ingest writer at a time. No SQLite fallback. `report` holds all events in memory.
- Existing databases built before this step have the old `rejections` table; run `reset --yes` once (the project
  database was empty, so nothing was lost).
- A lead CSV with a bad byte is read with a replacement character; if that byte was inside a name the stored name
  differs from the file. We never alter phones this way (a bad byte there makes the phone invalid).
