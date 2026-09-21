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


def injected_source(repository: str = "function") -> str:
    tree = ast.parse(async_source())
    provider = ast.parse(
        "async def get_session():\n"
        "    async with AsyncSession(engine) as session:\n"
        "        yield session\n"
    ).body[0]
    helpers = []
    methods = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or not node.decorator_list:
            continue
        scope = node.body[-1]
        assert isinstance(scope, ast.AsyncWith)
        arguments = [arg.arg for arg in node.args.args]
        if repository == "direct":
            node.body[-1:] = scope.body
        elif repository == "function":
            helper = ast.parse(
                f"async def repo_{node.name}(session: AsyncSession, {', '.join(arguments)}):\n"
                "    pass"
            ).body[0]
            helper.body = scope.body
            helpers.append(helper)
            node.body[-1] = ast.parse(
                f"return await repo_{node.name}(session, {', '.join(arguments)})"
            ).body[0]
        else:
            helper = ast.parse(
                f"async def {node.name}(self, {', '.join(arguments)}):\n    pass"
            ).body[0]
            helper.body = ast.parse(
                "\n".join(ast.unparse(item) for item in scope.body).replace(
                    "session.", "self.session."
                )
            ).body
            methods.append(helper)
            node.body[-1:] = ast.parse(
                "repository = WidgetRepository(session)\n"
                f"return await repository.{node.name}({', '.join(arguments)})"
            ).body
        node.args.args.append(ast.arg(arg="session", annotation=ast.Name(id="AsyncSession")))
        node.args.defaults.append(ast.parse("Depends(get_session)", mode="eval").body)
    if methods:
        cls = ast.parse(
            "class WidgetRepository:\n"
            "    def __init__(self, session: AsyncSession):\n"
            "        self.session = session\n"
        ).body[0]
        cls.body.extend(methods)
        helpers.append(cls)
    index = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.AsyncFunctionDef))
    tree.body[index:index] = [provider, *helpers]
    tree.body.insert(0, ast.parse("from fastapi import Depends").body[0])
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("repository", ["direct", "function", "class"])
def test_injected_sessions_reuse_crud(tmp_path: Path, target: str, repository: str) -> None:
    sync = captured_source(tmp_path, "fastapi", target, native_fastapi_schema_source())
    captured = captured_source(tmp_path, "fastapi", target, injected_source(repository))
    assert captured["gaps"] == []
    assert captured["routes"] == sync["routes"]
    assert {k: v for k, v in render(captured).items() if k != "contract.json"} == {
        k: v for k, v in render(sync).items() if k != "contract.json"
    }
    assert captured_source(tmp_path, "fastapi", target, injected_source(repository)) == captured


@pytest.mark.parametrize(
    "before,after",
    [
        ("yield session", "yield session\n        await session.commit()"),
        ("Depends(get_session)", "Depends(get_session, use_cache=False)"),
        ("Depends(get_session)", "Depends(unknown_provider)"),
        ("self.session = session", "self.session = session\n        notify()"),
        ("WidgetRepository(session)", "WidgetRepository(None)"),
        ("await self.session.commit()", "await self.session.rollback()"),
        ("await self.session.refresh(item)", "self.session.refresh(item)"),
        ("class WidgetRepository:", "class WidgetRepository(UnknownBase):"),
        ("async def create_widget(self, data):", "async def create_widget(self, data=None):"),
        ("self.session.add(item)", "await self.session.add(item)"),
        (
            "return await repository.create_widget(data)",
            "return await repository.create_widget({})",
        ),
    ],
)
def test_injected_session_changes_block(tmp_path: Path, before: str, after: str) -> None:
    source = injected_source("class")
    assert before in source
    assert captured_source(tmp_path, "fastapi", text=source.replace(before, after))["gaps"]


def test_unconsumed_repository_is_not_marked_lowered(tmp_path: Path) -> None:
    source = injected_source("function")
    (tmp_path / "unrelated.py").write_text(
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "async def repo_create_widget(session: AsyncSession, data):\n"
        "    await session.commit()\n"
    )
    captured = captured_source(tmp_path, "fastapi", text=source)
    assert "persistence: captured contracts require Go lowering" in captured["gaps"]


def test_repository_class_extra_method_is_not_discarded(tmp_path: Path) -> None:
    source = injected_source("class").replace(
        "class WidgetRepository:",
        "class WidgetRepository:\n    async def notify(self):\n        pass",
    )
    assert captured_source(tmp_path, "fastapi", text=source)["gaps"]


@pytest.mark.parametrize("method", ["__init__", "__new__", "__getattribute__", "__setattr__"])
def test_repository_special_methods_cannot_override_construction(
    tmp_path: Path, method: str
) -> None:
    source = (
        injected_source("class")
        .replace("async def create_widget(self, data):", f"async def {method}(self, data):")
        .replace("repository.create_widget(data)", f"repository.{method}(data)")
    )
    assert captured_source(tmp_path, "fastapi", text=source)["gaps"]


def test_imported_injected_repository_is_hashed(tmp_path: Path) -> None:
    tree = ast.parse(injected_source("class"))
    repository = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WidgetRepository"
    )
    tree.body.remove(repository)
    tree.body.insert(0, ast.parse("from repository import WidgetRepository").body[0])
    imports = ast.parse(
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "from fastapi import HTTPException\nfrom models import Widget\n"
    ).body
    module = tmp_path / "repository.py"
    module.write_text(ast.unparse(ast.Module(body=[*imports, repository], type_ignores=[])))
    source = ast.unparse(tree)
    captured = captured_source(tmp_path, "fastapi", text=source)
    assert captured["gaps"] == []
    module.write_text(
        module.read_text().replace("await self.session.commit()", "await self.session.rollback()")
    )
    changed = captured_source(tmp_path, "fastapi", text=source)
    assert changed["gaps"]
    assert changed != captured


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
