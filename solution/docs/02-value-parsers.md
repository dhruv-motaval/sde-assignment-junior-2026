# Step 2: Value parsers

Code: [`../parsers.py`](../parsers.py). Tests: [`../tests/test_parsers.py`](../tests/test_parsers.py)
(72 tests, all passing).

## What we did

Wrote pure functions, one per spec rule, with no file or database access:

| Function | Rule it implements |
|---|---|
| `normalize_header` | lower-case, trim, collapse runs of `_` / whitespace to one space |
| `parse_phone` | strip non-digits; 12 digits starting `91` drops `91`; 11 digits starting `0` drops `0`; valid only if 10 digits starting 6-9; returns `+91XXXXXXXXXX` |
| `parse_date` | `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`; day-first; impossible dates invalid |
| `parse_decimal` | optional `₹`/`Rs`/`Rs.`/`INR` prefix, commas ignored, must be >= 0, rounded to 2 places |
| `parse_enum` | case-insensitive match against allowed values, stored lower-case |
| `parse_timestamp` | `Z`, offset, or naive (IST) -> timezone-aware UTC `datetime` |

Every parser takes a trimmed, non-empty string and returns the normalised value, or `None` if
invalid. "Empty means missing" is the caller's job (step 3).

## Why we did it this way

- **Pure functions in one file.** These are the rules most likely to break (the assignment
  says so), so they get the most tests. In the live interview we may be asked to change one
  ("accept a new date format"); one small function per rule makes that a two-minute edit.
- **Regex as the gate, then convert.** Python's `Decimal("NaN")`, `Decimal("Infinity")` and
  `Decimal("1e5")` all parse successfully. A strict regex decides what is allowed first, so
  junk like `NaN` is rejected as `None`.
- **`Decimal`, never `float`.** Floats cannot represent `0.1` exactly; money must not drift.
  Rounding is `ROUND_HALF_UP` (12.345 -> 12.35). Python's default for `Decimal.quantize` is
  half-even (banker's), which would give 12.34. The spec only says "round to 2 places", so this
  is a decision we record.
- **ASCII digits only** (`[0-9]`, not `\d`). `\d` also matches digits from other scripts, which
  would let non-Indian-numeral text slip through as a phone number.
- **Own month table instead of `strptime("%b")`.** `%b` depends on the machine's locale. A
  dictionary of `jan..dec` behaves the same on every machine.
- **Timestamps converted to UTC datetimes.** The sanity test shows why: `"2026-10-05T10:00:00Z"`
  sorts *before* `"2026-10-05T15:00:00+05:30"` as text, yet the second is 09:30 UTC and happens
  *earlier*. "Latest event wins" (effective disposition, last call) is only right on real
  datetimes.
- **One leading zero only.** `0 0987654321` is not valid: the rule drops a single `0` from an
  11-digit number, and the result must still be 10 digits starting 6-9.
- **A 10-digit number starting `91` is left alone** (`9187654321` is a valid mobile). Only
  12-digit numbers lose the `91`.

## Alternatives we could have used

| Area | Alternative | Why we did not |
|---|---|---|
| Phone | `phonenumbers` library | Its idea of "valid" differs from the spec's exact rules (it would accept numbers the spec rejects and vice versa), and the graders diff against the spec. Adds a dependency for ~6 lines of logic. |
| Date | `dateutil.parser.parse` | Guesses formats, so `01/02/2026` could come out month-first. The spec requires day-first and a closed list of formats. |
| Date | `datetime.strptime` with 4 formats | Works, but `%b` is locale-dependent and the 4-attempt try/except loop is noisier than 3 regexes. |
| Decimal | `float` and `round()` | Binary rounding errors; `round()` is also banker's rounding. |
| Decimal | `Decimal(s)` directly | Accepts `NaN`, `Infinity`, `1e5`. |
| Decimal | `babel` / `money` libraries | Overkill for a fixed list of four prefixes. |
| Timestamp | `dateutil.parser` | Another dependency; `datetime.fromisoformat` handles all three forms in Python 3.11+. |
| Timestamp | Store the raw string and sort as text | Gives wrong order across mixed timezones (shown above). |
| Errors | Raise exceptions on invalid input | The caller needs "invalid" as a normal outcome (reject the row or warn), so returning `None` is simpler than try/except around every cell. Only `parse_timestamp` raises, since the spec does not say what an unparseable timestamp means yet. |
| Tests | Golden-file tests | Parametrized tables show each input and expected output next to each other, which is easier to extend during the interview. |

## Findings / results

- `pytest`: **72 passed**.
- We also ran every parser over every cell of the seed-42 lead CSVs (columns mapped through
  `normalize_header` and the schema aliases). Valid / invalid counts:

  | Type | Valid | Invalid |
  |---|---|---|
  | string | 5,087 | 0 |
  | phone | 2,001 | 42 |
  | date | 2,009 | 48 |
  | decimal | 1,008 | 24 |
  | enum | 1,596 | 63 |

  Only `agent_notes` was an unmatched column. The invalid rates (about 2-4%) match "some bad
  values on purpose". All six phone formats the generator emits normalise correctly.
  These counts are per cell, not per row, so they are not the final rejection numbers.
- **A bug in our own test** was caught by running it: see "Where the AI was wrong" in
  [`../LOGBOOK.md`](../LOGBOOK.md). The parser was correct; the test example was not.

## Open questions

| Question | Our call |
|---|---|
| Are 1-digit days/months (`1/2/2026`) valid? | Yes, like `strptime`. Never appears in the generated data. |
| Rounding mode | Half-up. |
| What does an unparseable event timestamp mean? | Undecided. Decide in step 5 (event ingestion). The data never produced one. |
