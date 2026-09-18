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


@pytest.mark.parametrize(
    "before,after",
    [
        ("strict=True", "strict=False"),
        ("WidgetInput", "str"),
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
        ("name: str = Field()", "name: str = Field(min_length=1)"),
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
    output = tmp_path / "candidate"
    for name, contents in render(captured).items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents)
    (output / "validation.json").write_text(json.dumps(cases))
    (output / "validation_test.go").write_text("""package backend
import ("encoding/json"; "os"; "reflect"; "testing")
func TestSchemaParity(t *testing.T) {
    data, err := os.ReadFile("validation.json"); if err != nil { t.Fatal(err) }
    var cases []struct { Body string; Partial, Valid bool; Expected map[string]any }
    if err := json.Unmarshal(data, &cases); err != nil { t.Fatal(err) }
    for _, c := range cases {
        item, seen, err := decodeWidget([]byte(c.Body), c.Partial)
        if (err == nil) != c.Valid { t.Fatalf("partial=%v %s: %v", c.Partial, c.Body, err) }
        if err != nil { continue }
        encoded, err := json.Marshal(item); if err != nil { t.Fatal(err) }
        var actual map[string]any
        if err := json.Unmarshal(encoded, &actual); err != nil { t.Fatal(err) }
        for key := range actual { if !seen[key] { delete(actual, key) } }
        if !reflect.DeepEqual(actual, c.Expected) {
            t.Fatalf("%s: got %#v want %#v", c.Body, actual, c.Expected)
        }
    }
}
""")
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
