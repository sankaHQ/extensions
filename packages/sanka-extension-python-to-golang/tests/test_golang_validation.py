# SPDX-License-Identifier: Apache-2.0
"""Strict request schemas reuse the qualified write contract without coercion."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import TARGETS, capture, configuration
from sanka_extension_python_to_golang.render import render
from test_golang_schema import model_source
from test_golang_writes import fastapi_write_source, flask_write_source, widget_validation


def schema_source(framework: str) -> str:
    source = (flask_write_source if framework == "flask" else fastapi_write_source)()
    definitions = "from pydantic import BaseModel, ConfigDict, Field, ValidationError\n"
    for partial, name in [(False, "WidgetInput"), (True, "WidgetPatch")]:
        definitions += f"""class {name}(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    name: str = Field({"default=None" if partial else ""})
    count: int = Field({"default=None, " if partial else ""}ge=-2147483648, le=2147483647)
    enabled: bool = Field({"default=None" if partial else ""})
    note: str | None = Field(default=None)
"""
        error = (
            'return jsonify({"error": "invalid request body"}), 400'
            if framework == "flask"
            else 'raise HTTPException(status_code=400, detail="invalid request body")'
        )
        source = source.replace(
            indent(widget_validation("data", partial, error), "    "),
            indent(
                f"try:\n    data = {name}.model_validate(data).model_dump(exclude_unset=True)\n"
                f"except ValidationError:\n    {error}\n",
                "    ",
            ),
        )
    return definitions + source


def native_fastapi_schema_source() -> str:
    source = fastapi_write_source()
    definitions = """from pydantic import BaseModel, Field
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
class WidgetInput(BaseModel):
    name: str
    count: int = Field(ge=-2147483648, le=2147483647)
    enabled: bool
    note: str | None = None
class WidgetPatch(BaseModel):
    name: str = None
    count: int = Field(default=None, ge=-2147483648, le=2147483647)
    enabled: bool = None
    note: str | None = None
def invalid_request(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "invalid request body"})
"""
    source = source.replace(
        "app = FastAPI()",
        "app = FastAPI(exception_handlers={RequestValidationError: invalid_request})",
    )
    for partial, name in [(False, "WidgetInput"), (True, "WidgetPatch")]:
        error = 'raise HTTPException(status_code=400, detail="invalid request body")'
        source = source.replace(
            indent(widget_validation("data", partial, error), "    "),
            "",
        )
        signature = "id: int, data: dict" if partial else "data: dict"
        source = source.replace(
            f"def {'patch_widget' if partial else 'create_widget'}({signature}):\n",
            f"def {'patch_widget' if partial else 'create_widget'}("
            + ("id: int, " if partial else "")
            + f"data: {name}):\n    data = data.model_dump(exclude_unset=True)\n",
        )
    source = source.replace(
        indent(widget_validation("data", False, error), "    "),
        "",
    ).replace(
        "def replace_widget(id: int, data: dict):\n",
        "def replace_widget(id: int, data: WidgetInput):\n"
        "    data = data.model_dump(exclude_unset=True)\n",
    )
    return definitions + source


def captured_source(root: Path, framework: str, target: str = "fiber", text: str | None = None):
    (root / "app.py").write_text(text if text is not None else schema_source(framework))
    (root / "models.py").write_text(model_source(framework))
    return capture(
        root,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
def test_strict_schema_reuses_write_contract(tmp_path: Path, framework: str, target: str) -> None:
    captured = captured_source(tmp_path, framework, target)
    assert captured["gaps"] == []
    manual = captured_source(
        tmp_path,
        framework,
        target,
        (flask_write_source if framework == "flask" else fastapi_write_source)(),
    )
    assert captured["routes"] == manual["routes"]
    assert captured["source_digest"] != manual["source_digest"]
    assert captured["models"] == manual["models"]
    # Generated source uses the existing decoder; only provenance hashes differ.
    assert render(captured)["app.go"] == render(manual)["app.go"]


@pytest.mark.parametrize("target", TARGETS)
def test_native_fastapi_schema_capture(tmp_path: Path, target: str) -> None:
    captured = captured_source(tmp_path, "fastapi", target, native_fastapi_schema_source())
    assert captured["gaps"] == []
    validations = [route["write"]["validation"] for route in captured["routes"][:3]]
    assert validations == [
        {
            "kind": "pydantic",
            "schema": "WidgetInput",
            "partial": False,
            "error": {"status": 422, "body": {"detail": "invalid request body"}},
        },
        {
            "kind": "pydantic",
            "schema": "WidgetPatch",
            "partial": True,
            "error": {"status": 422, "body": {"detail": "invalid request body"}},
        },
        {
            "kind": "pydantic",
            "schema": "WidgetInput",
            "partial": False,
            "error": {"status": 422, "body": {"detail": "invalid request body"}},
        },
    ]
    assert capture(tmp_path, captured["configuration"]) == captured


@pytest.mark.parametrize("target", TARGETS)
def test_native_fastapi_uses_captured_validation_error(tmp_path: Path, target: str) -> None:
    captured = captured_source(tmp_path, "fastapi", target, native_fastapi_schema_source())
    app = render(captured)["app.go"]
    assert "invalid request body" in app
    assert "422" in app


@pytest.mark.parametrize(
    "before,after",
    [
        ("status_code=422", "status_code=400"),
        ('{"detail": "invalid request body"}', '{"error": "invalid request body"}'),
        ("ge=-2147483648", "ge=-2147483649"),
        ("le=2147483647", "le=2147483648"),
        ("exclude_unset=True", "exclude_unset=False"),
        ("from pydantic import BaseModel, Field", "from counterfeit import BaseModel, Field"),
    ],
)
def test_unqualified_native_pydantic_semantics_block(
    tmp_path: Path, before: str, after: str
) -> None:
    source = native_fastapi_schema_source().replace(before, after, 1)
    assert captured_source(tmp_path, "fastapi", text=source)["gaps"]


def test_native_fastapi_source_validation_error(tmp_path: Path) -> None:
    captured_source(tmp_path, "fastapi", text=native_fastapi_schema_source())
    script = """
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
for body in ({}, {"name": 1, "count": "bad", "enabled": []}, []):
    response = client.post('/widgets', json=body)
    assert response.status_code == 422, (body, response.status_code)
    assert response.json() == {"detail": "invalid request body"}
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {"DATABASE_URL": "sqlite://"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
@pytest.mark.parametrize("target", TARGETS)
def test_generated_native_fastapi_validation_error(tmp_path: Path, target: str) -> None:
    captured = captured_source(tmp_path, "fastapi", target, native_fastapi_schema_source())
    output = tmp_path / "candidate"
    for name, contents in render(captured).items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents)
    exchange = """response, err := app.Test(request)
        if err != nil { t.Fatal(err) }
        body, err := io.ReadAll(response.Body); response.Body.Close()
        if err != nil { t.Fatal(err) }
        status := response.StatusCode"""
    extra_import = '; "io"'
    if target != "fiber":
        exchange = """response := httptest.NewRecorder()
        app.ServeHTTP(response, request)
        body, status := response.Body.Bytes(), response.Code"""
        extra_import = ""
    (output / "native_validation_test.go").write_text(
        f'''package backend
import ("net/http/httptest"; "strings"; "testing"; "github.com/jackc/pgx/v5/pgxpool"{extra_import})
func TestNativeValidationError(t *testing.T) {{
    app := NewApp(&pgxpool.Pool{{}})
    for _, payload := range []string{{`{{}}`, `[]`, `{{"name":1,"count":"bad","enabled":[]}}`}} {{
        request := httptest.NewRequest("POST", "/widgets", strings.NewReader(payload))
        request.Header.Set("Content-Type", "application/json")
        {exchange}
        invalidBody := strings.TrimSpace(string(body)) != `{{"detail":"invalid request body"}}`
        if status != 422 || invalidBody {{
            t.Fatalf("status=%d body=%s", status, body)
        }}
    }}
}}
'''
    )
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "TestNativeValidationError", "."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
@pytest.mark.parametrize("target", TARGETS)
def test_native_pydantic_and_go_decoder_agree(tmp_path: Path, target: str) -> None:
    from pydantic import BaseModel, Field, ValidationError

    class WidgetInput(BaseModel):
        name: str
        count: int = Field(ge=-2147483648, le=2147483647)
        enabled: bool
        note: str | None = None

    class WidgetPatch(BaseModel):
        name: str = None
        count: int = Field(default=None, ge=-2147483648, le=2147483647)
        enabled: bool = None
        note: str | None = None

    payloads = [
        *validation_cases(),
        {"name": "coerced", "count": "1", "enabled": "false", "extra": 1},
        {"name": "coerced", "count": 1.0, "enabled": 1},
        {"name": "coerced", "count": "1.0", "enabled": "YES"},
        {"name": "coerced", "count": True, "enabled": 0.0},
        {"name": "invalid", "count": 1, "enabled": " true "},
        {"name": "invalid", "count": "1e2", "enabled": "maybe"},
        {"name": "invalid", "count": 1.5, "enabled": 2},
    ]
    cases = []
    for partial, schema in [(False, WidgetInput), (True, WidgetPatch)]:
        for payload in payloads:
            try:
                result = schema.model_validate(payload).model_dump(exclude_unset=True)
            except ValidationError:
                result = None
            cases.append(
                {
                    "body": json.dumps(payload),
                    "partial": partial,
                    "valid": result is not None,
                    "expected": result,
                }
            )
    captured = captured_source(tmp_path, "fastapi", target, native_fastapi_schema_source())
    assert_go_decoder_parity(tmp_path, captured, cases, "decodeWidget_pydantic")


@pytest.mark.parametrize(
    "before,after",
    [
        ("strict=True", "strict=False"),
        ("WidgetInput", "str"),
        ("WidgetInput", "data"),
        ("WidgetInput", "item"),
        ("WidgetInput", "session"),
        ("extra='forbid'", "extra='ignore'"),
        ("le=2147483647", "le=2147483648"),
        ("name: str", "name: int"),
        ("name: str", "name: str | None"),
        ("Field(default=None)", "Field(default='default')"),
        ("exclude_unset=True", "exclude_unset=False"),
        ("except ValidationError:", "except Exception:"),
        ("BaseModel):", "BaseModel, object):"),
        ("class WidgetInput", "@custom\nclass WidgetInput"),
        ("    model_config", "    def custom(self):\n        return 1\n    model_config"),
        ("name: str = Field()", "name: str = Field(min_length=-1)"),
        ("name: str = Field()", "name: str = Field(min_length=True)"),
        ("name: str = Field()", "name: str = Field(max_length=9223372036854775808)"),
        ("name: str = Field()", "name: str = Field(min_length=4, max_length=2)"),
        ("ge=-2147483648, le=2147483647", "ge=4, le=2"),
        ("name: str = Field()", "name: str = Field(alias='title')"),
        ("name: str = Field()", "name: list[str] = Field()"),
        ("from pydantic import BaseModel", "from counterfeit import BaseModel"),
        ("from pydantic import BaseModel", "from pydantic import BaseModel as Other"),
    ],
)
def test_unqualified_schema_semantics_block(tmp_path: Path, before: str, after: str) -> None:
    captured = captured_source(
        tmp_path, "fastapi", text=schema_source("fastapi").replace(before, after)
    )
    assert captured["gaps"]


def test_imported_schemas_are_captured_and_hashed(tmp_path: Path) -> None:
    source = schema_source("fastapi")
    marker = source.index("from fastapi")
    (tmp_path / "schemas.py").write_text(source[:marker])
    captured = captured_source(
        tmp_path,
        "fastapi",
        text="from schemas import WidgetInput, WidgetPatch\nfrom pydantic import ValidationError\n"
        + source[marker:],
    )
    assert captured["gaps"] == []
    assert captured["source_modules"] == ["schemas.py"]
    (tmp_path / "schemas.py").write_text(source[:marker].replace("strict=True", "strict=False"))
    changed = capture(tmp_path, captured["configuration"])
    assert changed["gaps"]
    assert changed["source_digest"] != captured["source_digest"]


def validation_cases():
    valid = {"name": "", "count": 0, "enabled": False}
    payloads = [
        valid,
        valid | {"note": None},
        valid | {"note": ""},
        {},
        None,
        [],
        valid | {"extra": 1},
    ]
    for field in valid:
        payloads.append({key: value for key, value in valid.items() if key != field})
    for field, values in {
        "name": [None, True, 1, [], {}, "日本語"],
        "count": [
            None,
            True,
            False,
            "1",
            1.0,
            1.5,
            -2147483649,
            -2147483648,
            2147483647,
            2147483648,
        ],
        "enabled": [None, 0, 1, "true", "false", True],
        "note": [False, 0, [], {}],
    }.items():
        payloads.extend(valid | {field: value} for value in values)
    return payloads


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("bits", [32, 64])
def test_python_schema_and_go_decoder_agree(
    tmp_path: Path, framework: str, target: str, bits: int
) -> None:
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires qualified Go toolchain and source framework environment")
    from pydantic import ValidationError

    captured = captured_source(tmp_path, framework, target)
    assert captured["gaps"] == []
    original = schema_source(framework)
    if bits == 64:
        original = original.replace("-2147483648", "-9223372036854775808").replace(
            "2147483647", "9223372036854775807"
        )
        (tmp_path / "app.py").write_text(original)
        (tmp_path / "models.py").write_text(
            model_source(framework).replace("mapped_column(Integer)", "mapped_column(BigInteger)")
        )
        captured = capture(tmp_path, captured["configuration"])
        assert captured["gaps"] == []
    # Execute only the fixture's original schema in the test environment. Capture
    # itself never imports Pydantic or executes user code.
    tree = ast.parse(original)
    namespace: dict = {}
    schemas = ast.Module(
        body=[
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            or (isinstance(node, ast.ImportFrom) and node.module == "pydantic")
        ],
        type_ignores=[],
    )
    exec(compile(schemas, "schemas.py", "exec"), namespace)
    cases = []
    for partial, name in [(False, "WidgetInput"), (True, "WidgetPatch")]:
        payloads = validation_cases() + [
            {"name": "limit", "count": count, "enabled": False}
            for count in [
                -(2 ** (bits - 1)) - 1,
                -(2 ** (bits - 1)),
                2 ** (bits - 1) - 1,
                2 ** (bits - 1),
            ]
        ]
        for payload in payloads:
            try:
                result = namespace[name].model_validate(payload).model_dump(exclude_unset=True)
            except ValidationError:
                result = None
            cases.append(
                {
                    "body": json.dumps(payload),
                    "partial": partial,
                    "valid": result is not None,
                    "expected": result,
                }
            )
    assert_go_decoder_parity(tmp_path, captured, cases)


def assert_go_decoder_parity(
    tmp_path: Path, captured: dict, cases: list[dict], decoder: str = "decodeWidget"
) -> None:
    output = tmp_path / "candidate"
    for name, contents in render(captured).items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents)
    (output / "validation.json").write_text(json.dumps(cases))
    (output / "validation_test.go").write_text(
        """package backend
import ("encoding/json"; "os"; "reflect"; "testing")
func TestSchemaParity(t *testing.T) {
    data, err := os.ReadFile("validation.json"); if err != nil { t.Fatal(err) }
    var cases []struct { Body string; Partial, Valid bool; Expected map[string]any }
    if err := json.Unmarshal(data, &cases); err != nil { t.Fatal(err) }
    for _, c := range cases {
        item, seen, err := decodeWidget([]byte(c.Body), c.Partial)
        if (err == nil) != c.Valid {
            t.Errorf("partial=%v %s: %v", c.Partial, c.Body, err); continue
        }
        if err != nil { continue }
        encoded, err := json.Marshal(item); if err != nil { t.Fatal(err) }
        var actual map[string]any
        if err := json.Unmarshal(encoded, &actual); err != nil { t.Fatal(err) }
        for key := range actual { if !seen[key] { delete(actual, key) } }
        if !reflect.DeepEqual(actual, c.Expected) {
            t.Errorf("%s: got %#v want %#v", c.Body, actual, c.Expected)
        }
    }
}
""".replace("decodeWidget(", decoder + "(")
    )
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "TestSchemaParity", "."],
        cwd=output,
        env=os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
def test_original_framework_rejects_invalid_objects(tmp_path: Path, framework: str) -> None:
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires source framework environment")
    captured_source(tmp_path, framework)
    # A real framework test client exercises the unchanged schema/catch path.
    # These invalid bodies return before any database access; no server is opened.
    (tmp_path / "cases.json").write_text(json.dumps(validation_cases()))
    script = """
import json
from app import app, WidgetInput, WidgetPatch
from pydantic import ValidationError
FRAMEWORK_CLIENT
for partial, schema in [(False, WidgetInput), (True, WidgetPatch)]:
    for body in json.load(open('cases.json')):
        if not isinstance(body, dict):
            continue
        try:
            schema.model_validate(body)
        except ValidationError:
            method, path = ('PATCH', '/widgets/1') if partial else ('POST', '/widgets')
            response = client.request(method, path, json=body)
            assert response.status_code == 400, (body, response.status_code)
            assert RESPONSE_JSON == {'ERROR_KEY': 'invalid request body'}
"""
    if framework == "fastapi":
        setup = "from fastapi.testclient import TestClient\nclient = TestClient(app)"
        response = "response.json()"
    else:
        setup = "client = app.test_client()"
        # Flask open takes the URL first, unlike TestClient.request.
        script = script.replace(
            "client.request(method, path, json=body)", "client.open(path, method=method, json=body)"
        )
        response = "response.get_json()"
    script = (
        script.replace("FRAMEWORK_CLIENT", setup)
        .replace("RESPONSE_JSON", response)
        .replace("ERROR_KEY", "detail" if framework == "fastapi" else "error")
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {"DATABASE_URL": "sqlite://"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.getenv("SANKA_GO_TESTS") != "1" or not os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN"),
    reason="requires qualified Go toolchain and explicit PostgreSQL fixture",
)
@pytest.mark.parametrize("framework", ["flask", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
def test_schema_generated_database_lifecycle(tmp_path: Path, framework: str, target: str) -> None:
    from test_golang_writes import test_write_lifecycle_and_database_effects

    test_write_lifecycle_and_database_effects(
        tmp_path, framework, lambda: schema_source(framework), target, True
    )


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
@pytest.mark.parametrize("target", TARGETS)
def test_field_constraints_preserve_endpoint_validation(
    tmp_path: Path, framework: str, target: str
) -> None:
    from pydantic import ValidationError

    original = (
        schema_source(framework)
        .replace("name: str = Field()", "name: str = Field(min_length=2, max_length=4)")
        .replace("ge=-2147483648, le=2147483647", "ge=-2, le=3", 1)
    )
    captured = captured_source(tmp_path, framework, target, original)
    assert captured["gaps"] == []
    creates = [
        (index, route)
        for index, route in enumerate(captured["routes"])
        if route["method"] == "POST"
    ]
    index, route = creates[0]
    assert route["write"]["constraints"] == {
        "name": {"min_length": 2, "max_length": 4},
        "count": {"ge": -2, "le": 3},
    }
    assert not next(r for r in captured["routes"] if r["method"] == "PATCH")["write"].get(
        "constraints"
    )
    assert capture(tmp_path, captured["configuration"]) == captured
    if os.getenv("SANKA_GO_TESTS") != "1":
        return
    tree = ast.parse(original)
    namespace: dict = {}
    schema = ast.Module(
        body=[
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            or (isinstance(node, ast.ImportFrom) and node.module == "pydantic")
        ],
        type_ignores=[],
    )
    exec(compile(schema, "schemas.py", "exec"), namespace)
    cases = []
    for name in ["", "a", "ab", "abcd", "abcde", "日本", "👩🏽", "e\u0301"]:
        for count in [-3, -2, 0, 3, 4]:
            payload = {"name": name, "count": count, "enabled": False}
            try:
                result = (
                    namespace["WidgetInput"].model_validate(payload).model_dump(exclude_unset=True)
                )
            except ValidationError:
                result = None
            cases.append(
                {
                    "body": json.dumps(payload),
                    "partial": False,
                    "valid": result is not None,
                    "expected": result,
                }
            )
    assert_go_decoder_parity(tmp_path, captured, cases, f"decodeWidget_write{index}")


@pytest.mark.parametrize("target", TARGETS)
def test_nullable_partial_constraints(tmp_path: Path, target: str) -> None:
    original = schema_source("fastapi").replace(
        "note: str | None = Field(default=None)",
        "note: str | None = Field(default=None, min_length=2, max_length=3)",
    )
    captured = captured_source(tmp_path, "fastapi", target, original)
    assert captured["gaps"] == []
    index = next(i for i, r in enumerate(captured["routes"]) if r["method"] == "PATCH")
    if os.getenv("SANKA_GO_TESTS") != "1":
        return
    cases = []
    for payload, valid in [
        ({}, True),
        ({"note": None}, True),
        ({"note": ""}, False),
        ({"note": "a"}, False),
        ({"note": "日本"}, True),
        ({"note": "abcd"}, False),
    ]:
        cases.append(
            {
                "body": json.dumps(payload),
                "partial": True,
                "valid": valid,
                "expected": payload if valid else None,
            }
        )
    assert_go_decoder_parity(tmp_path, captured, cases, f"decodeWidget_write{index}")


@pytest.mark.parametrize("target", TARGETS)
def test_native_field_constraints(tmp_path: Path, target: str) -> None:
    text = (
        native_fastapi_schema_source()
        .replace("ge=-2147483648", "ge=0")
        .replace("le=2147483647", "le=100")
    )
    text = text.replace("name: str\n", "name: str = Field(min_length=2, max_length=40)\n")
    text = text.replace(
        "name: str = None", "name: str = Field(default=None, min_length=2, max_length=40)"
    )
    captured = captured_source(tmp_path, "fastapi", target, text)
    assert captured["gaps"] == []
    assert captured["routes"][0]["write"]["constraints"] == {
        "name": {"min_length": 2, "max_length": 40},
        "count": {"ge": 0, "le": 100},
    }

    if os.getenv("SANKA_GO_TESTS") != "1":
        return
    from pydantic import BaseModel, Field, ValidationError

    namespace = {"BaseModel": BaseModel, "Field": Field}
    declarations = ast.Module(
        body=[n for n in ast.parse(text).body if isinstance(n, ast.ClassDef)], type_ignores=[]
    )
    exec(compile(declarations, "schemas.py", "exec"), namespace)
    cases = []
    for partial, name in [(False, "WidgetInput"), (True, "WidgetPatch")]:
        for payload in [
            {},
            {"name": "ok", "count": 1, "enabled": True},
            {"name": "a", "count": "1", "enabled": "yes"},
            {"name": "😀😀", "count": "100", "enabled": "false"},
            {"name": "valid", "count": 101, "enabled": True},
            {"name": "valid", "count": -1, "enabled": True},
            {"name": "valid", "count": True, "enabled": 1},
            {"note": None},
            {"name": None},
            {"name": "a" * 41},
        ]:
            try:
                result = namespace[name].model_validate(payload).model_dump(exclude_unset=True)
            except ValidationError:
                result = None
            cases.append(
                {
                    "body": json.dumps(payload),
                    "partial": partial,
                    "valid": result is not None,
                    "expected": result,
                }
            )
    index = next(
        i
        for i, r in enumerate(captured["routes"])
        if r.get("write", {}).get("operation") == "create"
    )
    assert_go_decoder_parity(tmp_path, captured, cases, f"decodeWidget_pydantic_write{index}")
