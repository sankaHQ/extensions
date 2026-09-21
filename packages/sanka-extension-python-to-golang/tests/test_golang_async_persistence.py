# SPDX-License-Identifier: Apache-2.0
"""Async source execution must preserve the qualified synchronous CRUD contract."""

import ast
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.capture import TARGETS
from sanka_extension_python_to_golang.render import render
from sanka_extension_python_to_golang.replay import SOURCE_PROBE
from test_golang_validation import captured_source, native_fastapi_schema_source


def async_source(repository: bool = False) -> str:
    tree = ast.parse(native_fastapi_schema_source())
    helpers = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.decorator_list:
            continue
        scope = node.body[-1]
        assert isinstance(scope, ast.With)
        scope.items[0].context_expr = ast.parse("AsyncSession(engine)", mode="eval").body
        for statement in ast.walk(scope):
            for field, value in ast.iter_fields(statement):
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and ast.unparse(value.func.value) == "session"
                    and value.func.attr in {"get", "commit", "refresh", "delete"}
                ):
                    setattr(statement, field, ast.Await(value=value))
        node.body[-1] = ast.AsyncWith(items=scope.items, body=scope.body, type_comment=None)
        if repository:
            args = [arg.arg for arg in node.args.args]
            helper = ast.parse(f"async def repo_{node.name}({', '.join(args)}):\n    pass").body[0]
            helper.body = [node.body[-1]]
            helpers.append(helper)
            call = ast.parse(f"await repo_{node.name}({', '.join(args)})", mode="eval").body
            node.body[-1] = ast.Return(value=call)
        node.__class__ = ast.AsyncFunctionDef
    tree.body[0:0] = helpers
    source = ast.unparse(ast.fix_missing_locations(tree))
    source = source.replace(
        "from sqlalchemy import create_engine", "from sqlalchemy.pool import NullPool"
    )
    source = source.replace(
        "from sqlalchemy.orm import Session",
        "from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine",
    )
    return source.replace(
        "create_engine(environ['DATABASE_URL'])",
        "create_async_engine(environ['DATABASE_URL'], poolclass=NullPool)",
    )


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("repository", [False, True])
def test_async_contract_reuses_generator(tmp_path: Path, target: str, repository: bool) -> None:
    sync = captured_source(tmp_path, "fastapi", target, native_fastapi_schema_source())
    captured = captured_source(tmp_path, "fastapi", target, async_source(repository))
    assert captured["gaps"] == []
    assert captured["routes"] == sync["routes"]
    assert {k: v for k, v in render(captured).items() if k != "contract.json"} == {
        k: v for k, v in render(sync).items() if k != "contract.json"
    }
    assert captured_source(tmp_path, "fastapi", target, async_source(repository)) == captured


@pytest.mark.parametrize(
    "before,after",
    [
        ("await session.commit()", "session.commit()"),
        ("await session.refresh(item)", "session.refresh(item)"),
        ("session.add(item)", "await session.add(item)"),
        ("await session.commit()", "await session.rollback()"),
        ("AsyncSession(engine)", "AsyncSession(engine, expire_on_commit=False)"),
        ("return await repo_create_widget(data)", "return repo_create_widget(data)"),
        ("return await repo_create_widget(data)", "return await repo_create_widget({})"),
    ],
)
def test_async_semantic_changes_block(tmp_path: Path, before: str, after: str) -> None:
    source = async_source(True)
    assert before in source
    captured = captured_source(tmp_path, "fastapi", text=source.replace(before, after))
    assert captured["gaps"]


def test_repository_module_is_consumed_and_hashed(tmp_path: Path) -> None:
    tree = ast.parse(async_source(True))
    repository = []
    app = []
    for node in tree.body:
        if (
            (isinstance(node, ast.AsyncFunctionDef) and not node.decorator_list)
            or (
                isinstance(node, ast.ImportFrom)
                and node.module in {"models", "os", "sqlalchemy.pool", "sqlalchemy.ext.asyncio"}
            )
            or (isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "engine")
        ):
            repository.append(node)
        else:
            app.append(node)
    helpers = [node.name for node in repository if isinstance(node, ast.AsyncFunctionDef)]
    # Repository functions raise the same explicitly captured HTTP error.
    repository.insert(0, ast.parse("from fastapi import HTTPException").body[0])
    module = tmp_path / "repository.py"
    module.write_text(ast.unparse(ast.Module(body=repository, type_ignores=[])))
    app.insert(0, ast.parse("from repository import " + ", ".join(helpers)).body[0])
    source = ast.unparse(ast.Module(body=app, type_ignores=[]))
    captured = captured_source(tmp_path, "fastapi", text=source)
    assert captured["gaps"] == []
    module.write_text(module.read_text() + "\n# changed source\n")
    changed = captured_source(tmp_path, "fastapi", text=source)
    assert changed["gaps"] == []
    assert changed != captured


def test_source_probe_awaits_async_engine_cleanup(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    source = tmp_path / "app.py"
    namespace = {
        "use_database": "1",
        "framework": "fastapi",
        "filename": str(source),
        "Path": Path,
        "sys": SimpleNamespace(
            modules={"source": SimpleNamespace(__file__=str(source), engine=engine)}
        ),
    }
    exec(SOURCE_PROBE[SOURCE_PROBE.index('if use_database == "1":\n    if framework') :], namespace)
    assert engine.disposed
