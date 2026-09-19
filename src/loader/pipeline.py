"""Core pipeline logic: extract -> transform -> load -> summarize.

Scheduler-agnostic on purpose (PRD §6) — the CLI is a thin wrapper around
these functions so this same code can later be called from cron, Task
Scheduler, or an Airflow operator without a rewrite.
"""

from __future__ import annotations

import logging

from loader.config import CsvSourceConfig, PipelineConfig, SqlSourceConfig
from loader.extractors import csv_extractor, sql_extractor
from loader.logging_utils import RunSummary
from loader.mongo_loader import get_client, get_database, load, write_run_summary
from loader.transform import RowRejected, transform_row

logger = logging.getLogger("loader.pipeline")


def run_csv_pipeline(config: PipelineConfig) -> RunSummary:
    assert isinstance(config.source, CsvSourceConfig)
    summary = RunSummary.start(
        source=f"csv:{config.source.path}",
        target=f"mongo:{config.target.collection}",
        mode=config.write_mode.value,
    )

    client = get_client()
    try:
        db = get_database(client)

        for file_path, extract_result in csv_extractor.extract(config.source):
            summary.rows_read += len(extract_result.rows) + extract_result.malformed_count
            summary.rows_rejected += extract_result.malformed_count
            summary.errors.extend(extract_result.malformed_reasons)

            docs: list[dict] = []
            for row_number, raw_row in zip(extract_result.row_numbers, extract_result.rows):
                try:
                    docs.append(transform_row(raw_row, config.mapping))
                except RowRejected as exc:
                    summary.rows_rejected += 1
                    summary.errors.append(f"{file_path.name} row {row_number}: {exc}")

            if docs:
                load_result = load(db, config.target, config.write_mode, docs, config.batch_size)
                summary.rows_loaded += load_result.loaded
                summary.rows_rejected += load_result.failed
                summary.errors.extend(load_result.failed_reasons)

            logger.info("processed file %s: %d rows", file_path, len(extract_result.rows))

        summary.finish()
        if not summary.reconciled:
            logger.warning(
                "reconciliation mismatch: rows_read=%d rows_loaded=%d rows_rejected=%d",
                summary.rows_read,
                summary.rows_loaded,
                summary.rows_rejected,
            )
        write_run_summary(db, summary.as_dict())
    finally:
        client.close()

    return summary


def run_sql_pipeline(config: PipelineConfig) -> RunSummary:
    assert isinstance(config.source, SqlSourceConfig)
    summary = RunSummary.start(
        source=f"sql:{config.source.table or config.source.query}",
        target=f"mongo:{config.target.collection}",
        mode=config.write_mode.value,
    )

    client = get_client()
    try:
        db = get_database(client)

        for chunk in sql_extractor.extract(config.source, incremental=True):
            summary.rows_read += len(chunk)

            docs: list[dict] = []
            for raw_row in chunk:
                try:
                    docs.append(transform_row(raw_row, config.mapping))
                except RowRejected as exc:
                    summary.rows_rejected += 1
                    identifier = raw_row.get(config.target.unique_key, raw_row)
                    summary.errors.append(f"row {identifier!r}: {exc}")

            if docs:
                load_result = load(db, config.target, config.write_mode, docs, config.batch_size)
                summary.rows_loaded += load_result.loaded
                summary.rows_rejected += load_result.failed
                summary.errors.extend(load_result.failed_reasons)

            logger.info("processed chunk of %d rows", len(chunk))

        summary.finish()
        if not summary.reconciled:
            logger.warning(
                "reconciliation mismatch: rows_read=%d rows_loaded=%d rows_rejected=%d",
                summary.rows_read,
                summary.rows_loaded,
                summary.rows_rejected,
            )
        write_run_summary(db, summary.as_dict())
    finally:
        client.close()

    return summary


def validate_pipeline(config: PipelineConfig) -> RunSummary:
    """Dry run (`loader validate`): extract + transform, report what would load.

    Never connects to MongoDB and never mutates re-run state (manifest, watermark).
    """
    if isinstance(config.source, CsvSourceConfig):
        return _validate_csv(config)
    return _validate_sql(config)


def _validate_csv(config: PipelineConfig) -> RunSummary:
    assert isinstance(config.source, CsvSourceConfig)
    summary = RunSummary.start(
        source=f"csv:{config.source.path}",
        target=f"mongo:{config.target.collection} (dry run)",
        mode=config.write_mode.value,
    )

    for file_path, extract_result in csv_extractor.extract(config.source, dry_run=True):
        summary.rows_read += len(extract_result.rows) + extract_result.malformed_count
        summary.rows_rejected += extract_result.malformed_count
        summary.errors.extend(extract_result.malformed_reasons)

        for row_number, raw_row in zip(extract_result.row_numbers, extract_result.rows):
            try:
                transform_row(raw_row, config.mapping)
                summary.rows_loaded += 1
            except RowRejected as exc:
                summary.rows_rejected += 1
                summary.errors.append(f"{file_path.name} row {row_number}: {exc}")

    summary.finish()
    return summary


def _validate_sql(config: PipelineConfig) -> RunSummary:
    assert isinstance(config.source, SqlSourceConfig)
    summary = RunSummary.start(
        source=f"sql:{config.source.table or config.source.query}",
        target=f"mongo:{config.target.collection} (dry run)",
        mode=config.write_mode.value,
    )

    for chunk in sql_extractor.extract(config.source, incremental=True, dry_run=True):
        summary.rows_read += len(chunk)
        for raw_row in chunk:
            try:
                transform_row(raw_row, config.mapping)
                summary.rows_loaded += 1
            except RowRejected as exc:
                summary.rows_rejected += 1
                identifier = raw_row.get(config.target.unique_key, raw_row)
                summary.errors.append(f"row {identifier!r}: {exc}")

    summary.finish()
    return summary
