# Logbook

Chronological record of the work. Newest entries go at the bottom of the log table.
Every step also gets its own write-up in [`docs/`](docs/) using the template below.
This file feeds `SUBMISSION.md` (decisions, time spent) and `AI_USAGE.md` (honest AI disclosure),
so keep it truthful, including mistakes.

## Convention (applies to every step)

Each step doc in `docs/NN-name.md` has these sections:

1. **What we did**: the concrete work and files touched.
2. **Why we did it this way**: the reasoning.
3. **Alternatives we could have used**: each with why we did not pick it.
4. **Findings / results**: numbers, surprises, test output.
5. **Open questions**: anything the spec left ambiguous (also copied to the table below).

## Log

| # | Date | Step | Doc | Status |
|---|------|------|-----|--------|
| 1 | 2026-11-13 | Chose Assignment A; cloned assignment repo; generated seed-42 data; inspected it | [01](docs/01-setup-and-data-inspection.md) | done |
| 2 | 2026-11-13 | Pure value parsers (header, phone, date, decimal, enum, timestamp) + 72 unit tests | [02](docs/02-value-parsers.md) | done |
| 3 | | Lead ingestion: CSV reading, header mapping, validation, rejections, merging | | next |
| 4 | | Database schema, PII protection (HMAC phone, encrypted name), append-only ledger | | |
| 5 | | Event ingestion: malformed / duplicate handling, idempotent file tracking | | |
| 6 | | Report derivation: calls, effective disposition, buckets, five output files | | |
| 7 | | Idempotency + drop-split equivalence tests; large-scale timing | | |
| 8 | | Fill in `SUBMISSION.md` and `AI_USAGE.md` | | |

**Time spent so far (fill in honestly, it goes in `SUBMISSION.md`):** _____ h

## Decisions the spec did not make for us

Copied into `SUBMISSION.md` at the end.

| Decision | Choice | Why | Alternative | Step |
|---|---|---|---|---|
| Decimal rounding | half-up (`ROUND_HALF_UP`) | What people expect for money; Python's default is half-even | banker's rounding | 2 |
| Day/month with 1 digit (`1/2/2026`) | accepted | `strptime` accepts it, so a reference built on it would too | strict 2-digit only | 2 |
| Header normalisation order | lower, trim, then collapse `_`/spaces (spec order) | Literal reading of the spec | collapse then trim | 2 |
| Bad event timestamp | `parse_timestamp` raises `ValueError`; caller decides | Spec does not say what to do | silently treat as malformed | 2 |

## AI usage notes (for `AI_USAGE.md`)

- Claude wrote the first draft of the parsers, the tests, and the data-inspection scripts.
- **Where the AI was wrong:** the first version of the timestamp ordering test used
  `"2026-10-05 15:00:00"` as the string that "sorts wrongly", but a space sorts before `T`, so
  the string comparison happened to agree with the datetime comparison and the test failed.
  Found by running it. Fixed by using `"2026-10-05T15:00:00+05:30"`. The parser was right; the
  test example was wrong.
- **How it was verified:** 72 unit tests, plus running every parser over every cell of the
  generated CSVs to confirm valid/invalid counts look plausible (see step 2 doc).
- Things still to understand line by line before the interview: the regexes in `parsers.py`.
