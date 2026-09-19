# SPDX-License-Identifier: Apache-2.0
"""Lower bounded response contracts to data; never execute source methods."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from typing import Any, cast

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


_OPERATIONS = {"list", "create", "retrieve", "update", "partial_update", "destroy"}
_RESERVED_HEADERS = {
    "allow",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "content-type",
    "content-length",
    "content-encoding",
    "set-cookie",
    "trailer",
    "transfer-encoding",
    "te",
    "upgrade",
    "vary",
    "www-authenticate",
}


def _body_contract(node: ast.expr) -> dict[str, Any]:
    if ast.dump(node) == ast.dump(ast.parse("response.data", mode="eval").body):
        return {"kind": "data"}
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise ValueError("non-finite literal")
        return {"kind": "literal", "value": node.value}
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) in (int, float)
    ):
        return _body_contract(ast.Constant(value=-cast(int | float, node.operand.value)))
    if isinstance(node, ast.List):
        return {"kind": "list", "items": [_body_contract(item) for item in node.elts]}
    if isinstance(node, ast.Dict) and all(
        isinstance(key, ast.Constant) and type(key.value) is str for key in node.keys
    ):
        return {
            "kind": "dict",
            "items": [
                [cast(ast.Constant, key).value, _body_contract(value)]
                for key, value in zip(node.keys, node.values, strict=True)
            ],
        }
    raise ValueError("dynamic response body")


def capture_response_overrides(carryover: dict[str, Any] | None) -> dict[str, Any] | None:
    if not carryover:
        return None
    methods = {}
    try:
        for method in carryover["methods"]:
            name = method["name"]
            (node,) = ast.parse(method["source"]).body
            if (
                not isinstance(node, ast.FunctionDef)
                or node.name != name
                or name not in _OPERATIONS
            ):
                return None
            args = node.args
            if (
                name in methods
                or node.decorator_list
                or args.posonlyargs
                or args.kwonlyargs
                or args.defaults
                or args.kw_defaults
                or [a.arg for a in args.args] != ["self", "request"]
                or not args.vararg
                or args.vararg.arg != "args"
                or not args.kwarg
                or args.kwarg.arg != "kwargs"
                or len(node.body) < 2
            ):
                return None
            first = ast.parse(f"response = super().{name}(request, *args, **kwargs)").body[0]
            last = ast.parse("return response").body[0]
            if ast.dump(node.body[0]) != ast.dump(first) or ast.dump(node.body[-1]) != ast.dump(
                last
            ):
                return None
            actions: list[dict[str, Any]] = []
            status = None
            body = None
            for statement in node.body[1:-1]:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    return None
                target = statement.targets[0]
                value = statement.value
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "response"
                    and isinstance(target.slice, ast.Constant)
                    and type(target.slice.value) is str
                    and isinstance(value, ast.Constant)
                    and type(value.value) is str
                ):
                    header = target.slice.value
                    if (
                        not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header)
                        or header.lower() in _RESERVED_HEADERS
                        or not value.value.isascii()
                        or any(
                            ord(character) < 32 or ord(character) == 127
                            for character in value.value
                        )
                    ):
                        return None
                    actions.append({"kind": "header", "name": header, "value": value.value})
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "response"
                    and target.attr == "status_code"
                ):
                    if (
                        status is not None
                        or not isinstance(value, ast.Constant)
                        or type(value.value) is not int
                        or not 200 <= value.value < 300
                        or value.value in {204, 205}
                    ):
                        return None
                    status = value.value
                    actions.append({"kind": "status", "value": status})
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "response"
                    and target.attr == "data"
                ):
                    if body is not None or not isinstance(value, ast.Dict | ast.List):
                        return None
                    if not any(
                        ast.dump(item) == ast.dump(ast.parse("response.data", mode="eval").body)
                        for item in ast.walk(value)
                    ):
                        return None
                    body = _body_contract(value)
                    actions.append({"kind": "body", "value": body})
                else:
                    return None
            if name == "destroy" and body is not None and status is None:
                return None
            methods[name] = actions
        covered = set(methods) | ({"partial_update"} if "update" in methods else set())
        if not methods or set(carryover["operations"]) != covered:
            return None
    except (SyntaxError, ValueError, TypeError, KeyError):
        return None
    if "update" in methods:
        methods["partial_update"] = methods["update"] + methods.get("partial_update", [])
    return methods


def _wrap_body(contract: dict[str, Any], data: Any) -> Any:
    if contract["kind"] == "data":
        return data
    if contract["kind"] == "literal":
        return contract["value"]
    if contract["kind"] == "list":
        return [_wrap_body(item, data) for item in contract["items"]]
    return {key: _wrap_body(value, data) for key, value in contract["items"]}


def override_response(response: Any, actions: list[dict[str, Any]]) -> Any:
    data = response.get_json() if response.is_json and response.get_data() else None
    if response.status_code == 204:
        response.headers.pop("Content-Type", None)
    for action in actions:
        if action["kind"] == "header":
            response.headers[action["name"]] = action["value"]
        elif action["kind"] == "status":
            response.status_code = action["value"]
        else:
            data = _wrap_body(action["value"], data)
            response.set_data(
                json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            )
            response.headers["Content-Type"] = "application/json"
    return response
