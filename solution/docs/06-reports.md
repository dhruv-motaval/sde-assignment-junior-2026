# Step 6: The five reports (and the CLI)

Code: [`../report.py`](../report.py) (derive + write), [`../cli.py`](../cli.py) + `__main__.py`
(commands), [`../store.py`](../store.py) (`load_dispositions`), [`../db.py`](../db.py)
(`dispositions` table). Tests: `test_report_derive.py` (25, no database), `test_report_db.py` (7).
Whole suite: **179 passed, 0 skipped**.

## What we did

Added the last piece of the pipeline and the commands to run it:

```bash
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/
```

`report` writes `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`
and `data_quality.json`. It needs Postgres running (`docker compose up -d`).

How a report is built:

1. **SQL fetches.** One query joins every ledger event to its lead on `(workflow_id, phone
   hash)` (a `LEFT JOIN`, so unlinked events come back with no lead).
2. **Python decides** (`derive`, pure functions):
   - *Orphan:* a non-override event with no lead, or an override whose call has no linked event.
   - *Unknown disposition:* a linked `disposition.*` event whose code is not in the workflow's
     catalogue. It is counted and ignored. (Orphan is checked first, as in the spec.)
   - *Call:* exists only if its `call.initiated` is linked. Connected / telephony-failed flags
     come from its other events.
   - *Effective disposition:* latest override, else latest set, else none.
3. **Roll up** into leads (bucket, counts, last call), days and batches, then write the files.

Nothing derived is stored. Every report is recomputed from the leads and the ledger.

## Decisions made with the user

| Question | Decision | Alternative not chosen |
|---|---|---|
| How to compute | **SQL fetches, Python decides** | All in SQL (views / CTEs) |
| Two events with identical `occurred_at` | **Higher `event_id` wins** (deterministic, independent of load order) | Later-ingested wins |
| `connectivity_rate` rounding | **Python `f"{x:.4f}"`** (an exact tie like 1/32 = 0.03125 gives `0.0312`) | Decimal half-up (`0.0313`) |
| `batch_summary` for a batch with no valid leads | **Not listed** (only batches that have leads) | List it with zeros |

## Other decisions (spec silent; written in the logbook for `SUBMISSION.md`)

- **Sorting happens in Python (code-point order), never in SQL.** A database collation can order
  `MOT/2026/100142` or upper/lower case differently from the grader.
- `lead_report` ties (same `workflow_id` and `external_ref`) are broken by `batch_id`, masked
  phone, then lead id. Stable, but the spec does not say.
- `rejections_by_reason` lists **only reasons that occurred** (no zero entries).
- A call with several `call.initiated` events uses the **earliest**. Never happens in the sample.
- If a call id appears with two different leads' phones, only the lead of its initiated event
  owns the call; the other lead's events are ignored for that call.
- An override whose own `workflow_id` is missing uses the workflow of its call's lead.
- `last_call_at` is written to the second (`+05:30`), as in the spec example.
- A linked event whose call has no linked `call.initiated` is **not** an orphan (it did link);
  it just does not create a call.

## Why we did it this way

- **Derive on demand, store nothing.** The ledger stays the single source of truth. Because
  orphan status is not stored, a lead arriving in `drop_2` automatically turns drop_1's orphans into
  calls, and loading the same data in a different order gives identical files (tested).
- **Pure `derive`.** It takes plain objects and returns plain objects, so 25 tests build tiny
  scenarios by hand (override vs later set, unknown codes, ties, IST midnight, bucket order...)
  with no database.
- **IST dates.** A call at 19:00 UTC belongs to the *next* IST date; there is a test at the
  23:59 / 00:30 boundary.
- **Booleans and blanks exactly as the contract says:** `true`/`false`, empty strings for "no
  calls", `\n` line endings, `+91******1234` masks, header rows.
- **No PII in outputs.** Reports only carry last-4 and the lead reference; a test scans every
  output file for the test name and phone.
- **One query, not one per lead.** Fetch everything once and group in Python: simple and
  fast enough (the whole run on the sample is under 2 seconds).

## Alternatives we could have used

| Alternative | Why not |
|---|---|
| Everything in SQL (CTEs, `DISTINCT ON`, window functions) | The stretch goal and the fastest at scale, but the tie-breaks and "latest by time" rules are much harder to unit-test and to change in a live interview. We can port the heaviest part later. |
| Store derived tables (a `calls` table, lead buckets) updated at ingest | Must be recomputed whenever a late lead arrives; invites drift from the ledger. |
| Link events to `lead_id` at ingest | Same problem: not append-only friendly, wrong for late leads. |
| pandas `groupby` | Heavy dependency; the rules read better as explicit functions. |
| Sort with `ORDER BY` | Collation differences (above). |
| Round with `Decimal` half-up | Might not match a reference built on Python's formatting. |
| One report per lead via N queries | Hundreds of thousands of round trips at scale. |

## Findings / results (seed 42, small)

- **179 tests pass.**
- **Independent oracle:** a second, from-scratch implementation inside the test (plain Python
  over the raw files, no database, no shared report code) produces **identical** `lead_report`,
  `daily_overview`, `batch_summary`, orphan and unknown counts. Caveat: the same person wrote
  both, so a shared misreading of the spec would not be caught; the hand-built derive tests
  and the spec re-reading cover that.
- **Load-order independence:** drop_1 then drop_2 gives byte-identical files to loading both
  drops' events first (newest first) and the leads last.
- **Re-runs:** ingesting twice and reporting twice gives byte-identical files.
- **Numbers:** 1,892 leads: 816 completed, 526 in_progress_dialed, 512 yet_to_reach_out, 38
  in_progress_telephony_failed. 33 daily rows, 2,536 calls (1,467 connected, about 57.8%).
  Of 2,681 call ids in the ledger, 2,536 are counted; the rest belong to unlinked phones.
- `data_quality`: 462 orphan events, 18 unknown-disposition events, plus the earlier counters
  (2,057 lead rows, 108 rejected, 57 merged, 122 warnings; 8,745 event lines, 8 malformed, 237
  duplicates).
- **Speed:** ingest of both drops plus the report takes about 1.7 s on the small set.

## Risks / not done

- `report` loads every event into memory (fine for ~600k, not for 100k leads/day over months).
  At that scale this becomes SQL aggregation or incremental derivation; that belongs in the
  production note.
- The oracle and the spec reading are the same author's. The rounding, tie-break,
  `rejections_by_reason` and empty-batch choices are guesses about the grader (all recorded).
- Still to do in step 7: a `reset` command, the large-scale timing (the lead upserts are still
  row by row), and the final docs.
