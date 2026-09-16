# Test Results

Two layers of verification back this project: a mocked/local unit test suite (fast, no live infra, runs the same in CI) and live smoke tests against real MongoDB, Postgres, and Docker (slower, run manually during development, catch what mocks can't — see [ROOT_CAUSE_ANALYSIS.md #2](ROOT_CAUSE_ANALYSIS.md#2-date-values-crashed-mongodb-writes) for a concrete example of a bug the unit suite alone would have missed). This document records both, as of the current state of the repo.

## Unit test suite

```bash
pytest tests/ -v
```

**Result: 48 passed, 0 failed, 9 warnings, ~1.1s wall time.** No live database of any kind required — `mongomock` stands in for MongoDB, plain SQLite files stand in for the SQL source.

| Test file | Count | What it covers |
|---|---|---|
| [test_config.py](../tests/test_config.py) | 4 | YAML config loading, pydantic validation, upsert-requires-unique-key rule |
| [test_csv_extractor.py](../tests/test_csv_extractor.py) | 6 | Single-file and directory-glob extraction, malformed-row skip vs. fail-fast, manifest-based re-run skip, move-to-processed |
| [test_logging_utils.py](../tests/test_logging_utils.py) | 3 | `RunSummary` reconciliation flag (both true and engineered-false cases), duration/timestamp computation |
| [test_mongo_loader.py](../tests/test_mongo_loader.py) | 9 | Upsert idempotency, upsert count correctness (regression test for the double-counting bug), full_replace swap, append mode, index creation (including the double-index-collision regression), `_pipeline_runs` write/read |
| [test_pipeline.py](../tests/test_pipeline.py) | 3 | End-to-end CSV pipeline against a mongomock-backed database: clean-run reconciliation, row-number-tagged rejection messages, malformed-row-doesn't-fail-the-run |
| [test_sql_extractor.py](../tests/test_sql_extractor.py) | 6 | Full extraction, chunked pagination correctness, incremental (watermark) extraction, mid-run-crash resumability, connection retry (both eventual-success and exhausted-retries paths) |
| [test_state.py](../tests/test_state.py) | 2 | Watermark persistence across a simulated process restart, overwrite behavior |
| [test_transform.py](../tests/test_transform.py) | 12 | Field mapping, every supported type coercion (parametrized), `Decimal128` conversion, invalid-value rejection, default-value application, required-field rejection, nested sub-document construction |
| [test_validate.py](../tests/test_validate.py) | 3 | `loader validate` never touches Mongo, never mutates the CSV manifest or moves files, never advances the SQL watermark |

**Warnings:** 9 instances of a single `DeprecationWarning` from `sqlalchemy`/`sqlite3` about the default datetime adapter being deprecated as of Python 3.12 — specific to the SQLite test fixtures (production SQL usage goes through `psycopg2`, which doesn't hit this path) and tracked, not silenced, since it costs nothing to leave visible.

### Lint and formatting

```bash
ruff check .     # → All checks passed!
black --check .  # → All done! 21 files would be left unchanged.
```

Ruff's rule selection is deliberately pinned (`select = ["E", "F", "I"]` in `pyproject.toml`) rather than left to ruff's version-dependent ambient defaults — see [ROOT_CAUSE_ANALYSIS.md #7](ROOT_CAUSE_ANALYSIS.md#7-ruffs-ambient-default-rule-set-didnt-match-the-projects-needs) for why.

## Live smoke tests

Everything below was run against **real** infrastructure, not mocks: a user-provided MongoDB Atlas cluster, a user-provided Supabase Postgres instance, and (once Docker Desktop was available) a local Docker Compose stack.

### Phase 1 — CSV → MongoDB Atlas

| Check | Result |
|---|---|
| First run loads `sample_data/orders_sample.csv` into the `orders` collection | ✅ 6/6 rows loaded, correct field mapping, nested `address` sub-document, `Decimal128` amounts, `datetime` order dates |
| File moved to `processed/` and recorded in the manifest after success | ✅ |
| Re-running the same source data a second time | ✅ still 6 documents afterward — confirmed via `db.orders.distinct('order_id')`, no duplicates |

### Phase 2 — Postgres (Supabase) → MongoDB Atlas, incremental

| Check | Result |
|---|---|
| Seed `customers` table via `sample_data/seed_customers.sql` | ✅ 5 rows seeded |
| Full load into the `customers` collection | ✅ 5/5 rows loaded, correct types |
| Insert 1 new row + update 1 existing row in Postgres, then re-run | ✅ incremental run read exactly 2 rows (only the changed/new ones) |
| — *this run is where the double-counting bug was caught* | `rows_read: 2` vs. `rows_loaded: 3` — fixed (see [RCA #3](ROOT_CAUSE_ANALYSIS.md#3-upsert-loaded-count-was-double-counted)), re-run afterward showed `rows_read: 2, rows_loaded: 2, reconciled: true` |
| Final document count | ✅ 6 total (5 original + 1 new), updated row reflected the new email/timestamp, no duplicates |
| Immediate re-run with nothing changed in Postgres | ✅ `rows_read: 0` — watermark correctly prevented reprocessing |

### Phase 3 — Malformed row handling

| Check | Result |
|---|---|
| A structurally malformed row (wrong column count) added to the sample CSV | ✅ rejected with `"orders_sample.csv row 8: expected 6 columns, got 5"`, logged in the run summary |
| Rest of the file still loads | ✅ 6/7 rows loaded, 1 rejected, run summary `reconciled: true`, process exit code 0 |

### Phase 4 — `loader status` / `loader validate`

| Check | Result |
|---|---|
| `loader validate` reports counts without touching Mongo | ✅ same rejected-row detection as a real run, `sample_data/` untouched (no move, no manifest write) afterward |
| `loader run` followed by `loader status --last N` | ✅ status correctly surfaced the just-completed run's exact summary from the `_pipeline_runs` collection |

### Phase 4 — Docker Compose stack (once Docker Desktop was available)

| Check | Result |
|---|---|
| `docker compose up -d mongo postgres` | ✅ both containers reached `healthy` within the CI workflow's health-check wait loop |
| Postgres auto-seed via `docker-entrypoint-initdb.d` | ✅ `customers` table populated from `sample_data/seed_customers.sql` with zero manual steps |
| `docker compose run --rm loader run --source csv ...` | ✅ built the loader image, ran successfully against the containerized Mongo: 6/7 rows loaded, 1 rejected (same malformed-row fixture) |
| `docker compose run --rm loader run --source sql ...` | ✅ 5/5 rows loaded from the auto-seeded Postgres container |
| Re-running the CSV load in a **fresh, ephemeral** container (`--rm`, no `.state/` volume) | ✅ `db.orders.countDocuments({})` still `6` afterward — confirms idempotency holds even when the manifest/watermark state doesn't persist at all between invocations, purely on Mongo's upsert semantics |
| `docker compose run --rm loader status --last N` | ✅ correctly listed prior runs from the containerized Mongo's `_pipeline_runs` collection |

### CI workflow commands, verified locally

The [.github/workflows/ci.yml](../.github/workflows/ci.yml) `smoke-test` job's exact shell commands were run locally, line-for-line, once Docker was available — this isn't the same as a real GitHub Actions execution (no remote is configured for this repo yet), but it does confirm the workflow's *logic* is correct rather than just plausible-looking YAML:

```bash
docker compose up -d mongo postgres
# health-check wait loop (docker inspect -f "{{.State.Health.Status}}" ...) — both reported healthy
loader run --source csv --config configs/orders_csv.yaml   # succeeded
loader run --source sql --config configs/customers_sql.yaml # succeeded
loader status --last 5                                       # correctly showed both runs
# induced-failure check:
loader run --source csv --config configs/does_not_exist.yaml
# → "config error: config file not found: configs\does_not_exist.yaml", non-zero exit — correctly detected as a failure
docker compose down -v
```

All steps behaved exactly as the workflow expects. What's still unverified is the literal "runs on GitHub's own Actions runners" part — see [tasks.md](../tasks.md) for that as an explicit open follow-up.

## Summary

| Layer | Status |
|---|---|
| Unit tests (48) | ✅ all passing, no live infra needed |
| Lint (`ruff`) / format (`black`) | ✅ clean |
| Live MongoDB Atlas + Supabase Postgres | ✅ full + incremental loads, idempotency, malformed-row handling all verified |
| Live Docker Compose (Mongo + Postgres + loader image) | ✅ full stack verified, including auto-seed and idempotency across ephemeral containers |
| GitHub Actions (real runner execution) | ⏳ not yet — no GitHub remote configured for this repo |
