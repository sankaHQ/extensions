# SPDX-License-Identifier: Apache-2.0
"""Optional CI sharding of the Go qualification by router target."""

import os
import uuid
import zlib
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS

SHARD_VARIABLE = "SANKA_GO_TARGET_SHARD"
TESTS = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep one target's cells; spread target-independent tests evenly by node id."""
    shard = os.environ.get(SHARD_VARIABLE)
    if not shard:
        return
    if shard not in TARGETS:
        raise pytest.UsageError(f"{SHARD_VARIABLE} must be one of {', '.join(TARGETS)}")
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if not item.path.is_relative_to(TESTS):
            selected.append(item)
            continue
        callspec = getattr(item, "callspec", None)
        target = callspec.params.get("target") if callspec is not None else None
        if target not in TARGETS:
            target = TARGETS[zlib.crc32(item.nodeid.encode()) % len(TARGETS)]
        (selected if target == shard else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture
def verify_database_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of the temporary source-test databases this test's own run generates.

    Shards run several workers against one PostgreSQL server, so a test cannot compare
    whole-server database lists before and after: another worker's temporary database
    would count as a leak. Checking the names generated here is exact and race-free.
    """
    names: list[str] = []
    real = uuid.uuid4

    def recording() -> uuid.UUID:
        value = real()
        names.append("sanka_verify_" + value.hex)
        return value

    monkeypatch.setattr(uuid, "uuid4", recording)
    return names
