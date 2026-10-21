# SPDX-License-Identifier: Apache-2.0
"""Parser ownership: accepted syntax retains behavior; unknown closures block."""

import json
from pathlib import Path

import pytest
from sanka_php_migration.capture import capture


def project(tmp_path: Path, source: str, **overrides: object) -> Path:
    (tmp_path / "api.php").write_text("<?php\n" + source)
    manifest = {
        "schema": "sanka.php-project/v1",
        "kind": "custom",
        "database": {"dialect": "none"},
        "routes": [{"method": "GET", "path": "/hello/{id}", "file": "api.php", "handler": "hello"}],
        **overrides,
    }
    (tmp_path / "sanka-php.json").write_text(json.dumps(manifest))
    return tmp_path


def test_captures_actual_function_and_auth_branch(tmp_path: Path) -> None:
    root = project(
        tmp_path,
        """
function hello(array $request) {
    if (($request['headers']['authorization'] ?? '') !== 'Bearer fixture') {
        return ['status' => 401, 'body' => ['error' => 'Unauthorized']];
    }
    $name = $request['query']['name'] ?? 'world';
    return ['status' => 200, 'body' => ['id' => $request['params']['id'], 'name' => $name]];
}
""",
    )
    data = capture(root)
    assert data["gaps"] == []
    route = data["routes"][0]
    assert route["body"][0]["op"] == "if"
    assert route["body"][0]["then"][0]["op"] == "return"
    assert route["body"][1]["op"] == "assign"
    assert route["parameters"] == ["request"]
    before = data["source_digest"]
    (root / "api.php").write_text((root / "api.php").read_text().replace("world", "visitor"))
    assert capture(root)["source_digest"] != before


@pytest.mark.parametrize(
    "statement",
    [
        "return shell_exec('whoami');",
        "return $request['query']['a'] == 0;",
        "require 'hidden.php'; return [];",
        "return $_ENV['SECRET'];",
        "$callback = 'hello'; return $callback($request);",
        "return ['01' => 'a', 1 => 'b'];",
    ],
)
def test_unknown_or_ambiguous_php_blocks_route(tmp_path: Path, statement: str) -> None:
    data = capture(project(tmp_path, f"function hello(array $request) {{ {statement} }}"))
    assert data["routes"][0]["gaps"]
    assert not data["routes"][0].get("body")


def test_top_level_effects_and_composer_plugins_are_global_blockers(tmp_path: Path) -> None:
    root = project(
        tmp_path,
        "file_put_contents('leak', 'executed'); function hello(array $request) { return []; }",
    )
    (root / "composer.json").write_text(json.dumps({"require": {"unknown/plugin": "1.0.0"}}))
    data = capture(root)
    assert len(data["gaps"]) >= 2
    assert not (root / "leak").exists()


def test_symlink_source_is_rejected(tmp_path: Path) -> None:
    root = project(tmp_path, "function hello(array $request) { return []; }")
    (root / "outside.txt").write_text("<?php function hello() {}")
    (root / "api.php").unlink()
    (root / "api.php").symlink_to(root / "outside.txt")
    with pytest.raises(ValueError):
        capture(root)


@pytest.mark.parametrize("parameters", ["string $request", "array &$request"])
def test_custom_request_contract_rejects_type_and_reference_changes(
    tmp_path: Path, parameters: str
) -> None:
    data = capture(project(tmp_path, f"function hello({parameters}) {{ return []; }}"))
    assert data["routes"][0]["gaps"]


def test_overlapping_routes_do_not_change_router_precedence(tmp_path: Path) -> None:
    root = project(tmp_path, "function hello(array $request) { return []; }")
    manifest = json.loads((root / "sanka-php.json").read_text())
    manifest["routes"].append(dict(manifest["routes"][0], path="/hello/static"))
    (root / "sanka-php.json").write_text(json.dumps(manifest))
    assert capture(root)["gaps"]


def test_existing_laravel_repository_can_be_assessed_without_modification(tmp_path: Path) -> None:
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes/api.php").write_text("""<?php
use Illuminate\\Support\\Facades\\Route;
Route::get('/products', [ProductController::class, 'index']);
""")
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^12.0"}}))
    data = capture(tmp_path)
    assert data["kind"] == "laravel"
    assert data["discovered_routes"] == 1
    assert data["gaps"] and data["routes"][0]["gaps"]
    assert not (tmp_path / "sanka-php.json").exists()


def test_laravel_closure_retains_validation_and_db_transaction(tmp_path: Path) -> None:
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes/api.php").write_text("""<?php
use Illuminate\\Support\\Facades\\Route;
use Illuminate\\Support\\Facades\\DB;
use Illuminate\\Http\\Request;
Route::post('/orders', function (Request $request) {
    $data = $request->validate(['name' => 'required|string|max:80']);
    return DB::transaction(function () use ($data) {
        DB::insert('INSERT INTO orders (name) VALUES (?)', [$data['name']]);
        return response()->json(['created' => true], 201);
    });
});
Route::get('/private', function (Request $request) {
    return response()->json(['ok' => true]);
})->middleware('auth:sanctum');
""")
    (tmp_path / "sanka-php.json").write_text(
        json.dumps(
            {
                "schema": "sanka.php-project/v1",
                "kind": "laravel",
                "route_files": ["routes/api.php"],
                "database": {"dialect": "mysql", "schema_file": "schema.sql", "tables": ["orders"]},
            }
        )
    )
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE orders (id BIGINT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(80) NOT NULL);"
    )
    data = capture(tmp_path)
    assert len(data["routes"]) == 2
    assert data["routes"][0]["gaps"] == []
    assert data["routes"][1]["gaps"]
    transaction = data["routes"][0]["body"][1]["value"]
    assert transaction["op"] == "transaction"
    assert transaction["body"][0]["value"]["op"] == "sql"
