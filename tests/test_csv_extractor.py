import shutil
from pathlib import Path

import pytest

from loader.config import CsvSourceConfig, OnMalformedRow
from loader.extractors import csv_extractor

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workdir(tmp_path):
    for name in ("valid_orders.csv", "malformed_orders.csv"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path


def test_extract_happy_path(workdir):
    config = CsvSourceConfig(
        path=str(workdir / "valid_orders.csv"),
        manifest_path=str(workdir / ".state/manifest.json"),
        move_processed_to=None,
    )
    results = list(csv_extractor.extract(config))

    assert len(results) == 1
    _, result = results[0]
    assert len(result.rows) == 2
    assert result.rows[0]["order_id"] == "2001"
    assert result.malformed_count == 0


def test_extract_skips_malformed_row_by_default(workdir):
    config = CsvSourceConfig(
        path=str(workdir / "malformed_orders.csv"),
        manifest_path=str(workdir / ".state/manifest.json"),
        move_processed_to=None,
    )
    _, result = next(csv_extractor.extract(config))

    assert len(result.rows) == 2
    assert result.malformed_count == 1
    assert "row 3" in result.malformed_reasons[0]


def test_extract_fail_fast_raises_on_malformed_row(workdir):
    config = CsvSourceConfig(
        path=str(workdir / "malformed_orders.csv"),
        manifest_path=str(workdir / ".state/manifest.json"),
        on_malformed_row=OnMalformedRow.FAIL,
        move_processed_to=None,
    )
    with pytest.raises(ValueError):
        list(csv_extractor.extract(config))


def test_rerun_skips_already_processed_file(workdir):
    config = CsvSourceConfig(
        path=str(workdir / "valid_orders.csv"),
        manifest_path=str(workdir / ".state/manifest.json"),
        move_processed_to=None,
    )
    first_run = list(csv_extractor.extract(config))
    second_run = list(csv_extractor.extract(config))

    assert len(first_run) == 1
    assert len(second_run) == 0


def test_directory_glob_matches_multiple_files(workdir):
    config = CsvSourceConfig(
        path=str(workdir),
        glob="*.csv",
        manifest_path=str(workdir / ".state/manifest.json"),
        move_processed_to=None,
    )
    results = list(csv_extractor.extract(config))

    matched_files = {p.name for p, _ in results}
    assert matched_files == {"valid_orders.csv", "malformed_orders.csv"}


def test_move_processed_to_relocates_file(workdir):
    config = CsvSourceConfig(
        path=str(workdir / "valid_orders.csv"),
        manifest_path=str(workdir / ".state/manifest.json"),
        move_processed_to="processed",
    )
    list(csv_extractor.extract(config))

    assert not (workdir / "valid_orders.csv").exists()
    assert (workdir / "processed" / "valid_orders.csv").exists()
