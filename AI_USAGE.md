# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Claude Code** (Anthropic's coding agent, Claude Sonnet 5.5) in the Claude desktop app, for the whole
  assignment. No other AI tool.

## What I used them for
Almost everything, in a conversation with the agent, one part at a time:
- Choosing between Assignment A and B (it read the repo and my GitHub profile and recommended A).
- Explaining the assignment from scratch and proposing approaches.
- Inspecting the generated data before any code was written.
- **Writing the code:** parsers, lead ingestion, PostgreSQL schema and PII protection, event ingestion, report
  derivation and the five output files, the command line, the bulk-loading speed-up, and `reset`.
- **Writing the tests** (212), including an independent re-implementation over the raw files used as an oracle.
- Benchmarking on the large dataset, starting Docker/Postgres, running a fresh-clone check, and the git commits and
  pushes to my GitHub repo.
- Writing the step-by-step docs in `solution/docs/`, `solution/LOGBOOK.md`, this file and `SUBMISSION.md`.

## What I wrote or decided myself
I did **not** write the code by hand. What I did:
- Picked the assignment, asked for each part to be built step by step, and asked that every step be documented with
  the reasoning and the alternatives (`solution/docs/`, `solution/LOGBOOK.md`).
- Made the design decisions where the agent stopped and asked me, choosing among options it laid out (it
  recommended one each time). I took its recommendation in all but one case. Examples:
  - first column wins when two columns match a field; reject `invalid_<field>`; skip unknown workflow folders;
    apply defaults on save;
  - encrypt the customer name; key from env var or an auto-created ignored file; keep only phone hash + last 4;
  - **PostgreSQL instead of SQLite, against the agent's recommendation** (and I let it launch Docker Desktop);
  - an event with a bad time/type counts as malformed; keep only file + line number for malformed lines; bulk `COPY`;
  - compute reports as "SQL fetches, Python decides"; tie goes to the higher `event_id`; Python `.4f` rounding;
    leave out batches with no leads;
  - use a staging-table bulk merge for the speed-up; `reset --yes` drops tables.
- Kept the GitHub repo private while working (the assignment README asks for that), then made it public because the
  submission form asks for a publicly accessible link. Also decided to be asked about opinions at decision points.
- I did not design the schema or write the SQL myself; the agent did, and I chose between the options it gave me.
- I made one file to understand the code whenever needed: `solution/docs/CODE_GUIDE.md` (what each file does, short
  answers to the design questions, and where to edit for a typical change).

## Where the AI was wrong or unhelpful
Found by running things; each was fixed:
1. **Timestamp test.** The first "string order is wrong but datetime order is right" test used `"2026-10-05 15:00:00"`
   as the misleading string. A space sorts before `T`, so the string order happened to be right and the test failed.
   The parser was correct, the test example was wrong; fixed with an offset timestamp.
2. **Real-data assumption.** A first test assumed every non-override event has a valid phone. The run showed 152 stored
   events with unparseable phones (the generator adds them on purpose). The code was right; the assertion was wrong.
   The finding is now documented and tested, and it is why those events are orphans.
3. **Seed-specific test.** One test asserted a number that only holds for seed 42 (`stored=4160`). The fresh-clone check
   was being prepared when I searched the tests for seed-specific numbers; it was replaced with a seed-independent check
   before submission.
4. **Fresh clone failure.** `pytest` failed on a fresh clone because pytest's temp folder lived in `.pytest_cache`, which
   did not exist yet. Found by actually cloning the repo and running the documented commands; fixed.
5. **Tooling slips.** A `\n` inside a print message was written as a real newline (SyntaxError), a wrong-string edit was
   rejected, a shell heredoc failed once, and an old crash-recovery test targeted a function that the speed-up removed
   (it was rewritten). All were caught by running the code or the tests.
6. **Crashes on hostile input.** The probing above showed my code crashed a whole file on a NUL character, `NaN`, year
   0001/9999 timestamps, an invalid UTF-8 byte and a 1 MB cell, silently lost the first event of a BOM-prefixed file, and
   exited 0 with no output for a mistyped path. None of this appeared in the generated data, so the original tests never
   saw it. Fixed in `solution/docs/08-edge-case-hardening.md`. While fixing it a literal NUL byte also ended up inside a
   source file through a scripting slip; the next test run caught it.
7. **A trade-off, not an error.** The agent recommended SQLite for simplicity (no Docker for a reviewer); I chose
   PostgreSQL instead. This adds a Docker requirement for anyone running the project; it is noted as a risk.

## How I verified AI-generated code
- **Tests:** 212 automated tests (unit, database, and end to end), run repeatedly during the work. The first 183 also
  passed on five other data seeds and on a fresh clone with seed 7.
- **Adversarial probing before submitting:** about 50 hostile inputs (NUL bytes, `NaN`, year 0001/9999, BOM, bad bytes,
  huge cells, mistyped paths, database down) were fed to the code; every crash found was fixed and covered by a test.
- **Independent oracle:** a second, from-scratch implementation inside the tests reads the raw files without the database
  and must produce identical reports and counters. Caveat: the same AI wrote both, so a shared misreading of the spec would
  not be caught.
- **Real data and invariants:** every run on the generated data checks the row-accounting invariants
  (`rows = rejected + merged + new leads`; `event lines = malformed + duplicates + stored`) at both small and large scale.
- **Behavioural checks:** re-ingest changes nothing; loading in a different order gives byte-identical reports; a simulated
  crash mid-file rolls back; no plaintext PII anywhere in the database or output files.
- **What I have NOT done:** a line-by-line review of the code. To understand it whenever needed I use one file,
  `solution/docs/CODE_GUIDE.md`.
