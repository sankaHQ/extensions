# SPDX-License-Identifier: Apache-2.0
"""Request parsing and parameterized PostgreSQL filters through real framework clients."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS
from sanka_extension_python_to_golang.replay import SOURCE_PROBE, request_paths
from test_golang_reads import read_source
from test_golang_schema import SOURCE_DDL, generate, model_source, schema_dsn
from test_python_to_golang import PAYLOAD, request, source


def filter_source(framework: str, *, default: str = "first") -> str:
    text = read_source(framework)
    if framework == "drf":
        return text.replace(
            ".objects.order_by",
            f'.objects.filter(name=request.query_params.get("q", {default!r})).order_by',
        )
    if framework == "flask":
        text = text.replace(
            "from flask import Flask, jsonify", "from flask import Flask, jsonify, request"
        )
        value = f'request.args.get("q", {default!r})'
    else:
        text = text.replace("def health():", f"def health(q: str = {default!r}):")
        value = "q"
    return text.replace(
        ".order_by(Widget.id)", f".where(Widget.name == {value}).order_by(Widget.id)"
    )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_filter_generation_and_native_query_parsing(
    tmp_path: Path, framework: str, target: str
) -> None:
    output = generate(
        tmp_path, framework, target, app_source=filter_source(framework, default="😀")
    )
    planned = json.loads((tmp_path / ".sanka/go/plan.json").read_text())
    route = planned["capture"]["routes"][0]
    assert route["read"]["filter"] == {"field": "name", "parameter": "q", "default": "😀"}
    assert 'WHERE \\"name\\" = $2' in (output / "app.go").read_text()
    if os.getenv("SANKA_GO_TESTS") != "1":
        return
    # Read values from the actual source framework, without needing a database.
    echo = source(framework)
    expression = 'request.query_params.get("q", "😀")'
    if framework == "flask":
        echo = echo.replace(
            "from flask import Flask, jsonify", "from flask import Flask, jsonify, request"
        )
        expression = 'request.args.get("q", "😀")'
    elif framework == "fastapi":
        echo = echo.replace("def health():", 'def health(q: str = "😀"):')
        expression = "q"
    echo = echo.replace(repr(PAYLOAD), '{"value": ' + expression + "}")
    echo_path = tmp_path / ".sanka/echo.py"
    echo_path.write_text(echo)
    observed = tmp_path / ".sanka/observed.json"
    paths = request_paths(route)
    # Exercise decoder boundaries against native clients, including invalid UTF-8 prefixes.
    encoded = [bytes([value]) for value in range(256)]
    encoded += [
        bytes([first, second])
        for first in (0xC2, 0xE0, 0xED, 0xEF, 0xF0, 0xF4)
        for second in (0x7F, 0x80, 0x8F, 0x90, 0x9F, 0xA0, 0xBF, 0xC0, 0xFF)
    ]
    paths.extend("/health?q=" + "".join(f"%{value:02X}" for value in chunk) for chunk in encoded)
    paths.extend(
        "/health?" + raw
        for raw in (
            "q=first&q",
            "q=first&&q=last",
            "q[]=first",
            "%71=first&q=last",
            "q=a=b",
            "q=%",
            "q=%0",
            "q=++",
            "q=%2b",
            "q=%ff",
            "q%FF=first",
        )
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            SOURCE_PROBE,
            framework,
            str(echo_path),
            json.dumps(paths),
            str(observed),
            "",
            "0",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    values = json.loads(observed.read_text())
    cases = ",\n".join(
        "{"
        + json.dumps(item["path"].partition("?")[2], ensure_ascii=False)
        + ", "
        + json.dumps(item["body"]["value"], ensure_ascii=False)
        + "}"
        for item in values
    )
    (output / "query_test.go").write_text(
        """package backend
import "testing"
func TestNativeQueryParity(t *testing.T) {
    cases := [][2]string{"""
        + cases
        + """,}
    for _, item := range cases {
        if got := queryValue(item[0], "q", "😀"); got != item[1] {
            t.Errorf("query %q: got %q, want %q", item[0], got, item[1])
        }
    }
}
"""
    )
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("change", ["numeric", "operator", "lookup", "untyped", "shadow", "unused"])
def test_unsupported_filter_semantics_block(tmp_path: Path, framework: str, change: str) -> None:
    text = filter_source(framework)
    if change == "numeric":
        text = text.replace("filter(name=", "filter(count=").replace(
            "where(Widget.name", "where(Widget.count"
        )
    elif change == "operator":
        text = text.replace("filter(name=", "exclude(name=").replace(
            "Widget.name ==", "Widget.name !="
        )
    elif change == "lookup":
        text = text.replace("filter(name=", "filter(name__contains=").replace(
            "Widget.name == q", "Widget.name.contains(q)"
        )
        text = text.replace(
            "Widget.name == request.args.get(\"q\", 'first')",
            "Widget.name.contains(request.args.get(\"q\", 'first'))",
        )
    elif change == "untyped":
        text = (
            text.replace("q: str =", "q =")
            if framework == "fastapi"
            else text.replace(".get(\"q\", 'first')", '.get("q")')
        )
    elif change == "shadow":
        text = (
            text.replace("q: str =", "engine: str =").replace(
                "Widget.name == q", "Widget.name == engine"
            )
            if framework == "fastapi"
            else text + "\ndef str():\n    return {}\n"
        )
    else:
        text = text.replace(".filter(name=request.query_params.get(\"q\", 'first'))", "")
        text = text.replace(".where(Widget.name == request.args.get(\"q\", 'first'))", "")
        text = text.replace(".where(Widget.name == q)", "")
        # Additional unused request parameters are not silently dropped.
        if framework != "fastapi":
            text = text.replace("def health(request):", "def health(request, q='first'):").replace(
                "def health():", "def health(q='first'):"
            )
    (tmp_path / "app.py").write_text(text)
    (tmp_path / "models.py").write_text(model_source(framework))
    result = handle(
        dataclasses.replace(
            request(tmp_path, framework),
            configuration={"source_framework": framework, "database_layer": "pgx"},
        )
    )
    assert result.data["capture"]["gaps"], text
    assert result.data["files"] == {}


@pytest.mark.skipif(
    not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN") or os.getenv("SANKA_GO_TESTS") != "1",
    reason="requires explicit PostgreSQL test DSN and Go qualification",
)
@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_postgres_filtered_http_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, target: str
) -> None:
    import psycopg
    from psycopg import sql

    text = filter_source(framework)
    # Qualify both non-null varchar and nullable text predicates in the same app.
    start = text.index("@api_view" if framework == "drf" else "@app.get")
    end = text.index("urlpatterns") if framework == "drf" else len(text)
    notes = text[start:end].replace("def health(", "def notes(").replace('"/health"', '"/notes"')
    notes = notes.replace("filter(name=", "filter(note=").replace(
        "where(Widget.name", "where(Widget.note"
    )
    text = text[:end] + notes + text[end:]
    text = text.replace(
        '[path("health", health)]', '[path("health", health), path("notes", notes)]'
    )
    output = generate(tmp_path, framework, target, app_source=text)
    dsn = os.environ["SANKA_MIGRATE_TEST_POSTGRES_DSN"]
    schemas = ["go_filter_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            for schema in schemas:
                admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_url, target_url = [schema_dsn(dsn, schema) for schema in schemas]
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SOURCE_DDL,
                    framework,
                    str(tmp_path / "models.py"),
                    source_url,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ["go", "run", "-mod=readonly", "-p=2", "./cmd/migrate", "up"],
                cwd=output,
                env=os.environ
                | {
                    "GOTOOLCHAIN": "local",
                    "GOWORK": "off",
                    "GOMAXPROCS": "2",
                    "DATABASE_URL": target_url,
                },
                check=True,
                capture_output=True,
                timeout=180,
            )
            names = [
                "first",
                "last",
                "",
                "sanka-filter",
                "日本語",
                "😀",
                "x' OR '1'='1",
                "a;b",
                "a b",
                "a+b",
                "SANKA-FILTER",
                "%ZZ",
                "�",
                "%FF",
                "%E2%82",
                "���",
            ]
            for url in (source_url, target_url):
                with psycopg.connect(url, autocommit=True) as connection:
                    for index, name in enumerate(names, 1):
                        connection.execute(
                            "INSERT INTO widgets (id,name,count,enabled,note) "
                            "VALUES (%s,%s,1,true,%s)",
                            (index, name, None if index == 1 else "first"),
                        )
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", target_url)
            monkeypatch.setenv(
                "SANKA_GO_SOURCE_TEST_DATABASE_URL",
                source_url
                if framework == "drf"
                else source_url.replace("postgresql://", "postgresql+psycopg://", 1),
            )
            req = dataclasses.replace(
                request(tmp_path, framework, target),
                command="verify",
                configuration={
                    "source_framework": framework,
                    "target_framework": target,
                    "database_layer": "pgx",
                },
            )
            verified = handle(req)
            assert verified.outcome == "success", verified.error
            responses = {item["path"]: item["body"] for item in verified.data["candidate"]}
            assert responses["/health"][0]["name"] == "first"
            assert [row["id"] for row in responses["/notes"]] == [2, 3]
            for name in names[:12]:
                assert [row["name"] for row in responses["/health?" + urlencode({"q": name})]] == [
                    name
                ]
            repeated = "/health?" + urlencode([("q", "first"), ("q", "last")])
            assert responses[repeated][0]["name"] == ("first" if framework == "flask" else "last")
            with psycopg.connect(target_url, autocommit=True) as connection:
                assert connection.execute("SELECT count(*) FROM widgets").fetchone() == (
                    len(names),
                )
                connection.execute("UPDATE widgets SET enabled=false WHERE name='first'")
            assert handle(req).outcome == "error"
        finally:
            for schema in schemas:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


@pytest.mark.parametrize("name", ["pk", "objects", "name__contains", "name_"])
def test_django_field_names_cannot_override_lookup_semantics(tmp_path: Path, name: str) -> None:
    (tmp_path / "app.py").write_text(filter_source("drf"))
    (tmp_path / "models.py").write_text(model_source("drf").replace("    name =", f"    {name} ="))
    planned = handle(
        dataclasses.replace(
            request(tmp_path, "drf"),
            configuration={"source_framework": "drf", "database_layer": "pgx"},
        )
    )
    assert planned.data["files"] == {}
    assert any("ORM lookup or manager" in gap for gap in planned.data["capture"]["gaps"])
