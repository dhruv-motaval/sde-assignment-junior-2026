# Step 1: Setup and data inspection

## What we did

1. Picked **Assignment A (Data)** over B (Workflow). Reasons are in the chat history; short
   version: A is graded by diffing deterministic output files, B is graded by `kill -9` crash
   tests, and the profile (FastAPI, ingestion pipelines, SQLite) fits A.
2. Cloned `AryamannNingombam/sde-assignment-junior-2026` into the project folder.
3. Generated the small dataset: `python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data`.
   `data/` is already in the repo's `.gitignore`, so it is never committed.
4. Inspected the data with throwaway scripts before writing any real code.
5. Created `solution/` (the package), `pytest.ini`, and this documentation.

## Why we did it this way

- **Inspect the data first.** The spec lists what is messy, but the generator shows what the
  grader's data actually looks like. Two things in the spec had hidden questions (does an
  override carry a `workflow_id`? is a `call_id` unique across workflows?). Answering them
  from the data avoids building on a wrong assumption.
- **Clone into the working folder, keep original files untouched.** The assignment says to
  start from this repo and put code in `solution/`. Originals stay unmodified so a diff shows
  only our work.
- **Package named `solution`, not a top-level module.** Run as `python -m solution ...`.
  We deliberately avoided a file called `types.py` or `json.py`, because a module with a
  standard-library name shadows the real one when its folder is on `sys.path`.
- **`pytest.ini` with `pythonpath = .`.** With the test files in `solution/tests/`, pytest
  would otherwise not find the `solution` package unless run as `python -m pytest`. One
  small file makes plain `pytest` work from a clean clone.

## Alternatives we could have used

| Alternative | Why we did not |
|---|---|
| GitHub fork of the assignment repo | A fork of a public repo is public, and the assignment says the solution must be private. |
| New empty repo, copy the files in | Loses the history link and risks accidentally editing originals. Cloning then re-pointing the remote is cleaner. |
| `pyproject.toml` with a `src/` layout and `pip install -e .` | Proper for a library, but an extra install step for a take-home. `pytest.ini` is enough. |
| Skip inspection, build straight from the spec | Faster for 10 minutes, risky for hours. The spec cannot tell you e.g. that phone formats differ *within the same call*. |

## Findings (seed 42, small)

**Files:** 2 drops, 7 lead CSVs, 2 event files (4,247 + 4,498 lines).

**Leads**
- `batch_001` and `batch_002` exist in *both* workflows. A batch id is only unique within a
  workflow, so every key and sort must use `(workflow_id, batch_id)`.
- `drop_2` renames and reorders columns (`Insured Name`, `Policy No`, `Mobile No`, `Expiry
  Date`, `Premium`, `Language`) and adds an extra `agent_notes` column that must be ignored.
- Values arrive padded and in mixed case (`" ta "`, `"HI"`), and money as `₹27,554`,
  `Rs. 26505`, `INR 33473.7`.

**Events** (8,737 parsed lines)
- 8 **malformed** lines, all truncated JSON (e.g. `{"event_id": "evt_00002099"`).
- Event ids repeated: at least 237 ids occur more than once, including across drops.
- Event types: initiated 2,757, ended 2,431, connected 1,596, disposition.set 1,560,
  telephony_failed 322, disposition.override 71.
- **All 71 overrides carry `workflow_id` but no `phone`**, and `call_id` is never shared
  between workflows. So an override can be linked as `(workflow_id, call_id)` and then to the
  phone of that call's other events.
- **The same call shows different phone strings in different events** (2,425 calls). So the
  phone must be normalised **per event** before hashing or matching. Never compare raw strings.
- Timestamps: 3,888 UTC (`Z`), 3,073 with offset, 1,776 naive (naive means IST).

## Open questions

None new from this step; the rules that are still ambiguous are tracked in
[`../LOGBOOK.md`](../LOGBOOK.md).

## Not done yet

- The remote `origin` still points at the assignment repo. It must be re-pointed to a
  **private** repo of our own before anything is pushed. Nothing is committed or pushed.
- Verified only on Python 3.14 here. The code uses features from 3.11 or older, but we have
  not run it on 3.11 itself.
