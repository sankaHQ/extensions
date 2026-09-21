# SPDX-License-Identifier: Apache-2.0
"""Static FastAPI application topology capture."""

from pathlib import Path

from sanka_extension_python_to_golang.capture import capture, configuration
from sanka_extension_python_to_golang.topology import capture_fastapi_topology


def test_factory_router_and_dependency_order(tmp_path: Path) -> None:
    files = {
        "app/main.py": """from fastapi import Depends, FastAPI
from app.api.router import api_router
from app.dependencies import app_dependency
from app.errors import install_exception_handlers
from app.middleware import RequestContextMiddleware

def create_lifespan(settings):
    return settings

def create_app(settings=None):
    app = FastAPI(dependencies=[Depends(app_dependency)], lifespan=create_lifespan(settings))
    app.add_middleware(RequestContextMiddleware, enabled=True)
    install_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v2")
    return app

app = create_app()
""",
        "app/api/router.py": """from fastapi import APIRouter, Depends
from app.api.widgets import router as widgets_router
from app.dependencies import api_dependency, include_dependency

api_router = APIRouter(dependencies=[Depends(api_dependency)])
api_router.include_router(widgets_router, dependencies=[Depends(include_dependency)])
""",
        "app/api/widgets.py": """from typing import Annotated
from fastapi import APIRouter, Depends, Query, Security
from app.dependencies import get_auth, require_route, require_router, require_scope

router = APIRouter(prefix="/widgets", tags=["widgets"], dependencies=[Depends(require_router)])

@router.get("/{widget_id}", dependencies=[Security(require_route, scopes=["route:read"])])
async def get_widget(
    widget_id: int,
    auth: Annotated[str, Depends(get_auth)],
    scope: Annotated[str, Security(require_scope, scopes=["widgets:read"])],
    page: Annotated[int, Query(ge=1)] = 1,
):
    return {"id": widget_id}
""",
        "app/errors.py": """def install_exception_handlers(app):
    app.add_exception_handler(ValueError, validation_error)
    app.add_exception_handler(Exception, unhandled_error)
""",
        "app/middleware.py": "class RequestContextMiddleware: pass\n",
        "app/dependencies.py": """def app_dependency(): pass
def api_dependency(): pass
def include_dependency(): pass
def get_auth(): pass
def require_route(): pass
def require_router(): pass
def require_scope(): pass
""",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    topology = capture_fastapi_topology(tmp_path, "app/main.py")

    assert topology["factory"] == "create_app"
    assert topology["lifespan"] == "create_lifespan(settings)"
    assert topology["dependencies"] == [{"kind": "Depends", "call": "app_dependency"}]
    assert topology["middleware"] == [
        {
            "name": "RequestContextMiddleware",
            "options": {"enabled": "True"},
            "registration_order": 0,
        }
    ]
    assert topology["exception_handlers"] == [
        {"exception": "ValueError", "handler": "validation_error", "order": 0},
        {"exception": "Exception", "handler": "unhandled_error", "order": 1},
    ]
    assert topology["routers"] == [
        {
            "module": "app/api/router.py",
            "name": "api_router",
            "prefix": "/api/v2",
            "options": {},
            "dependencies": [{"kind": "Depends", "call": "api_dependency"}],
            "include_dependencies": [],
            "routes": [],
        },
        {
            "module": "app/api/widgets.py",
            "name": "router",
            "prefix": "/api/v2/widgets",
            "options": {"tags": "['widgets']"},
            "dependencies": [{"kind": "Depends", "call": "require_router"}],
            "include_dependencies": [{"kind": "Depends", "call": "include_dependency"}],
            "routes": [
                {
                    "method": "GET",
                    "path": "/api/v2/widgets/{widget_id}",
                    "handler": "get_widget",
                    "async": True,
                    "decorator_dependencies": [
                        {
                            "kind": "Security",
                            "call": "require_route",
                            "scopes": ["route:read"],
                        }
                    ],
                    "parameter_dependencies": [
                        {"parameter": "auth", "kind": "Depends", "call": "get_auth"},
                        {
                            "parameter": "scope",
                            "kind": "Security",
                            "call": "require_scope",
                            "scopes": ["widgets:read"],
                        },
                    ],
                }
            ],
        },
    ]
    assert topology["gaps"] == []
    assert (
        capture(
            tmp_path,
            configuration({"source_framework": "fastapi", "source_file": "app/main.py"}),
        )["fastapi_topology"]
        == topology
    )


def test_dynamic_router_wiring_is_a_gap(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(make_router())\n"
    )

    topology = capture_fastapi_topology(tmp_path, "app.py")

    assert topology["gaps"] == ["app.py: dynamic included router"]


def test_dynamic_dependency_lists_are_gaps(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import APIRouter, FastAPI\n"
        "app = FastAPI(dependencies=application_dependencies())\n"
        "router = APIRouter()\n"
        "@router.get('/', dependencies=route_dependencies())\n"
        "def route(): return {}\n"
        "app.include_router(router)\n"
    )

    topology = capture_fastapi_topology(tmp_path, "app.py")

    assert topology["gaps"] == [
        "app.py: dynamic application dependencies",
        "app.py:route: dynamic decorator dependencies",
    ]
