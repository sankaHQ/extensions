# SPDX-License-Identifier: Apache-2.0
"""Session qualification must pass the public reviewed migration lifecycle."""

import json
from pathlib import Path

import pytest
from test_lifecycle import call
from test_sqlalchemy_sessions import _session_project


@pytest.mark.parametrize("default_user", [False, True])
def test_session_plan_preserves_required_runtime_configuration(tmp_path, monkeypatch, default_user):
    _session_project(tmp_path)
    if default_user:
        settings = tmp_path / "settings.py"
        settings.write_text(settings.read_text().replace("AUTH_USER_MODEL = 'catalog.Account'", ""))
    monkeypatch.setenv(
        "SANKA_SOURCE_TEST_DATABASE",
        json.dumps({"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp_path / "source.db")}),
    )
    config = {"settings_module": "settings", "orm": "sqlalchemy"}
    scanned = call(tmp_path, "scan", config)
    assert scanned["outcome"] == "success", scanned
    planned = call(tmp_path, "plan", config)
    assert planned["outcome"] == "success", planned
    files = planned["data"]["files"]
    architecture = json.loads(files["architecture.json"])
    assert not architecture["services"]
    assert not architecture["repositories"]
    assert "fixture-current-secret" not in json.dumps(files)
    assert "fixture-old-secret" not in json.dumps(files)
    assert "SANKA_DJANGO_SECRET_KEY" in files[".env.example"]
    assert "SANKA_DJANGO_SECRET_KEY_FALLBACKS" in files[".env.example"]
    config["extension_plan_hash"] = planned["data"]["plan_hash"]
    applied = call(tmp_path, "apply", config, "reviewed-session")
    assert applied["outcome"] == "success", applied
    assert (Path(applied["data"]["output"]) / "native_contract.json").is_file()
    assert not (tmp_path / "source.db").exists()


def test_session_naive_timezone_is_not_silently_interpreted_as_utc(tmp_path, monkeypatch):
    _session_project(tmp_path)
    with (tmp_path / "settings.py").open("a") as settings:
        settings.write("\nUSE_TZ = False\nTIME_ZONE = 'Asia/Tokyo'\n")
    monkeypatch.setenv(
        "SANKA_SOURCE_TEST_DATABASE",
        json.dumps({"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}),
    )
    config = {"settings_module": "settings", "orm": "sqlalchemy"}
    assert call(tmp_path, "scan", config)["outcome"] == "success"
    result = call(tmp_path, "plan", config)
    assert result["outcome"] == "error", result
    assert "session-authentication" in json.dumps(result)
