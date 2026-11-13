# Step 4: Database, PII protection, append-only ledger

Code: [`../db.py`](../db.py) (schema), [`../pii.py`](../pii.py) (keys, hashing, encryption),
[`../store.py`](../store.py) (leads saved to Postgres). Tests: `test_pii.py` (7), `test_store.py` (12).
Whole suite: **113 passed, 0 skipped**.

## What we did

1. Started PostgreSQL 16 with the assignment's `docker-compose.yml` (this meant launching Docker
   Desktop first).
2. Designed the tables (`db.py`): `leads`, `rejections`, `events` (the ledger), `ingested_files`,
   `meta`.
3. Built PII protection (`pii.py`): keyed phone hash + encrypted name, one master key.
4. Saved leads into Postgres (`store.py`): the same merge rules as the in-memory `LeadBook`,
   now as SQL, one transaction per file.
5. Proved it: the database result is compared with `LeadBook` on the real data, and re-running
   changes nothing.

## The schema

| Table | Purpose | Key points |
|---|---|---|
| `leads` | One row per `(workflow, phone)` | `phone_hash` (HMAC, bytea), `phone_last4`, permanent `batch_id`, `external_ref`, `pii` (jsonb of encrypted fields), `attributes` (jsonb of other fields). `UNIQUE(workflow_id, phone_hash)` is the lead identity. |
| `rejections` | Rejected lead rows | `workflow_id, batch_id, row_number, reason`. No PII, primary key on those four. |
| `events` | The interaction ledger | `event_id` primary key = free dedupe. `phone_hash` is NULL for overrides. **Triggers block UPDATE, DELETE and TRUNCATE.** Filled in step 5. |
| `ingested_files` | Which input files were fully applied, plus their counters | Primary key `(drop_name, path)`; stores a SHA-256 of the file. |
| `meta` | Holds a fingerprint of the PII key | Detects a wrong or lost key. |

## Decisions made with the user

| Question | Decision | Alternative not chosen |
|---|---|---|
| How to store the customer name | **Encrypt with Fernet** (reversible with the key) | Keyed hash only; do not store it |
| Where the secret key comes from | **Env var `AU_PII_KEY`, else auto-create the git-ignored `solution/.pii_key`** | Env var required; a fixed key in the code |
| Keep the full phone? | **No: only keyed hash + last 4 digits** | Also store it encrypted |
| Database | **PostgreSQL** (the user chose this over our SQLite recommendation) | SQLite |

## Why we did it this way

- **HMAC, not a plain hash, for phones.** There are only ~4 billion Indian mobile numbers, so
  a plain SHA-256 could be reversed by hashing all of them. A keyed HMAC cannot be reversed
  without the key, but is still deterministic, so events can be matched to leads by equality.
  A test asserts our hash is not a plain SHA-256.
- **Phone hashed after normalising.** All six phone formats give the same canonical number and
  therefore the same hash (tested). The step 2 finding that one call's events use different
  formats makes this essential.
- **Two derived sub-keys from one master key** (one for HMAC, one for Fernet). Using one key
  for two jobs is a classic mistake.
- **Key fingerprint in `meta`.** If the database survives but the key file is lost, a new key
  would hash every phone differently, and re-ingesting would silently create duplicate leads.
  Now `init_schema` refuses to start (`PiiKeyMismatch`) instead.
- **`jsonb` for workflow-specific fields.** The two workflows have different non-PII columns
  (`policy_expiry`/`premium_amount` versus `product`/`order_date`). One table with `attributes
  jsonb` avoids a table per workflow or many mostly-empty columns. Merge is one SQL expression:
  `attributes || incoming` replaces only the keys we send, and null values are never sent, so a
  later null cannot erase data. Reports only need `external_ref`, `batch_id`, `phone_last4`,
  which are real columns.
- **Encrypted PII as `jsonb` of tokens** (`{"customer_name": "gAAAA..."}`). It merges the same
  way as `attributes`, in SQL, without decrypting.
- **One transaction per file, with the `ingested_files` row inside it.** "Listed in
  `ingested_files`" therefore means "fully applied". A crash mid-file leaves nothing behind (test
  `test_failure_mid_file_rolls_back_the_whole_file`), and a retry starts clean.
- **Idempotency = skip files already ingested.** Counters like "duplicate rows merged" would
  otherwise go up on every re-run. Same path with different content is **refused**
  (`FileContentChanged`) instead of silently double-counting.
- **Append-only enforced by the database**, not by convention: `BEFORE UPDATE OR DELETE` and
  `BEFORE TRUNCATE` triggers raise an error. Even a buggy report query cannot rewrite history.
- **Tests use a fresh Postgres schema each**, so they never touch real data and clean up after
  themselves. If Postgres is not running they skip with a hint.
- **`LeadBook` is the oracle.** The big test ingests the real data both ways and compares every
  lead's batch, reference, attributes and decrypted name.

## Alternatives we could have used

| Area | Alternative | Why not |
|---|---|---|
| Database | SQLite | Our recommendation (zero setup, no Docker, graders can run it from a clean clone). The user chose Postgres; see risks below. |
| Phone matching | Encrypt phones (deterministic) | Deterministic encryption leaks equality like a hash does but is reversible, i.e. more PII at rest than needed. |
| Phone matching | Random-salt hash per row | Cannot be matched by equality, which defeats the purpose. |
| Name | Store plaintext | Violates the PII requirement. |
| Name | Postgres `pgcrypto` | Ties encryption to the database and puts the key in SQL statements and logs. |
| Fields | One table per workflow | More tables, more code per new workflow. |
| Fields | EAV (entity-attribute-value) | Slow, awkward to query. |
| Merge | Read, merge in Python, write back | More round trips and a race if two ingests ever overlap. The SQL way is atomic per row. |
| Idempotency | Dedupe by content hash only | Would let a changed file of the same name through. |
| Idempotency | Make every insert `ON CONFLICT DO NOTHING` and ignore the file | Row inserts would be safe but the counters (merged, warnings) would be double-counted. |
| Ledger | Enforce append-only in the app only | One bug could edit history. Triggers cannot be forgotten. |

## Findings / results

- **113 tests pass**, none skipped. New: 7 PII tests and 12 database tests.
- The database matches the in-memory model on the real seed-42 data: 1,892 leads, 108
  rejections, and the file counters sum to 2,057 / 108 / 57 / 1,892 / 122 (rows / rejected /
  merged / new / warnings).
- Re-ingesting both drops returns "already ingested" for every file and leaves a checksum of
  the `leads` table unchanged.
- A stored row looks like: hash `f5bafc0a...`, last4 `5499`, batch `batch_001`, ref
  `MOT/2026/100142`, `pii: {"customer_name": "gAAAAA..."}`, attributes with dates and amounts.
  No readable name or phone anywhere (a test scans every table).
- **Speed:** ~2,057 lead rows in 1.3 s (~1,600 rows/s), because each row does its own
  statements. Fine for the small set, but the large set is ~140k lead rows and ~600k events,
  so step 7 will need batching (staging table + `COPY`/`executemany`) to hit the 2-minute goal.

## Risks and caveats

- **Postgres means Docker for graders.** They must run `docker compose up -d` before our
  pipeline. SQLite would not need that. Documented here and to be repeated in `SUBMISSION.md`.
- **Fresh database for a fresh seed.** A different seed reuses names like `drop_1`, so an
  existing database would reject the changed files (`FileContentChanged`). A `reset` command
  is planned for step 7 and will be documented.
- **Key lives next to the code** (a git-ignored file). Fine for a take-home that must run from
  a clean clone; production would use a secrets manager / KMS.
- `ingest_seq` and `ingested_at` record order but nothing uses them yet.
- Docker Desktop was started from this session, and the compose project created a volume
  `agenticuniverse_pgdata`. The database name and password come from the assignment's
  compose file (local only).
