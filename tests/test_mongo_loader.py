import mongomock
import pytest

from loader.config import IndexSpec, TargetConfig, WriteMode
from loader.mongo_loader import ensure_indexes, get_recent_runs, load, write_run_summary


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["test_db"]


def test_upsert_idempotent(db):
    target = TargetConfig(collection="orders", unique_key="order_id")
    docs = [{"order_id": "1", "amount": 10}, {"order_id": "2", "amount": 20}]

    load(db, target, WriteMode.UPSERT, docs, batch_size=10)
    load(db, target, WriteMode.UPSERT, docs, batch_size=10)

    assert db["orders"].count_documents({}) == 2


def test_upsert_loaded_count_not_double_counted_on_mixed_batch(db):
    target = TargetConfig(collection="orders", unique_key="order_id")
    load(db, target, WriteMode.UPSERT, [{"order_id": "1", "amount": 10}], batch_size=10)

    result = load(
        db,
        target,
        WriteMode.UPSERT,
        [{"order_id": "1", "amount": 999}, {"order_id": "2", "amount": 20}],
        batch_size=10,
    )

    assert result.loaded == 2
    assert result.failed == 0


def test_upsert_updates_existing_doc(db):
    target = TargetConfig(collection="orders", unique_key="order_id")
    load(db, target, WriteMode.UPSERT, [{"order_id": "1", "amount": 10}], batch_size=10)
    load(db, target, WriteMode.UPSERT, [{"order_id": "1", "amount": 99}], batch_size=10)

    doc = db["orders"].find_one({"order_id": "1"})
    assert doc["amount"] == 99
    assert db["orders"].count_documents({}) == 1


def test_full_replace_swaps_collection(db):
    target = TargetConfig(collection="orders", unique_key="order_id")
    load(db, target, WriteMode.FULL_REPLACE, [{"order_id": "1"}], batch_size=10)
    load(db, target, WriteMode.FULL_REPLACE, [{"order_id": "2"}, {"order_id": "3"}], batch_size=10)

    docs = list(db["orders"].find({}))
    assert {d["order_id"] for d in docs} == {"2", "3"}


def test_append_mode_allows_duplicates(db):
    target = TargetConfig(collection="events")
    load(db, target, WriteMode.APPEND, [{"event": "click"}], batch_size=10)
    load(db, target, WriteMode.APPEND, [{"event": "click"}], batch_size=10)

    assert db["events"].count_documents({}) == 2


def test_ensure_indexes_creates_unique_key_index(db):
    target = TargetConfig(collection="orders", unique_key="order_id")
    ensure_indexes(db["orders"], target)

    index_info = db["orders"].index_information()
    assert any(
        info["key"] == [("order_id", 1)] and info.get("unique") for info in index_info.values()
    )


def test_run_summary_written_and_retrievable(db):
    write_run_summary(db, {"started_at": "2024-01-01T00:00:00", "rows_loaded": 5})
    write_run_summary(db, {"started_at": "2024-01-02T00:00:00", "rows_loaded": 7})

    runs = get_recent_runs(db, last=10)

    assert len(runs) == 2
    assert runs[0]["started_at"] == "2024-01-02T00:00:00"  # most recent first


def test_get_recent_runs_respects_last_limit(db):
    for i in range(5):
        write_run_summary(db, {"started_at": f"2024-01-0{i + 1}T00:00:00"})

    runs = get_recent_runs(db, last=2)

    assert len(runs) == 2


def test_ensure_indexes_includes_declared_indexes(db):
    target = TargetConfig(
        collection="orders",
        unique_key="order_id",
        indexes=[IndexSpec(fields=["order_date"], unique=False)],
    )
    ensure_indexes(db["orders"], target)

    index_info = db["orders"].index_information()
    assert any(info["key"] == [("order_date", 1)] for info in index_info.values())
