import sqlite3

from loader.config import (
    CsvSourceConfig,
    FieldMapping,
    FieldType,
    MappingConfig,
    PipelineConfig,
    SqlSourceConfig,
    TargetConfig,
    WriteMode,
)
from loader.pipeline import validate_pipeline
from loader.state import WatermarkStore


def test_validate_csv_does_not_mark_files_processed_or_move(tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text("order_id,amount\n1,10\n2,20\n", encoding="utf-8")
    manifest_path = tmp_path / ".state/manifest.json"

    config = PipelineConfig(
        source=CsvSourceConfig(
            path=str(csv_path),
            manifest_path=str(manifest_path),
            move_processed_to="processed",
        ),
        target=TargetConfig(collection="orders", unique_key="order_id"),
        mapping=MappingConfig(
            fields=[
                FieldMapping(source="order_id", target="order_id", type=FieldType.STR),
                FieldMapping(source="amount", target="amount", type=FieldType.INT),
            ]
        ),
        write_mode=WriteMode.UPSERT,
    )

    summary = validate_pipeline(config)

    assert summary.rows_read == 2
    assert summary.rows_loaded == 2
    assert summary.rows_rejected == 0
    assert csv_path.exists(), "validate must not move the source file"
    assert not manifest_path.exists(), "validate must not write the manifest"


def test_validate_csv_reports_rejected_rows(tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text("order_id,amount\n1,10\n,20\n", encoding="utf-8")

    config = PipelineConfig(
        source=CsvSourceConfig(
            path=str(csv_path),
            manifest_path=str(tmp_path / ".state/manifest.json"),
            move_processed_to=None,
        ),
        target=TargetConfig(collection="orders", unique_key="order_id"),
        mapping=MappingConfig(
            fields=[
                FieldMapping(
                    source="order_id", target="order_id", type=FieldType.STR, required=True
                ),
                FieldMapping(source="amount", target="amount", type=FieldType.INT),
            ]
        ),
        write_mode=WriteMode.UPSERT,
    )

    summary = validate_pipeline(config)

    assert summary.rows_read == 2
    assert summary.rows_loaded == 1
    assert summary.rows_rejected == 1


def test_validate_sql_does_not_advance_watermark(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE customers (id INTEGER, name TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO customers VALUES (1, 'Alice', '2024-01-01 09:00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("TEST_SQL_CONN", f"sqlite:///{db_path}")

    state_path = tmp_path / "watermark.json"
    config = PipelineConfig(
        source=SqlSourceConfig(
            connection_env="TEST_SQL_CONN",
            table="customers",
            watermark_column="updated_at",
            chunk_size=10,
            max_retries=1,
            state_path=str(state_path),
        ),
        target=TargetConfig(collection="customers", unique_key="customer_id"),
        mapping=MappingConfig(
            fields=[FieldMapping(source="id", target="customer_id", type=FieldType.STR)]
        ),
        write_mode=WriteMode.UPSERT,
    )

    summary = validate_pipeline(config)

    assert summary.rows_read == 1
    assert summary.rows_loaded == 1
    assert WatermarkStore(state_path).get() is None, "validate must not persist the watermark"
