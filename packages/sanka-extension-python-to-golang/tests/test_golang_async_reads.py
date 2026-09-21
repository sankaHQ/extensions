# SPDX-License-Identifier: Apache-2.0
"""Async reads must reuse the qualified synchronous query contract."""

import ast
import os
import subprocess
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS
from test_golang_async_persistence import async_source, factory_source, injected_source
from test_golang_filters import filter_source
from test_golang_reads import detail_source, read_source
from test_golang_schema import generate
from test_golang_validation import captured_source, native_fastapi_schema_source


def async_read_backend(style: str = "class") -> str:
    tree = ast.parse(native_fastapi_schema_source())
    tree.body.insert(0, ast.parse("from sqlalchemy import select").body[0])
    for kind, source in [
        ("lookup", detail_source("fastapi")),
        ("list", read_source("fastapi")),
        ("filtered", filter_source("fastapi")),
        ("page", paginated_source()),
        ("filtered_page", paginated_source(True)),
    ]:
        function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef))
        function.name = f"read_{kind}"
        if kind != "lookup":
            function.decorator_list[0].args[0] = ast.Constant(value="/" + kind)
        tree.body.append(function)
    source = ast.unparse(tree)
    if style == "factory":
        return factory_source("class", "default", source)
    return async_source(source=source) if style == "owned" else injected_source(style, source)


def read_scenarios() -> list[dict]:
    cases = [
        {
            "id": f"create-{i}",
            "method": "POST",
            "path": "/widgets",
            "expected_status": 201,
            "body": {"name": name, "count": i, "enabled": True},
        }
        for i, name in enumerate(["alpha", "bravo", "charlie"], 1)
    ]
    paths = [
        ("/widgets/1", 200),
        ("/widgets/999", 404),
        ("/list", 200),
        ("/filtered?q=alpha", 200),
        ("/filtered?q=bravo&q=alpha", 200),
        ("/filtered?q=%E6%97%A5%E6%9C%AC%E8%AA%9E", 200),
        ("/page", 200),
        ("/page?limit=1&offset=1", 200),
        ("/filtered_page?q=alpha&limit=1&offset=1", 200),
        ("/page?limit=0001&offset=0000000001", 200),
        ("/page?limit=1000&offset=2147483647", 200),
        ("/page?limit=bad&limit=1", 200),
        ("/page?limit=1&limit=bad", 400),
    ]
    paths += [
        ("/page?" + query, 400)
        for query in [
            "limit=",
            "limit=0",
            "limit=-1",
            "limit=1001",
            "limit=00001",
            "limit=1.0",
            "limit=%2B1",
            "limit=%FF",
            "limit=%D9%A1",
            "offset=-1",
            "offset=2147483648",
            "offset=00000000000",
            "offset=",
            "offset=1e0",
        ]
    ]
    cases.extend(
        {"id": f"read-{i}", "method": "GET", "path": path, "expected_status": status}
        for i, (path, status) in enumerate(paths)
    )
    cases.extend(
        [
            {"id": "delete", "method": "DELETE", "path": "/widgets/1", "expected_status": 204},
            {"id": "deleted-lookup", "method": "GET", "path": "/widgets/1", "expected_status": 404},
            {
                "id": "after-delete",
                "method": "GET",
                "path": "/page?limit=1&offset=1",
                "expected_status": 200,
            },
        ]
    )
    return cases


@pytest.mark.parametrize("style", ["owned", "function", "class", "factory"])
def test_combined_read_backend(tmp_path: Path, style: str) -> None:
    assert captured_source(tmp_path, "fastapi", text=async_read_backend(style))["gaps"] == []


def test_lookup_with_annotated_session_factory(tmp_path: Path) -> None:
    source = factory_source(source=detail_source("fastapi"))
    assert captured_source(tmp_path, "fastapi", text=source)["gaps"] == []


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("target", TARGETS)
def test_pagination_compiles_and_checks_boundaries(tmp_path: Path, target: str) -> None:
    output = generate(
        tmp_path, "fastapi", target, app_source=async_source(source=paginated_source(True))
    )
    (output / "pagination_test.go").write_text("""package backend
import "testing"
func TestPaginationBoundaries(t *testing.T) {
    for _, raw := range []string{"", "0", "1001", "-1", "+1", "1.0", "\\u0661", "00001"} {
        if _, err := pageValue(raw, 4, 1, 1000); err == nil { t.Fatalf("accepted %q", raw) }
    }
    if value, err := pageValue("0001", 4, 1, 1000); err != nil || value != 1 { t.Fatal(value, err) }
    value, err := pageValue("2147483647", 10, 0, 2147483647)
    if err != nil || value != 2147483647 { t.Fatal(value, err) }
    if _, err := pageValue("2147483648", 10, 0, 2147483647); err == nil {
        t.Fatal("accepted overflow")
    }
}
""")
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def paginated_source(filtered: bool = False) -> str:
    source = filter_source("fastapi") if filtered else read_source("fastapi")
    signature = "q: str = 'first', " if filtered else ""
    start = source.index("def health(")
    end = source.index("):", start)
    source = (
        source[:start] + f'def health({signature}limit: str = "2", offset: str = "0"' + source[end:]
    )
    source = source.replace(".limit(2)", ".limit(int(limit)).offset(int(offset))")
    return source.replace(
        "        return ",
        """        if not (limit.isascii() and limit.isdecimal() and len(limit) <= 4
                and 1 <= int(limit) <= 1000 and offset.isascii() and offset.isdecimal()
                and len(offset) <= 10 and 0 <= int(offset) <= 2147483647):
            raise HTTPException(status_code=400, detail="invalid pagination")
        return """,
    ).replace("from fastapi import FastAPI", "from fastapi import HTTPException, FastAPI")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("kind", ["lookup", "list", "filter"])
@pytest.mark.parametrize("style", ["owned", "helper", "direct", "function", "class", "factory"])
def test_async_reads_match_sync(tmp_path: Path, target: str, kind: str, style: str) -> None:
    sync = {"lookup": detail_source, "list": read_source, "filter": filter_source}[kind]("fastapi")
    source = (
        async_source(style == "helper", sync)
        if style in {"owned", "helper"}
        else injected_source(style, sync)
    )
    if style == "factory":
        source = factory_source("class", "default", sync)
    expected = captured_source(tmp_path, "fastapi", target, sync)
    captured = captured_source(tmp_path, "fastapi", target, source)
    assert captured["gaps"] == []
    assert captured["routes"] == expected["routes"]
    assert captured_source(tmp_path, "fastapi", target, source) == captured


@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("style", ["owned", "direct", "function", "class"])
def test_async_pagination(tmp_path: Path, filtered: bool, style: str) -> None:
    sync = paginated_source(filtered)
    source = async_source(source=sync) if style == "owned" else injected_source(style, sync)
    captured = captured_source(tmp_path, "fastapi", text=source)
    assert captured["gaps"] == []
    assert captured["routes"][0]["read"]["pagination"] == {"limit": "2", "offset": "0"}


@pytest.mark.parametrize(
    "before,after",
    [
        ("await session.execute", "session.execute"),
        ("await session.execute", "await session.stream"),
        (".order_by(Widget.id)", ".order_by(Widget.name)"),
        (".limit(2)", ".limit(2000)"),
    ],
)
def test_changed_async_reads_block(tmp_path: Path, before: str, after: str) -> None:
    source = async_source(source=read_source("fastapi"))
    assert before in source
    assert captured_source(tmp_path, "fastapi", text=source.replace(before, after))["gaps"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("limit.isascii() and ", ""),
        ("limit.isdecimal()", "limit.isdigit()"),
        ("len(limit) <= 4", "len(limit) <= 5"),
        ("int(limit) <= 1000", "int(limit) <= 1001"),
        ("status_code=400", "status_code=422"),
        (".offset(int(offset))", ".offset(0)"),
        (".limit(int(limit))", ".limit(2)"),
        ('limit: str = "2"', 'limit: str = "0"'),
        ('offset: str = "0"', 'offset: str = "-1"'),
    ],
)
def test_changed_pagination_contract_blocks(tmp_path: Path, before: str, after: str) -> None:
    source = paginated_source()
    assert before in source
    assert captured_source(
        tmp_path, "fastapi", text=async_source(source=source.replace(before, after))
    )["gaps"]


def test_pagination_error_symbol_cannot_be_shadowed(tmp_path: Path) -> None:
    source = (
        paginated_source(True)
        .replace("q: str", "HTTPException: str")
        .replace("== q)", "== HTTPException)")
    )
    assert captured_source(tmp_path, "fastapi", text=async_source(source=source))["gaps"]


@pytest.mark.parametrize("name", ["len", "int"])
def test_request_schema_cannot_shadow_pagination_builtins(tmp_path: Path, name: str) -> None:
    source = async_read_backend().replace("WidgetInput", name)
    assert captured_source(tmp_path, "fastapi", text=source)["gaps"]
