# SPDX-License-Identifier: Apache-2.0
"""Capture DRF's OPTIONS metadata per path so the generated app can answer it exactly.

DRF answers OPTIONS with ``SimpleMetadata``: the view name, description, renderer and
parser media types, and — when the caller passes the permission checks — an ``actions``
map of serializer field metadata for POST (collection paths) and PUT (detail paths, only
when the object exists). Rather than re-implementing that table, the scan asks the
installed DRF for it twice: once as the anonymous caller and once with the permission
checks satisfied. The generated runtime picks the variant the incoming request earns and
drops the PUT block when the object is missing or not the caller's.
"""

from __future__ import annotations

import importlib
import json
import re
from typing import Any

_PATH_PARAMETER = re.compile(r"\{[^}]*\}")


def route_options_metadata(
    *,
    view_class: type[Any],
    callback: Any,
    actions: dict[str, str] | None,
    path: str,
) -> dict[str, Any]:
    """Return ``{"anonymous": …, "authorized": …}`` metadata bodies, or ``{}`` on failure."""
    try:
        anonymous = _capture(view_class, callback, actions, path, authorized=False)
        authorized = _capture(view_class, callback, actions, path, authorized=True)
    except Exception:
        return {}
    if anonymous is None or authorized is None:
        return {}
    return {"anonymous": anonymous, "authorized": authorized}


def _bound_view(view_class: type[Any], callback: Any, actions: dict[str, str] | None) -> Any | None:
    initkwargs = dict(getattr(callback, "initkwargs", None) or {})
    allowed = {"suffix", "detail", "basename", "name", "description"}
    try:
        view = view_class(**{key: value for key, value in initkwargs.items() if key in allowed})
    except Exception:
        return None
    if actions is not None:
        view.action_map = dict(actions)
        for method, action in actions.items():
            handler = getattr(view, action, None)
            if handler is not None:
                setattr(view, method, handler)
        view.action = None  # OPTIONS carries no action, exactly as the router dispatches it
    view.args = ()
    view.kwargs = {}
    view.format_kwarg = None
    view.headers = {}
    return view


def _capture(
    view_class: type[Any],
    callback: Any,
    actions: dict[str, str] | None,
    path: str,
    *,
    authorized: bool,
) -> dict[str, Any] | None:
    view = _bound_view(view_class, callback, actions)
    if view is None:
        return None
    test_module = importlib.import_module("rest_framework.test")
    encoders = importlib.import_module("rest_framework.utils.encoders")
    factory = test_module.APIRequestFactory()
    concrete = _PATH_PARAMETER.sub("1", path) or "/"
    if not concrete.startswith("/"):
        concrete = "/" + concrete
    request = view.initialize_request(factory.options(concrete))
    view.request = request
    # PUT metadata is only offered when the object exists; that is a runtime fact the
    # generated app checks itself, so the capture treats the object as present.
    view.get_object = lambda: object()
    if authorized:
        view.check_permissions = lambda request: None
    metadata_class: Any = getattr(view, "metadata_class", None)
    if not callable(metadata_class):
        return None
    metadata: Any = metadata_class()
    payload = metadata.determine_metadata(request, view)
    normalized = json.loads(json.dumps(payload, cls=encoders.JSONEncoder))
    return normalized if isinstance(normalized, dict) else None


__all__ = ["route_options_metadata"]
