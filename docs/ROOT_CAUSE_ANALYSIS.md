# Root Cause Analysis

Every issue found while building and live-testing this project, in the order it was discovered. Each entry follows: **Symptom → Root cause → Fix → Verification**. Kept here rather than in commit messages because several of these are non-obvious footguns (BSON's actual type support, library version compatibility) that are worth understanding rather than just having been silently patched.

---

## 1. `Decimal` values crashed MongoDB writes

**Phase found:** 1 (Core CSV loader), during initial development — caught before the first live run.

**Symptom:** Coercing a CSV `amount` column to Python's `decimal.Decimal` and passing it straight into a MongoDB document would fail. `pymongo`/`bson` has no encoder for the built-in `Decimal` type at all.

**Root cause:** BSON (MongoDB's wire/storage format) supports an exact decimal type, but it's `bson.decimal128.Decimal128`, not Python's standard-library `decimal.Decimal`. `pymongo` does not implicitly convert one to the other — passing a raw `Decimal` into `insert_many`/`bulk_write` raises `bson.errors.InvalidDocument`.

**Fix:** [transform.py](../src/loader/transform.py)'s `coerce_value` for `FieldType.DECIMAL` wraps the parsed value: `Decimal128(Decimal(str(value).strip()))` instead of returning the bare `Decimal`. This also applies to `default` values for decimal-typed fields — they're coerced the same way `_apply_field` coerces real values, so a default like `default: "0"` becomes `Decimal128("0")`, not the raw string `"0"` or a bare `Decimal(0)`.

**Verification:** `test_coerce_value_decimal_returns_decimal128` and `test_default_applied_on_null` in [tests/test_transform.py](../tests/test_transform.py) assert the exact `Decimal128` type comes back. Confirmed again live: the first real load against MongoDB Atlas (Phase 1 smoke test) wrote `amount` fields as `Decimal128(49.99)` etc. with no errors.

---

## 2. `date` values crashed MongoDB writes

**Phase found:** 1, during the first live smoke test against MongoDB Atlas — this one **wasn't** caught by unit tests (the mongomock-backed tests never encode to real BSON, so the encoding failure never surfaced until pymongo/bson actually serialized a document).

**Symptom:** The very first live CLI run against Atlas failed with a `bson.errors.InvalidDocument` traceback while encoding an `order_date` field, coerced via `FieldType.DATE`.

**Root cause:** Same class of bug as #1: BSON has a `datetime` type, but no bare "date-only" type. The original `coerce_value` implementation for `FieldType.DATE` returned a Python `datetime.date` (via `.date()`), which — like `Decimal` — has no BSON encoder.

**Fix:** Changed `FieldType.DATE` coercion to return a `datetime.datetime` at midnight (`datetime(value.year, value.month, value.day)`) instead of a bare `date`. This keeps a semantic distinction from `FieldType.DATETIME` in the config schema (a user writing `type: date` is declaring intent — "this is a calendar date, I don't care about time-of-day") while storing something BSON can actually round-trip.

**Verification:** `test_coerce_value_supported_types[date-2024-01-05-expected5]` in [tests/test_transform.py](../tests/test_transform.py) asserts a `datetime` (not `date`) comes back. Confirmed live immediately after the fix: the same Atlas run succeeded, and inspecting the written documents showed `order_date` as e.g. `datetime.datetime(2024, 1, 5, 0, 0)`.

**Lesson for the test suite:** this bug's escape from unit testing (mongomock never encodes to real wire-format BSON, so a type that pymongo's *encoder* rejects still "writes" fine against a mock) is exactly why the project's phased build plan called for live smoke tests against a real database at the end of each phase, not just mocked unit tests — see [TEST_RESULTS.md](TEST_RESULTS.md) for what those caught versus what mongomock alone would have missed.

---

## 3. Upsert `loaded` count was double-counted

**Phase found:** 2 (SQL loader), during the live incremental-load smoke test against Supabase Postgres + MongoDB Atlas.

**Symptom:** A run that should have processed exactly 2 rows (1 update to an existing document, 1 insert of a new one) reported `rows_read: 2, rows_loaded: 3` in its summary — and logged a `reconciliation mismatch` warning, since `2 != 3`.

**Root cause:** [mongo_loader.py](../src/loader/mongo_loader.py)'s `_load_upsert` computed the loaded count as:

```python
result.loaded += res.upserted_count + res.modified_count + res.matched_count
```

MongoDB's `BulkWriteResult` reports `matched_count` as *every* document that matched an upsert's filter — whether or not it was actually changed — and `modified_count` as the subset of those that *were* changed. A document that matches and gets modified is counted in **both** `matched_count` and `modified_count`; summing all three double-counts every modified (as opposed to newly-inserted or no-op-matched) document.

**Fix:** Changed the calculation to `result.loaded += res.upserted_count + res.matched_count` — dropping `modified_count` entirely, since `matched_count` already covers it (a matched document was either modified or was already correct; either way it counts once as "loaded").

**Verification:** Added a regression test, `test_upsert_loaded_count_not_double_counted_on_mixed_batch` in [tests/test_mongo_loader.py](../tests/test_mongo_loader.py), which specifically exercises a mixed batch (one update + one insert) and asserts `loaded == 2`. Re-ran the live Supabase/Atlas incremental smoke test after the fix — no reconciliation warning, correct counts, and `docker compose`-based testing later confirmed the same on ephemeral local containers.

**Why this one mattered more than it looks:** `rows_loaded` isn't just a cosmetic number — the run summary's `reconciled` flag (`rows_read == rows_loaded + rows_rejected`) is the mechanism this project uses to automatically flag "something doesn't add up" without a human staring at logs (PRD §7.5/§7.6). A silent off-by-N in the load count would have made that safety net actively misleading rather than merely wrong.

---

## 4. Mid-run resumability (design note, not a bug)

**Phase found:** 4 (Hardening), while addressing the PRD's "killing the SQL source mid-run and re-running completes successfully" acceptance criterion.

This isn't a bug that was found and fixed — it's included here because it was a deliberate design decision made *in response to* thinking through failure modes, and it's easy to get wrong in the opposite direction (i.e., someone "fixing" it into an actual bug later).

**The requirement:** if a SQL extraction is interrupted partway through (source killed, network drop, process crash), a subsequent re-run of the same command must complete successfully without losing or duplicating data.

**The design:** [sql_extractor.py](../src/loader/extractors/sql_extractor.py)'s `extract()` is a generator that only calls `watermark_store.set(max_seen_watermark)` **after** its internal `while True` read loop exits normally (all pages read, generator about to return). If the generator is abandoned early — an exception propagates out of the consumer's `for chunk in extract(...)` loop, or the generator is garbage-collected mid-iteration — that final `watermark_store.set(...)` call is never reached, because it sits *outside* the `try/finally` block that only guarantees `engine.dispose()` runs.

**Why that's correct:** it means an aborted run leaves the persisted watermark exactly where it was before the run started. A fresh process re-running the same config sees the same "new rows since watermark" window it would have seen if the first attempt had never happened, and — because the target write mode is `upsert` by default — reprocessing that same window is idempotent. No explicit checkpoint/resume bookkeeping is needed; the ordering of "commit state" strictly after "confirm full success" is the entire mechanism.

**Verification:** `test_mid_run_crash_does_not_advance_watermark_and_rerun_recovers` in [tests/test_sql_extractor.py](../tests/test_sql_extractor.py) — simulates a crash after the first chunk, asserts the watermark file wasn't created, then asserts a full re-run afterward sees all 5 original rows again (not 3, not 0). This was judged a better and more deterministic test than literally killing a live Postgres connection mid-query, which is hard to orchestrate reliably and would only exercise the same code path less precisely.

---

## 5. mongomock and pymongo version incompatibility

**Phase found:** 1, while first running the mongomock-based test suite.

**Symptom:** Every test that called `collection.bulk_write([UpdateOne(...)])` failed with `TypeError: BulkOperationBuilder.add_update() got an unexpected keyword argument 'sort'`.

**Root cause:** `pymongo` (installed at `4.18.1`, the latest at the time) added a `sort` parameter to `UpdateOne` in a `4.9.x` release. `pymongo`'s own internal `UpdateOne._add_to_bulk()` unconditionally forwards that parameter to whatever bulk-operation builder it's handed. `mongomock` (latest published version: `4.3.0`, which predates that pymongo release) implements its own `BulkOperationBuilder.add_update()` with an older signature that doesn't accept `sort` — so real `pymongo` code calling into `mongomock`'s mock builder crashes, even though neither library is "broken" in isolation. `mongomock` has had no release since to catch up.

**Fix:** Pinned `pymongo>=4.6,<4.9` in [pyproject.toml](../pyproject.toml), with an inline comment explaining why. This keeps the installed pymongo below the version that introduced the incompatible parameter, while staying well within a modern, supported pymongo range for actual production MongoDB use.

**Verification:** All `mongomock`-based tests in [tests/test_mongo_loader.py](../tests/test_mongo_loader.py) pass after the downgrade. This constraint should be revisited if `mongomock` ever ships a release compatible with newer `pymongo`.

---

## 6. `create_index(name=None)` collided across different indexes

**Phase found:** 1, immediately after fixing #5, while running the same test suite.

**Symptom:** `test_ensure_indexes_includes_declared_indexes` failed with `pymongo.errors.OperationFailure: Index with name: None already exists with different options` — even though the two indexes being created had completely different key patterns.

**Root cause:** [mongo_loader.py](../src/loader/mongo_loader.py)'s `ensure_indexes` called `collection.create_index(keys, unique=spec.unique, name=spec.name)`, where `spec.name` is `None` for any index the config doesn't explicitly name. Passing `name=None` **explicitly** is different from not passing `name` at all: `mongomock`'s implementation does `kwargs.pop('name', <auto-generated-name>)` — since the `name` key is present in `kwargs` (with value `None`), `.pop` returns that literal `None` rather than falling through to the default auto-generated name. Two differently-keyed indexes both created with the explicit `name=None` therefore both got the literal index name `None`, and the second one collided with the first.

**Fix:** Changed `ensure_indexes` to build a `kwargs` dict and only add the `name` key when `spec.name` is actually set:

```python
kwargs: dict[str, Any] = {"unique": spec.unique}
if spec.name:
    kwargs["name"] = spec.name
collection.create_index(keys, **kwargs)
```

**Verification:** `test_ensure_indexes_includes_declared_indexes` and `test_ensure_indexes_creates_unique_key_index` both pass, confirming two differently-keyed, unnamed indexes on the same collection each get their own auto-generated name rather than colliding.

---

## 7. ruff's ambient default rule set didn't match the project's needs

**Phase found:** 4 (Hardening), while wiring up the lint step of the CI workflow and verifying it locally before trusting it.

**Symptom:** `ruff check .` reported 20 errors — mostly `DTZ001`/`DTZ007`/`DTZ901` ("`datetime.datetime()` called without a `tzinfo` argument") on test fixtures that use naive datetimes on purpose (matching how this project's own domain code stores dates — see #1/#2 above, none of which use timezone-aware datetimes), plus a scattering of `UP017`/`UP037`/`C408` style-preference rules.

**Root cause:** the project's `pyproject.toml` had a `[tool.ruff]` section (line length, target Python version) but no explicit `[tool.ruff.lint] select = [...]`. Without one, `ruff` (installed at `0.16.7`) fell back to a much broader ambient default rule set than the commonly-assumed minimal default (`E4`/`E7`/`E9`/`F`) — pulling in an entire `flake8-datetimez` plugin's worth of rules that actively fight this project's intentional choice of naive datetimes throughout.

**Fix:** Added an explicit `[tool.ruff.lint] select = ["E", "F", "I"]` (pycodestyle errors, pyflakes, import sorting) to [pyproject.toml](../pyproject.toml) — a deliberate, minimal, version-independent rule selection instead of relying on whatever a given ruff version's ambient defaults happen to be.

**Fix (real issue found, not just noise):** one genuine `F401` unused-import was caught in the same pass (`ProcessedFilesManifest` imported but never used in [tests/test_validate.py](../tests/test_validate.py)) and removed.

**Verification:** `ruff check .` and `black --check .` (after running `black .` once to normalize pre-existing formatting) both report clean on the full `src/` + `tests/` tree — confirmed as part of the CI workflow's exact commands, run locally against live Docker containers (see [TEST_RESULTS.md](TEST_RESULTS.md#ci-workflow-commands-verified-locally)).

---

## 8. Operational incident: sample CSV briefly deleted during cleanup

**Phase found:** 3 (Validation & Reconciliation), during manual cleanup after a live smoke test — not a code bug, but worth recording since it happened and shaped how later cleanup was done.

**Symptom:** After a CLI run moved `sample_data/orders_sample.csv` into `sample_data/processed/` (the documented `move_processed_to` behavior), a cleanup command `rm -rf sample_data/processed` was run to tidy up test-run artifacts — which deleted the moved file along with the folder, since the file itself was inside `processed/` at that moment.

**Root cause:** blind `rm -rf` on a directory that happened to currently contain the very file being "cleaned up," rather than first checking what was inside it or moving the file back out before removing the now-empty folder.

**Fix / recovery:** the file's content was still fully known (it had been written earlier in the same session), so it was recreated from that known content rather than lost. No data was actually unrecoverable, but it was a close call for a tracked (if not-yet-committed) sample file.

**Process change:** all subsequent cleanup in this project moved the CSV back out of `processed/` explicitly (`mv sample_data/processed/orders_sample.csv sample_data/orders_sample.csv`) before removing the now-empty `processed/` directory, rather than deleting the directory and its contents in one step. This is also exactly the kind of situation the project's own git-safety habits (checking `git status`/contents before a destructive operation) are meant to catch — worth calling out precisely because it happened despite generally being careful.
