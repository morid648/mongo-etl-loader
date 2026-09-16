"""Pipeline configuration schema and loader.

Config files are YAML, validated through pydantic before a run starts so
malformed config fails loudly and early rather than mid-run.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class WriteMode(str, Enum):
    FULL_REPLACE = "full_replace"
    UPSERT = "upsert"
    APPEND = "append"


class OnMalformedRow(str, Enum):
    SKIP = "skip"
    FAIL = "fail"


class FieldType(str, Enum):
    STR = "str"
    INT = "int"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOL = "bool"
    DATE = "date"
    DATETIME = "datetime"


class FieldMapping(BaseModel):
    source: str
    target: str
    type: FieldType = FieldType.STR
    required: bool = False
    default: Any = None


class NestedMapping(BaseModel):
    """Combine several flat source columns into one nested sub-document.

    `fields` maps source column name -> key name inside the nested document.
    """

    target: str
    fields: dict[str, str]


class MappingConfig(BaseModel):
    fields: list[FieldMapping] = Field(default_factory=list)
    nested: list[NestedMapping] = Field(default_factory=list)


class IndexSpec(BaseModel):
    fields: list[str]
    unique: bool = False
    name: str | None = None


class TargetConfig(BaseModel):
    collection: str
    unique_key: str | None = None
    indexes: list[IndexSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _upsert_needs_unique_key(self) -> "TargetConfig":
        return self


class CsvSourceConfig(BaseModel):
    type: Literal["csv"] = "csv"
    path: str
    glob: str | None = None
    delimiter: str = ","
    encoding: str = "utf-8"
    has_header: bool = True
    on_malformed_row: OnMalformedRow = OnMalformedRow.SKIP
    move_processed_to: str | None = "processed"
    manifest_path: str = ".state/processed_files.json"


class SqlSourceConfig(BaseModel):
    type: Literal["sql"] = "sql"
    connection_env: str
    table: str | None = None
    query: str | None = None
    watermark_column: str | None = None
    chunk_size: int = 5000
    max_retries: int = 3
    state_path: str = ".state/watermark.json"

    @model_validator(mode="after")
    def _table_or_query(self) -> "SqlSourceConfig":
        if not self.table and not self.query:
            raise ValueError("sql source requires either 'table' or 'query'")
        return self


class PipelineConfig(BaseModel):
    source: CsvSourceConfig | SqlSourceConfig = Field(discriminator="type")
    target: TargetConfig
    mapping: MappingConfig = Field(default_factory=MappingConfig)
    write_mode: WriteMode = WriteMode.UPSERT
    batch_size: int = 1000

    @field_validator("batch_size")
    @classmethod
    def _batch_size_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("batch_size must be a positive integer")
        return v

    @model_validator(mode="after")
    def _upsert_requires_unique_key(self) -> "PipelineConfig":
        if self.write_mode == WriteMode.UPSERT and not self.target.unique_key:
            raise ValueError("write_mode 'upsert' requires target.unique_key to be set")
        return self


class ConfigError(ValueError):
    """Raised when a config file fails to parse or validate."""


def load_config(path: str | Path) -> PipelineConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    try:
        return PipelineConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError, etc.
        raise ConfigError(f"invalid config in {path}: {exc}") from exc


def resolve_env(var_name: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise ConfigError(f"environment variable '{var_name}' is not set (check your .env file)")
    return value
