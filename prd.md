# PRD: MongoDB Database Load Process

**Author:** Anshul
**Status:** Draft v1.0
**Doc type:** Product Requirements Document (data engineering / ETL utility)

---

## 1. Context

An earlier attempt at this project (`DatabaseLoadScript`, Python 3.8, referencing `program.py` / `File.py`, alongside a sibling `DatabaseController` project) exists only as an IDE project shell — no working source code survived. This PRD reconstructs the requirements for a clean rebuild, scoped as a standalone, portfolio-quality data pipeline: pull data out of a **SQL database and CSV files**, and load it into **MongoDB**.

## 2. Problem Statement

Teams frequently need to consolidate structured data that lives in two different worlds — relational tables and flat files dropped by upstream systems (exports, vendor feeds, manual uploads) — into a single document-oriented store for downstream use (APIs, analytics, search, reporting). Doing this reliably means handling two very different source shapes, validating and reshaping the data consistently, and loading it into MongoDB in a way that's safe to re-run without creating duplicates or corrupting collections.

## 3. Goals

- Load data from a **relational SQL database** (e.g., PostgreSQL/MySQL via SQLAlchemy) into MongoDB collections.
- Load data from **CSV files** (single file or a batch/directory of files) into MongoDB collections.
- Support both **full loads** (truncate-and-reload) and **incremental loads** (upsert by key / load-since-timestamp).
- Be **idempotent and safely re-runnable** — re-running a load must not create duplicate documents.
- Validate and clean data before it reaches MongoDB (type coercion, required-field checks, malformed-row handling).
- Be **configuration-driven**, not hardcoded — source connection, target collection, and field mappings should live in config, not in code.
- Produce clear **logs and a run summary** (rows read, rows loaded, rows rejected, duration) for every run.
- Be runnable **on demand from the command line**, with a clean path to later scheduling.

## 4. Non-Goals

- Not building a full orchestration platform (Airflow/Prefect) — this is a load utility that a scheduler can call, not the scheduler itself.
- Not building a general-purpose ETL framework for arbitrary source types — scope is SQL + CSV → MongoDB only.
- Not handling real-time/streaming ingestion (e.g., CDC, Kafka) — batch loads only in v1.
- Not building a UI — this is a CLI/library tool.
- Not handling MongoDB cluster administration (sharding, replica set setup) — assumes a target MongoDB instance/Atlas cluster already exists.

## 5. Users

- **Primary:** the developer/data engineer running or scheduling loads (you, in a portfolio-project context — this doubles as a demonstrable ETL/data-engineering artifact).
- **Secondary:** downstream consumers of the MongoDB collections (an API, a dashboard, an analyst running queries) who need the data to be complete, deduplicated, and up to date.

## 6. Recommended Run Pattern

You weren't sure how this should run — here's the recommendation and why.

**Build it as an on-demand CLI tool first, designed to be schedule-ready from day one.**

Concretely:
- The core logic is a plain Python function/class (`load(source_config, target_config)`) with no scheduler dependency baked in.
- A thin CLI wrapper (`python -m loader run --source sql --table customers --collection customers`) triggers it manually.
- Because every load is idempotent and config-driven, dropping it into `cron`, Windows Task Scheduler, or an Airflow `PythonOperator`/`BashOperator` later is a one-line addition, not a rewrite.

This avoids over-building (a full scheduler nobody asked for) while not painting you into a corner — and for CSV sources specifically, "on demand / triggered" fits naturally since files usually arrive irregularly (a `--watch` flag over a folder is a cheap v1.1 add if needed later).

## 7. Functional Requirements

### 7.1 Source: SQL Database
- Connect via SQLAlchemy (supports PostgreSQL, MySQL, SQL Server with driver swap).
- Load a full table, a specific view, or an arbitrary SQL query (config-specified).
- Support **incremental extraction** via a watermark column (e.g., `updated_at > last_run_timestamp`), with the watermark persisted between runs (a small local state file or a `_pipeline_state` collection in MongoDB itself).
- Batch/paginate large table reads (configurable chunk size, e.g., 5,000–10,000 rows) rather than loading an entire table into memory.
- Handle connection failures with retry + backoff (configurable max retries).

### 7.2 Source: CSV Files
- Load a single file or every matching file in a directory (configurable glob pattern, e.g., `orders_*.csv`).
- Configurable delimiter, encoding, and header handling.
- Skip and log malformed rows rather than failing the entire file (configurable: fail-fast vs. skip-and-continue mode).
- Track processed files (so re-running doesn't reprocess files already loaded) — e.g., an `_processed_files` manifest with filename + hash + timestamp.
- Move or tag processed files (e.g., move to a `processed/` subfolder, or rename with a `.done` suffix) so a scheduled run doesn't double-load.

### 7.3 Transformation Layer
- Field mapping: source column/field → target MongoDB field name (config-driven, e.g., a YAML/JSON mapping file per source).
- Type coercion: strings → dates, decimals, booleans, nested objects, based on declared schema.
- Null/default handling: explicit rules for missing values per field.
- Optional light denormalization: combine columns into nested sub-documents (e.g., flat `address_line1`, `address_city` SQL columns → a nested `address: {...}` document) — this is where the relational → document shape change actually happens, and it should be config-declared, not hardcoded per source.

### 7.4 Load into MongoDB
- Support three write modes, selectable per run:
  1. **Full replace** — drop/replace the target collection (for reference/lookup data).
  2. **Upsert by key** — `update_one(..., upsert=True)` on a declared unique key (most common; keeps the load idempotent).
  3. **Append-only** — plain inserts, for event/log-style data where duplicates are acceptable or de-duped downstream.
- Use `bulk_write` / `insert_many` with reasonable batch sizes (not one `insert_one` per row) for throughput.
- Ensure target collection has the right indexes (declared in config, created if missing) — especially the unique key used for upserts.
- Write failures for individual documents (e.g., a bad type) should not abort the whole batch; log the failed document(s) and continue.

### 7.5 Validation & Data Quality
- Pre-load schema validation: required fields present, types match expectations.
- Row-level rejection with reason logged (e.g., `orders.csv row 214: missing required field 'order_id'`).
- Post-load reconciliation: compare source row count vs. loaded document count vs. rejected count; flag if the numbers don't add up.

### 7.6 Logging, Observability & Run Reporting
- Structured logging (Python `logging` module, JSON-formatted lines) — log level configurable.
- Every run produces a summary: source, target, mode, rows read, rows loaded, rows rejected/skipped, duration, start/end timestamps.
- Non-zero exit code on failure, so a scheduler or CI step can detect and alert on it.
- Optional: write the run summary itself as a document into a `_pipeline_runs` MongoDB collection — gives you a queryable load history for free.

### 7.7 Configuration
- All connection strings and credentials read from environment variables or a `.env` file (never hardcoded, never committed).
- Per-source config (what to load, mapping, write mode, batch size) in a version-controlled YAML/JSON file — separates "how the tool works" (code) from "what to load" (config).

### 7.8 CLI Interface
```
loader run --source sql --config configs/customers_sql.yaml
loader run --source csv --config configs/orders_csv.yaml
loader status --last 10          # show recent run summaries
loader validate --config configs/customers_sql.yaml   # dry-run: report what would load, load nothing
```

## 8. Non-Functional Requirements

| Category | Requirement |
|---|---|
| Reliability | A failed run must leave MongoDB in a consistent state — no partial upserts left half-applied without being logged; safe to simply re-run. |
| Performance | Should comfortably handle source tables/files in the low millions of rows on a single machine, via batching/chunking rather than loading everything into memory at once. |
| Idempotency | Running the same load twice with the same source data produces the same end state (no duplicate documents). |
| Security | No secrets in source control; connection strings via env vars; principle of least privilege on the DB user and Mongo user used. |
| Maintainability | Adding a new source table/CSV feed should mean adding a config file, not writing new code. |
| Testability | Core transform/load logic is unit-testable without a live database (use fixtures + a local/mocked Mongo instance, e.g., `mongomock` or a Dockerized test Mongo). |
| Portability | Runs the same way locally, in CI, and in a container (Dockerfile provided). |

## 9. Proposed Architecture

```
                ┌──────────────────┐
                │   Config (YAML)  │
                │ source + mapping │
                │ + write mode     │
                └────────┬─────────┘
                         │
   ┌─────────────────────┼─────────────────────┐
   │                     │                     │
┌──▼───────┐      ┌──────▼──────┐       ┌──────▼──────┐
│ SQL      │      │ CSV         │       │  State /    │
│ Extractor│      │ Extractor   │       │  Watermark  │
│(SQLAlchemy)     │(pandas/csv) │       │  store      │
└──┬───────┘      └──────┬──────┘       └──────┬──────┘
   │                     │                     │
   └─────────┬───────────┘                     │
             │                                 │
      ┌──────▼───────┐                         │
      │ Transform /   │◄────────────────────────┘
      │ Validate layer│
      └──────┬────────┘
             │
      ┌──────▼────────┐
      │ Mongo Loader   │──► MongoDB (target collection)
      │ (bulk upsert/  │
      │  insert/replace)│
      └──────┬────────┘
             │
      ┌──────▼────────┐
      │ Run summary /  │──► logs + optional _pipeline_runs collection
      │ reconciliation │
      └────────────────┘
```

## 10. Suggested Tech Stack

- **Language:** Python 3.11+ (the original targeted 3.8; no reason not to move up)
- **SQL extraction:** SQLAlchemy + the relevant DB driver (`psycopg2` for Postgres, `pymysql`/`mysqlclient` for MySQL)
- **CSV handling:** `pandas` (easiest for type inference/chunked reads) or stdlib `csv` for lower memory overhead on very large files
- **Mongo client:** `pymongo` (use `bulk_write` for batched upserts)
- **Config:** `PyYAML` + `pydantic` (or `dataclasses`) for validating config shape before a run even starts
- **CLI:** `click` or `typer`
- **Testing:** `pytest`, `mongomock` or a Dockerized MongoDB for integration tests
- **Packaging/runtime:** `Dockerfile` + `docker-compose.yml` (spin up a local MongoDB + optional Postgres for local dev/testing)
- **CI:** GitHub Actions — lint + unit tests + a smoke-test load against a throwaway Mongo container on every push

## 11. Suggested Project Structure

```
mongodb-database-load-process/
├── README.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml            # local mongo (+ postgres for test fixtures)
├── configs/
│   ├── customers_sql.yaml
│   └── orders_csv.yaml
├── src/
│   └── loader/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py             # config schema + loader
│       ├── extractors/
│       │   ├── sql_extractor.py
│       │   └── csv_extractor.py
│       ├── transform.py
│       ├── mongo_loader.py
│       ├── state.py              # watermark / processed-file tracking
│       └── logging_utils.py
├── tests/
│   ├── fixtures/
│   ├── test_sql_extractor.py
│   ├── test_csv_extractor.py
│   ├── test_transform.py
│   └── test_mongo_loader.py
└── sample_data/
    └── orders_sample.csv         # synthetic sample data, not real/proprietary data
```

## 12. Delivery Plan (Phased)

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 — Core CSV loader | CSV extractor, transform layer, Mongo upsert loader, CLI, logging | Can load a sample CSV into a local MongoDB idempotently, with a run summary |
| 2 — SQL loader | SQL extractor (full + incremental via watermark), same transform/load path | Can load a sample Postgres table into MongoDB, incrementally on a second run |
| 3 — Validation & reconciliation | Row-level validation, rejection logging, post-load count reconciliation | Bad rows are rejected and logged, not silently dropped or crashing the run |
| 4 — Hardening | Retry/backoff on SQL connection, batch tuning, Dockerization, CI pipeline | `docker compose up` + one command runs an end-to-end demo load in CI |
| 5 — Docs & portfolio polish | README with architecture diagram, setup instructions, sample config, before/after data screenshots | A stranger can clone the repo and run a working demo load in under 10 minutes |

## 13. Acceptance Criteria (v1 "done")

- [ ] Running the CLI against the sample CSV and a sample SQL table loads both into MongoDB with correct field mappings.
- [ ] Running the same load twice produces the same document count (no duplicates) via upsert mode.
- [ ] A deliberately malformed row in the sample CSV is rejected and appears in the run summary, without failing the whole run.
- [ ] Killing the SQL source mid-run and re-running completes successfully (retry/backoff or clean resumability).
- [ ] `docker compose up` brings up a working local MongoDB the loader can target out of the box.
- [ ] README documents setup, config format, and how to add a new source.

## 14. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Schema drift in source SQL table/CSV breaks the load silently | Schema validation step before load; fail loudly with a clear error rather than loading malformed data |
| Large source tables blow memory | Chunked/batched reads everywhere; never `SELECT *` into a single DataFrame for big tables |
| Duplicate documents from re-runs | Enforce upsert-by-key as the default write mode; unique index on the key field in MongoDB |
| Credentials leaked in config files | `.env` + `.gitignore`; config files reference env var names, never raw secrets |
| Scope creep into a full orchestrator | Explicitly out of scope (Section 4); keep the tool callable by any scheduler rather than becoming one |

## 15. Open Questions

- Which specific SQL dialect (PostgreSQL vs MySQL vs SQL Server) should the first working example target?
- Is there a real (or realistic synthetic) dataset in mind, or should sample data be generated from scratch for the repo?
- Should the `_pipeline_runs` history collection and `_processed_files` manifest live in MongoDB itself, or in a lightweight local store (SQLite) — matters if this needs to run in an environment without guaranteed Mongo write access for metadata?
