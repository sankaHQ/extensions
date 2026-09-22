# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Signed bearer recipes exercised through the real source and target clients."""

from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from test_golang_security import access_body, secured_source

JWT_BODY = """token = request.headers.get("Authorization", "").strip(" \\t")
secret = environ.get("AUTH_JWT_SECRET", "")
issuer = environ.get("AUTH_JWT_ISSUER", "")
audience = environ.get("AUTH_JWT_AUDIENCE", "")
if not 32 <= len(secret) <= 256 or not secret.isascii() or not issuer or not audience or not issuer.isascii() or not audience.isascii():
    UNAVAILABLE
if len(token) > 8192 or fullmatch(r"Bearer [A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+", token) is None:
    UNAUTHENTICATED
try:
    if get_unverified_header(token[7:]) != {"alg": "HS256", "typ": "JWT"}:
        raise InvalidTokenError()
    claims = decode(token[7:], secret, algorithms=["HS256"], issuer=issuer, audience=audience, options={"require": ["exp", "sub", "tenant", "role", "iss", "aud"], "strict_aud": True, "verify_exp": False, "verify_iat": False, "verify_nbf": False})
    if set(claims) - {"exp", "iat", "nbf", "sub", "tenant", "role", "iss", "aud"}:
        raise InvalidTokenError()
    for name in ("sub", "tenant", "role"):
        if type(claims[name]) is not str or fullmatch(r"[A-Za-z0-9_-]{1,128}", claims[name]) is None:
            raise InvalidTokenError()
    for name in ("exp", "iat", "nbf"):
        if name in claims and (type(claims[name]) is not int or not 0 <= claims[name] <= 9007199254740991):
            raise InvalidTokenError()
    now = int(time())
    if claims["exp"] <= now or claims.get("iat", 0) > now or claims.get("nbf", 0) > now:
        raise InvalidTokenError()
except InvalidTokenError:
    UNAUTHENTICATED
if claims["role"] not in ("reader", "writer") or (request.method not in ("GET", "HEAD", "OPTIONS") and claims["role"] != "writer"):
    FORBIDDEN
"""


def jwt_source(framework: str, base: str | None = None) -> str:
    import ast

    old = access_body(framework)
    tree = ast.parse(old)
    denials = [ast.unparse(tree.body[i].body[0]) for i in (3, 6, 7)]
    body = (
        JWT_BODY.replace("UNAVAILABLE", denials[0])
        .replace("UNAUTHENTICATED", denials[1])
        .replace("FORBIDDEN", denials[2])
    )
    text = secured_source(framework, base)
    depth = 8 if framework == "drf" else 4
    assert indent(old, " " * depth) in text
    return text.replace(
        "from hmac import compare_digest",
        "from jwt import decode, get_unverified_header, InvalidTokenError\nfrom re import fullmatch\nfrom time import time",
    ).replace(indent(old, " " * depth), indent(body, " " * depth))


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_jwt_capture(tmp_path: Path, framework: str, target: str) -> None:
    (tmp_path / "app.py").write_text(jwt_source(framework))
    config = configuration({"source_framework": framework, "target_framework": target})
    result = capture(tmp_path, config)
    assert not result["gaps"], result["gaps"]
    assert result["security"]["kind"] == "jwt-hs256-roles"
    assert result == capture(tmp_path, config)


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "before,after",
    [
        ('algorithms=["HS256"]', 'algorithms=["HS256", "HS512"]'),
        ('"strict_aud": True', '"strict_aud": False'),
        ('claims["exp"] <= now', 'claims["exp"] < now'),
        ("32 <= len(secret)", "1 <= len(secret)"),
        ("from jwt import", "from untrusted import"),
        ('claims["role"] != "writer"', 'claims["role"] != "reader"'),
    ],
)
def test_changed_jwt_policy_blocks(tmp_path: Path, framework: str, before: str, after: str) -> None:
    text = jwt_source(framework)
    assert before in text
    (tmp_path / "app.py").write_text(text.replace(before, after))
    assert capture(tmp_path, configuration({"source_framework": framework}))["gaps"]


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_jwt_public_verify(tmp_path: Path, framework: str, target: str) -> None:
    import os

    from sanka_extension_python_to_golang.replay import replay
    from test_python_to_golang import apply

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    (tmp_path / "app.py").write_text(jwt_source(framework))
    output = apply(tmp_path, framework, target)
    captured = capture(
        tmp_path, configuration({"source_framework": framework, "target_framework": target})
    )
    report = replay(tmp_path, output, captured, "verify")
    assert report["ok"], report
    assert {c["status"] for c in report["candidate"]} == {200, 401, 403}
    assert report["security_headers"]["ok"]


@pytest.mark.parametrize("framework", SOURCES)
def test_jwt_configuration_and_repeated_headers(tmp_path: Path, framework: str) -> None:
    import json
    import sys

    from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV, replay_roles
    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _client_lifecycle, _run

    path = tmp_path / "app.py"
    path.write_text(jwt_source(framework))
    observed = tmp_path / "observed.json"
    token = replay_roles({"method": "GET", "expected_status": 200}, first=False)[0][1]
    variants = [
        {"AUTH_JWT_SECRET": ""},
        {"AUTH_JWT_SECRET": "short"},
        {"AUTH_JWT_SECRET": "a" * 257},
        {"AUTH_JWT_SECRET": "読" * 32},
        {"AUTH_JWT_ISSUER": ""},
        {"AUTH_JWT_AUDIENCE": ""},
    ]
    for values in variants:
        _run(
            [
                sys.executable,
                "-I",
                "-c",
                _client_lifecycle(SOURCE_PROBE),
                framework,
                str(path),
                json.dumps([{"path": "/health", "headers": {"Authorization": token}}]),
                str(observed),
                "",
                "0",
            ],
            tmp_path,
            environment=REPLAY_JWT_ENV | values | {"SANKA_GO_REPLAY_HEADERS": "[]"},
        )
        assert json.loads(observed.read_text())[0]["status"] == 503
    if framework != "drf":
        requests = [
            {"path": "/health", "headers": [["Authorization", token], ["Authorization", "bad"]]}
        ]
        _run(
            [
                sys.executable,
                "-I",
                "-c",
                _client_lifecycle(SOURCE_PROBE),
                framework,
                str(path),
                json.dumps(requests),
                str(observed),
                "",
                "0",
            ],
            tmp_path,
            environment=REPLAY_JWT_ENV | {"SANKA_GO_REPLAY_HEADERS": "[]"},
        )
        assert json.loads(observed.read_text())[0]["status"] == (
            401 if framework == "flask" else 200
        )


@pytest.mark.parametrize("target", TARGETS)
def test_generated_jwt_configuration(tmp_path: Path, target: str) -> None:
    import json
    import os

    from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV, replay_roles
    from sanka_extension_python_to_golang.replay import _run
    from test_python_to_golang import apply

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    (tmp_path / "app.py").write_text(jwt_source("fastapi"))
    output = apply(tmp_path, "fastapi", target)
    token = replay_roles({"method": "GET", "expected_status": 200}, first=False)[0][1]
    for secret in REPLAY_JWT_ENV.values():
        assert all(secret not in p.read_text() for p in output.rglob("*") if p.is_file())
    (output / "jwt_policy_test.go").write_text(
        """package backend
import ("testing"; "strings")
func TestJWTConfiguration(t *testing.T) {
    ENVIRONMENT
    for _, c := range []struct { key, value string }{
        {"AUTH_JWT_SECRET", ""}, {"AUTH_JWT_SECRET", "short"},
        {"AUTH_JWT_SECRET", strings.Repeat("a",257)}, {"AUTH_JWT_SECRET", strings.Repeat("読",32)},
        {"AUTH_JWT_ISSUER", ""}, {"AUTH_JWT_AUDIENCE", ""},
    } {
        t.Run(c.key+c.value,func(t *testing.T) {
            t.Setenv(c.key,c.value)
            if accessStatus(TOKEN,"GET","/health") != 503 { t.Fatal("bad configuration accepted") }
        })
    }
}
""".replace(
            "ENVIRONMENT",
            "\n".join(
                f"t.Setenv({json.dumps(k)}, {json.dumps(v)})" for k, v in REPLAY_JWT_ENV.items()
            ),
        ).replace("TOKEN", json.dumps(token))
    )
    _run(["go", "test", "-p=2", "./..."], output)


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("name", ["len", "set", "type", "str", "int"])
def test_jwt_builtin_shadow_blocks(tmp_path: Path, framework: str, name: str) -> None:
    text = (
        jwt_source(framework)
        .replace("def health(", f"def {name}(")
        .replace('path("health", health)', f'path("health", {name})')
    )
    (tmp_path / "app.py").write_text(text)
    assert capture(tmp_path, configuration({"source_framework": framework}))["gaps"]
