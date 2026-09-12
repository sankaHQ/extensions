# SPDX-License-Identifier: Apache-2.0
"""The unified SDK preserves published class identities and contract behavior."""

import importlib
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("legacy_first", [True, False])
def test_import_orders_share_system_and_code_types(legacy_first: bool) -> None:
    legacy = "import sanka_connector as old_systems; import sanka_extension_sdk as old_code"
    canonical = "from sanka_extensions import data, systems, code, flow"
    imports = [legacy, canonical] if legacy_first else [canonical, legacy]
    program = (
        "\n".join(imports)
        + """
assert systems.SystemReader is old_systems.SourceConnector
assert systems.SystemWriter is old_systems.DestinationConnector
assert data.DataReader is systems.SystemReader is old_systems.SourceConnector
assert data.DataWriter is systems.SystemWriter is old_systems.DestinationConnector
assert data.DataIdentity is systems.SystemIdentity is old_systems.ProviderIdentity
assert data.DataAccessError is systems.SystemAccessError is old_systems.ConnectorError
assert data.DataTimeoutError is systems.SystemTimeoutError is old_systems.ProviderTimeoutError
assert data.TransientDataError is systems.TransientSystemError is old_systems.TransientProviderError
assert data.require_identity_values is systems.require_identity_values
assert systems.ExtensionRegistration is old_systems.ConnectorRegistration
assert code.ExtensionRequest is old_code.ExtensionRequest
assert code.ExtensionResponse is old_code.ExtensionResponse
assert code.decode_request is old_code.decode_request
assert flow.create(type="crm").type == "crm"
assert flow.decode_definition(flow.encode_definition(flow.create(type="billing"))).type == "billing"
try:
    systems.require_identity_values({"id": None}, ["id"])
except old_systems.DataError:
    pass
else:
    raise AssertionError("incomplete identity accepted")
assert "sanka.runtime" not in __import__("sys").modules
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, env=os.environ.copy()
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "",
        ".protocols",
        ".schema",
        ".errors",
        ".records",
        ".credentials",
        ".provisioning",
        ".registration",
        ".__about__",
    ],
)
def test_systems_submodules_are_aliases_of_data(module: str) -> None:
    old = importlib.import_module("sanka_extensions.systems" + module)
    new = importlib.import_module("sanka_extensions.data" + module)
    names = {
        "SystemReader": "DataReader",
        "SystemWriter": "DataWriter",
        "SystemIdentity": "DataIdentity",
        "SystemAccessError": "DataAccessError",
        "SystemTimeoutError": "DataTimeoutError",
        "TransientSystemError": "TransientDataError",
    }
    for name in old.__all__:
        assert getattr(old, name) is getattr(new, names.get(name, name)), name
