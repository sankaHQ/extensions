# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL extension: source + destination roles for Sanka migrations.

The DSN arrives in ``settings["connection"]`` (``postgres://``,
``postgresql://``, or a libpq keyword string); ``settings["schema"]`` picks
the schema (default ``public``). The source keyset-paginates on single-column
primary keys and returns JSON-safe values; the destination creates and widens
tables from the records written to them. See the package README for the
detailed typing, pagination, and identity semantics.
"""

from __future__ import annotations

from sanka_connector_postgres._destination import PostgresDestination
from sanka_connector_postgres._source import PostgresSource
from sanka_extensions.systems import ExtensionRegistration

__all__ = ["CONNECTOR", "EXTENSION", "PostgresDestination", "PostgresSource"]

EXTENSION = ExtensionRegistration(
    name="postgres",
    source=PostgresSource(),
    destination=PostgresDestination(),
)

# Compatibility target for existing sanka.connectors entry points.
CONNECTOR = EXTENSION
