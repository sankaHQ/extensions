# SPDX-License-Identifier: Apache-2.0
"""Published import paths keep the same registration objects after the rename."""

from importlib import import_module


def test_data_extension_modules_keep_old_import_identity() -> None:
    for name in ("clickhouse", "csv", "markdown", "postgres", "sqlite"):
        assert import_module(f"sanka_connector_{name}") is import_module(f"sanka_extension_{name}")
    for part in ("_base", "_source", "_destination"):
        assert import_module(f"sanka_connector_postgres.{part}") is import_module(
            f"sanka_extension_postgres.{part}"
        )
