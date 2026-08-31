# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from sanka_connector import DataError, require_identity_values


def test_require_identity_values_returns_the_complete_reviewed_tuple() -> None:
    assert require_identity_values(
        {"tenant": "acme", "external_id": "42"}, ["tenant", "external_id"]
    ) == [("tenant", "acme"), ("external_id", "42")]


@pytest.mark.parametrize(
    ("properties", "fields", "message"),
    [
        ({"tenant": "acme"}, ["tenant", "external_id"], "missing"),
        ({"tenant": "acme", "external_id": None}, ["tenant", "external_id"], "NULL"),
        ({"tenant": "acme"}, ["tenant", "tenant"], "unique"),
        ({"tenant": "acme"}, ["tenant", ""], "empty"),
    ],
)
def test_require_identity_values_rejects_weakened_identities(
    properties: dict[str, object], fields: list[str], message: str
) -> None:
    with pytest.raises(DataError, match=message):
        require_identity_values(properties, fields)
