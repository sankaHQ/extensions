# SPDX-License-Identifier: Apache-2.0
"""The published and canonical interfaces must interoperate in one interpreter."""

import pytest

import sanka_connector as legacy
import sanka_extensions.systems as canonical


def test_old_registration_and_errors_work_with_canonical_interfaces() -> None:
    class Reader:
        provider = "example"
        binding_kind = "fixture"

        async def discover_objects(self, credentials):
            return []

        async def inventory(self, credentials, *, object_types=None):
            return canonical.Inventory(provider="example")

        async def read_records(self, credentials, **kwargs):
            return canonical.RecordPage(object_key="items")

    reader = Reader()
    registration = legacy.ConnectorRegistration(name="example", source=reader)
    assert isinstance(registration, canonical.ExtensionRegistration)
    assert isinstance(reader, canonical.SystemReader)
    assert legacy.SourceConnector is canonical.SystemReader
    assert legacy.DestinationConnector is canonical.SystemWriter
    with pytest.raises(legacy.ConnectorError):
        raise canonical.DataError("missing identity")
    with pytest.raises(canonical.SystemAccessError):
        legacy.require_identity_values({"id": None}, ["id"])
    with pytest.raises(legacy.DataError):
        canonical.require_identity_values({"id": 1}, ["id", "scope"])
