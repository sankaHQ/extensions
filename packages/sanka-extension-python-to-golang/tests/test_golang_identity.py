# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Native authentication hooks and request-local verified identity."""

import ast
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from test_golang_jwt import JWT_BODY
from test_python_to_golang import source


def identity_source(framework: str, base: str | None = None) -> str:
    imports = "from os import environ\nfrom jwt import decode, get_unverified_header, InvalidTokenError\nfrom re import fullmatch\nfrom time import time\n"
    body = JWT_BODY
    if framework == "fastapi":
        imports += "from fastapi import Depends, HTTPException, Request\n"
        body = (
            body.replace(
                "UNAVAILABLE",
                'raise HTTPException(status_code=503, detail="authentication unavailable")',
            )
            .replace(
                "UNAUTHENTICATED",
                'raise HTTPException(status_code=401, detail="not authenticated", headers={"WWW-Authenticate": "Bearer"})',
            )
            .replace(
                "FORBIDDEN", 'raise HTTPException(status_code=403, detail="permission denied")'
            )
        )
        guard = "def authenticate(request: Request):\n" + indent(
            body
            + 'return {"sub": claims["sub"], "tenant": claims["tenant"], "role": claims["role"]}\n',
            "    ",
        )
        receiver = "principal"
    elif framework == "drf":
        imports += "from rest_framework.authentication import BaseAuthentication\nfrom rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied\nfrom types import SimpleNamespace\n"
        body = (
            body.replace("UNAVAILABLE", "raise AuthenticationUnavailable()")
            .replace("UNAUTHENTICATED", 'raise AuthenticationFailed("not authenticated")')
            .replace("FORBIDDEN", 'raise PermissionDenied("permission denied")')
        )
        guard = (
            """class AuthenticationUnavailable(APIException):
    status_code = 503
    default_detail = "authentication unavailable"
class JWTAuthentication(BaseAuthentication):
    def authenticate(self, request):
"""
            + indent(
                body + 'return SimpleNamespace(is_authenticated=True, pk=claims["sub"]), claims\n',
                "        ",
            )
            + """    def authenticate_header(self, request):
        return "Bearer"
"""
        )
        receiver = "request.auth"
    else:
        imports += "from flask import request, jsonify, g\n"
        body = (
            body.replace(
                "UNAVAILABLE", 'return jsonify({"error": "authentication unavailable"}), 503'
            )
            .replace(
                "UNAUTHENTICATED",
                'return jsonify({"error": "not authenticated"}), 401, {"WWW-Authenticate": "Bearer"}',
            )
            .replace("FORBIDDEN", 'return jsonify({"error": "permission denied"}), 403')
        )
        guard = "@app.before_request\ndef authenticate():\n" + indent(
            body
            + 'g.principal = {"sub": claims["sub"], "tenant": claims["tenant"], "role": claims["role"]}\n',
            "    ",
        )
        receiver = "g.principal"
    tree = ast.parse(source(framework) if base is None else base)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if framework == "fastapi":
                node.args.args.append(
                    ast.arg(arg="principal", annotation=ast.Name(id="dict", ctx=ast.Load()))
                )
                node.args.defaults.append(ast.parse("Depends(authenticate)", mode="eval").body)
            elif framework == "drf":
                for decorator in node.decorator_list:
                    if ast.unparse(decorator) == "authentication_classes([])":
                        decorator.args = [ast.parse("[JWTAuthentication]", mode="eval").body]
            if node.name == "health":
                value = (
                    '{"user": '
                    + receiver
                    + '["sub"], "tenant": '
                    + receiver
                    + '["tenant"], "role": '
                    + receiver
                    + '["role"]}'
                )
                if framework != "fastapi":
                    value = ("Response" if framework == "drf" else "jsonify") + "(" + value + ")"
                node.body = ast.parse("return " + value).body
    text = ast.unparse(ast.fix_missing_locations(tree))
    text = imports + (text + "\n" + guard if framework == "flask" else guard + "\n" + text)
    # Combine identical imports without changing symbol identity or declaration order.
    tree = ast.parse(text)
    seen = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            node.names = [a for a in node.names if (node.module, a.name) not in seen]
            seen.update((node.module, a.name) for a in node.names)
    tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom) or n.names]
    result = ast.unparse(tree)
    if framework == "drf":
        result = result.replace("AllowAny", "IsAuthenticated").replace(
            "request.auth['sub']", "request.user.pk"
        )
    return result


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_native_identity_capture(tmp_path: Path, framework: str, target: str) -> None:
    (tmp_path / "app.py").write_text(identity_source(framework))
    config = configuration({"source_framework": framework, "target_framework": target})
    captured = capture(tmp_path, config)
    assert not captured["gaps"], captured["gaps"]
    assert captured["routes"][0]["identity"] == {"user": "sub", "tenant": "tenant", "role": "role"}
    assert captured == capture(tmp_path, config)


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_native_identity_verify(
    tmp_path: Path, framework: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from sanka_extension_python_to_golang.replay import replay
    from test_python_to_golang import apply

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    (tmp_path / "app.py").write_text(identity_source(framework))
    output = apply(tmp_path, framework, target)
    captured = capture(
        tmp_path, configuration({"source_framework": framework, "target_framework": target})
    )
    report = replay(tmp_path, output, captured, "verify")
    assert report["ok"], report
    successful = [c["body"] for c in report["candidate"] if c["status"] == 200]
    assert {c["user"] for c in successful} == {"fixture-user", "other-user"}
    assert {c["tenant"] for c in successful} == {"fixture-tenant", "other-tenant"}
    if target == "fiber":
        from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV

        with monkeypatch.context() as isolated:
            isolated.setitem(REPLAY_JWT_ENV, "AUTH_JWT_SECRET", "")
            unavailable = replay(tmp_path, output, captured, "verify")
        assert not unavailable["ok"]
        key = "error" if framework == "flask" else "detail"
        for observations in (unavailable["candidate"], unavailable["source"]):
            assert observations
            assert all(
                row["status"] == 503 and row["body"] == {key: "authentication unavailable"}
                for row in observations
            )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "before,after",
    [
        ("algorithms=['HS256']", "algorithms=['HS512']"),
        ("from jwt import", "from untrusted import"),
        ("['sub']", "['exp']"),
        ("'Bearer'", "'Basic'"),
    ],
)
def test_changed_identity_policy_blocks(
    tmp_path: Path, framework: str, before: str, after: str
) -> None:
    text = identity_source(framework)
    assert before in text
    (tmp_path / "app.py").write_text(text.replace(before, after))
    assert capture(tmp_path, configuration({"source_framework": framework}))["gaps"]


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("name", ["len", "set", "type", "str", "int", "dict"])
def test_identity_builtin_shadow_blocks(tmp_path: Path, framework: str, name: str) -> None:
    text = (
        identity_source(framework)
        .replace("def health(", f"def {name}(")
        .replace("path('health', health)", f"path('health', {name})")
    )
    (tmp_path / "app.py").write_text(text)
    assert capture(tmp_path, configuration({"source_framework": framework}))["gaps"]


@pytest.mark.parametrize("framework", ["drf", "fastapi"])
def test_unprotected_native_view_blocks(tmp_path: Path, framework: str) -> None:
    text = identity_source(framework)
    if framework == "drf":
        text = text.replace(
            "authentication_classes([JWTAuthentication])", "authentication_classes([])"
        )
    else:
        text = text.replace("principal: dict=Depends(authenticate)", "principal: dict")
    (tmp_path / "app.py").write_text(text)
    assert capture(tmp_path, configuration({"source_framework": framework}))["gaps"]
