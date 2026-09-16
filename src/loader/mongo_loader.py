"""MongoDB loading: batched writes for full_replace / upsert / append modes."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import BulkWriteError

from loader.config import IndexSpec, TargetConfig, WriteMode, resolve_env

logger = logging.getLogger("loader.mongo_loader")


@dataclass
class LoadResult:
    loaded: int = 0
    failed: int = 0
    failed_reasons: list[str] = field(default_factory=list)


def get_client(uri_env: str = "MONGO_URI") -> MongoClient:
    return MongoClient(resolve_env(uri_env))


def get_database(client: MongoClient, db_name_env: str = "MONGO_DB") -> Database:
    return client[resolve_env(db_name_env)]


RUN_HISTORY_COLLECTION = "_pipeline_runs"


def write_run_summary(db: Database, summary_dict: dict) -> None:
    """Persist a run summary into `_pipeline_runs` (PRD §7.6) — best-effort, non-fatal."""
    try:
        db[RUN_HISTORY_COLLECTION].insert_one(dict(summary_dict))
    except Exception:
        logger.exception("failed to write run summary to %s", RUN_HISTORY_COLLECTION)


def get_recent_runs(db: Database, last: int = 10) -> list[dict]:
    cursor = db[RUN_HISTORY_COLLECTION].find({}).sort("started_at", -1).limit(last)
    return list(cursor)


def ensure_indexes(collection: Collection, target: TargetConfig) -> None:
    specs: list[IndexSpec] = list(target.indexes)

    if target.unique_key and not any(spec.fields == [target.unique_key] for spec in specs):
        specs.append(IndexSpec(fields=[target.unique_key], unique=True))

    for spec in specs:
        keys = [(f, 1) for f in spec.fields]
        kwargs: dict[str, Any] = {"unique": spec.unique}
        if spec.name:
            kwargs["name"] = spec.name
        collection.create_index(keys, **kwargs)


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _load_upsert(
    collection: Collection, docs: list[dict], unique_key: str, batch_size: int
) -> LoadResult:
    result = LoadResult()

    for batch in _chunks(docs, batch_size):
        operations = [
            UpdateOne({unique_key: doc[unique_key]}, {"$set": doc}, upsert=True)
            for doc in batch
            if unique_key in doc
        ]
        skipped = len(batch) - len(operations)
        if skipped:
            result.failed += skipped
            result.failed_reasons.append(
                f"{skipped} document(s) skipped: missing unique key '{unique_key}'"
            )

        if not operations:
            continue

        try:
            res = collection.bulk_write(operations, ordered=False)
            # matched_count already includes modified docs; summing both would double-count.
            result.loaded += res.upserted_count + res.matched_count
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", [])
            result.failed += len(write_errors)
            for err in write_errors:
                result.failed_reasons.append(f"index {err.get('index')}: {err.get('errmsg')}")
            succeeded = len(operations) - len(write_errors)
            result.loaded += max(succeeded, 0)

    return result


def _load_append(collection: Collection, docs: list[dict], batch_size: int) -> LoadResult:
    result = LoadResult()

    for batch in _chunks(docs, batch_size):
        try:
            res = collection.insert_many(batch, ordered=False)
            result.loaded += len(res.inserted_ids)
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", [])
            result.failed += len(write_errors)
            for err in write_errors:
                result.failed_reasons.append(f"index {err.get('index')}: {err.get('errmsg')}")
            result.loaded += len(batch) - len(write_errors)

    return result


def _load_full_replace(
    db: Database, target: TargetConfig, docs: list[dict], batch_size: int
) -> LoadResult:
    tmp_name = f"{target.collection}__load_tmp_{int(time.time())}"
    tmp_collection = db[tmp_name]
    result = LoadResult()

    try:
        for batch in _chunks(docs, batch_size):
            if not batch:
                continue
            try:
                res = tmp_collection.insert_many(batch, ordered=False)
                result.loaded += len(res.inserted_ids)
            except BulkWriteError as exc:
                write_errors = exc.details.get("writeErrors", [])
                result.failed += len(write_errors)
                for err in write_errors:
                    result.failed_reasons.append(f"index {err.get('index')}: {err.get('errmsg')}")
                result.loaded += len(batch) - len(write_errors)

        ensure_indexes(tmp_collection, target)
        db[target.collection].drop()
        db[tmp_name].rename(target.collection)
    except Exception:
        db.drop_collection(tmp_name)
        raise

    return result


def load(
    db: Database,
    target: TargetConfig,
    write_mode: WriteMode,
    docs: list[dict],
    batch_size: int = 1000,
) -> LoadResult:
    if write_mode == WriteMode.FULL_REPLACE:
        return _load_full_replace(db, target, docs, batch_size)

    collection = db[target.collection]
    ensure_indexes(collection, target)

    if write_mode == WriteMode.UPSERT:
        assert target.unique_key, "upsert mode requires target.unique_key"
        return _load_upsert(collection, docs, target.unique_key, batch_size)

    if write_mode == WriteMode.APPEND:
        return _load_append(collection, docs, batch_size)

    raise ValueError(f"unsupported write_mode: {write_mode}")
