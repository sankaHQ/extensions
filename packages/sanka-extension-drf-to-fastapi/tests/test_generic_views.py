# SPDX-License-Identifier: Apache-2.0
"""Stock generic CRUD support; custom request and access rules stay fail closed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_generic_view_capture_boundaries(tmp_path: Path) -> None:
    script = r"""
import json, sys
sys.path[:] = json.loads(sys.argv[1])
from django.conf import settings
settings.configure(INSTALLED_APPS=['django.contrib.contenttypes', 'rest_framework'],
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}})
import django
django.setup()
from rest_framework import generics, permissions, serializers
from django.db import models
from sanka_extension_drf_to_fastapi.django_fastapi import (
    _generic_actions, _generic_view_adaptation_reason, _viewset_overrides,
    _view_auth_support, _native_route_support, _WalkResult, _route_methods,
)
class Item(models.Model):
    name = models.CharField(max_length=50)
    class Meta: app_label = 'generic_probe'
class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ['id', 'name']
classes = {
    'CreateAPIView': {'post':'create'},
    'ListAPIView': {'get':'list'},
    'RetrieveAPIView': {'get':'retrieve'},
    'DestroyAPIView': {'delete':'destroy'},
    'UpdateAPIView': {'put':'update','patch':'partial_update'},
    'ListCreateAPIView': {'get':'list','post':'create'},
    'RetrieveUpdateAPIView': {'get':'retrieve','put':'update','patch':'partial_update'},
    'RetrieveDestroyAPIView': {'get':'retrieve','delete':'destroy'},
    'RetrieveUpdateDestroyAPIView': {
        'get':'retrieve','put':'update','patch':'partial_update','delete':'destroy'},
}
for name, expected in classes.items():
    view = type('Stock'+name, (getattr(generics, name),), {
        'queryset':Item.objects.all(), 'serializer_class':ItemSerializer,
        'permission_classes':[permissions.AllowAny]})
    assert _generic_actions(view) == expected, name
    assert not _viewset_overrides(view, allow_missing=True), name
    assert _generic_view_adaptation_reason(view, view.as_view()) is None, name
    assert _view_auth_support(view, Item) is not None, name
    result = _WalkResult()
    native, reasons, operations = _native_route_support(result, view_class=view,
        callback=view.as_view(), actions=expected, path='/items/{pk}/', supported=True,
        serializer_name=__name__+'.ItemSerializer', middleware=())
    assert native, (name, reasons)
    assert not operations
class Base(generics.ListCreateAPIView):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    permission_classes = [permissions.AllowAny]
class CustomGet(Base):
    def get(self, request, *args, **kwargs): return super().get(request, *args, **kwargs)
assert _generic_actions(CustomGet) is None
assert _generic_view_adaptation_reason(CustomGet, CustomGet.as_view()) is not None
class CustomPermission(Base):
    def get_permissions(self): return []
reason = _generic_view_adaptation_reason(CustomPermission, CustomPermission.as_view())
assert 'get_permissions' in reason.message
class CustomQuery(Base):
    def get_queryset(self): return Item.objects.filter(name='private')
assert 'get_queryset' in _viewset_overrides(CustomQuery, allow_missing=True)
class CustomCreate(Base):
    def perform_create(self, serializer): serializer.save(name='changed')
assert _view_auth_support(CustomCreate, Item) is None
class GetOnly(Base):
    http_method_names = ['get', 'head', 'options']
assert _generic_actions(GetOnly) == {'get':'list'}
assert _route_methods(GetOnly, _generic_actions(GetOnly)) == [('GET','list')]
reason = _generic_view_adaptation_reason(Base, Base.as_view(permission_classes=[]))
assert reason.code == 'SANKA_DRF_GENERIC_INITKWARGS_UNSUPPORTED'
class CustomHead(Base):
    def head(self, request): pass
assert _generic_view_adaptation_reason(CustomHead, CustomHead.as_view()) is not None
class CustomPagination(Base):
    def get_paginated_response(self, data): return data
reason = _generic_view_adaptation_reason(CustomPagination, CustomPagination.as_view())
assert 'get_paginated_response' in reason.message
class CustomSetup(Base):
    def setup(self, request, *args, **kwargs): pass
assert _generic_view_adaptation_reason(CustomSetup, CustomSetup.as_view()) is not None
class WrongMethod(Base):
    post = generics.ListAPIView.get
assert _generic_actions(WrongMethod) is None
print('generic capture boundaries passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(sys.path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux",
    reason="Real conversion acceptance runs in disposable Linux CI.",
)
def test_generic_crud_generated_http_and_database_parity(tmp_path: Path) -> None:
    from test_native_fastapi import FIXTURES, SCENARIOS, _generate, _run_probe

    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    (project / "inventory/views.py").write_text("""
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from inventory.models import Gadget
from inventory.serializers import GadgetSerializer
class GadgetList(ListCreateAPIView):
    queryset = Gadget.objects.all()
    serializer_class = GadgetSerializer
class GadgetDetail(RetrieveUpdateDestroyAPIView):
    queryset = Gadget.objects.all()
    serializer_class = GadgetSerializer
""")
    (project / "crud_config/urls.py").write_text("""
from django.urls import path
from inventory.views import GadgetList, GadgetDetail
urlpatterns = [path('api/gadgets/', GadgetList.as_view()),
               path('api/gadgets/<str:pk>/', GadgetDetail.as_view())]
""")
    output = _generate(project)
    scenarios = [case for case in SCENARIOS if case["path"] != "/api/"]
    original = _run_probe("source", project, tmp_path / "source.sqlite3", scenarios=scenarios)
    generated = _run_probe(
        "native", project, tmp_path / "target.sqlite3", output=output, scenarios=scenarios
    )
    assert generated == original
