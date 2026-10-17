# SPDX-License-Identifier: Apache-2.0
"""Target qualification must retain source policies when waiving scan limitations."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_native_plan import native_project
from test_sqlalchemy_sessions import _session_project

CAPTURE = """
import json, sys
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides, qualify_routes
scan = scan_django(sys.argv[1], settings_module=sys.argv[2])
print(json.dumps(qualify_routes(scan, capture_sqlalchemy_overrides(scan))))
"""


def qualified(root: Path, settings: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, "-c", CAPTURE, str(root), settings],
        cwd=root,
        env=os.environ
        | {
            "SANKA_SOURCE_TEST_DATABASE": json.dumps(
                {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            )
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("action", ["destroy", "update", "retrieve"])
@pytest.mark.parametrize("listing", ["pagination", "filter"])
def test_listing_waiver_keeps_overridden_actions_manual(
    tmp_path: Path, action: str, listing: str
) -> None:
    native_project(tmp_path)
    views = tmp_path / "inventory/views.py"
    listing_source = (
        "    from rest_framework.pagination import PageNumberPagination\n"
        "    pagination_class = PageNumberPagination\n"
        if listing == "pagination"
        else "    from django_filters.rest_framework import DjangoFilterBackend\n"
        "    filter_backends = [DjangoFilterBackend]\n"
        "    filterset_fields = ['name']\n"
    )
    views.write_text(
        views.read_text() + listing_source + f"    def {action}(self, request, *args, **kwargs):\n"
        "        from rest_framework.exceptions import PermissionDenied\n"
        "        if not request.user or not request.user.is_staff:\n"
        "            raise PermissionDenied()\n"
        f"        return super().{action}(request, *args, **kwargs)\n"
    )
    rows = qualified(tmp_path, "crud_config.settings")
    blocked = {action, "partial_update"} if action == "update" else {action}
    affected = [row for row in rows if row["operation"] in blocked]
    assert affected
    assert all(not row["native"] for row in affected), affected
    assert all("SANKA_DRF_VIEWSET_OVERRIDES_UNSUPPORTED" in row["gaps"] for row in affected)
    assert all(row["native"] for row in rows if row["operation"] not in blocked), rows


@pytest.mark.parametrize("policy", ["staff-check", "owner-assignment", "stock"])
@pytest.mark.parametrize("expose_create", [False, True])
def test_session_auth_waiver_requires_stock_perform_create(
    tmp_path: Path, policy: str, expose_create: bool
) -> None:
    _session_project(tmp_path)
    models = tmp_path / "catalog/models.py"
    models.write_text(
        models.read_text().replace(
            "class Note(models.Model):",
            "class Note(models.Model):\n"
            "    owner = models.ForeignKey(Account, on_delete=models.CASCADE, null=True)",
        )
    )
    if policy != "stock":
        body = (
            "        from rest_framework.exceptions import PermissionDenied\n"
            "        if not self.request.user.is_superuser:\n"
            "            raise PermissionDenied()\n"
            "        serializer.save()\n"
            if policy == "staff-check"
            else "        serializer.save(owner=self.request.user)\n"
        )
        views = tmp_path / "catalog/views.py"
        views.write_text(views.read_text() + "    def perform_create(self, serializer):\n" + body)
    if not expose_create:
        (tmp_path / "urls.py").write_text(
            "from django.urls import path\nfrom catalog.views import NoteViewSet\n"
            "urlpatterns = [path('notes/', NoteViewSet.as_view({'get': 'list'})),\n"
            "    path('notes/<int:pk>/', NoteViewSet.as_view({'get': 'retrieve', "
            "'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}))]\n"
        )
    rows = qualified(tmp_path, "settings")
    creates = [row for row in rows if row["operation"] == "create"]
    assert bool(creates) is expose_create
    other_actions = [row for row in rows if row["operation"] != "create"]
    assert other_actions and all(row["native"] for row in other_actions), rows
    if policy == "stock":
        assert all(row["native"] for row in rows), rows
    else:
        assert all(not row["native"] for row in creates), creates
        assert all("SANKA_DRF_VIEWSET_OVERRIDES_UNSUPPORTED" in row["gaps"] for row in creates)
