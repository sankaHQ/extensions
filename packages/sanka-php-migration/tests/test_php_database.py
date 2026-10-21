# SPDX-License-Identifier: Apache-2.0
"""Schema boundary: replay may only provision the declared disposable tables."""

import pytest
from sanka_php_migration.database import schema_tables


@pytest.mark.parametrize(
    "dialect,ddl",
    [
        (
            "mysql",
            "CREATE TABLE orders (id BIGINT PRIMARY KEY AUTO_INCREMENT, "
            "name VARCHAR(80) NOT NULL, amount DECIMAL(12,2) NOT NULL);",
        ),
        (
            "sqlite",
            "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, amount TEXT NOT NULL);",
        ),
    ],
)
def test_flat_schema_is_preserved(dialect: str, ddl: str) -> None:
    tables = schema_tables(ddl, dialect, ["orders"])
    assert tables[0]["primary_key"] == "id"
    assert tables[0]["columns"] == ["id", "name", "amount"]
    assert tables[0]["statement"] == ddl.removesuffix(";")


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE orders (id BIGINT PRIMARY KEY); DROP DATABASE customer;",
        "CREATE TABLE other.orders (id BIGINT PRIMARY KEY);",
        "CREATE TABLE orders (id BIGINT PRIMARY KEY); "
        "CREATE TRIGGER hook AFTER INSERT ON orders BEGIN SELECT 1; END;",
        "CREATE TABLE orders (id BIGINT PRIMARY KEY, data JSON);",
        "CREATE TABLE orders (id BIGINT PRIMARY KEY) ENGINE=MyISAM;",
        "CREATE TABLE orders (id BIGINT PRIMARY KEY, foreign_id BIGINT REFERENCES private(id));",
    ],
)
def test_unknown_schema_effects_fail_closed(ddl: str) -> None:
    with pytest.raises(ValueError):
        schema_tables(ddl, "mysql", ["orders"])
