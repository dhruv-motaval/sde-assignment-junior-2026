# Step 5: Event ingestion into the ledger

Code: [`../events.py`](../events.py) (parse one line), [`../store.py`](../store.py)
(`ingest_event_file`, `ingest_event_drop`, `ingest_drop`), [`../db.py`](../db.py) (`malformed_lines`
table). Tests: [`test_events.py`](../tests/test_events.py) (34), shared fixture in
[`conftest.py`](../tests/conftest.py). Whole suite: **147 passed, 0 skipped**.

## What we did

Each line of each `events_*.jsonl` file goes through the spec's order, and the survivors are
written to the append-only `events` table:

1. **Malformed** -> count it, skip it, remember only *file + line number*.
2. **Duplicate `event_id`** (earlier in the file, or in any earlier file or drop) -> count it,
   skip it. The first copy is kept.
3. Otherwise **store it** in the ledger with: normalised UTC time, a keyed phone hash (or NULL),
   the payload, and where it came from (file + line).

Steps 3 and 4 of the spec (**orphan**, **unknown disposition**) are deliberately *not* decided
here. They are derived at report time (step 6), because a lead can arrive in a later drop.

## Why we did it this way

- **Pure parser, then an I/O loop.** `parse_event_line` is a plain function (a line in, an
  `Event` or `None` out), tested with 20+ cases without a database.
- **Orphans are stored, not dropped.** An event that is an orphan after `drop_1` must count
  once its lead arrives in `drop_2`. If we threw it away at ingest we could never recover it.
  Linking is a join at report time, so late leads "just work".
- **Phone normalised, then hashed, per event.** The data shows one call's events carrying
  several different phone formats (step 1), so hashing the raw string would break matching. We
  normalise first (all six formats give one hash, tested).
- **`event_id` as the primary key.** The database itself guarantees no event is stored twice.
  On top of that we count duplicates exactly: a Python `set` finds repeats inside the file,
  and one query per chunk finds ids already in the ledger.
- **Bulk `COPY` per 5,000 events** (the user chose this for speed). `COPY` is the fastest way
  into Postgres. It cannot skip conflicts, which is why we filter duplicates *before* copying.
- **One transaction per file, with its `ingested_files` row inside it.** A crash half-way leaves
  nothing (a test makes the crash happen after one chunk was already copied). A retry starts
  clean. The `COPY` does not fire the append-only triggers, which only guard UPDATE / DELETE /
  TRUNCATE, so normal loading works while history stays protected.
- **Re-ingest is skipped by file hash**, as for leads. Same path with changed content is refused.
- **`errors="replace"` when reading.** A stray bad byte makes *that line* malformed instead of
  crashing the whole file.

## Decisions made with the user

| Question | Decision | Alternative not chosen |
|---|---|---|
| Valid JSON with an `event_id`, but missing/unparseable `occurred_at` or missing `event_type` | **Count as malformed and skip** | Store with a placeholder time; fail the file |
| What to keep about malformed lines | **File name + line number only** (new `malformed_lines` table) | Count only; store the raw text |
| How to write events | **Bulk COPY per file** | Row-by-row `INSERT ... ON CONFLICT` |

Why not store the raw text: a truncated line can still contain a customer's phone number, so
keeping it would break the PII rule. A test checks that no plaintext phone appears anywhere in
the database, including after a truncated line containing one.

## Alternatives we could have used

| Alternative | Why not |
|---|---|
| Decide "orphan" at ingest and discard orphans | Loses events whose lead arrives later. |
| Link each event to a `lead_id` at ingest | Needs relinking whenever a new lead arrives, and the ledger is append-only. A join at report time is simpler and always right. |
| Rely only on `ON CONFLICT DO NOTHING` to count duplicates | Works row by row but not with `COPY`, and the conflict count is awkward to get. |
| Row-by-row inserts | About 1,600 rows/s would put the 600k-event goal near 6 minutes. |
| A staging table + `INSERT ... SELECT ... ON CONFLICT` | Also fast, but more moving parts. Python-side filtering is short and easy to explain. |
| Keep every field of a malformed line | PII risk (above). |
| Read the whole file into memory | Fine for the small set, but chunking keeps memory flat for the large one. (The `seen` set still holds one id per event: about 600k strings, acceptable.) |

## Findings / results

- **147 tests pass**: 34 new event tests plus the earlier 113.
- **Real data (seed 42):** 8,745 non-empty lines, **8 malformed**, **237 duplicates**, so
  **8,500 events stored**. `8,745 = 8 + 237 + 8,500` holds. An independent plain-Python count of the
  raw files agrees exactly with the loader.
- Stored by type: initiated 2,681, ended 2,367, connected 1,553, disposition.set 1,515,
  telephony_failed 314, override 70.
- **A finding worth remembering:** 152 stored non-override events have a **phone that cannot be
  normalised** (the generator emits `N/A`, landlines like `022-...`, 9-digit and bad-prefix
  numbers, and a US number). They are stored with a NULL hash. By the spec's rule ("an event links
  via `(workflow_id, normalised phone)`") they are **orphans**. All overrides have a NULL
  hash by design (no phone), and they link through their call instead.
- **Speed:** both drops, leads and events, load in about **1.4 s** in total, so events at
  `COPY` speed are no longer the bottleneck. The remaining slow part is the row-by-row *lead*
  upserts (step 4), to be batched in step 7 if the large-scale timing needs it.
- Re-ingesting both drops returns "already ingested" for every file and the set of event ids is
  unchanged (checksum).

## Open questions / assumptions for `SUBMISSION.md`

- An event whose phone is missing or invalid, but whose `call_id` matches linked events, is
  still an **orphan** (the spec only lets overrides link through `call_id`). Step 6 must follow
  this, and a call whose `call.initiated` event is such an orphan is not counted.
- A line with a non-string or empty `event_id` / `event_type` counts as malformed.
- Event payloads are stored exactly as given. We assume they hold no customer PII (they hold
  reasons, codes, durations and supervisor ids).
- Events with an unknown `event_type` are stored (the ledger records everything) and simply
  ignored by the derived facts.
- If the same `event_id` appears twice with different content, the **first** copy wins.
