# Step 3: Lead ingestion (in memory)

Code: [`../leads.py`](../leads.py). Tests: [`../tests/test_leads.py`](../tests/test_leads.py)
(22 tests; 94 in the whole suite, all passing).

## What we did

Turned a lead CSV into either a **rejection with one reason** or a **normalised lead**, and
merged duplicates, all in memory. Nothing touches a database yet (that is step 4).

| Piece | Job |
|---|---|
| `load_schemas` | `schemas/*.json` -> `Schema` / `Field` objects |
| `Schema.header_map` | CSV header -> field, via name + aliases, using `normalize_header` |
| `read_rows` | CSV -> `(row_number, {field: raw cell})`; skips blank rows; numbers only non-blank rows |
| `evaluate_row` | walks fields **in schema order**; first failure = the single rejection reason; optional bad value = `None` + 1 warning |
| `LeadBook.add` | merge rule: first valid row creates the lead (its batch id is permanent); later rows fill only non-null fields; defaults applied on save |
| `ingest_file` | one file through all of the above -> `FileResult` counters |
| `discover_lead_files` | the files of one drop, sorted by workflow then filename |

## Why we did it this way

- **Split reading, judging and merging into separate functions.** Each rule in the spec lands
  in exactly one place, so a live change ("also reject X") is a small edit.
- **`csv.reader`, not `DictReader`.** `DictReader` silently drops a duplicate header and
  fills short rows with `None`. With `csv.reader` we control both: the first matching column
  wins, and a short row just has missing cells.
- **Blank-row test is "every cell is blank"**, which is also true for an empty line (`[]`).
  Blank rows are skipped *before* numbering, so `row_number` matches the spec ("non-blank data
  rows, starting at 1, header not counted").
- **Warnings returned as 0 for rejected rows.** The spec says warnings count only for rows
  that are not rejected. A bad optional field that appears *before* the failing field must not
  leak into the total, so the count is only reported when the walk finishes.
- **Merged rows still count their warnings.** They are valid rows, and the spec says count
  warnings for "rows that are not rejected". Recorded as an assumption.
- **Reading files as `utf-8-sig`.** Excel adds a byte-order mark that would otherwise turn the
  first header into `﻿customer_name` and silently match nothing.
- **`LeadBook` is deliberately a plain in-memory reference.** In step 4 the real merge runs in
  SQL. Having this simple version means we can compare the database result with it in tests
  (an oracle), which catches SQL mistakes.

## Decisions made with the user

The user was asked about each of these and chose the recommended option.

| Question | Decision | Alternative not chosen |
|---|---|---|
| Two columns match the same field | First column wins; the other is ignored | First non-empty cell wins; or reject the file |
| Required non-phone field present but invalid | Reject as `invalid_<field>` (no current schema triggers this) | Treat as `missing_required` |
| `leads/<folder>` with no schema | Skip it with a warning | Fail the whole ingest |
| When to apply defaults | On save, to still-null fields | At read/report time |

Defaults on save give the same result as defaults at the very end, because a default only fills
a field that is still null: a later real value overwrites it, a later null leaves it alone (test
`test_defaults_apply_only_to_still_null_fields`).

## Alternatives we could have used

| Alternative | Why we did not |
|---|---|
| `pandas.read_csv(dtype=str)` and vectorised cleanup | Row numbering, "first failure in schema order", and "walk the file in order for last-write-wins" are all awkward in a dataframe. Also a heavy dependency for ~2,000 rows. |
| `csv.DictReader` | Hides duplicate headers and short rows (see above). |
| A validation library (pydantic, cerberus) | Our rules (single reason, schema order, warning vs reject) are simpler as 20 explicit lines than as a model with custom validators. |
| Do the merge directly in SQL now | Harder to test and debug first. Build the simple version, then check SQL against it. |
| Reject the whole file when a header is odd | One odd header should not block hundreds of good rows. |
| Count "rows" including blank lines | Contradicts the spec's numbering rule; `rejections.csv` row numbers would no longer match. |

## Findings / results

**Tests:** 94 passed (72 parsers + 22 lead tests). The lead tests cover header aliases / case /
underscores / reordering / extra columns, BOM, blank and whitespace-only rows, short rows,
every rejection reason, schema-order precedence, warnings (and none for rejected rows), merge
semantics (batch id permanent, null never overwrites), identity = workflow + phone, defaults,
the row-accounting invariant, file ordering, and a run over the real generated data.

**Real data (seed 42, small), all 6 lead files, both drops:**

| File | Rows | New leads | Merged | Rejected | Warnings |
|---|---|---|---|---|---|
| drop_1 motor batch_001 | 416 | 389 | 14 | 13 | 28 |
| drop_1 motor batch_002 | 350 | 326 | 4 | 20 | 28 |
| drop_1 feedback batch_001 | 377 | 356 | 3 | 18 | 15 |
| drop_1 feedback batch_002 | 372 | 344 | 8 | 20 | 12 |
| drop_2 motor batch_003 | 266 | 236 | 14 | 16 | 23 |
| drop_2 feedback batch_003 | 276 | 241 | 14 | 21 | 16 |
| **Total** | **2,057** | **1,892** | **57** | **108** | **122** |

- Invariant holds for every file and overall: `2,057 = 1,892 + 57 + 108`.
- Rejection reasons: `invalid_phone` 42 (20 + 22), `missing_required:customer_name` 25,
  `missing_required:policy_number` 14, `missing_required:order_id` 13, `missing_required:phone`
  14. Together 108.
- Row counts match the files (e.g. `batch_001` has 419 lines: 1 header, some blank lines, 416 data rows).
- No row was ever rejected for an invalid *optional* field; those became warnings.

**Environment issue (not a code bug):** pytest's `tmp_path` failed with `PermissionError` on
the system temp folder. Fixed by pointing pytest's temp directory inside the project
(`--basetemp=.pytest_cache/tmp` in `pytest.ini`; `.pytest_cache/` is already git-ignored).

## Open questions / assumptions to repeat in `SUBMISSION.md`

- Warnings count for merged duplicate rows too.
- A file with the same field matched by two columns uses the first column.
- Non-UTF-8 files would raise an error; the generator only writes UTF-8.
- Only the schema's first phone-typed field is the lead identity (all current schemas have one).
- Whether a batch whose rows were all rejected appears in `batch_summary` is still open (step 6).
