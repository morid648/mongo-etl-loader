"""Row transformation: field mapping, type coercion, defaults, nesting."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from bson.decimal128 import Decimal128

from loader.config import FieldMapping, FieldType, MappingConfig

_TRUE_STRINGS = {"true", "1", "yes", "y", "t"}
_FALSE_STRINGS = {"false", "0", "no", "n", "f"}

_DATE_FORMATS = ("%Y-%m-%d",)
_DATETIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")


class RowRejected(Exception):
    """Raised when a row fails validation/coercion and should be rejected, not loaded."""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def coerce_value(value: Any, field_type: FieldType, field_name: str) -> Any:
    if field_type == FieldType.STR:
        return str(value)
    if field_type == FieldType.INT:
        try:
            return int(str(value).strip())
        except (ValueError, TypeError) as exc:
            raise RowRejected(f"field '{field_name}': cannot coerce {value!r} to int") from exc
    if field_type == FieldType.FLOAT:
        try:
            return float(str(value).strip())
        except (ValueError, TypeError) as exc:
            raise RowRejected(f"field '{field_name}': cannot coerce {value!r} to float") from exc
    if field_type == FieldType.DECIMAL:
        try:
            return Decimal128(Decimal(str(value).strip()))
        except (InvalidOperation, TypeError) as exc:
            raise RowRejected(f"field '{field_name}': cannot coerce {value!r} to decimal") from exc
    if field_type == FieldType.BOOL:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
        raise RowRejected(f"field '{field_name}': cannot coerce {value!r} to bool")
    if field_type == FieldType.DATE:
        # BSON has no bare `date` type, only `datetime` — store as midnight UTC.
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(str(value).strip(), fmt)
            except ValueError:
                continue
        raise RowRejected(f"field '{field_name}': cannot coerce {value!r} to date")
    if field_type == FieldType.DATETIME:
        if isinstance(value, datetime):
            return value
        for fmt in _DATETIME_FORMATS:
            try:
                return datetime.strptime(str(value).strip(), fmt)
            except ValueError:
                continue
        raise RowRejected(f"field '{field_name}': cannot coerce {value!r} to datetime")
    raise RowRejected(f"field '{field_name}': unsupported type {field_type!r}")


def _apply_field(raw_row: dict[str, Any], field: FieldMapping) -> tuple[str, Any] | None:
    value = raw_row.get(field.source)

    if _is_blank(value):
        if field.required and field.default is None:
            raise RowRejected(f"missing required field '{field.source}'")
        if field.default is not None:
            return field.target, coerce_value(field.default, field.type, field.source)
        return field.target, None

    return field.target, coerce_value(value, field.type, field.source)


def transform_row(raw_row: dict[str, Any], mapping: MappingConfig) -> dict[str, Any]:
    """Map, coerce, and nest one raw row. Raises RowRejected on validation failure."""
    doc: dict[str, Any] = {}

    for field in mapping.fields:
        target, value = _apply_field(raw_row, field)
        doc[target] = value

    for nested in mapping.nested:
        sub_doc = {}
        for source_col, target_key in nested.fields.items():
            sub_doc[target_key] = raw_row.get(source_col)
        doc[nested.target] = sub_doc

    return doc
