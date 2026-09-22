# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Security contracts are preserved through actual source and generated clients."""

from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from test_golang_write_parity import requires_database
from test_python_to_golang import source


def access_body(framework: str) -> str:
    def denied(status: int, message: str) -> str:
        headers = ', headers={"WWW-Authenticate": "Bearer"}' if status == 401 else ""
        if framework == "fastapi":
            return f'return JSONResponse(status_code={status}, content={{"detail": "{message}"}}{headers})'
        if framework == "drf":
            return f'return JsonResponse({{"error": "{message}"}}, status={status}{headers})'
        suffix = ', {"WWW-Authenticate": "Bearer"}' if status == 401 else ""
        return f'return jsonify({{"error": "{message}"}}), {status}{suffix}'

    return f"""token = request.headers.get("Authorization", "").strip(" \\t")
reader = environ.get("AUTH_READ_TOKEN", "")
writer = environ.get("AUTH_WRITE_TOKEN", "")
if not reader or not writer or not reader.isascii() or not writer.isascii() or reader == writer:
    {denied(503, "authentication unavailable")}
is_reader = compare_digest(token.encode("utf-8"), ("Bearer " + reader).encode("utf-8"))
is_writer = compare_digest(token.encode("utf-8"), ("Bearer " + writer).encode("utf-8"))
if not is_reader and not is_writer:
    {denied(401, "not authenticated")}
if request.method not in ("GET", "HEAD", "OPTIONS") and not is_writer:
    {denied(403, "permission denied")}
"""


def secured_source(framework: str, base: str | None = None, headers_outside: bool = True) -> str:
    text = source(framework) if base is None else base
    imports = "from os import environ\nfrom hmac import compare_digest\n"
    # Source fixtures already import environ when they use a database.
    text = text.replace("from os import environ\n", "")
    if framework == "fastapi":
        imports += "from fastapi import Request\nfrom fastapi.responses import JSONResponse\n"
        text = text.replace("from fastapi import Request\n", "").replace(
            "from fastapi.responses import JSONResponse\n", ""
        )
        access = (
            """@app.middleware("http")
async def authorize(request: Request, call_next):
"""
            + indent(access_body(framework), "    ")
            + "    return await call_next(request)\n"
        )
        headers = """@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response
"""
        return imports + text + "\n" + (access + headers if headers_outside else headers + access)
    if framework == "flask":
        import ast

        if not any(
            isinstance(n, ast.ImportFrom)
            and n.module == "flask"
            and any(a.name == "request" for a in n.names)
            for n in ast.parse(text).body
        ):
            imports += "from flask import request\n"
        return (
            imports
            + text
            + """
@app.before_request
def authorize():
"""
            + indent(access_body(framework), "    ")
            + """
@app.after_request
def response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response
"""
        )
    imports += "from django.http import JsonResponse\nfrom django.utils.decorators import decorator_from_middleware\n"
    middleware = (
        """class AccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def process_request(self, request):
"""
        + indent(access_body(framework), "        ")
        + """    def process_response(self, request, response):
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "no-store"
        return response
"""
    )
    return (
        imports
        + middleware
        + text.replace("@api_view(", "@decorator_from_middleware(AccessMiddleware)\n@api_view(")
    )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_capture_security(tmp_path: Path, framework: str, target: str) -> None:
    (tmp_path / "app.py").write_text(secured_source(framework))
    config = configuration({"source_framework": framework, "target_framework": target})
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    assert result["security"]["kind"] == "bearer-read-write"
    assert result["security"]["success_headers"]["cache-control"] == "no-store"
    assert result["security"]["denied_headers"] == (
        {} if framework == "drf" else result["security"]["success_headers"]
    )
    assert capture(tmp_path, config) == result


def test_fastapi_middleware_early_return_order(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(secured_source("fastapi", headers_outside=False))
    result = capture(tmp_path, configuration({"source_framework": "fastapi"}))
    assert result["gaps"] == []
    assert result["security"]["denied_headers"] == {}
    assert result["security"]["success_headers"]["cache-control"] == "no-store"


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_generated_security_executes(tmp_path: Path, framework: str, target: str) -> None:
    import os
    import subprocess

    from test_python_to_golang import apply

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    (tmp_path / "app.py").write_text(secured_source(framework))
    output = apply(tmp_path, framework, target)
    exchange = "response, err := app.Test(req); if err != nil { t.Fatal(err) }; defer response.Body.Close(); status, headers := response.StatusCode, response.Header"
    imports = '"net/http/httptest"'
    if target != "fiber":
        exchange = "response := httptest.NewRecorder(); app.ServeHTTP(response, req); status, headers := response.Code, response.Header()"
    denied_header = '""' if framework == "drf" else '"no-store"'
    (output / "security_test.go").write_text(
        """package backend
import ("testing"; IMPORTS)
func TestAccessPolicy(t *testing.T) {
    t.Setenv("AUTH_READ_TOKEN", "reader-fixture")
    t.Setenv("AUTH_WRITE_TOKEN", "writer-fixture")
    app := NewApp()
    for _, c := range []struct { method, token string; status int }{
        {"GET", "", 401}, {"GET", "bad", 401}, {"GET", "bearer reader-fixture", 401},
        {"GET", "Bearer reader-fixture ", 200}, {"GET", "Bearer reader-fixture", 200},
        {"POST", "Bearer reader-fixture", 403}, {"GET", "Bearer writer-fixture", 200},
    } {
        req := httptest.NewRequest(c.method, "/health", nil)
        req.Header.Set("Authorization", c.token)
        EXCHANGE
        if status != c.status { t.Fatalf("%s: status %d != %d", c.method, status, c.status) }
        expected := "no-store"; if c.status != 200 { expected = DENIED_HEADER }
        if headers.Get("Cache-Control") != expected { t.Fatal("middleware order changed") }
        challenge := ""; if status == 401 { challenge = "Bearer" }
        if headers.Get("WWW-Authenticate") != challenge { t.Fatal("challenge changed") }
    }
    DUPLICATE_CHECK
    for _, value := range []string{"", "reader-fixture", "読者"} {
        t.Setenv("AUTH_WRITE_TOKEN", value)
        req := httptest.NewRequest("GET", "/health", nil)
        req.Header.Set("Authorization", "Bearer reader-fixture")
        EXCHANGE
        _ = headers
        if status != 503 { t.Fatal("invalid credential configuration accepted") }
    }
}
""".replace("IMPORTS", imports)
        .replace("EXCHANGE", exchange)
        .replace("DENIED_HEADER", denied_header)
        .replace(
            "DUPLICATE_CHECK",
            (
                """{
        req := httptest.NewRequest("GET", "/health", nil)
        req.Header.Set("Authorization", "Bearer writer-fixture")
        req.Header.Add("Authorization", "bad")
        EXCHANGE
        _ = headers
        if status != EXPECTED { t.Fatalf("duplicate authorization: %d", status) }
    }""".replace("EXCHANGE", exchange).replace("EXPECTED", "401" if framework == "flask" else "200")
            )
            if framework != "drf"
            else "",
        )
    )
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "before,after",
    [
        ("compare_digest(token.encode", "custom_compare(token.encode"),
        ('("GET", "HEAD", "OPTIONS")', '("GET", "POST")'),
        ("reader == writer", "reader != writer"),
        ('.strip(" \\t")', ""),
        ('"AUTH_READ_TOKEN"', '"DATABASE_URL"'),
        ('"no-store"', '"bad\\r\\nheader"'),
        ('"Cache-Control"', '"Content-Length"'),
    ],
)
def test_security_changes_block(tmp_path: Path, framework: str, before: str, after: str) -> None:
    text = secured_source(framework)
    assert before in text
    (tmp_path / "app.py").write_text(text.replace(before, after))
    try:
        result = capture(tmp_path, configuration({"source_framework": framework}))
    except SyntaxError:
        return
    assert result["gaps"]


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_security_public_verify(tmp_path: Path, framework: str, target: str) -> None:
    import os

    from sanka_extension_python_to_golang.replay import replay
    from test_python_to_golang import apply

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    (tmp_path / "app.py").write_text(secured_source(framework))
    output = apply(tmp_path, framework, target)
    captured = capture(
        tmp_path, configuration({"source_framework": framework, "target_framework": target})
    )
    report = replay(tmp_path, output, captured, "verify")
    assert report["ok"], report
    assert {item["status"] for item in report["candidate"]} == {200, 401}
    assert report["security_headers"]["ok"]
    if target == "fiber":
        path = output / "security.go"
        path.write_text(path.read_text().replace('"no-store"', '"public"'))
        report = replay(tmp_path, output, captured, "verify")
        assert not report["ok"]
        assert not report["security_headers"]["ok"]


def test_security_database_capture_and_scenarios(tmp_path: Path) -> None:
    from sanka_extension_python_to_golang.security import security_cases
    from test_golang_drf_validation import drf_serializer_source
    from test_golang_schema import generate
    from test_golang_validation import schema_source

    from sanka_http_replay import cases_document, validate_scenarios

    for framework in SOURCES:
        root = tmp_path / framework
        root.mkdir()
        base = drf_serializer_source() if framework == "drf" else schema_source(framework)
        generate(root, framework, "fiber", app_source=secured_source(framework, base))
    cases = [
        {"id": "create", "method": "POST", "path": "/widgets", "body": {}, "expected_status": 400}
    ]
    expanded = security_cases(cases)
    assert validate_scenarios(cases_document(expanded)) == expanded
    assert [c["expected_status"] for c in expanded] == [400, 401, 401, 403]
    with pytest.raises(ValueError, match="synthetic Authorization"):
        security_cases([dict(cases[0], headers={"authorization": "private"})])


@requires_database
@pytest.mark.parametrize("policy", ["bearer", "jwt", "native", "scoped-tenant", "scoped-owner"])
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_security_database_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str, policy: str
) -> None:
    import json
    import os
    import uuid

    import psycopg
    from psycopg import sql
    from sanka_extension_python_to_golang.replay import replay
    from test_golang_schema import schema_dsn

    output, captured = prepare_security_fixture(tmp_path, framework, target, policy)
    assert not captured["gaps"], captured["gaps"]
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    created = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for _ in range(2):
                name = "go_security_" + uuid.uuid4().hex
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
                created.append(name)
            source_url, target_url = [schema_dsn(dsn, name) for name in created]
            if framework != "drf":
                source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", source_url)
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            report = replay(tmp_path, output, captured, "verify")
            assert report["ok"], json.dumps(report)
            assert report["denied_writes_unchanged"]
            assert report["security_headers"]["ok"]
            assert {401, 403, 201, 204} <= {c["status"] for c in report["candidate"]}
            if framework == "fastapi" and target == "fiber":
                if policy.startswith("scoped-"):
                    import re

                    app = output / "app.go"
                    original_app = app.read_text()
                    lines = original_app.splitlines(keepends=True)
                    for index, line in enumerate(lines):
                        if "UPDATE" in line and "RETURNING" in line and "fmt.Sprintf" not in line:
                            lines[index] = re.sub(
                                r'AND \\"name\\" = (\$\d+)', r"AND (\1::text IS NOT NULL)", line
                            )
                    changed = "".join(lines)
                    assert changed != original_app
                    app.write_text(changed)
                    missing_scope = replay(tmp_path, output, captured, "verify")
                    assert not missing_scope["ok"]
                    # compare() reports only the first JSON difference. A leaked
                    # update changes both the response and rows; inspect its evidence
                    # directly rather than depending on which difference is reported.
                    assert any(
                        candidate["method"] == "PUT"
                        and ".cross-row-" in candidate["id"]
                        and candidate["status"] == 200
                        and source["status"] == 404
                        and candidate["tables"] != source["tables"]
                        for candidate, source in zip(
                            missing_scope["candidate"], missing_scope["source"], strict=True
                        )
                    ), missing_scope["steps"]
                    app.write_text(original_app)
                path = output / "security.go"
                original = path.read_text()
                path.write_text(
                    original.replace("return nil, 403", "return nil, 0")
                    if policy in {"native", "scoped-tenant", "scoped-owner"}
                    else original.replace("return 403", "return 0")
                )
                tampered = replay(tmp_path, output, captured, "verify")
                assert not tampered["ok"]
                if policy == "bearer" or policy == "jwt":
                    path.write_text(original.replace('"no-store"', '"public"'))
                    tampered = replay(tmp_path, output, captured, "verify")
                    assert not tampered["ok"]
                    assert not tampered["security_headers"]["ok"]
        finally:
            for name in created:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("policy", ["bearer", "jwt", "native"])
def test_security_write_probe_compiles(tmp_path: Path, target: str, policy: str) -> None:
    import os

    from sanka_extension_python_to_golang.replay import _run
    from sanka_extension_python_to_golang.security import header_probe
    from sanka_extension_python_to_golang.write_replay import write_probe
    from test_golang_schema import generate
    from test_golang_validation import schema_source

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    from test_golang_identity import identity_source
    from test_golang_jwt import jwt_source

    source_factory = {"jwt": jwt_source, "native": identity_source, "bearer": secured_source}[
        policy
    ]
    output = generate(
        tmp_path, "fastapi", target, app_source=source_factory("fastapi", schema_source("fastapi"))
    )
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": "fastapi", "target_framework": target, "database_layer": "pgx"}
        ),
    )
    (output / "sanka_contract_probe_test.go").write_text(
        header_probe(write_probe(captured), captured)
    )
    _run(["go", "test", "-p=2", "-run", "^$", "./..."], output)


def prepare_security_fixture(
    root: Path, framework: str, target: str, policy: str = "bearer"
) -> tuple[Path, dict]:
    import tempfile

    if policy.startswith("scoped-"):
        from test_golang_row_security import prepare_scoped_fixture

        return prepare_scoped_fixture(
            root, framework, target, "tenant" if policy == "scoped-tenant" else "sub"
        )

    from test_golang_schema import generate
    from test_golang_shared_replay import prepare_write_fixture

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        _, captured = prepare_write_fixture(base, framework, target)
        (root / "sanka-verify.json").write_bytes((base / "sanka-verify.json").read_bytes())
        from test_golang_identity import identity_source
        from test_golang_jwt import jwt_source

        source_factory = {"jwt": jwt_source, "native": identity_source, "bearer": secured_source}[
            policy
        ]
        text = source_factory(framework, (base / "app.py").read_text())
    output = generate(root, framework, target, app_source=text)
    return output, capture(root, captured["configuration"])


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("policy", ["bearer", "jwt", "native"])
def test_security_database_fixture_capture(tmp_path: Path, framework: str, policy: str) -> None:
    _, captured = prepare_security_fixture(tmp_path, framework, "fiber", policy)
    assert not captured["gaps"]


@pytest.mark.parametrize("framework,status", [("flask", 401), ("fastapi", 200)])
def test_source_duplicate_authorization(tmp_path: Path, framework: str, status: int) -> None:
    import json
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _client_lifecycle, _run

    path = tmp_path / "app.py"
    path.write_text(secured_source(framework))
    observed = tmp_path / "observed.json"
    requests = [
        {
            "path": "/health",
            "headers": [["Authorization", "Bearer writer-fixture"], ["Authorization", "bad"]],
        }
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
        environment={
            "AUTH_READ_TOKEN": "reader-fixture",
            "AUTH_WRITE_TOKEN": "writer-fixture",
            "SANKA_GO_REPLAY_HEADERS": "[]",
        },
    )
    assert json.loads(observed.read_text())[0]["status"] == status


def test_composed_probe_cleanup_without_header_capture(tmp_path: Path) -> None:
    import json
    import sys

    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _run

    # Existing database qualification replaces the request loop while reusing
    # setup/cleanup. Its cleanup must work without enabling header capture.
    probe = (
        SOURCE_PROBE[: SOURCE_PROBE.index("observed = []")]
        + "observed = []\n"
        + SOURCE_PROBE[SOURCE_PROBE.index("Path(destination).write_text") :]
    )
    path = tmp_path / "app.py"
    path.write_text(source("flask"))
    observed = tmp_path / "observed.json"
    _run(
        [sys.executable, "-I", "-c", probe, "flask", str(path), "[]", str(observed), "", "0"],
        tmp_path,
        environment={"SANKA_GO_REPLAY_HEADERS": "[]"},
    )
    assert json.loads(observed.read_text()) == []
