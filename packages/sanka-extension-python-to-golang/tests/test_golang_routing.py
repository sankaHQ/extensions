# SPDX-License-Identifier: Apache-2.0
"""Routing composition compared with real source clients and generated Go."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle
from sanka_extension_python_to_golang.capture import SOURCES, TARGETS
from test_python_to_golang import apply, request, source


def routed_source(framework: str, factory: bool = False) -> str:
    text = source(framework)
    if framework == "drf":
        return text.replace("import path", "import path, include").replace(
            'urlpatterns = [path("health", health)]',
            'urlpatterns = [path("api/", include(['
            'path("v1/", include([path("health", health)]))]))]',
        )
    if framework == "flask":
        text = (
            text.replace("Flask, jsonify", "Flask, Blueprint, jsonify")
            .replace(
                "app = Flask(__name__)", 'api = Blueprint("api", __name__, url_prefix="/ignored")'
            )
            .replace('@app.get("/health")', '@api.get("/health")')
        )
        if factory:
            return (
                text + "\ndef create_app():\n    app = Flask(__name__)\n"
                '    app.register_blueprint(api, url_prefix="/api/v1")\n'
                "    return app\napp = create_app()\n"
            )
        return text + '\napp = Flask(__name__)\napp.register_blueprint(api, url_prefix="/api/v1")\n'
    return (
        text.replace("import FastAPI", "import FastAPI, APIRouter")
        .replace("app = FastAPI()", 'app = FastAPI()\napi = APIRouter(prefix="/v1")')
        .replace('@app.get("/health")', '@api.get("/health")')
        + '\napp.include_router(api, prefix="/api")\n'
    )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_grouped_routes(tmp_path: Path, framework: str, target: str) -> None:
    (tmp_path / "app.py").write_text(routed_source(framework))
    result = handle(request(tmp_path, framework, target))
    assert result.outcome == "success", result.error
    assert result.data["capture"]["gaps"] == []
    assert result.data["capture"]["routes"][0]["path"] == "/api/v1/health"
    apply(tmp_path, framework, target)
    if os.getenv("SANKA_GO_TESTS") == "1":
        verified = handle(
            dataclasses.replace(request(tmp_path, framework, target), command="verify")
        )
        assert verified.outcome == "success", verified.error


@pytest.mark.parametrize("target", TARGETS)
def test_flask_factory(tmp_path: Path, target: str) -> None:
    (tmp_path / "app.py").write_text(routed_source("flask", factory=True))
    apply(tmp_path, "flask", target)
    if os.getenv("SANKA_GO_TESTS") == "1":
        verified = handle(dataclasses.replace(request(tmp_path, "flask", target), command="verify"))
        assert verified.outcome == "success", verified.error


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
@pytest.mark.parametrize(
    "mutation", ["unregistered", "late", "duplicate", "options", "shadow", "side_effect"]
)
def test_unsupported_composition_blocks(tmp_path: Path, framework: str, mutation: str) -> None:
    text = routed_source(framework)
    registration = text.splitlines()[-1]
    if mutation == "unregistered":
        text = text.replace(registration, "")
    elif mutation == "late":
        text += '\n@api.get("/late")\ndef late():\n    return {}\n'
    elif mutation == "duplicate":
        text += registration + "\n"
    elif mutation == "options":
        text = (
            text.replace('prefix="/api"', 'prefix="/api", dependencies=[]')
            if framework == "fastapi"
            else text.replace('url_prefix="/api/v1"', 'url_prefix="/api/v1", subdomain="private"')
        )
    elif mutation == "shadow":
        text += "api = None\n"
    else:
        text += "raise RuntimeError('never execute')\n"
    (tmp_path / "app.py").write_text(text)
    planned = handle(request(tmp_path, framework))
    assert planned.data["capture"]["gaps"]
    assert planned.data["files"] == {}


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_routes_from_local_module(tmp_path: Path, framework: str, target: str) -> None:
    text = routed_source(framework, factory=framework == "flask")
    if framework == "drf":
        views, urls = text.split("urlpatterns =", 1)
        (tmp_path / "views.py").write_text(views)
        text = (
            "from django.urls import path, include\nfrom views import health\nurlpatterns =" + urls
        )
    elif framework == "flask":
        routes, factory = text.split("def create_app():", 1)
        (tmp_path / "routes.py").write_text(routes)
        text = "from flask import Flask\nfrom routes import api\ndef create_app():" + factory
    else:
        routes, registration = text.split("app.include_router", 1)
        (tmp_path / "routes.py").write_text(routes.replace("app = FastAPI()\n", ""))
        text = (
            "from fastapi import FastAPI\n"
            "from routes import api\n"
            "app = FastAPI()\n"
            "app.include_router" + registration
        )
    (tmp_path / "app.py").write_text(text)
    planned = handle(request(tmp_path, framework, target))
    assert not planned.data["capture"]["gaps"], planned.data
    assert len(planned.data["capture"]["source_modules"]) == 1
    apply(tmp_path, framework, target)
    if os.getenv("SANKA_GO_TESTS") == "1":
        verified = handle(
            dataclasses.replace(request(tmp_path, framework, target), command="verify")
        )
        assert verified.outcome == "success", verified.error
    imported = tmp_path / planned.data["capture"]["source_modules"][0]
    imported.write_text(imported.read_text() + "\n# changed source snapshot\n")
    stale = handle(
        dataclasses.replace(
            request(tmp_path, framework, target),
            command="apply",
            reviewed_plan_hash="reviewed",
            configuration=request(tmp_path, framework, target).configuration
            | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert stale.outcome == "error"


@pytest.mark.parametrize(
    "bad_source",
    [
        "from app import api\n",
        "from flask import Blueprint\n"
        "api = Blueprint('api', __name__)\n"
        "raise RuntimeError('never execute')\n",
        "from flask import Blueprint\n"
        "api = Blueprint('api', __name__)\n"
        "@api.get('/health')\n"
        "def health():\n"
        "    return jsonify({'ok': True})\n",
    ],
)
def test_invalid_import_graph_blocks(tmp_path: Path, bad_source: str) -> None:
    (tmp_path / "app.py").write_text(
        "from flask import Flask, jsonify\n"
        "from routes import api\n"
        "app = Flask(__name__)\n"
        "app.register_blueprint(api)\n"
    )
    (tmp_path / "routes.py").write_text(bad_source)
    result = handle(request(tmp_path))
    assert result.data["capture"]["gaps"]
    assert result.data["files"] == {}


def group_backend(text: str, framework: str) -> str:
    """Wrap a complete qualified backend in the framework's route grouping."""
    if framework == "drf":
        return (
            text.replace("import path", "import path, include")
            .replace("urlpatterns = [", 'urlpatterns = [path("api/", include([')
            .rstrip()
            .removesuffix("]")
            + "]))]\n"
        )
    if framework == "flask":
        return (
            text.replace("Flask,", "Flask, Blueprint,")
            .replace(
                "app = Flask(__name__)", 'app = Flask(__name__)\napi = Blueprint("api", __name__)'
            )
            .replace("@app.", "@api.")
            + '\napp.register_blueprint(api, url_prefix="/api")\n'
        )
    return (
        text.replace("FastAPI,", "FastAPI, APIRouter,")
        .replace("app = FastAPI()", "app = FastAPI()\napi = APIRouter()")
        .replace("@app.", "@api.")
        + '\napp.include_router(api, prefix="/api")\n'
    )


@pytest.mark.parametrize("framework", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_grouped_crud_capture(tmp_path: Path, framework: str, target: str) -> None:
    from test_golang_schema import generate
    from test_golang_writes import drf_write_source, fastapi_write_source, flask_write_source

    text = {"drf": drf_write_source, "fastapi": fastapi_write_source, "flask": flask_write_source}[
        framework
    ]()
    output = generate(tmp_path, framework, target, app_source=group_backend(text, framework))
    assert "/api/widgets" in (output / "app.go").read_text()


@pytest.mark.parametrize("framework", SOURCES)
def test_independent_project_plans_match(tmp_path: Path, framework: str) -> None:
    plans = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        (root / "app.py").write_text(routed_source(framework))
        result = handle(request(root, framework))
        assert result.outcome == "success", result.error
        plans.append(result.data)
    assert plans[0]["files"] == plans[1]["files"]
    assert plans[0]["plan_hash"] == plans[1]["plan_hash"]


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
def test_registration_before_app_blocks(tmp_path: Path, framework: str) -> None:
    text = routed_source(framework)
    app = "app = Flask(__name__)" if framework == "flask" else "app = FastAPI()"
    text = text.replace(app + "\n", "") + app + "\n"
    (tmp_path / "app.py").write_text(text)
    result = handle(request(tmp_path, framework))
    assert result.data["capture"]["gaps"]
    assert result.data["files"] == {}


def test_factory_hooks_block(tmp_path: Path) -> None:
    text = routed_source("flask", factory=True).replace(
        "    return app", '    app.config["SECRET_KEY"] = "unhandled"\n    return app'
    )
    (tmp_path / "app.py").write_text(text)
    result = handle(request(tmp_path))
    assert result.data["capture"]["gaps"]
    assert result.data["files"] == {}
