# SPDX-License-Identifier: Apache-2.0
"""Known external effects block native generation without executing source effects."""

import json

import pytest
from test_lifecycle import call
from test_native_plan import native_project


@pytest.mark.parametrize(
    ("feature", "source"),
    [
        (
            "raw-sql",
            "def report():\n    return Gadget.objects.raw('SELECT * FROM inventory_gadget')\n",
        ),
        (
            "external-network-call",
            "def deliver():\n"
            "    from urllib.request import urlopen as fetch\n"
            "    return fetch('https://example.invalid')\n"
            "",
        ),
        (
            "email-operation",
            "def deliver():\n"
            "    from django.core.mail import send_mail as mail\n"
            "    return mail('subject', 'body', 'a@example.invalid', ['b@example.invalid'])\n"
            "",
        ),
        (
            "storage-operation",
            "def save():\n"
            "    from django.core.files.storage import default_storage as store\n"
            "    return store.save('remote.txt', content)\n"
            "",
        ),
        ("storage-configuration", "STORAGES = {'default': {'BACKEND': 'custom.RemoteStorage'}}\n"),
    ],
)
def test_native_plan_refuses_known_external_behavior_without_executing_it(
    tmp_path, feature, source
):
    native_project(tmp_path)
    (tmp_path / "external_effects.py").write_text(
        source + "\nraise RuntimeError('the inventory must not import this module')\n"
    )
    config = {"settings_module": "crud_config.settings", "orm": "sqlalchemy"}
    scan = call(tmp_path, "scan", config)
    assert scan["outcome"] == "success", scan
    inventory = json.loads((tmp_path / ".sanka/scan.json").read_text())["behavior_inventory"]
    assert len(inventory) == 1
    assert inventory[0]["feature"] == feature
    assert inventory[0]["source"] == "external_effects.py"
    rejected = call(tmp_path, "plan", config)
    assert rejected["outcome"] == "error", rejected
    assert "backend behavior requires explicit migration" in rejected["error"]["message"]
    assert feature in rejected["error"]["message"]
    assert not (tmp_path / ".sanka/output/flask").exists()
