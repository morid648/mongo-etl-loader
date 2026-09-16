# Tasks: MongoDB Database Load Process

Derived from [prd.md](prd.md). Tasks are atomic (one commit-sized unit of work each) and grouped into phases ordered strictly by dependency — do not start a phase until the prior phase's tasks are checked off, unless a task explicitly says it can run in parallel.

Checkbox convention: `[ ]` not started, `[x]` done.

---

## Phase 0 — Project Scaffolding

No functional code depends on anything else yet; this phase just creates the skeleton everything else is written into.

- [x] 0.1 Init git repo (`git init`), add `.gitignore` (Python, `.env`, `__pycache__/`, `.venv/`, `*.egg-info`, IDE folders)
- [x] 0.2 Create directory tree per PRD §11: `src/loader/`, `src/loader/extractors/`, `configs/`, `tests/`, `tests/fixtures/`, `sample_data/`
- [x] 0.3 Create `pyproject.toml` with project metadata, Python 3.11+ requirement, and dependency groups (runtime: `sqlalchemy`, `pymongo`, `pandas`, `pyyaml`, `pydantic`, `click`/`typer`, `python-dotenv`; dev: `pytest`, `mongomock`, `ruff`/`black`)
- [x] 0.4 Add empty `__init__.py` files for `src/loader/` and `src/loader/extractors/`
- [x] 0.5 Create `.env.example` documenting expected env vars (`MONGO_URI`, `SQL_CONN_STRING`, etc.) with placeholder values, no real secrets
- [x] 0.6 Create stub `README.md` with project title and one-line description (full docs come in Phase 5)
- [x] 0.7 Verify `pip install -e .` (or `uv sync`) succeeds in a clean virtualenv

---

## Phase 1 — Core CSV Loader

Depends on: Phase 0. Establishes the config schema, transform layer, and Mongo loader that every later phase reuses.

### 1.1 Config schema
- [x] 1.1.1 Define `config.py` pydantic models: `SourceConfig` (base), `FieldMapping`, `WriteMode` enum (`full_replace`, `upsert`, `append`), `TargetConfig` (collection name, unique key, indexes)
- [x] 1.1.2 Add `load_config(path: str) -> PipelineConfig` function that reads YAML and validates via pydantic, raising a clear error on schema mismatch
- [x] 1.1.3 Write `configs/orders_csv.yaml` sample config (source path/glob, delimiter, field mapping, write mode = upsert, unique key)
- [x] 1.1.4 Unit test: valid config loads; malformed config (missing required key, wrong type) raises a descriptive validation error

### 1.2 CSV extractor
- [x] 1.2.1 Implement `extractors/csv_extractor.py`: read a single file path (configurable delimiter/encoding/header row)
- [x] 1.2.2 Extend extractor to accept a directory + glob pattern and iterate matching files in a deterministic order
- [x] 1.2.3 Implement skip-and-continue vs fail-fast malformed-row handling (configurable), logging the row number + reason on skip
- [x] 1.2.4 Implement `_processed_files` manifest (local JSON or SQLite file): record filename + content hash + timestamp after successful processing
- [x] 1.2.5 On extractor start, skip any file already present in the manifest with a matching hash
- [x] 1.2.6 Implement post-processing file handling: move processed file to `processed/` subfolder (config toggle for move vs `.done`-suffix rename)
- [x] 1.2.7 Unit tests: happy-path read, malformed-row skip, duplicate-run skip (manifest hit), directory glob matching

### 1.3 Transform layer
- [x] 1.3.1 Implement `transform.py`: apply field mapping (source column → target Mongo field) from config
- [x] 1.3.2 Implement type coercion per declared schema (string → date/decimal/bool/int)
- [x] 1.3.3 Implement null/default handling rules per field (explicit default, required-but-missing → reject)
- [x] 1.3.4 Implement nested sub-document construction from flat columns (e.g. `address_*` → `address: {...}`), config-declared
- [x] 1.3.5 Unit tests: mapping applied correctly, type coercion for each supported type, default applied on null, row rejected when required field missing, nested doc built correctly

### 1.4 Mongo loader
- [x] 1.4.1 Implement `mongo_loader.py`: Mongo client setup from env var connection string
- [x] 1.4.2 Implement `upsert` write mode using `bulk_write` with `UpdateOne(..., upsert=True)` on the declared unique key, batched (configurable batch size)
- [x] 1.4.3 Implement `full_replace` write mode (load to temp collection, then atomic rename/swap, or drop-and-reinsert)
- [x] 1.4.4 Implement `append` write mode using `insert_many` batched
- [x] 1.4.5 Implement index creation from config (create declared indexes, including the unique key index, if missing) on loader startup
- [x] 1.4.6 Implement per-document write-failure handling: catch `BulkWriteError`, log failed documents with reason, continue rather than abort the batch
- [x] 1.4.7 Unit tests (using `mongomock` or Dockerized Mongo): upsert idempotency (same input twice → same doc count), full_replace swap, append behavior, index creation, partial-batch-failure continues

### 1.5 Logging & run summary
- [x] 1.5.1 Implement `logging_utils.py`: structured JSON-formatted logging setup, configurable level
- [x] 1.5.2 Implement run-summary object: source, target, mode, rows read/loaded/rejected, start/end timestamps, duration
- [x] 1.5.3 Wire non-zero process exit code on run failure

### 1.6 CLI wiring (CSV path only)
- [x] 1.6.1 Implement `cli.py` with `click`/`typer`: `loader run --source csv --config <path>` wired end-to-end (extract → transform → load → summary)
- [x] 1.6.2 Print/log run summary to stdout at end of run
- [x] 1.6.3 Manual smoke test: run CLI against `sample_data/orders_sample.csv` into a local MongoDB, confirm documents land correctly (verified live against user-provided MongoDB Atlas cluster — no local Docker/MongoDB available on this machine)
- [x] 1.6.4 Manual smoke test: re-run the same command, confirm no duplicate documents (upsert idempotency end-to-end)

**Phase 1 exit criteria (PRD §12):** sample CSV loads into local MongoDB idempotently with a run summary. ✅ **DONE** — 1.6.3–1.6.4 pass (verified against Atlas cluster).

---

## Phase 2 — SQL Loader

Depends on: Phase 1 (reuses config schema, transform layer, Mongo loader, CLI, logging — only adds a new extractor + state store + CLI wiring).

### 2.1 State / watermark store
- [x] 2.1.1 Implement `state.py`: read/write a watermark value (last successful `updated_at` per source) to a local state file (or `_pipeline_state` Mongo collection — pick one per PRD open question, default to local file for v1 simplicity)
- [x] 2.1.2 Unit test: watermark persists across a simulated process restart

### 2.2 SQL extractor
- [x] 2.2.1 Implement `extractors/sql_extractor.py`: connect via SQLAlchemy using env-var connection string
- [x] 2.2.2 Implement full-table / view / arbitrary-query extraction per config
- [x] 2.2.3 Implement chunked/paginated reads (configurable chunk size, default 5,000–10,000 rows) — never load full table into memory
- [x] 2.2.4 Implement incremental extraction: `WHERE <watermark_col> > :last_run_timestamp`, reading/writing via `state.py`
- [x] 2.2.5 Implement connection retry with exponential backoff (configurable max retries)
- [x] 2.2.6 Unit tests (against a throwaway/dockerized Postgres or SQLite fixture): full extraction, chunked pagination correctness, incremental extraction returns only new/changed rows, retry triggers on simulated connection failure

### 2.3 Config + sample data
- [x] 2.3.1 Write `configs/customers_sql.yaml` sample config (connection env var name, table/query, watermark column, chunk size, field mapping, write mode)
- [x] 2.3.2 Add SQL fixture/seed script to populate a sample `customers` table for local dev/testing (synthetic data only)

### 2.4 CLI wiring (SQL path)
- [x] 2.4.1 Extend `cli.py`: `loader run --source sql --config <path>` wired end-to-end
- [x] 2.4.2 Manual smoke test: full load of sample Postgres table into MongoDB (verified live against user-provided Supabase Postgres instance)
- [x] 2.4.3 Manual smoke test: insert/update a few source rows, re-run, confirm only the incremental delta is loaded (watermark advances correctly) — caught and fixed a real double-counting bug in `_load_upsert`'s `loaded` count (`matched_count` already includes `modified_count`) during this test

**Phase 2 exit criteria (PRD §12):** sample Postgres table loads into MongoDB, incrementally on a second run. ✅ **DONE** — 2.4.2–2.4.3 pass (verified against Supabase Postgres + Atlas cluster).

---

## Phase 3 — Validation & Reconciliation

Depends on: Phase 1 and Phase 2 (adds a validation step in front of the existing transform/load path for both source types).

- [x] 3.1 Implement pre-load schema validation: required-fields-present + type-match check, reusable by both extractors
- [x] 3.2 Implement row-level rejection with structured reason logging (e.g. `orders.csv row 214: missing required field 'order_id'`)
- [x] 3.3 Wire rejected-row count and sample reasons into the run summary (extends 1.5.2)
- [x] 3.4 Implement post-load reconciliation: compare source rows read vs. documents loaded vs. rows rejected; log a warning/flag if counts don't reconcile
- [x] 3.5 Add a deliberately malformed row to `sample_data/orders_sample.csv` for use as a regression fixture
- [x] 3.6 Unit tests: required-field-missing row rejected with correct reason, type-mismatch row rejected, reconciliation flags a mismatch when counts are engineered to disagree
- [x] 3.7 Manual smoke test: run CLI against the CSV with the malformed row, confirm it's rejected and logged in the summary without failing the run (verified live against Atlas — row 8 rejected with exact reason, 6/7 loaded, reconciled)

**Phase 3 exit criteria (PRD §12/§13):** bad rows are rejected and logged, not silently dropped or crashing the run. ✅ **DONE**.

---

## Phase 4 — Hardening

Depends on: Phases 1–3 (adds resilience, tooling, and CI around the now-functionally-complete pipeline).

### 4.1 Reliability polish
- [x] 4.1.1 Confirm/extend SQL retry+backoff (2.2.5) covers mid-run connection drops, not just initial connect — resolved by design: the watermark is only persisted after the extraction generator fully drains, so an aborted run leaves it untouched and a fresh re-run reprocesses the same safe window idempotently (upsert). Verified with `test_mid_run_crash_does_not_advance_watermark_and_rerun_recovers` in [tests/test_sql_extractor.py](tests/test_sql_extractor.py)
- [x] 4.1.2 Audit batch-tuning defaults (chunk size, Mongo batch size) and make both configurable with sane defaults — `chunk_size` (SQL, default 5000) and `batch_size` (Mongo, default 1000) are both config-driven per [config.py](src/loader/config.py)
- [x] 4.1.3 Implement `loader status --last N`: read and print recent run summaries (from `_pipeline_runs` Mongo collection — resolves PRD open question in favor of Mongo, matching the write side in 4.2)
- [x] 4.1.4 Implement `loader validate --config <path>`: dry-run that reports what would load without writing to Mongo (never opens a Mongo connection; never mutates the CSV manifest or SQL watermark)
- [x] 4.1.5 Unit tests for `status` and `validate` CLI commands (via the underlying `validate_pipeline`/`write_run_summary`/`get_recent_runs` functions in [tests/test_validate.py](tests/test_validate.py) and [tests/test_mongo_loader.py](tests/test_mongo_loader.py))

### 4.2 Optional run-history persistence
- [x] 4.2.1 Implement writing each run summary as a document into a `_pipeline_runs` MongoDB collection (PRD §7.6, optional but needed for `loader status`) — best-effort/non-fatal write in [mongo_loader.py](src/loader/mongo_loader.py)

### 4.3 Containerization
- [x] 4.3.1 Write `Dockerfile` for the loader (Python 3.11+ base, installs package, entrypoint = CLI)
- [x] 4.3.2 Write `docker-compose.yml`: local MongoDB service + Postgres service (seeded with sample data for 2.3.2) + loader service
- [x] 4.3.3 Manual test: `docker compose up` brings up Mongo + Postgres reachable by the loader out of the box — verified live once Docker Desktop was started: `docker compose up -d mongo postgres` (both healthy), `docker compose run --rm loader run --source csv/sql ...` succeeded against both, Postgres auto-seeded via `docker-entrypoint-initdb.d`, re-run confirmed idempotent (6 orders / 5 customers, no duplicates, even across fresh ephemeral containers with no persisted `.state/`)

### 4.4 CI pipeline
- [x] 4.4.1 Add GitHub Actions workflow: lint (ruff/black check) on push — [.github/workflows/ci.yml](.github/workflows/ci.yml); lint ruleset pinned to `["E","F","I"]` in [pyproject.toml](pyproject.toml) (ruff's ambient default pulled in DTZ/UP/C4 rules not relevant to this project); verified locally with `ruff check .` and `black --check .` (both pass, 21 files)
- [x] 4.4.2 Add unit test job (pytest) on push — same workflow, `pytest tests/ -v`; verified locally (48/48 pass)
- [x] 4.4.3 Add smoke-test job: spin up throwaway Mongo (+ Postgres) containers via compose, run an end-to-end demo load, assert non-zero exit on induced failure and zero exit on success — verified for real on GitHub Actions ([run 35065963555](https://github.com/morid648/mongodb-load/actions/runs/35065963555)): `test` (28s), `lint` (23s), `smoke-test` (55s) all passed on a fresh GitHub-hosted runner after pushing to https://github.com/morid648/mongodb-load
- [x] 4.4.4 Confirm CI fails the build on lint or test failure (sanity-check by introducing then reverting a deliberate break) — pushed a commit with a deliberately failing test ([run 35066130537](https://github.com/morid648/mongodb-load/actions/runs/35066130537)): `test` job failed on the broken assertion, `lint` job *also* failed (an incidental line-length violation in the test's own docstring), and `smoke-test` was correctly skipped since it depends on both — then reverted and confirmed CI went back to green ([run 35066236697](https://github.com/morid648/mongodb-load/actions/runs/35066236697), all 3 jobs `success`)

**Phase 4 fully closed.** Repo pushed to GitHub (private): https://github.com/morid648/mongodb-load

**Phase 4 exit criteria (PRD §12):** `docker compose up` + one command runs an end-to-end demo load in CI. ✅ when 4.3.3 and 4.4.3 pass.

---

## Phase 5 — Docs & Portfolio Polish

Depends on: Phase 4 (documents the finished, hardened tool).

- [x] 5.1 Write full `README.md`: problem statement, architecture diagram (reuse PRD §9 ASCII diagram or redraw), setup instructions (local + Docker), config file format reference
- [x] 5.2 Document "how to add a new source" walkthrough (new YAML config, no code changes) in README
- [x] 5.3 Add before/after data screenshots or sample output (source CSV/SQL rows vs. resulting Mongo documents) to README — used real before/after JSON captured from a live smoke test rather than a screenshot (more useful in a git-rendered README)
- [x] 5.4 Verify `sample_data/orders_sample.csv` and SQL seed data are fully synthetic (no real/proprietary data) — confirmed: fictional names + fictional Simpsons-town placenames throughout, no real/proprietary data
- [x] 5.5 Final walkthrough: clone repo fresh into a clean directory, follow only the README, confirm a working demo load completes in under 10 minutes — simulated a fresh clone (clean copy minus `.venv`/`.git`/`.env`/state), followed only the README's documented commands end-to-end (venv, install, `.env`, `loader run` ×2, `status`, `validate`); **total time: ~128 seconds**, well under 10 minutes
- [ ] 5.6 Tag v1.0 release / final commit — **not done**: repo has no commits yet (git safety policy: never commit without the user explicitly asking); ask the user before doing this

**Phase 5 exit criteria (PRD §12):** a stranger can clone the repo and run a working demo load in under 10 minutes. ✅ **DONE** — 5.5 verified at ~128s.

---

## Acceptance Criteria Cross-Check (PRD §13)

Mapped to the tasks that satisfy each item — use this to confirm "done" once all phases are checked off:

| PRD Acceptance Criterion | Satisfied by | Status |
|---|---|---|
| CLI loads sample CSV + sample SQL table with correct field mappings | 1.6.3, 2.4.2 | ✅ verified live (Atlas + Supabase) |
| Same load run twice → same doc count (no duplicates) | 1.6.4, 2.4.3 | ✅ verified live |
| Malformed CSV row rejected, appears in run summary, doesn't fail run | 3.7 | ✅ verified live |
| Killing SQL source mid-run + re-run completes successfully | 4.1.1 | ✅ verified by design + unit test |
| `docker compose up` brings up working local MongoDB out of the box | 4.3.3 | ✅ verified live |
| README documents setup, config format, adding a new source | 5.1, 5.2 | ✅ done |

---

## Open Questions Blocking Full Precision (carried from PRD §15) — now resolved

- SQL dialect for the first working example: **Postgres** (used throughout — Supabase-hosted instance for live testing).
- Real vs. synthetic sample dataset: **synthetic** — all sample CSV/SQL data is fictional (`sample_data/orders_sample.csv`, `sample_data/seed_customers.sql`).
- `_pipeline_runs` / `_processed_files` location: **`_pipeline_runs` lives in MongoDB** (queryable via `loader status`); **watermark + processed-file manifest live in local JSON files** under `.state/` (simpler for v1, no Mongo write dependency for state tracking specifically).

---

## Remaining follow-ups (not yet done)

- **5.6 Tag v1.0 release** — repo is committed and pushed (https://github.com/morid648/mongodb-load); a `v1.0` git tag hasn't been cut yet. Ask before tagging.
