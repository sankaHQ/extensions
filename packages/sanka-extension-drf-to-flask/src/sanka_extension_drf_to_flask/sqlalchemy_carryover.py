# SPDX-License-Identifier: Apache-2.0
"""Recognize one complete conditional-response contract; execute no source code."""

from __future__ import annotations

import ast
import hashlib
import json
from typing import Any

_EXPECTED = """
def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
    response = super().retrieve(request, *args, **kwargs)
    return self._conditional_response(request, response)
def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
    response = super().update(request, *args, **kwargs)
    return self._conditional_response(request, response)
def _conditional_response(self, request: Request, response: Response) -> Response:
    etag = self._etag(response.data)
    headers = {"Cache-Control": "private, max-age=0", "ETag": etag, "Vary": "Accept"}
    if self._etag_matches(request.headers.get("If-None-Match", ""), etag):
        return Response(status=status.HTTP_304_NOT_MODIFIED, headers=headers)
    for name, value in headers.items():
        response[name] = value
    return response
@staticmethod
def _etag(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f'"{hashlib.sha256(canonical).hexdigest()}"'
@staticmethod
def _etag_matches(raw_header: str, etag: str) -> bool:
    if not raw_header:
        return False
    return any(candidate.strip() in {"*", etag} for candidate in raw_header.split(","))
"""
_IMPORTS = {
    ("Any", "typing", "Any"),
    ("Request", "__sanka_view_shim__", "Request"),
    ("Response", "__sanka_view_shim__", "Response"),
    ("hashlib", "hashlib", None),
    ("json", "json", None),
    ("status", "__sanka_view_shim__", "status"),
}


def capture_conditional(carryover: dict[str, Any] | None) -> dict[str, Any] | None:
    if not carryover:
        return None
    if set(map(tuple, carryover["imports"])) != _IMPORTS or carryover["operations"] != [
        "retrieve",
        "update",
        "partial_update",
    ]:
        return None
    expected = {
        node.name: ast.dump(node)
        for node in ast.parse(_EXPECTED).body
        if isinstance(node, ast.FunctionDef)
    }
    observed = {}
    try:
        for method in carryover["methods"]:
            (node,) = ast.parse(method["source"]).body
            if method["name"] in observed:
                return None
            observed[method["name"]] = ast.dump(node)
    except (SyntaxError, ValueError, KeyError, TypeError):
        return None
    if observed != expected:
        return None
    return {"kind": "sha256-json-etag", "operations": list(carryover["operations"])}


def conditional_response(response: Any, header: str) -> Any:
    from flask import Response

    class ConditionalResponse(Response):
        def get_wsgi_headers(self, environ: Any) -> Any:
            headers = super().get_wsgi_headers(environ)
            if self.status_code == 304 and "Allow" in self.headers:
                headers["Allow"] = self.headers["Allow"]
            return headers

    canonical = json.dumps(
        response.get_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    etag = '"' + hashlib.sha256(canonical).hexdigest() + '"'
    headers = {"Cache-Control": "private, max-age=0", "ETag": etag, "Vary": "Accept"}
    if header and any(part.strip() in {"*", etag} for part in header.split(",")):
        return ConditionalResponse(status=304, headers=headers)
    response.headers.update(headers)
    return response
