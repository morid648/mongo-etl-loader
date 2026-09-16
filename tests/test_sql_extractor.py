import sqlite3

import pytest

from loader.config import SqlSourceConfig
from loader.extractors import sql_extractor


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, updated_at TEXT)")
    rows = [
        (1, "Alice", "2024-01-01 09:00:00"),
        (2, "Bob", "2024-01-02 09:00:00"),
        (3, "Carol", "2024-01-03 09:00:00"),
        (4, "Dave", "2024-01-04 09:00:00"),
        (5, "Eve", "2024-01-05 09:00:00"),
    ]
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setenv("TEST_SQL_CONN", f"sqlite:///{db_path}")
    return db_path


def _config(tmp_path, **overrides) -> SqlSourceConfig:
    defaults = dict(
        connection_env="TEST_SQL_CONN",
        table="customers",
        watermark_column="updated_at",
        chunk_size=2,
        max_retries=1,
        state_path=str(tmp_path / "watermark.json"),
    )
    defaults.update(overrides)
    return SqlSourceConfig(**defaults)


def test_full_extraction_returns_all_rows(sqlite_db, tmp_path):
    config = _config(tmp_path)
    chunks = list(sql_extractor.extract(config, incremental=False))

    total_rows = sum(len(c) for c in chunks)
    assert total_rows == 5


def test_chunked_pagination_correctness(sqlite_db, tmp_path):
    config = _config(tmp_path, chunk_size=2)
    chunks = list(sql_extractor.extract(config, incremental=False))

    assert [len(c) for c in chunks] == [2, 2, 1]
    all_ids = [row["id"] for chunk in chunks for row in chunk]
    assert all_ids == [1, 2, 3, 4, 5]


def test_incremental_extraction_returns_only_new_rows(sqlite_db, tmp_path):
    config = _config(tmp_path)

    first_run = [row for chunk in sql_extractor.extract(config, incremental=True) for row in chunk]
    assert len(first_run) == 5

    conn = sqlite3.connect(str(sqlite_db))
    conn.execute("INSERT INTO customers VALUES (6, 'Frank', '2024-01-06 09:00:00')")
    conn.commit()
    conn.close()

    second_run = [row for chunk in sql_extractor.extract(config, incremental=True) for row in chunk]
    assert len(second_run) == 1
    assert second_run[0]["name"] == "Frank"


def test_mid_run_crash_does_not_advance_watermark_and_rerun_recovers(sqlite_db, tmp_path):
    """PRD acceptance criterion: killing the SQL source mid-run and re-running must
    complete successfully. Since the watermark is only persisted after the generator
    is fully drained, an aborted run leaves it untouched — a fresh process re-runs
    from the same safe point and (with upsert) reprocesses idempotently."""
    config = _config(tmp_path, chunk_size=2)

    class SimulatedCrash(Exception):
        pass

    consumed = []
    with pytest.raises(SimulatedCrash):
        for i, chunk in enumerate(sql_extractor.extract(config, incremental=True)):
            consumed.extend(chunk)
            if i == 0:
                raise SimulatedCrash("source killed mid-run")

    assert len(consumed) == 2
    from loader.state import WatermarkStore

    assert WatermarkStore(config.state_path).get() is None

    rerun = [row for chunk in sql_extractor.extract(config, incremental=True) for row in chunk]
    assert len(rerun) == 5, "re-run after a mid-run crash must see all rows again, not lose any"


def test_retry_triggers_on_simulated_connection_failure(monkeypatch, tmp_path):
    config = _config(tmp_path, connection_env="TEST_SQL_CONN_BAD", max_retries=3)
    monkeypatch.setenv("TEST_SQL_CONN_BAD", "sqlite:////nonexistent_dir_xyz/bad.db")

    call_count = {"n": 0}
    real_create_engine = sql_extractor.create_engine

    def flaky_create_engine(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ConnectionError("simulated connection failure")
        return real_create_engine("sqlite:///:memory:")

    monkeypatch.setattr(sql_extractor, "create_engine", flaky_create_engine)

    engine = sql_extractor.build_engine(config, min_wait=0.01, max_wait=0.02)
    assert call_count["n"] == 3
    engine.dispose()


def test_retry_exhausts_and_raises(monkeypatch, tmp_path):
    config = _config(tmp_path, connection_env="TEST_SQL_CONN_BAD", max_retries=2)
    monkeypatch.setenv("TEST_SQL_CONN_BAD", "sqlite:////nonexistent_dir_xyz/bad.db")

    def always_fails(*args, **kwargs):
        raise ConnectionError("simulated permanent failure")

    monkeypatch.setattr(sql_extractor, "create_engine", always_fails)

    with pytest.raises(ConnectionError):
        sql_extractor.build_engine(config, min_wait=0.01, max_wait=0.02)
