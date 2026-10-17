# SPDX-License-Identifier: Apache-2.0
"""Recognize only framework URL callback wrappers whose behavior is captured."""

from __future__ import annotations

from importlib import import_module
from typing import Any


def stock_callback(callback: Any) -> bool:
    View = import_module("django.views").View
    no_append_slash = import_module("django.views.decorators.common").no_append_slash
    csrf_exempt = import_module("django.views.decorators.csrf").csrf_exempt
    APIView = import_module("rest_framework.views").APIView
    ViewSet = import_module("rest_framework.viewsets").ViewSet

    # Compare code objects, not spoofable module names copied by functools.wraps.
    plain = View.as_view()
    wrapper_codes = {
        csrf_exempt(plain).__code__,
        no_append_slash(plain).__code__,
    }
    seen: set[int] = set()
    while getattr(callback, "__code__", None) in wrapper_codes:
        if id(callback) in seen:
            return False
        seen.add(id(callback))
        callback = getattr(callback, "__wrapped__", None)
    viewset = ViewSet.as_view({"get": "list"}).__wrapped__
    code = getattr(callback, "__code__", None)
    if code is plain.__code__:
        return not hasattr(callback, "__wrapped__")
    return code is viewset.__code__ and getattr(callback, "__wrapped__", None) is APIView.dispatch
