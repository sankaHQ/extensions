# SPDX-License-Identifier: Apache-2.0
"""Explicitly disabled DRF authentication must not prevent safe project capture."""

import dataclasses
import shutil
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from test_python_to_golang import request


@pytest.mark.parametrize("view_override", [False, True])
@pytest.mark.parametrize("include_auth_app", [False, True])
@pytest.mark.parametrize(
    "authentication", [[], ["rest_framework.authentication.BasicAuthentication"], None]
)
def test_anonymous_project_capture(tmp_path, include_auth_app, authentication, view_override):
    fixture = Path(__file__).parent / "fixtures/drf_project"
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    settings = tmp_path / "shop_config/settings.py"
    source = settings.read_text().replace(
        '"UNAUTHENTICATED_USER": None,',
        f'"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": {authentication!r},',
    )
    if not include_auth_app:
        source = source.replace('    "django.contrib.auth",\n', "")
    settings.write_text(source)
    if view_override:
        views = tmp_path / "orders/views.py"
        views.write_text(
            "from rest_framework.authentication import BasicAuthentication\n"
            + views.read_text().replace(
                "    serializer_class = OrderSerializer",
                "    serializer_class = OrderSerializer\n"
                "    authentication_classes = [BasicAuthentication]",
            )
        )
    base = dataclasses.replace(
        request(tmp_path, "drf", "fiber"),
        configuration={
            "source_framework": "drf",
            "source_file": "shop_config/urls.py",
            "models_file": "orders/models.py",
            "database_layer": "pgx",
            "target_framework": "fiber",
        },
    )
    scanned = handle(dataclasses.replace(base, command="scan"))
    planned = handle(dataclasses.replace(base, command="plan"))
    assert scanned.outcome == planned.outcome == "success"
    if authentication == [] and not view_override:
        assert scanned.data["generation_ready"], scanned.data["gaps"]
        assert planned.data["files"]
        assert not planned.data["capture"]["gaps"]
    else:
        assert not scanned.data["generation_ready"]
        assert planned.data["files"] == {}
        applied = handle(dataclasses.replace(base, command="apply"))
        assert applied.outcome == "error"
