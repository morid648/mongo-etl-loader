# Usage Guide

Detailed reference for running, configuring, and extending the loader. For a quick start, see the top-level [README.md](../README.md); for build history and phase-by-phase status, see [tasks.md](../tasks.md).

## Contents

- [Installation](#installation)
- [Environment variables](#environment-variables)
- [CLI reference](#cli-reference)
- [Config file reference](#config-file-reference)
- [Write modes in depth](#write-modes-in-depth)
- [Field types and coercion](#field-types-and-coercion)
- [Nested document mapping](#nested-document-mapping)
- [Incremental SQL loading](#incremental-sql-loading)
- [CSV file tracking](#csv-file-tracking)
- [Row rejection and reconciliation](#row-rejection-and-reconciliation)
- [Running with Docker](#running-with-docker)
- [Troubleshooting](#troubleshooting)

## Installation

### Local (no Docker)

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\Activate.ps1       # Windows PowerShell
# source .venv/bin/activate        # macOS/Linux

pip install -e ".[dev,postgres]"   # add `mysql` extra instead/also if you need MySQL
cp .env.example .env               # then fill in MONGO_URI / SQL_CONN_STRING
```

Requires Python 3.11+.

### Docker

```bash
docker compose up -d mongo postgres
docker compose run --rm loader run --source csv --config configs/orders_csv.yaml
```

See [Running with Docker](#running-with-docker) for the full picture.

## Environment variables

All connection strings and credentials come from environment variables — never from config files — read via a `.env` file (loaded automatically by every CLI command) or your shell/CI environment.

| Variable | Used by | Example |
|---|---|---|
| `MONGO_URI` | Every command (target database) | `mongodb://localhost:27017` or `mongodb+srv://user:pass@cluster.mongodb.net/?appName=...` |
| `MONGO_DB` | Every command (target database name) | `portfolio_load` |
| `SQL_CONN_STRING` (or whatever name `source.connection_env` in your config points at) | `--source sql` runs only | `postgresql+psycopg2://user:pass@host:5432/dbname` |
| `LOG_LEVEL` | Every command | `INFO` (default), `DEBUG`, `WARNING`, `ERROR` |

A config's `source.connection_env` field for SQL sources is the **name** of an environment variable, not the connection string itself — this keeps credentials out of version-controlled config files entirely. You can point different SQL source configs at different env vars (e.g. `PROD_SQL_CONN`, `STAGING_SQL_CONN`) if you have more than one database.

**Note on special characters in connection strings:** a password containing `@`, `:`, `/`, or other URL-reserved characters must be percent-encoded (e.g. `Pass@123` → `Pass%40123`) or SQLAlchemy/PyMongo will misparse the userinfo/host boundary.

## CLI reference

### `loader run --source <csv|sql> --config <path>`

Runs one load end-to-end: extract → transform → load → summarize. Prints a JSON run summary to stdout and exits non-zero on failure.

```bash
loader run --source csv --config configs/orders_csv.yaml
loader run --source sql --config configs/customers_sql.yaml
```

### `loader status --last <N>`

Prints the last N run summaries from the `_pipeline_runs` MongoDB collection (default `N=10`), most recent first. Requires `MONGO_URI`/`MONGO_DB` — it doesn't need the source config at all, since it's just reading history.

```bash
loader status --last 5
```

If no runs have ever written to `_pipeline_runs`, it prints `no run history found` and exits 0.

### `loader validate --config <path>`

Dry-runs a config: extracts and transforms exactly like `run` would, reports the same JSON summary shape, but:

- **Never opens a MongoDB connection** — nothing is written, no indexes are created.
- **Never mutates re-run state** — the CSV processed-files manifest isn't updated and files aren't moved; the SQL watermark isn't advanced.

Use it before wiring up a new config, or before a first production run, to see exactly what would load and what would be rejected.

```bash
loader validate --config configs/customers_sql.yaml
```

## Config file reference

Every config is a YAML file with four top-level keys: `source`, `target`, `mapping`, `write_mode` (plus optional `batch_size`).

### `source` (csv)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `type` | `"csv"` | — | discriminator, must be `csv` |
| `path` | string | — | a single file, **or** a directory to glob over |
| `glob` | string \| null | `null` | glob pattern (e.g. `"orders_*.csv"`), only used when `path` is a directory |
| `delimiter` | string | `,` | CSV field delimiter |
| `encoding` | string | `utf-8` | file encoding |
| `has_header` | bool | `true` | whether the first row is a header |
| `on_malformed_row` | `skip` \| `fail` | `skip` | a row whose column count doesn't match the header is logged+skipped (`skip`) or aborts the whole file (`fail`) |
| `move_processed_to` | string \| null | `processed` | subfolder (relative to the source file) files are moved into after a successful load; `null` disables moving |
| `manifest_path` | string | `.state/processed_files.json` | where the filename+hash+timestamp manifest is stored |

### `source` (sql)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `type` | `"sql"` | — | discriminator, must be `sql` |
| `connection_env` | string | — | **name** of the env var holding the connection string |
| `table` | string \| null | `null` | table (or view) name — mutually exclusive-ish with `query` (at least one required) |
| `query` | string \| null | `null` | an arbitrary `SELECT` — wrapped in a subquery internally so pagination/incremental filtering still apply |
| `watermark_column` | string \| null | `null` | column used for incremental extraction (e.g. `updated_at`); omit for a full-table-every-time load |
| `chunk_size` | int | `5000` | rows fetched per paginated read |
| `max_retries` | int | `3` | connection retry attempts (exponential backoff) before giving up |
| `state_path` | string | `.state/watermark.json` | where the last-seen watermark is persisted |

### `target`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `collection` | string | — | MongoDB collection name |
| `unique_key` | string \| null | `null` | target field used as the upsert key; **required** when `write_mode: upsert` |
| `indexes` | list of `{fields, unique, name}` | `[]` | extra indexes to create if missing (the unique-key index is always added automatically for `upsert` mode) |

### `mapping`

```yaml
mapping:
  fields:
    - source: <source column/field name>
      target: <target Mongo field name>
      type: str | int | float | decimal | bool | date | datetime   # default: str
      required: true | false                                        # default: false
      default: <any value, coerced to `type` if the field is blank/missing>
  nested:
    - target: <target sub-document field name>
      fields:
        <source column>: <key inside the nested document>
        <source column>: <key inside the nested document>
```

### `write_mode` and `batch_size`

`write_mode` is one of `full_replace`, `upsert` (default), `append`. `batch_size` (default `1000`) controls how many documents go into each MongoDB bulk write call — tune it down for very large/complex documents, up for small ones, to balance throughput against memory.

## Write modes in depth

| Mode | Mechanism | Idempotent? | Best for |
|---|---|---|---|
| `upsert` | `bulk_write([UpdateOne({unique_key: v}, {"$set": doc}, upsert=True), ...])`, batched | Yes — re-running with the same data leaves the same document count | Most sources — the safe default |
| `full_replace` | Loads every document into a fresh temp collection, then does `drop` + `rename` on the target — the swap only happens after the temp collection is fully populated, so a failure mid-load never leaves the target half-updated | Yes, by construction (each run fully replaces the collection) | Small reference/lookup tables you want fully refreshed every run |
| `append` | Plain `insert_many`, batched | **No** — every run adds new documents, duplicates included | Event/log-style data where the source itself doesn't repeat, or de-duplication happens downstream |

## Field types and coercion

| `type` | Python/BSON result | Notes |
|---|---|---|
| `str` | `str` | default type if omitted |
| `int` | `int` | via `int(str(value).strip())` |
| `float` | `float` | via `float(str(value).strip())` |
| `decimal` | `bson.Decimal128` | plain Python `Decimal` is **not** BSON-serializable — the transform layer converts automatically, see [Root Cause Analysis](ROOT_CAUSE_ANALYSIS.md#1-decimal-values-crashed-mongodb-writes) |
| `bool` | `bool` | accepts `true/1/yes/y/t` and `false/0/no/n/f` (case-insensitive) |
| `date` | `datetime.datetime` at midnight | BSON has no bare "date" type either — stored as a `datetime` with a zero time component, see [Root Cause Analysis](ROOT_CAUSE_ANALYSIS.md#2-date-values-crashed-mongodb-writes) |
| `datetime` | `datetime.datetime` | parsed against `%Y-%m-%dT%H:%M:%S`, `%Y-%m-%d %H:%M:%S`, or `%Y-%m-%dT%H:%M:%S.%f` |

A value that fails coercion, or a `required: true` field that's blank/missing with no `default`, raises a rejection for that row — the row is skipped and logged, the run continues (see [Row rejection and reconciliation](#row-rejection-and-reconciliation)).

## Nested document mapping

`mapping.nested` folds several flat source columns into one sub-document — this is the actual "relational → document shape change" the PRD calls out. Example:

```yaml
nested:
  - target: address
    fields:
      address_line1: line1
      address_city: city
```

Given a source row `{address_line1: "123 Main St", address_city: "Springfield", ...}`, this produces `doc["address"] = {"line1": "123 Main St", "city": "Springfield"}`. Nested fields are copied as-is (no type coercion) — if you need coercion on a nested value, coerce it in a flat `fields` entry first and reference the coerced target name isn't currently supported; nested mapping reads directly from the *raw* source row.

## Incremental SQL loading

When `watermark_column` is set, every SQL run:

1. Reads the last-persisted watermark from `state_path` (or treats it as "the beginning of time" on a first run).
2. Queries only rows where `watermark_column > last_watermark`, ordered by that column, paginated by `chunk_size`.
3. Tracks the maximum watermark value seen across all rows read.
4. **Only after the entire read completes successfully** persists that new maximum back to `state_path`.

Step 4's ordering is deliberate — see [Root Cause Analysis: mid-run resumability](ROOT_CAUSE_ANALYSIS.md#4-mid-run-resumability-design-note-not-a-bug) for why this makes a killed-mid-run-and-restarted load safe by construction rather than requiring explicit checkpoint/resume logic.

To force a full re-read ignoring the watermark, delete the `state_path` file (or point the config at a fresh one).

## CSV file tracking

When `source.path` is a directory, every file the extractor successfully reads gets recorded in the manifest at `manifest_path` (filename + SHA-256 content hash + timestamp) and — unless `move_processed_to: null` — moved into that subfolder. On the next run, a file already in the manifest **with a matching hash** is skipped entirely (not even opened). Changing a file's content (even keeping the same name) changes its hash, so it's treated as new.

This is separate from, and complementary to, upsert idempotency: the manifest stops you from *re-reading* a file at all; upsert stops re-reading the same data from creating duplicates if you do.

## Row rejection and reconciliation

A row is rejected (counted in `rows_rejected`, logged in the run summary's `errors` list, run continues) when:

- **CSV-structural**: the row has a different number of columns than the header (message: `"<file> row <n>: expected <k> columns, got <j>"`).
- **Validation**: a `required: true` field is blank with no `default`, or a value fails type coercion (message: `"<file> row <n>: <reason>"` for CSV, `"row <key>: <reason>"` for SQL).
- **Load-level**: MongoDB itself rejects an individual document in a batch (e.g. a duplicate key violation outside the upsert path) — the rest of the batch still writes.

Every run summary carries `reconciled: rows_read == rows_loaded + rows_rejected`. If that's ever `false`, something in the counting itself is wrong (this happened once during development — see [Root Cause Analysis #3](ROOT_CAUSE_ANALYSIS.md#3-upsert-loaded-count-was-double-counted)) — a warning is logged, and it's worth treating as a bug report rather than a normal outcome.

## Running with Docker

```bash
docker compose up -d mongo postgres     # brings up local Mongo (27017) + Postgres (5432), both with healthchecks
docker compose run --rm loader run --source csv --config configs/orders_csv.yaml
docker compose run --rm loader run --source sql --config configs/customers_sql.yaml
docker compose run --rm loader status --last 5
docker compose run --rm loader validate --config configs/customers_sql.yaml
```

- `docker compose up` (with no service names, or including `loader`) brings the `loader` service up idle — it just runs `--help` and exits — because there's no single meaningful default command for an ETL CLI. Always invoke real commands via `docker compose run --rm loader ...`.
- The Postgres service auto-seeds the sample `customers` table from `sample_data/seed_customers.sql` via `docker-entrypoint-initdb.d` — this only runs the **first** time a fresh Postgres data volume is created. `docker compose down -v` (note the `-v`) removes that volume if you want a truly clean re-seed.
- The `loader` container's `MONGO_URI`/`SQL_CONN_STRING` in `docker-compose.yml` point at the `mongo`/`postgres` service hostnames (Docker's internal DNS), not `localhost` — that's specific to running *inside* the compose network. If you run `loader` locally (outside Docker) against the same compose-started databases, use `localhost` instead, since compose publishes both ports to the host.
- The image bakes in `sample_data/` and `configs/` at build time (`COPY` in the [Dockerfile](../Dockerfile)) rather than mounting them — so each `docker compose run --rm` starts from a fresh, unmodified copy of the sample CSV every time (no manifest state persists between container runs unless you add a volume for `.state/`). This was actually useful during testing: it let idempotency get verified against completely fresh containers, not just a warm one.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `config error: environment variable 'MONGO_URI' is not set` | `.env` missing or not in the working directory | `cp .env.example .env` and fill it in; confirm you're running from the repo root |
| `config error: invalid config in ...: ... unique_key` | `write_mode: upsert` with no `target.unique_key` | Set `target.unique_key` to a mapped target field name |
| A password with `@`/`:`/`/` breaks the connection string | URL-reserved characters not percent-encoded | Percent-encode the password (e.g. `@` → `%40`) |
| `bson.errors.InvalidDocument` mentioning `Decimal` or `date` | A mapping field typed as something other than `decimal`/`date`/`datetime` is actually producing a raw Python `Decimal`/`date` | Shouldn't happen if you use this loader's own `type:` coercions — see [Root Cause Analysis](ROOT_CAUSE_ANALYSIS.md) if you hit this on a custom code path |
| A CSV file never gets reprocessed even after editing it | Manifest hash still matches — check that the file's content actually changed, or delete the relevant `manifest_path` file | Delete/edit `.state/processed_files.json` |
| SQL incremental load returns 0 rows on every run | Watermark already advanced past all existing rows | Delete `state_path`'s file to force a full re-read, or insert rows with a newer `watermark_column` value |
| `pymongo`/`mongomock` `TypeError` about an unexpected `sort` keyword in tests | `pymongo>=4.9` combined with `mongomock` (latest is 4.3.0, predates that pymongo API change) | This project pins `pymongo<4.9` in `pyproject.toml` specifically to avoid this — see [Root Cause Analysis](ROOT_CAUSE_ANALYSIS.md#5-mongomock-and-pymongo-version-incompatibility) |
