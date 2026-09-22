# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Native authentication bindings for the qualified signed bearer policy."""

from __future__ import annotations

import ast
import copy
from typing import Any

from .application import _bindings
from .jwt_security import ACCESS_BODY

PRINCIPAL = '{"sub": claims["sub"], "tenant": claims["tenant"], "role": claims["role"]}'
UNAVAILABLE = """class AuthenticationUnavailable(APIException):
    status_code = 503
    default_detail = "authentication unavailable"
"""


def _body(framework: str) -> list[ast.stmt]:
    if framework == "fastapi":
        unavailable = 'raise HTTPException(status_code=503, detail="authentication unavailable")'
        unauthenticated = 'raise HTTPException(status_code=401, detail="not authenticated", headers={"WWW-Authenticate": "Bearer"})'
        forbidden = 'raise HTTPException(status_code=403, detail="permission denied")'
    elif framework == "drf":
        unavailable = "raise AuthenticationUnavailable()"
        unauthenticated = 'raise AuthenticationFailed("not authenticated")'
        forbidden = 'raise PermissionDenied("permission denied")'
    else:
        unavailable = 'return jsonify({"error": "authentication unavailable"}), 503'
        unauthenticated = (
            'return jsonify({"error": "not authenticated"}), 401, {"WWW-Authenticate": "Bearer"}'
        )
        forbidden = 'return jsonify({"error": "permission denied"}), 403'
    return ast.parse(
        ACCESS_BODY.replace("UNAVAILABLE", unavailable)
        .replace("UNAUTHENTICATED", unauthenticated)
        .replace("FORBIDDEN", forbidden)
    ).body


def normalize_native_security(
    tree: ast.Module, framework: str
) -> tuple[ast.Module, dict[str, Any]] | None:
    from .security import _same, _signature

    tree = copy.deepcopy(tree)
    consumed: list[ast.stmt] = []
    guard: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None = None
    for node in tree.body:
        matches = False
        if framework == "fastapi" and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            matches = (
                not node.decorator_list
                and _signature(
                    node, "request: Request", asynchronous=isinstance(node, ast.AsyncFunctionDef)
                )
                and _same(node.body, _body(framework) + ast.parse("return " + PRINCIPAL).body)
            )
        elif framework == "flask" and isinstance(node, ast.FunctionDef):
            matches = (
                [ast.unparse(d) for d in node.decorator_list] == ["app.before_request"]
                and _signature(node, "")
                and _same(
                    node.body, _body(framework) + ast.parse("g.principal = " + PRINCIPAL).body
                )
            )
        elif framework == "drf" and isinstance(node, ast.ClassDef):
            expected = ast.parse("""class JWTAuthentication(BaseAuthentication):
    def authenticate(self, request):
        pass
    def authenticate_header(self, request):
        return "Bearer"
""").body[0]
            assert isinstance(expected, ast.ClassDef) and isinstance(
                expected.body[0], ast.FunctionDef
            )
            expected.name = node.name
            expected.body[0].body = (
                _body(framework)
                + ast.parse(
                    'return SimpleNamespace(is_authenticated=True, pk=claims["sub"]), claims'
                ).body
            )
            matches = _same([node], [expected])
        if matches:
            if guard is not None:
                raise ValueError("multiple native authentication policies require capture")
            assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            guard = node
            consumed.append(node)
    if guard is None:
        return None
    required = {
        ("os", "environ"),
        ("jwt", "decode"),
        ("jwt", "get_unverified_header"),
        ("jwt", "InvalidTokenError"),
        ("re", "fullmatch"),
        ("time", "time"),
    }
    required |= {
        "fastapi": {("fastapi", "Request"), ("fastapi", "Depends"), ("fastapi", "HTTPException")},
        "flask": {("flask", "g"), ("flask", "request"), ("flask", "jsonify")},
        "drf": {
            ("rest_framework.authentication", "BaseAuthentication"),
            ("rest_framework.exceptions", "APIException"),
            ("rest_framework.exceptions", "AuthenticationFailed"),
            ("rest_framework.exceptions", "PermissionDenied"),
            ("types", "SimpleNamespace"),
        },
    }[framework]
    imports = {
        (n.module, a.name): i
        for i, n in enumerate(tree.body)
        if isinstance(n, ast.ImportFrom) and not n.level
        for a in n.names
        if a.asname is None
    }
    if not required <= imports.keys():
        raise ValueError("native authentication requires qualified imports")
    if {"len", "set", "type", "str", "int", "dict"}.intersection(
        name for node in tree.body for name in _bindings(node)
    ):
        raise ValueError("native authentication builtins must not be shadowed")
    guard_index = tree.body.index(guard)
    if framework == "fastapi" and imports[("fastapi", "Request")] >= guard_index:
        raise ValueError("Request must be imported before the authentication dependency")
    if framework == "drf":
        unavailable = [n for n in tree.body if _same([n], ast.parse(UNAVAILABLE).body)]
        if (
            len(unavailable) != 1
            or not imports[("rest_framework.exceptions", "APIException")]
            < tree.body.index(unavailable[0])
            < guard_index
            or imports[("rest_framework.authentication", "BaseAuthentication")] >= guard_index
        ):
            raise ValueError(
                "native DRF authentication requires its explicit availability exception"
            )
        consumed += unavailable
    projections: dict[str, dict[str, str]] = {}
    receivers: dict[str, str] = {}
    routes = 0
    for node in tree.body:
        if (
            node in consumed
            or not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            or not node.decorator_list
        ):
            continue
        decorators = [ast.unparse(d) for d in node.decorator_list]
        if framework == "drf":
            if not any(d.startswith("api_view(") for d in decorators):
                continue
            wanted = f"authentication_classes([{guard.name}])"
            if wanted not in decorators or tree.body.index(node) <= guard_index:
                raise ValueError("every DRF view must select the captured authenticator")
            node.decorator_list[decorators.index(wanted)] = ast.parse(
                "authentication_classes([])", mode="eval"
            ).body
            receiver = "request.auth"
        else:
            if not any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"get", "post", "put", "patch", "delete"}
                for d in node.decorator_list
            ):
                continue
            receiver = "g.principal"
            if framework == "fastapi":
                defaults = (
                    dict(
                        zip(
                            (a.arg for a in node.args.args[-len(node.args.defaults) :]),
                            node.args.defaults,
                            strict=True,
                        )
                    )
                    if node.args.defaults
                    else {}
                )
                parameters = [
                    a
                    for a in node.args.args
                    if a.arg in defaults
                    and ast.unparse(defaults[a.arg]) == f"Depends({guard.name})"
                    and a.annotation is not None
                    and ast.unparse(a.annotation) == "dict"
                ]
                if (
                    len(parameters) != 1
                    or tree.body.index(node) <= guard_index
                    or imports[("fastapi", "Depends")] >= tree.body.index(node)
                ):
                    raise ValueError("every FastAPI route must inject the captured principal")
                argument = parameters[0]
                receiver = argument.arg
                index = node.args.args.index(argument)
                node.args.defaults.pop(index - (len(node.args.args) - len(node.args.defaults)))
                node.args.args.pop(index)
        receivers[node.name] = receiver
        routes += 1
        if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
            continue
        value = node.body[0].value
        if (
            framework != "fastapi"
            and isinstance(value, ast.Call)
            and ast.unparse(value.func) == ("Response" if framework == "drf" else "jsonify")
            and len(value.args) == 1
            and not value.keywords
        ):
            value = value.args[0]

        def claim(item: ast.expr, receiver: str = receiver) -> str | None:
            if framework == "drf" and ast.unparse(item) == "request.user.pk":
                return "sub"
            if (
                isinstance(item, ast.Subscript)
                and ast.unparse(item.value) == receiver
                and isinstance(item.slice, ast.Constant)
                and type(item.slice.value) is str
                and item.slice.value in {"sub", "tenant", "role"}
            ):
                return item.slice.value
            return None

        if not isinstance(value, ast.Dict) or not any(claim(v) for v in value.values):
            continue
        projection = {}
        for key, item in zip(value.keys, value.values, strict=True):
            field = claim(item)
            if (
                not isinstance(key, ast.Constant)
                or type(key.value) is not str
                or field is None
                or key.value in projection
            ):
                raise ValueError("identity responses require explicit verified claim projections")
            projection[key.value] = field
        projections[node.name] = projection
        value.values = [ast.Constant("") for _ in value.values]
    if not routes:
        raise ValueError("unused native authentication policy")
    tree.body = [n for n in tree.body if n not in consumed]
    referenced = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            node.names = [
                a
                for a in node.names
                if (node.module, a.name) not in required or (a.asname or a.name) in referenced
            ]
    tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom) or n.names]
    return tree, {
        "kind": "jwt-hs256-roles",
        "native": True,
        "scope": "application" if framework == "flask" else "views",
        "error_key": "error" if framework == "flask" else "detail",
        "success_headers": {},
        "denied_headers": {},
        "projections": projections,
        "receivers": receivers,
    }
