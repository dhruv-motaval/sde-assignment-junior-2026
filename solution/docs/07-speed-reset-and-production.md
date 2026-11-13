# Step 7: Speed on the large dataset, `reset`, and the production note

Code: [`../store.py`](../store.py) (bulk lead loading), [`../pii.py`](../pii.py) (one-shot HMAC),
[`../db.py`](../db.py) (`reset_schema`), [`../cli.py`](../cli.py) (`reset` command).
Tests: `test_reset.py` (2 new), three new/rewritten lead-loading tests in `test_store.py`.
Whole suite: **183 passed, 0 skipped**.

## What we did

1. Generated the **large** dataset (`--scale large`, seed 42): 132,903 leads, 632,602 event lines
   (144 MB, git-ignored in `data_large/`).
2. Measured: the first full run took **152 s**, above the stretch goal of 2 minutes.
3. Replaced row-by-row lead saving with a **staging-table bulk merge** (the user's choice).
4. Added **`python -m solution reset --yes`** (the user's choice).
5. Re-measured through the real command line: **66.8 s** end to end.
6. Wrote the production note below.

## Speed

| Stage | Before | After |
|---|---|---|
| Lead loading (both drops) | ~101 s | ~13 s |
| Event loading (both drops) | ~39 s | ~40-48 s (run-to-run noise; not changed except a faster HMAC call) |
| Report | ~11 s | ~9-10 s |
| **Full run (benchmark script)** | **152 s** | **72 s** |
| **Full run (actual CLI: ingest drop_1, drop_2, report)** | not measured | **66.8 s** (31.5 + 26.9 + 8.5) |
| Re-ingest of an already-loaded drop | n/a | 0.9 s (every file skipped) |

Laptop-class Windows machine, Postgres 16 in Docker on the same machine. Numbers vary by a few
seconds between runs.

**How the lead loading works now.** For each CSV: parse and validate in memory (no database
traffic per row); merge repeated phones in file order (first row creates, later rows overwrite
only non-null values); encrypt each lead's name once; then in **one transaction** `COPY` the
distinct leads into a temp table, `UPDATE` the ones that already exist, and `INSERT` the missing
ones in file order. New leads = rows inserted; merged = valid rows minus new leads, which is
exactly the spec's counting rule.

**Why results did not change.** The earlier oracle tests still pass unchanged: the database equals
the in-memory `LeadBook` on the real data, and the reports equal the independent oracle. We added
tests for the trickiest bits of the new path: a phone repeated inside one file, and an existing
lead that must not get defaults or nulls from a later file.

**What we did not optimise:** event loading (~45 s) is now the biggest part. It is Python
parsing plus `COPY` file by file; the next steps would be parallel workers per file or a lighter
parser. Not needed to meet the goal.

## `reset`

`python -m solution reset --yes` drops every table the pipeline owns (leads, events, rejections,
file records, dispositions, key fingerprint). Without `--yes` it prints a warning and deletes
nothing (exit code 2). The next `ingest` recreates the tables. The PII key file is kept.

Why it matters: a different seed reuses names like `drop_1` with different content. Without a
reset, the pipeline refuses the changed file (`FileContentChanged`) instead of silently
double-counting. A test shows exactly that sequence.

Dropping is allowed on the append-only ledger because its triggers guard rows (UPDATE, DELETE,
TRUNCATE), not the table itself. That is also why we do not use TRUNCATE for `reset`.

## Decisions made with the user

| Question | Decision | Alternative not chosen |
|---|---|---|
| How to fix the 152 s run | **Staging-table bulk merge for leads** | `executemany` only; leave it and document |
| How `reset` behaves | **`reset --yes` drops all our tables** | TRUNCATE keeping tables; documentation only (`docker compose down -v`) |

## Alternatives we could have used

| Alternative | Why not |
|---|---|
| `executemany` of the old two statements per row | Still about two statements per lead; probably 2-3x faster, likely not enough for 2 minutes. |
| One `INSERT ... ON CONFLICT DO UPDATE` per row | Cannot tell "created" from "merged" without tricks (`xmax = 0`), and still row by row. |
| `COPY` straight into `leads` | `COPY` cannot merge into rows that already exist. |
| Disable indexes / `UNLOGGED` tables during load | Faster but risks durability or a long rebuild; not worth it at this size. |
| Parallel ingest workers | Needs locking around the merge to keep counts exact; unnecessary for the goal. |
| `reset` by TRUNCATE | Our append-only trigger blocks it (on purpose). |
| `reset` with no confirmation | Too easy to wipe data by mistake. |

## Production note: 100k leads a day

**Shape of the load.** At the large-sample ratio, 100k leads/day is roughly 450k event lines/day,
about 160M events/year. A day's drops load in a few minutes with this design; the report is the
part that does not scale as written.

**Scheduling.**
- A scheduler (Airflow, Temporal or plain cron plus a queue) starts `ingest` when a drop lands in
  object storage, then `report` on a fixed cadence (for example 06:00 IST for the morning client
  reports) and on demand.
- One ingest writer at a time per database. To go parallel, add an advisory lock (or per-workflow
  lock) around the lead merge so counters stay exact.

**Failures.**
- Safe to retry by design: each file is one transaction recorded in `ingested_files` (with its
  SHA-256). A crashed or repeated run changes nothing; a changed file under the same name is refused.
- Bad data never blocks good data: malformed lines and rejected rows are counted and located (file,
  line, row number), without storing their content.
- Alerts on rates, not just errors: malformed %, rejection %, orphan %, and "an expected drop did not
  arrive". The row-accounting invariants we assert in tests
  (`lines = malformed + duplicates + stored`, `rows = rejected + merged + new`) double as
  production health checks.
- A wrong or lost PII key fails fast (`PiiKeyMismatch`) instead of silently duplicating leads.

**Backfills and late data.**
- The ledger is append-only and every report is derived, so a late lead or late events need no
  repair: re-run `report`.
- Corrections arrive as new events (overrides), never edits.
- A corrected file gets a new name or version (so it is a new file, not a "changed" one). A full
  rebuild is `reset`, then re-ingest in order, which is safe because everything is idempotent.

**What changes at that scale.**
1. **Reports:** loading every event into Python memory stops working. Move the aggregation into SQL
   (the "all in SQL" option we deliberately did not start with) and make it incremental by IST date:
   recompute only dates and leads touched since the last run, or keep a materialised `calls` table
   refreshed from new ledger rows.
2. **Storage:** partition `events` by month on `occurred_at`; indexes on `(workflow_id, phone_hash)`
   and `call_id`; archive old partitions.
3. **Loading:** parallel workers per file and `COPY` batches (already used); keep leads on fast
   storage.
4. **Keys:** the PII key moves from a local file to a KMS / secrets manager, with a key id stored
   next to each hash so keys can be rotated (re-hash leads during rotation).
5. **Privacy:** reports carry only masked phones and references; delete requests remove a lead by
   hash and its encrypted fields; access to the database is restricted and encrypted at rest.
6. **Operations:** managed Postgres with point-in-time recovery, dashboards for ingest lag and the
   rates above.

## Findings

- **Large run totals** (seed 42): 143,990 lead rows = 7,097 rejected + 3,990 merged + 132,903 new
  leads. 632,602 event lines = 631 malformed + 18,490 duplicates + 613,481 stored. 34,424 orphan events,
  1,005 unknown dispositions. Both invariants hold at this scale too.
- Rejection mix on large: `invalid_phone` 2,811, `missing_required:customer_name` 2,082,
  `missing_required:order_id` 761, `missing_required:phone` 726,
  `missing_required:policy_number` 717.
- 37 lines in `daily_overview.csv` (36 days-by-workflow rows plus the header), 7 in `batch_summary.csv`.

## Limits, honestly

- Timings are from one machine, one run each, with Docker on the same machine.
- Only drop_1-then-drop_2 was run at large scale; the load-order independence and idempotency tests
  run on the small data set only.
- Event loading is the slowest stage and was not optimised in this step.
