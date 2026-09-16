from datetime import datetime

import pytest
from bson.decimal128 import Decimal128

from loader.config import FieldMapping, FieldType, MappingConfig, NestedMapping
from loader.transform import RowRejected, coerce_value, transform_row


def test_mapping_applied():
    mapping = MappingConfig(
        fields=[FieldMapping(source="id", target="order_id", type=FieldType.STR)]
    )
    doc = transform_row({"id": "42"}, mapping)
    assert doc == {"order_id": "42"}


@pytest.mark.parametrize(
    "field_type,raw,expected",
    [
        (FieldType.STR, "hello", "hello"),
        (FieldType.INT, "42", 42),
        (FieldType.FLOAT, "3.14", 3.14),
        (FieldType.BOOL, "true", True),
        (FieldType.BOOL, "0", False),
        (FieldType.DATE, "2024-01-05", datetime(2024, 1, 5)),
    ],
)
def test_coerce_value_supported_types(field_type, raw, expected):
    assert coerce_value(raw, field_type, "f") == expected


def test_coerce_value_decimal_returns_decimal128():
    result = coerce_value("19.99", FieldType.DECIMAL, "amount")
    assert isinstance(result, Decimal128)
    assert str(result) == "19.99"


def test_coerce_value_invalid_raises_row_rejected():
    with pytest.raises(RowRejected):
        coerce_value("not-a-number", FieldType.INT, "qty")


def test_default_applied_on_null():
    mapping = MappingConfig(
        fields=[FieldMapping(source="amount", target="amount", type=FieldType.DECIMAL, default="0")]
    )
    doc = transform_row({"amount": ""}, mapping)
    assert doc["amount"] == Decimal128("0")


def test_required_field_missing_raises_row_rejected():
    mapping = MappingConfig(
        fields=[FieldMapping(source="order_id", target="order_id", required=True)]
    )
    with pytest.raises(RowRejected):
        transform_row({"order_id": ""}, mapping)


def test_nested_document_built_correctly():
    mapping = MappingConfig(
        nested=[
            NestedMapping(
                target="address",
                fields={"address_line1": "line1", "address_city": "city"},
            )
        ]
    )
    doc = transform_row({"address_line1": "123 Main St", "address_city": "Springfield"}, mapping)
    assert doc == {"address": {"line1": "123 Main St", "city": "Springfield"}}
