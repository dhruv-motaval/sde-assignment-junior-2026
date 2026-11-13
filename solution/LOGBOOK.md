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

## GitHub

- Private repo: https://github.com/dhruv-motaval/sde-assignment-junior-2026 (`origin`).
  The assignment's own repo is kept as the `upstream` remote and is never pushed to.
- Routine: at the end of every step, update this logbook and the step doc, then commit and
  push to `origin main`. One commit per step, message says which step.
- Never commit `data/`, `data_large/`, `.env`, databases or the PII key file (`.gitignore`
  covers the first four; the key file name will be added when step 4 creates it).
- Before submitting: give the reviewers read access (see the main README) and email the link.

## Log

| # | Date | Step | Doc | Status |
|---|------|------|-----|--------|
| 1 | 2026-11-13 | Chose Assignment A; cloned assignment repo; generated seed-42 data; inspected it | [01](docs/01-setup-and-data-inspection.md) | done |
| 2 | 2026-11-13 | Pure value parsers (header, phone, date, decimal, enum, timestamp) + 72 unit tests | [02](docs/02-value-parsers.md) | done |
| - | 2026-11-13 | Created private GitHub repo and pushed steps 1-2 | - | done |
| 3 | 2026-11-13 | Lead ingestion in memory: CSV reading, header mapping, validation, rejections, merging (22 tests) | [03](docs/03-lead-ingestion.md) | done |
| 4 | 2026-11-13 | PostgreSQL schema, PII protection (HMAC phone, Fernet name), append-only ledger, leads saved to DB (19 new tests) | [04](docs/04-database-and-pii.md) | done |
| 5 | 2026-11-13 | Event ingestion: parse, malformed/duplicate handling, COPY into the ledger, idempotent per file (34 new tests) | [05](docs/05-event-ingestion.md) | done |
| 6 | 2026-11-13 | Reports: derive calls / effective disposition / buckets, five output files, CLI (32 new tests incl. independent oracle) | [06](docs/06-reports.md) | done |
| 7 | | `reset` command, large-scale timing and batching, production note | | next |
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
| Two columns match one field | first column wins | deterministic, simplest | first non-empty / reject file | 3 |
| Required non-phone field invalid | reject `invalid_<field>` | consistent with `invalid_phone` | treat as missing | 3 |
| Folder with no schema | skip with warning | one bad folder must not stop the drop | fail ingest | 3 |
| Defaults | applied on save | same result as at the end; stored data is complete | apply at read | 3 |
| Warnings on merged rows | counted | merged rows are valid, not rejected | count only new leads | 3 |
| Database | PostgreSQL (user's choice over SQLite) | closer to production; user preference | SQLite (zero setup for graders) | 4 |
| Customer name | Fernet-encrypted | reversible, record stays complete | keyed hash / not stored | 4 |
| Secret key | env var, else auto-created git-ignored file | works from a clean clone | env var required | 4 |
| Full phone | not stored (HMAC + last 4 only) | reports only need masked phone | store encrypted too | 4 |
| Workflow-specific fields | `attributes jsonb` | one table, SQL merge with `||` | table per workflow / EAV | 4 |
| Same file path, new content | refuse (`FileContentChanged`) | avoid silent double counting | re-ingest anyway | 4 |
| Event with missing/bad time or type | counted malformed, skipped | ledger needs a real time and type | placeholder time / fail file | 5 |
| Malformed lines | keep file + line number only | no PII in the database | count only / raw text | 5 |
| Event load method | bulk COPY per file | speed for 600k events | row-by-row insert | 5 |
| Event with invalid phone | stored, NULL hash, orphan at report time | spec: events link by normalised phone | drop at ingest | 5 |
| Same event_id, different content | first copy wins | spec: skip later duplicates | last wins | 5 |
| How to compute reports | SQL fetches, Python decides | pure functions are easy to test and change live | all in SQL | 6 |
| Tie on occurred_at | higher event_id wins | deterministic, load-order independent | later ingested wins | 6 |
| connectivity_rate rounding | Python f"{x:.4f}" (half-even on exact ties) | likely what a reference does | Decimal half-up | 6 |
| Batch with zero valid leads | not listed in batch_summary | total_leads counts leads | list with zeros | 6 |
| Sorting | in Python, code-point order | DB collations can differ from the grader | ORDER BY in SQL | 6 |
| lead_report tie-break | batch_id, masked phone, lead id | spec only gives workflow, external_ref | none | 6 |
| rejections_by_reason | only reasons that occurred | natural Counter output | include all possible reasons with 0 | 6 |
| Call with several call.initiated | earliest one | simplest deterministic rule | latest | 6 |
| Linked event whose call has no linked initiated | not an orphan, creates no call | it did link by phone | count as orphan | 6 |

## AI usage notes (for `AI_USAGE.md`)

- Claude wrote the first draft of the parsers, the tests, and the data-inspection scripts.
- **Where the AI was wrong:** the first version of the timestamp ordering test used
  `"2026-10-05 15:00:00"` as the string that "sorts wrongly", but a space sorts before `T`, so
  the string comparison happened to agree with the datetime comparison and the test failed.
  Found by running it. Fixed by using `"2026-10-05T15:00:00+05:30"`. The parser was right; the
  test example was wrong.
- **How it was verified:** 72 unit tests, plus running every parser over every cell of the
  generated CSVs to confirm valid/invalid counts look plausible (see step 2 doc).
- Step 3: Claude wrote `leads.py` and its tests. Four open design questions were put to the user, who chose the recommended option for all four. Environment issue: pytest temp dir `PermissionError`, fixed with `--basetemp` in `pytest.ini`.
- Step 4: Claude wrote `pii.py`, `db.py`, `store.py` and 19 tests. The user chose Postgres against Claude's SQLite recommendation; Docker Desktop was not running, so Claude launched it with the user's permission and started the compose Postgres. Claude's first draft of one test asserted `[None] * 3` results for a drop that has 4 files; spotted on review before the first run and fixed.
- Step 5: Claude wrote `events.py`, the event part of `store.py`, and 34 tests. Three design questions were put to the user, who chose the recommended option each time. Where the AI was wrong: its first real-data test assumed every non-override event has a valid phone; the run showed 152 events with unparseable phones (the generator emits them on purpose). The code was right and the assertion was wrong; the finding is now documented and tested.
- Step 6: Claude wrote `report.py`, `cli.py` and 32 tests, including an independent re-implementation over the raw files used as an oracle. Four design questions were put to the user, who chose the recommended option each time. Limitation to disclose: the oracle and the pipeline were written by the same AI from the same reading of the spec, so a shared misunderstanding would not be caught. A shell heredoc failed once while writing a test file (tooling, not logic); the file was re-created with the file tool.
- Things still to understand line by line before the interview: the regexes in `parsers.py`.
