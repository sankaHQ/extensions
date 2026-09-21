# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Captured row ownership must survive generated reads and writes."""

import ast
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS, capture, configuration
from test_golang_identity import identity_source
from test_golang_reads import detail_source, read_source
from test_golang_schema import generate
from test_golang_writes import drf_write_source, fastapi_write_source, flask_write_source


def scoped_source(framework: str, claim: str = "tenant") -> str:
    base = {
        "drf": lambda: drf_write_source(combined=False),
        "fastapi": fastapi_write_source,
        "flask": flask_write_source,
    }[framework]()
    tree = ast.parse(base)
    for source in (
        detail_source(framework),
        read_source(framework).replace("health", "list_widgets"),
    ):
        extra = ast.parse(source)
        tree.body.extend(n for n in extra.body if isinstance(n, ast.FunctionDef))
        if framework == "drf":
            urls = next(
                n
                for n in tree.body
                if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns"
            )
            other = next(
                n
                for n in extra.body
                if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns"
            )
            urls.value.elts.extend(other.value.elts)
    if framework != "drf":
        tree.body.insert(0, ast.parse("from sqlalchemy import select").body[0])
    if framework == "drf":
        # Distinct explicit paths retain Django's first-match URL dispatch.
        urls = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "urlpatterns"
        )
        for call in urls.value.elts:
            name = call.args[1].id
            if name in {"patch_widget", "replace_widget", "delete_widget"}:
                call.args[0].value = name + "/<int:id>"
        tree.body.remove(urls)
        tree.body.append(urls)
    tree = ast.parse(identity_source(framework, ast.unparse(tree)))
    receiver = {"drf": "request.auth", "flask": "g.principal", "fastapi": "principal"}[framework]
    identity = f"{receiver}[{claim!r}]"
    data = "request.data" if framework == "drf" else "data"
    denied = {
        "drf": 'return Response({"error": "permission denied"}, status=403)',
        "flask": 'return jsonify({"error": "permission denied"}), 403',
        "fastapi": 'raise HTTPException(status_code=403, detail="permission denied")',
    }[framework]
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name == "authenticate":
            continue
        for item in ast.walk(node):
            if isinstance(item, ast.If) and ast.unparse(item.test) == "item is None":
                item.test = ast.parse(f"item is None or item.name != {identity}", mode="eval").body
            if (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "order_by"
            ):
                original = ast.unparse(item.func.value)
                where = (
                    f".filter(name={identity})"
                    if framework == "drf"
                    else f".where(Widget.name == {identity})"
                )
                item.func.value = ast.parse(original + where, mode="eval").body
        if node.name in {"create_widget", "patch_widget", "replace_widget"}:
            guard = ast.parse(
                f"if 'name' in {data} and {data}['name'] != {identity}:\n    {denied}"
            ).body[0]
            index = next(
                i
                for i, n in enumerate(node.body)
                if isinstance(n, ast.With)
                or (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "item")
            )
            node.body.insert(index, guard)
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("claim", ["tenant", "sub"])
def test_scoped_crud_capture(tmp_path: Path, framework: str, target: str, claim: str) -> None:
    output = generate(tmp_path, framework, target, app_source=scoped_source(framework, claim))
    captured = capture(
        tmp_path,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    assert not captured["gaps"], captured["gaps"]
    assert len(captured["routes"]) == 6
    for route in captured["routes"]:
        operation = route.get("read", route.get("write"))
        assert operation["scope"] == {"name": claim}
    assert captured == capture(tmp_path, captured["configuration"])
    assert "principal" in (output / "app.go").read_text()


def prepare_scoped_fixture(root: Path, framework: str, target: str, claim: str = "tenant"):
    import json

    identity = "fixture-tenant" if claim == "tenant" else "fixture-user"
    body = {"name": identity, "count": 1, "enabled": True}

    def case(method, path, status, data=None):
        return {
            "method": method,
            "path": path,
            "expected_status": status,
            **({"body": data} if data is not None else {}),
        }

    patch = "/patch_widget/1" if framework == "drf" else "/widgets/1"
    put = "/replace_widget/1" if framework == "drf" else "/widgets/1"
    delete = "/delete_widget/1" if framework == "drf" else "/widgets/1"
    cases = [
        case("GET", "/list_widgets", 200),
        case("POST", "/widgets", 400, {}),
        case("POST", "/widgets", 201, body),
        case("GET", "/widgets/1", 200),
        case("GET", "/list_widgets", 200),
        case("PUT", put, 200, body | {"count": 2}),
        case("PATCH", patch, 200, {}),
        case("PATCH", patch, 403, {"name": "outsider"}),
        case("PATCH", patch, 200, {"count": 3}),
        case("DELETE", delete, 204),
        case("GET", "/widgets/1", 404),
        case("DELETE", delete, 404),
    ]
    (root / "sanka-verify.json").write_text(
        json.dumps({"scenarios": [dict(c, id=str(i)) for i, c in enumerate(cases)]})
    )
    output = generate(root, framework, target, app_source=scoped_source(framework, claim))
    captured = capture(
        root,
        configuration(
            {"source_framework": framework, "target_framework": target, "database_layer": "pgx"}
        ),
    )
    return output, captured


@pytest.mark.parametrize("target", TARGETS)
def test_scoped_go_compiles(tmp_path: Path, target: str) -> None:
    import os

    from sanka_extension_python_to_golang.replay import _run
    from sanka_extension_python_to_golang.write_replay import write_probe

    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires Go toolchain")
    output, captured = prepare_scoped_fixture(tmp_path, "fastapi", target)
    (output / "sanka_contract_probe_test.go").write_text(write_probe(captured))
    post = next(i for i, r in enumerate(captured["routes"]) if r["method"] == "POST")
    patch = next(i for i, r in enumerate(captured["routes"]) if r["method"] == "PATCH")
    (output / "row_policy_test.go").write_text(
        """package backend
import ("testing"; "context"; "errors")
func TestRowPolicy(t *testing.T) {
    ctx := context.WithValue(context.Background(), principalContextKey{}, map[string]string{"sub":"u", "tenant":"tenant-a"})
    if _, err := writeRowPOST(ctx, nil, []byte(`{"name":"tenant-b","count":1,"enabled":true}`)); !errors.Is(err, errScopeDenied) { t.Fatal(err) }
    if _, err := writeRowPATCH(ctx, nil, []byte(`{"name":"tenant-b"}`), 1); !errors.Is(err, errScopeDenied) { t.Fatal(err) }
    if _, err := writeRowPOST(context.Background(), nil, []byte(`{}`)); !errors.Is(err, errScopeDenied) { t.Fatal(err) }
    if _, err := writeRowPOST(ctx, nil, []byte(`{}`)); !errors.Is(err, errInvalidWrite) { t.Fatal(err) }
}
""".replace("writeRowPOST", f"writeRow{post}").replace("writeRowPATCH", f"writeRow{patch}")
    )
    _run(["go", "test", "-p=2", "-run", "TestRowPolicy", "./..."], output)


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize(
    "change", ["body-missing", "wrong-claim", "role", "operator", "late-guard", "rebound"]
)
def test_unqualified_row_policy_blocks(tmp_path: Path, framework: str, change: str) -> None:
    from test_golang_schema import model_source

    tree = ast.parse(scoped_source(framework))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "patch_widget")
    receiver = {"drf": "request.auth", "flask": "g.principal", "fastapi": "principal"}[framework]
    guard = next(
        n for n in node.body if isinstance(n, ast.If) and "permission denied" in ast.unparse(n)
    )
    if change == "body-missing":
        node.body.remove(guard)
    elif change == "wrong-claim":
        node.body[node.body.index(guard)] = ast.parse(
            ast.unparse(guard).replace("'tenant'", "'sub'")
        ).body[0]
    elif change == "late-guard":
        node.body.remove(guard)
        node.body.append(guard)
    elif change == "rebound":
        node.body.insert(0, ast.parse(receiver.split(".")[0] + " = {}").body[0])
    else:
        for n in ast.walk(node):
            if (
                change == "role"
                and isinstance(n, ast.Subscript)
                and ast.unparse(n.value) == receiver
            ):
                n.slice = ast.Constant("role")
            if (
                change == "operator"
                and isinstance(n, ast.Compare)
                and any(isinstance(op, ast.NotEq) for op in n.ops)
            ):
                n.ops = [ast.Eq()]
    (tmp_path / "app.py").write_text(ast.unparse(tree))
    (tmp_path / "models.py").write_text(model_source(framework))
    result = capture(
        tmp_path, configuration({"source_framework": framework, "database_layer": "pgx"})
    )
    assert result["gaps"]


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("claim", ["tenant", "sub"])
def test_scoped_replay_cases(tmp_path: Path, framework: str, claim: str) -> None:
    from sanka_extension_python_to_golang.security import security_cases
    from sanka_extension_python_to_golang.write_replay import scenarios_for

    _, captured = prepare_scoped_fixture(tmp_path, framework, "fiber", claim)
    cases = security_cases(scenarios_for(tmp_path, captured), "jwt-hs256-roles", captured["routes"])
    cross = [c for c in cases if ".cross-" in c["id"]]
    assert {c["expected_status"] for c in cross} == {200, 403, 404}
    assert any(
        c["method"] == "PATCH" and c["body"] == {} and c["expected_status"] == 404 for c in cross
    )
    deletion = next(i for i, c in enumerate(cases) if c["method"] == "DELETE")
    assert cases[deletion]["id"].endswith(".cross-" + claim)
    assert cases[deletion]["expected_status"] == 404
    put = next(c for c in cases if c["method"] == "PUT" and ".cross-row-" in c["id"])
    assert put["body"]["name"] == ("other-tenant" if claim == "tenant" else "other-user")
    assert put["expected_status"] == 404


def test_first_denial_checks_initial_database_state() -> None:
    from sanka_extension_python_to_golang.write_replay import denied_writes_unchanged

    initial = {"tables": {"widgets": []}, "sequences": {"widgets": ["1", False]}}
    denial = initial | {"id": "security.0.cross-tenant", "status": 403}
    assert denied_writes_unchanged(initial, [denial])
    assert not denied_writes_unchanged(initial, [denial | {"sequences": {"widgets": ["1", True]}}])
    assert not denied_writes_unchanged(
        initial, [denial | {"status": 404, "tables": {"widgets": [{"id": "1"}]}}]
    )


@pytest.mark.parametrize("framework", SOURCES)
def test_source_body_policy_denies_before_database(tmp_path: Path, framework: str) -> None:
    import json
    import sys

    from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV, replay_roles
    from sanka_extension_python_to_golang.replay import SOURCE_PROBE, _run
    from test_golang_schema import model_source

    (tmp_path / "app.py").write_text(scoped_source(framework))
    (tmp_path / "models.py").write_text(model_source(framework))
    token = replay_roles({"method": "POST", "expected_status": 201}, first=False)[0][1]
    probe = (
        SOURCE_PROBE.split("observed = []", 1)[0]
        + """
observed = []
for data in ({}, {"name": "outsider", "count": 1, "enabled": True}):
    headers = {"Authorization": routes, "Content-Type": "application/json"}
    body = json.dumps(data)
    if framework == "drf":
        response = client.generic("POST", "/widgets", data=body, content_type="application/json", headers=headers)
    elif framework == "flask":
        response = client.open("/widgets", method="POST", data=body, headers=headers)
    else:
        response = client.post("/widgets", content=body, headers=headers)
    raw = response.data if framework == "flask" else response.content
    observed.append([response.status_code, json.loads(raw)])
Path(destination).write_text(json.dumps(observed))
"""
    )
    result = tmp_path / "observed.json"
    _run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            framework,
            str(tmp_path / "app.py"),
            token,
            str(result),
            str(tmp_path / "models.py"),
            "1",
        ],
        tmp_path,
        environment=REPLAY_JWT_ENV
        | {"DATABASE_URL": "postgresql+psycopg://fixture@127.0.0.1:1/unused"},
    )
    key = "detail" if framework == "fastapi" else "error"
    assert json.loads(result.read_text()) == [
        [400, {key: "invalid request body"}],
        [403, {key: "permission denied"}],
    ]
