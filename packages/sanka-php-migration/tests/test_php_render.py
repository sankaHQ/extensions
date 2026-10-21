# SPDX-License-Identifier: Apache-2.0
"""Generated handler boundary: PHP request semantics survive native generation."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sanka_php_migration.capture import capture
from sanka_php_migration.render import render


def load_app(
    tmp_path: Path, source: str, *, kind: str = "custom", database: dict[str, object] | None = None
) -> TestClient:
    if kind == "laravel":
        fixture = Path(__file__).parent / "fixtures/laravel"
        for name in ("composer.json", "composer.lock"):
            shutil.copyfile(fixture / name, tmp_path / name)
    (tmp_path / "api.php").write_text("<?php\n" + source)
    settings = (
        {
            "routes": [
                {"method": "GET", "path": "/items/{id}", "file": "api.php", "handler": "items"}
            ]
        }
        if kind == "custom"
        else {"route_files": ["api.php"]}
    )
    (tmp_path / "sanka-php.json").write_text(
        json.dumps(
            {
                "schema": "sanka.php-project/v1",
                "kind": kind,
                "database": database or {"dialect": "none"},
                **settings,
            }
        )
    )
    data = capture(tmp_path)
    files = render(data, "fastapi")
    output = tmp_path / "candidate"
    output.mkdir()
    for name, content in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    sys.path.insert(0, str(output))
    try:
        for name in ["php_compat", "handlers"]:
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("candidate_app", output / "app.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return TestClient(module.app, raise_server_exceptions=False)
    finally:
        sys.path.pop(0)


def test_native_handler_keeps_authorization_false_like_strings_and_strict_types(
    tmp_path: Path,
) -> None:
    client = load_app(
        tmp_path,
        """
function items(array $request) {
    if (($request['headers']['authorization'] ?? '') !== 'Bearer fixture') {
        return ['status' => 401, 'body' => ['error' => 'Unauthorized']];
    }
    $zero = $request['query']['zero'] ?? 'fallback';
    return ['status' => 200, 'body' => ['id' => $request['params']['id'], 'zero' => $zero,
        'truthy' => !!$zero, 'integer' => $zero === 0]];
}
""",
    )
    assert client.get("/items/7").status_code == 401
    response = client.get("/items/7?zero=0", headers={"Authorization": "Bearer fixture"})
    assert response.status_code == 200
    assert response.json() == {"id": "7", "zero": "0", "truthy": False, "integer": False}


def test_laravel_validation_preserves_errors_and_filters_unknown_fields(tmp_path: Path) -> None:
    client = load_app(
        tmp_path,
        """
use Illuminate\\Support\\Facades\\Route;
use Illuminate\\Http\\Request;
Route::post('/items', function (Request $request) {
    $data = $request->validate(['name' => 'required|string|max:4']);
    return response()->json($data, 201);
});
""",
        kind="laravel",
    )
    assert client.post("/items", json={}).json() == {
        "message": "The name field is required.",
        "errors": {"name": ["The name field is required."]},
    }
    assert client.post("/items", json={"name": 1}).status_code == 422
    assert client.post("/items", json={"name": "éééé", "ignored": True}).json() == {"name": "éééé"}


def test_unknown_behavior_cannot_render(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_app(tmp_path, "function items(array $request) { return eval('1'); }")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, []),
        ({"0": "a", "1": "b"}, ["a", "b"]),
        ({"1": "a", "0": "b"}, {"1": "a", "0": "b"}),
        ({"nested": {}}, {"nested": []}),
    ],
)
def test_custom_json_keeps_php_array_shape(
    tmp_path: Path, payload: object, expected: object
) -> None:
    client = load_app(
        tmp_path,
        "function items(array $request) { return ['status'=>200, 'body'=>$request['body']]; }",
    )
    response = client.request("GET", "/items/1", json=payload)
    assert response.status_code == 200
    assert response.json() == expected


def test_transaction_failure_rolls_back_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    path = tmp_path / "fixture.sqlite"
    schema = "CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    (tmp_path / "schema.sql").write_text(schema)
    with sqlite3.connect(path) as db:
        db.execute(schema)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(path))
    client = load_app(
        tmp_path,
        """
use Illuminate\\Support\\Facades\\Route;
use Illuminate\\Support\\Facades\\DB;
use Illuminate\\Http\\Request;
Route::post('/orders', function (Request $request) {
    return DB::transaction(function () {
        DB::insert('INSERT INTO orders (id, name) VALUES (?, ?)', [1, 'rollback']);
        throw new RuntimeException('rollback');
    });
});
Route::post('/orders/commit', function (Request $request) {
    return DB::transaction(function () {
        DB::insert('INSERT INTO orders (id, name) VALUES (?, ?)', [2, 'commit']);
        return response()->json(['ok' => true], 201);
    });
});
Route::get('/orders', function (Request $request) {
    $rows = DB::select('SELECT id, name FROM orders ORDER BY id');
    $missing = DB::select('SELECT id, name FROM orders WHERE id = ?', [999]);
    return response()->json(['items' => $rows, 'empty' => $missing === []]);
});
""",
        kind="laravel",
        database={"dialect": "sqlite", "schema_file": "schema.sql", "tables": ["orders"]},
    )
    assert client.post("/orders/commit", json={}).status_code == 201
    assert client.post("/orders", json={}).status_code == 500
    assert client.get("/orders").json() == {"items": [{"id": 2, "name": "commit"}], "empty": True}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id, name FROM orders").fetchall() == [(2, "commit")]
