# SPDX-License-Identifier: Apache-2.0
"""Shipped plan settings declarations follow sanka-extension-settings/v1."""

from __future__ import annotations

import json
from importlib import resources

import pytest

SETTINGS_FILE = "sanka-extension-settings.json"
TYPES = {"choice", "boolean", "integer", "text", "path"}
STAGES = {"scan", "plan", "apply", "test", "verify"}


def _label(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("en"), str)
        and isinstance(value.get("ja"), str)
        and bool(value["en"])
        and bool(value["ja"])
    )


def settings_errors(document: object) -> list[str]:
    """Every rule the CLI and hosted runs rely on when they render or validate settings."""
    if not isinstance(document, dict):
        return ["document must be an object"]
    errors = []
    if document.get("schema_version") != "sanka-extension-settings/v1":
        errors.append("unknown schema_version")
    display = document.get("display")
    if not isinstance(display, dict) or not _label(display.get("name")):
        errors.append("display.name needs en and ja")
    settings = document.get("settings")
    if not isinstance(settings, list):
        return [*errors, "settings must be a list"]
    ids = [item.get("id") for item in settings if isinstance(item, dict)]
    for item in settings:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("each setting needs an id")
            continue
        name = item["id"]
        kind, default = item.get("type"), item.get("default")
        if kind not in TYPES or item.get("stage") not in STAGES or not _label(item.get("label")):
            errors.append(f"{name}: type, stage and label are required")
            continue
        if "description" in item and not _label(item["description"]):
            errors.append(f"{name}: description needs en and ja")
        optional = item.get("optional") is True
        if kind == "choice":
            choices = item.get("choices")
            values = [c.get("value") for c in choices or [] if isinstance(c, dict)]
            if (
                not choices
                or len(values) != len(choices)
                or len(set(values)) != len(values)
                or not all(
                    isinstance(v, str) and _label(c.get("label"))
                    for v, c in zip(values, choices, strict=True)
                )
            ):
                errors.append(f"{name}: choices need unique string values and labels")
            elif default not in values:
                errors.append(f"{name}: default must be one of its choices")
        elif kind == "boolean" and not isinstance(default, bool):
            errors.append(f"{name}: default must be a boolean")
        elif kind == "integer":
            low, high = item.get("minimum"), item.get("maximum")
            if not all(type(v) is int for v in (default, low, high)) or not low <= default <= high:
                errors.append(f"{name}: integer default must sit within minimum and maximum")
        elif kind in {"text", "path"} and not (
            isinstance(default, str) or (default is None and optional)
        ):
            errors.append(f"{name}: default must be a string unless the setting is optional")
        when = item.get("when", {})
        if not isinstance(when, dict) or any(key not in ids or key == name for key in when):
            errors.append(f"{name}: when must refer to other settings")
    if len(set(ids)) != len(ids):
        errors.append("setting ids must be unique")
    return errors


@pytest.mark.parametrize(
    "package", ["sanka_extension_drf_to_fastapi", "sanka_extension_drf_to_flask"]
)
def test_shipped_settings_follow_the_declared_format(package):
    document = json.loads(resources.files(package).joinpath(SETTINGS_FILE).read_text("utf-8"))
    assert settings_errors(document) == []


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"default": "sqlalchemy2"}, "default must be one of its choices"),
        ({"when": {"missing": "x"}}, "when must refer to other settings"),
        ({"label": {"en": "ORM"}}, "type, stage and label are required"),
    ],
)
def test_settings_format_rejects_unusable_declarations(change, error):
    setting = {
        "id": "orm",
        "stage": "plan",
        "type": "choice",
        "default": "django",
        "label": {"en": "ORM", "ja": "ORM"},
        "choices": [{"value": "django", "label": {"en": "Django", "ja": "Django"}}],
    }
    document = {
        "schema_version": "sanka-extension-settings/v1",
        "display": {"name": {"en": "x", "ja": "x"}},
        "settings": [{**setting, **change}],
    }
    assert any(error in message for message in settings_errors(document))
