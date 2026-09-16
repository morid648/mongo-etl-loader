import mongomock
import pytest

import loader.pipeline as pipeline_module
from loader.config import (
    CsvSourceConfig,
    FieldMapping,
    FieldType,
    MappingConfig,
    PipelineConfig,
    TargetConfig,
    WriteMode,
)


@pytest.fixture
def mongo_patch(monkeypatch):
    client = mongomock.MongoClient()
    monkeypatch.setattr(pipeline_module, "get_client", lambda: client)
    monkeypatch.setattr(pipeline_module, "get_database", lambda c: c["test_db"])
    return client


def _write_csv(path, content):
    path.write_text(content, encoding="utf-8")
    return path


def test_reconciliation_counts_match_on_clean_run(tmp_path, mongo_patch):
    csv_path = _write_csv(
        tmp_path / "orders.csv",
        "order_id,amount\n1,10\n2,20\n",
    )
    config = PipelineConfig(
        source=CsvSourceConfig(
            path=str(csv_path),
            manifest_path=str(tmp_path / ".state/manifest.json"),
            move_processed_to=None,
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

    summary = pipeline_module.run_csv_pipeline(config)

    assert summary.rows_read == 2
    assert summary.rows_loaded == 2
    assert summary.rows_rejected == 0
    assert summary.reconciled is True


def test_rejected_row_includes_row_number_and_reason(tmp_path, mongo_patch):
    csv_path = _write_csv(
        tmp_path / "orders.csv",
        "order_id,amount\n1,10\n,20\n3,30\n",
    )
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

    summary = pipeline_module.run_csv_pipeline(config)

    assert summary.rows_read == 3
    assert summary.rows_loaded == 2
    assert summary.rows_rejected == 1
    assert summary.reconciled is True
    assert len(summary.errors) == 1
    assert "orders.csv row 3" in summary.errors[0]
    assert "order_id" in summary.errors[0]


def test_malformed_row_rejected_without_failing_run(tmp_path, mongo_patch):
    csv_path = _write_csv(
        tmp_path / "orders.csv",
        "order_id,amount,city\n1,10,Springfield\n2,20\n3,30,Ogdenville\n",
    )
    config = PipelineConfig(
        source=CsvSourceConfig(
            path=str(csv_path),
            manifest_path=str(tmp_path / ".state/manifest.json"),
            move_processed_to=None,
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

    summary = pipeline_module.run_csv_pipeline(config)

    assert summary.rows_read == 3
    assert summary.rows_loaded == 2
    assert summary.rows_rejected == 1
    assert summary.reconciled is True
    assert any("row 3" in e and "expected 3 columns" in e for e in summary.errors)
