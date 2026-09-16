from pathlib import Path

import pytest

from loader.config import ConfigError, WriteMode, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_config():
    config = load_config(FIXTURES / "valid_config.yaml")

    assert config.source.type == "csv"
    assert config.target.collection == "orders_test"
    assert config.target.unique_key == "order_id"
    assert config.write_mode == WriteMode.UPSERT
    assert len(config.mapping.fields) == 3
    assert config.mapping.nested[0].target == "address"


def test_load_invalid_config_raises_config_error():
    with pytest.raises(ConfigError):
        load_config(FIXTURES / "invalid_config.yaml")


def test_missing_config_file_raises_config_error():
    with pytest.raises(ConfigError):
        load_config(FIXTURES / "does_not_exist.yaml")


def test_upsert_without_unique_key_rejected(tmp_path):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text(
        """
source:
  type: csv
  path: tests/fixtures/valid_orders.csv
target:
  collection: orders_test
mapping:
  fields:
    - source: order_id
      target: order_id
      type: str
write_mode: upsert
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(bad_config)
