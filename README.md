# MongoDB Database Load Process

A config-driven ETL utility that loads data from a **SQL database** and **CSV files** into **MongoDB** — idempotent, batched, validated, and schedule-ready. Built as a portfolio-quality data engineering artifact per [prd.md](prd.md); build history and task breakdown in [tasks.md](tasks.md).

**Further reading:** [docs/USAGE.md](docs/USAGE.md) for the full CLI/config reference and troubleshooting guide, [docs/ROOT_CAUSE_ANALYSIS.md](docs/ROOT_CAUSE_ANALYSIS.md) for every bug found during development (BSON type gotchas, a real counting bug, library version conflicts) with root cause and fix, and [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) for the full test suite breakdown plus live smoke-test results against real MongoDB/Postgres/Docker.

## Why

Teams routinely need to consolidate two very different source shapes — relational tables and flat files dropped by upstream systems — into one document store for downstream use (APIs, analytics, search). Doing that reliably means handling both source shapes, validating and reshaping data consistently, and loading it in a way that's safe to re-run without duplicating or corrupting data. This tool does exactly that, nothing more:

- **Not** an orchestration platform — a scheduler (cron, Task Scheduler, Airflow) calls this, this isn't the scheduler.
- **Not** a general ETL framework — SQL + CSV → MongoDB only.
- **Not** streaming/CDC — batch loads.

## Architecture

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
│(SQLAlchemy)     │(csv module) │       │  store      │
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
      │ Run summary /  │──► logs + `_pipeline_runs` collection
      │ reconciliation │
      └────────────────┘
```

Core logic (`src/loader/pipeline.py`) is a plain set of functions with zero scheduler dependency — `cli.py` is a thin wrapper around it, so dropping this into cron/Task Scheduler/Airflow later is a one-line addition, not a rewrite.

## Project layout

```
mongoDB-load/
├── configs/                    # per-source YAML configs (what to load — not code)
│   ├── customers_sql.yaml
│   └── orders_csv.yaml
├── sample_data/
│   ├── orders_sample.csv       # synthetic sample CSV
│   └── seed_customers.sql      # synthetic sample Postgres seed data
├── src/loader/
│   ├── cli.py                  # `loader run|status|validate`
│   ├── config.py                # pydantic config schema + YAML loader
│   ├── extractors/
│   │   ├── csv_extractor.py    # single file or directory glob, manifest-tracked
│   │   └── sql_extractor.py    # chunked, incremental (watermark), retry+backoff
│   ├── transform.py            # field mapping, type coercion, nesting, rejection
│   ├── mongo_loader.py          # batched upsert/full_replace/append + indexes
│   ├── state.py                 # watermark persistence
│   ├── logging_utils.py         # JSON logging + RunSummary
│   └── pipeline.py              # extract → transform → load → summarize
├── tests/                       # pytest, mongomock + SQLite fixtures — no live DB needed
├── Dockerfile
├── docker-compose.yml           # local Mongo + Postgres (+ loader) for dev/CI
└── .github/workflows/ci.yml     # lint, test, docker-compose smoke test
```

## Setup

### Local (no Docker)

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate on macOS/Linux
pip install -e ".[dev,postgres]"
cp .env.example .env            # then fill in MONGO_URI / SQL_CONN_STRING
```

`.env` is never committed (see `.gitignore`) — connection strings and credentials always come from environment variables, never hardcoded config.

### With Docker

```bash
docker compose up -d mongo postgres
docker compose run --rm loader run --source csv --config configs/orders_csv.yaml
docker compose run --rm loader run --source sql --config configs/customers_sql.yaml
```

`docker compose up` (no service names) just brings the `loader` service up idle (`--help`) — invoke real loads with `docker compose run --rm loader ...` as above, pointed at the `mongo`/`postgres` service hostnames baked into the compose file's environment block.

## Usage

```bash
loader run --source csv --config configs/orders_csv.yaml
loader run --source sql --config configs/customers_sql.yaml
loader status --last 10          # recent run summaries from the `_pipeline_runs` collection
loader validate --config configs/customers_sql.yaml   # dry-run: report what would load, load nothing
```

Every run prints a JSON summary and exits non-zero on failure:

```json
{
  "source": "csv:sample_data",
  "target": "mongo:orders",
  "mode": "upsert",
  "rows_read": 7,
  "rows_loaded": 6,
  "rows_rejected": 1,
  "reconciled": true,
  "errors": ["orders_sample.csv row 8: expected 6 columns, got 5"]
}
```

## Config format

One YAML file per source under `configs/`. Four top-level keys:

```yaml
source:            # csv or sql — see below
target:             # MongoDB collection + write key + indexes
mapping:            # source field -> target field, types, defaults, nesting
write_mode:         # full_replace | upsert | append
batch_size:         # Mongo write batch size (default 1000)
```

**CSV source** ([configs/orders_csv.yaml](configs/orders_csv.yaml)):

```yaml
source:
  type: csv
  path: sample_data          # a file, or a directory to glob over
  glob: "orders_*.csv"       # only used when path is a directory
  delimiter: ","
  encoding: "utf-8"
  has_header: true
  on_malformed_row: skip      # skip | fail
  move_processed_to: processed  # subfolder files are moved to after a successful load
  manifest_path: .state/orders_processed_files.json  # tracks filename+hash so re-runs don't reprocess
```

**SQL source** ([configs/customers_sql.yaml](configs/customers_sql.yaml)):

```yaml
source:
  type: sql
  connection_env: SQL_CONN_STRING   # env var name, not the connection string itself
  table: customers                   # or `query: "SELECT ..."` for an arbitrary query
  watermark_column: updated_at        # enables incremental extraction
  chunk_size: 5000                    # rows per paginated read
  max_retries: 3                      # connection retry with exponential backoff
  state_path: .state/customers_watermark.json
```

**Mapping** — field-by-field type coercion (`str`, `int`, `float`, `decimal`, `bool`, `date`, `datetime`), required/default handling, and optional flattened-columns → nested-document construction:

```yaml
mapping:
  fields:
    - source: order_id
      target: order_id
      type: str
      required: true
    - source: amount
      target: amount
      type: decimal
      required: true
      default: "0"
  nested:
    - target: address
      fields:
        address_line1: line1
        address_city: city
```

A row that fails a required-field or type-coercion check is **rejected and logged**, not silently dropped and not fatal to the run — it shows up in the run summary's `errors` list with the source file/row (or SQL row's key) and reason.

## Write modes

| Mode | Behavior | When to use |
|---|---|---|
| `upsert` | `bulk_write` with `UpdateOne(..., upsert=True)` on `target.unique_key` | Default — keeps re-runs idempotent |
| `full_replace` | Loads into a temp collection, then atomically drops + renames over the target | Reference/lookup data you want to fully refresh each run |
| `append` | Plain batched `insert_many` | Event/log-style data where duplicates are acceptable or de-duped downstream |

## Before / after: what a load actually does

CSV row (`sample_data/orders_sample.csv`):

```csv
order_id,order_date,customer_email,amount,address_line1,address_city
1001,2024-01-05,alice@example.com,49.99,123 Main St,Springfield
```

...becomes this MongoDB document in the `orders` collection after `loader run --source csv --config configs/orders_csv.yaml`:

```json
{
  "_id": "ObjectId(...)",
  "order_id": "1001",
  "order_date": "2024-01-05T00:00:00Z",
  "customer_email": "alice@example.com",
  "amount": "Decimal128(49.99)",
  "address": { "line1": "123 Main St", "city": "Springfield" }
}
```

Note the flat `address_line1`/`address_city` CSV columns became a nested `address` sub-document, `order_date` became a real `datetime`, and `amount` became a `Decimal128` (BSON's exact-decimal type — plain Python `Decimal` isn't BSON-serializable, so the transform layer converts it automatically) — all driven purely by the `mapping` block in the config, no code changes.

## How to add a new source

Adding a new table or CSV feed means **adding a config file, not writing code**:

1. Copy the closest existing config (`configs/orders_csv.yaml` or `configs/customers_sql.yaml`) as a starting point.
2. Point `source` at the new file/directory (CSV) or table/query + connection env var (SQL).
3. List the fields you want in `mapping.fields`, with their target names/types; add a `nested` block if you want flat columns folded into a sub-document.
4. Pick a `target.collection`, a `target.unique_key` if using `upsert`, and any extra `target.indexes` you want created.
5. Run `loader validate --config configs/your_new_config.yaml` first — it reports exactly what would load (and what would be rejected) without touching MongoDB or mutating any re-run state (CSV manifest, SQL watermark).
6. Once it looks right, `loader run --source csv|sql --config configs/your_new_config.yaml`.

No changes to `src/loader/` are needed for a new source of the same kind (CSV or SQL) — that's the whole point of the config-driven design (PRD §NFR "Maintainability").

## Testing

```bash
pytest tests/ -v
```

The full suite (48 tests) runs with **no live database** — `mongomock` stands in for MongoDB and SQLite stands in for the SQL source, so it runs the same locally, in CI, and in a container.

```bash
ruff check .
black --check .
```

## Reliability notes

- **Idempotency**: `upsert` mode plus a unique-key index means re-running the same load twice produces the same document count — verified in `tests/test_mongo_loader.py` and live against both MongoDB Atlas and a Supabase Postgres instance during development.
- **Safe to kill mid-run**: the SQL extractor only persists its watermark *after* a run's extraction generator fully drains. If a run is killed partway through, the watermark stays at its last safe value, so a fresh re-run simply reprocesses that same window — safely, because loads are idempotent. See `test_mid_run_crash_does_not_advance_watermark_and_rerun_recovers` in `tests/test_sql_extractor.py`.
- **Malformed rows never fail a run**: a structurally malformed CSV row (wrong column count) or a row that fails required-field/type validation is rejected, logged with its exact location and reason, and the run continues — the run summary's `reconciled` flag flags any count mismatch as a warning.

## Status

All phases complete and verified. See [tasks.md](tasks.md) for the full phased build log, [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) for the full test breakdown, and [docs/ROOT_CAUSE_ANALYSIS.md](docs/ROOT_CAUSE_ANALYSIS.md) for every bug found and fixed along the way. Verified live against: MongoDB Atlas, Supabase Postgres, the local `docker compose` stack, and — with a deliberately-broken-then-reverted test to confirm CI actually catches failures — [GitHub Actions](https://github.com/morid648/mongodb-load/actions) on the repo itself.
