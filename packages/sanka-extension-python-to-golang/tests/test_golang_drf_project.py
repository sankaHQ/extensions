# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Conventional Django projects must retain serializer and transaction semantics."""

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path
from textwrap import indent

import pytest
from sanka_extension_python_to_golang.capture import capture, configuration
from sanka_extension_python_to_golang.drf_replay import replay_project, scenario_groups
from sanka_extension_python_to_golang.render import render

FIXTURE = Path(__file__).parent / "fixtures/drf_project"


def authenticated_project(tmp_path):
    from test_golang_jwt import JWT_BODY

    config = readonly_project(tmp_path)
    (tmp_path / "orders/auth.py").write_text(
        "from os import environ\n"
        "from jwt import decode, get_unverified_header, InvalidTokenError\n"
        "from re import fullmatch\n"
        "from time import time\n"
        "from rest_framework.authentication import BaseAuthentication\n"
        "from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied\n"
        "from types import SimpleNamespace\n"
        "class AuthenticationUnavailable(APIException):\n"
        "    status_code = 503\n"
        '    default_detail = "authentication unavailable"\n'
        "class JWTAuthentication(BaseAuthentication):\n"
        "    def authenticate(self, request):\n"
        + indent(
            JWT_BODY.replace("UNAVAILABLE", "raise AuthenticationUnavailable()")
            .replace("UNAUTHENTICATED", 'raise AuthenticationFailed("not authenticated")')
            .replace("FORBIDDEN", 'raise PermissionDenied("permission denied")')
            + 'return SimpleNamespace(is_authenticated=True, pk=claims["sub"]), claims\n',
            "        ",
        )
        + '    def authenticate_header(self, request):\n        return "Bearer"\n'
    )
    views = tmp_path / "orders/views.py"
    source = views.read_text()
    source = (
        "from orders.auth import JWTAuthentication\n"
        "from rest_framework.permissions import IsAuthenticated\n" + source
    )
    source = source.replace(
        "    serializer_class = OrderSerializer",
        "    serializer_class = OrderSerializer\n"
        "    authentication_classes = [JWTAuthentication]\n"
        "    permission_classes = [IsAuthenticated]",
    )
    views.write_text(source)
    scenarios = tmp_path / "sanka-verify.json"
    document = json.loads(scenarios.read_text())
    document["scenarios"].append(
        {
            "id": "signed-token-with-irrelevant-session-cookie",
            "method": "GET",
            "path": "/api/orders/",
            "headers": {"Cookie": "sessionid=legacy"},
            "expected_status": 200,
        }
    )
    scenarios.write_text(json.dumps(document))
    return config


def multiapp_project(tmp_path):
    config = project(tmp_path)
    shutil.copytree(tmp_path / "orders", tmp_path / "sales")
    for path in (tmp_path / "sales").rglob("*.py"):
        path.write_text(path.read_text().replace("orders", "sales"))
    settings = tmp_path / "shop_config/settings.py"
    settings.write_text(settings.read_text().replace('"orders",', '"orders", "sales",'))
    for app in ("orders", "sales"):
        (tmp_path / app / "urls.py").write_text(
            "from django.urls import path, include\n"
            "from rest_framework.routers import DefaultRouter\n"
            f"from {app}.views import OrderViewSet as View\n"
            "router = DefaultRouter()\n"
            "router.register('orders', View, basename='order')\n"
            "urlpatterns = [path('', include(router.urls))]\n"
        )
    (tmp_path / "shop_config/urls.py").write_text(
        "from django.urls import path, include\n"
        "urlpatterns = [path('shop/', include('orders.urls')), "
        "path('sales/', include('sales.urls'))]\n"
    )
    return config


def readonly_project(tmp_path):
    config = project(tmp_path)
    views = tmp_path / "orders/views.py"
    views.write_text(
        views.read_text().replace(
            "from rest_framework.viewsets import ModelViewSet",
            "from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet",
        )
        + "\nclass OrderReadOnlyViewSet(ReadOnlyModelViewSet):\n"
        "    queryset = Order.objects.all()\n"
        "    serializer_class = OrderSerializer\n"
    )
    urls = tmp_path / "shop_config/urls.py"
    urls.write_text(
        urls.read_text()
        .replace("import OrderViewSet\n", "import OrderViewSet, OrderReadOnlyViewSet\n")
        .replace(
            'router.register("orders", OrderViewSet, basename="order")',
            'router.register("orders", OrderViewSet, basename="order")\n'
            'router.register("readonly-orders", OrderReadOnlyViewSet, basename="readonly-order")',
        )
    )
    document = tmp_path / "sanka-verify.json"
    payload = json.loads(document.read_text())
    seeded = payload["scenarios"][0]["setup"]
    payload["scenarios"] += [
        {
            "id": "readonly-list",
            "method": "GET",
            "path": "/api/readonly-orders/",
            "expected_status": 200,
            "setup": seeded,
        },
        {
            "id": "readonly-detail",
            "method": "GET",
            "path": "/api/readonly-orders/1/",
            "expected_status": 200,
            "setup": seeded,
        },
        {
            "id": "readonly-head",
            "method": "HEAD",
            "path": "/api/readonly-orders/",
            "expected_status": 200,
        },
        {
            "id": "readonly-post-denied",
            "method": "POST",
            "path": "/api/readonly-orders/",
            "body": payload["scenarios"][2]["body"],
            "expected_status": 405,
        },
        {
            "id": "readonly-delete-denied",
            "method": "DELETE",
            "path": "/api/readonly-orders/1/",
            "expected_status": 405,
            "setup": seeded,
        },
        *[
            {
                "id": "readonly-" + method.lower() + "-denied",
                "method": method,
                "path": "/api/readonly-orders/1/",
                "body": payload["scenarios"][2]["body"],
                "expected_status": 405,
                "setup": seeded,
            }
            for method in ("PUT", "PATCH")
        ],
    ]
    document.write_text(json.dumps(payload))
    return config


def test_multiapp_drf_duplicate_model_names_and_nested_urls(tmp_path):
    config = multiapp_project(tmp_path)
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    assert len({m["name"] for m in result["models"]}) == 4
    assert {m["table"] for m in result["models"]} == {
        "orders_order",
        "orders_orderitem",
        "sales_order",
        "sales_orderitem",
    }
    assert [v["path"] for v in result["drf_project"]["views"]] == [
        "/shop/orders/",
        "/sales/orders/",
    ]
    assert capture(tmp_path, config) == result


def test_conventional_main_wrapper_and_safe_aliases(tmp_path):
    config = project(tmp_path)
    (tmp_path / "manage.py").write_text(
        "import os as environment\nimport sys\n"
        "def main():\n"
        '    environment.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop_config.settings")\n'
        "    from django.core.management import execute_from_command_line as execute\n"
        "    execute(sys.argv)\n"
        'if __name__ == "__main__":\n    main()\n'
    )
    assert capture(tmp_path, config)["gaps"] == []


def test_standard_django_startup_and_environment_secret(tmp_path):
    config = project(tmp_path)
    (tmp_path / "manage.py").write_text('''import os
import sys
def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop_config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError("Could not import Django") from exc
    execute_from_command_line(sys.argv)
if __name__ == "__main__":
    main()
''')
    path = tmp_path / "shop_config/settings.py"
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "SECRET_KEY":
            node.value = ast.parse('os.environ["FIXTURE_SECRET"]', mode="eval").body
    path.write_text(ast.unparse(tree))
    assert capture(tmp_path, config)["gaps"] == []


def test_startup_import_fallback_cannot_execute_custom_code(tmp_path):
    config = project(tmp_path)
    path = tmp_path / "manage.py"
    path.write_text(path.read_text() + '\nopen("executed", "w").write("bad")\n')
    assert capture(tmp_path, config)["gaps"]
    assert not (tmp_path / "executed").exists()


def test_multiapp_url_cycle_has_location(tmp_path):
    config = multiapp_project(tmp_path)
    (tmp_path / "orders/urls.py").write_text(
        "from django.urls import path, include\n"
        "urlpatterns = [path('', include('shop_config.urls'))]\n"
    )
    result = capture(tmp_path, config)
    assert not result["generation_ready"]
    assert "orders/urls.py" in str(result["gaps"])
    assert "cycle" in str(result["gaps"]).lower()


def test_multiapp_import_cycle_rejected(tmp_path):
    config = multiapp_project(tmp_path)
    for app, other in (("orders", "sales"), ("sales", "orders")):
        path = tmp_path / app / "models.py"
        path.write_text(f"from {other}.models import Order as Foreign\n" + path.read_text())
    result = capture(tmp_path, config)
    assert "import cycle" in str(result["gaps"])


def test_duplicate_basename_on_one_router_rejected(tmp_path):
    config = multiapp_project(tmp_path)
    (tmp_path / "shop_config/urls.py").write_text("""from django.urls import path, include
from rest_framework.routers import DefaultRouter
from orders.views import OrderViewSet as First
from sales.views import OrderViewSet as Second
router = DefaultRouter()
router.register('orders', First, basename='order')
router.register('sales', Second, basename='order')
urlpatterns = [path('', include(router.urls))]
""")
    # Remove the now-unreachable modules rather than hiding them from inventory.
    for app in ("orders", "sales"):
        (tmp_path / app / "urls.py").unlink()
    assert capture(tmp_path, config)["gaps"]


def test_multiapp_generation_is_location_and_hashseed_independent(tmp_path):
    roots = [tmp_path / "a", tmp_path / "b"]
    results = []
    for seed, root in enumerate(roots):
        config = multiapp_project(root)
        completed = subprocess.run(
            [
                os.sys.executable,
                "-c",
                "import json,sys; from pathlib import Path; from sanka_extension_python_to_golang.capture import capture; from sanka_extension_python_to_golang.render import render; print(json.dumps(render(capture(Path(sys.argv[1]),json.loads(sys.argv[2]))),sort_keys=True))",
                str(root),
                json.dumps(config),
            ],
            env=os.environ | {"PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(completed.stdout)
    assert results[0] == results[1]


def crossapp_project(tmp_path):
    config = project(tmp_path)
    (tmp_path / "shipping/migrations").mkdir(parents=True)
    for name in ("shipping/__init__.py", "shipping/migrations/__init__.py"):
        (tmp_path / name).write_text("")
    settings = tmp_path / "shop_config/settings.py"
    settings.write_text(settings.read_text().replace('"orders",', '"orders", "shipping",'))
    path = tmp_path / "orders/models.py"
    tree = ast.parse(path.read_text())
    child = tree.body.pop()
    child.body[0].value.args[0] = ast.Constant("orders.Order")
    path.write_text(ast.unparse(tree))
    (tmp_path / "shipping/models.py").write_text(
        "from django.db import models\n" + ast.unparse(child)
    )
    path = tmp_path / "orders/serializers.py"
    path.write_text(
        path.read_text().replace(
            "from orders.models import Order, OrderItem",
            "from orders.models import Order\nfrom shipping.models import OrderItem",
        )
    )
    path = tmp_path / "orders/migrations/0001_initial.py"
    tree = ast.parse(path.read_text())
    operations = next(
        n.value
        for n in tree.body[-1].body
        if isinstance(n, ast.Assign) and n.targets[0].id == "operations"
    )
    child = operations.elts.pop()
    path.write_text(ast.unparse(tree))
    operations.elts = [child]
    dependency = next(
        n
        for n in tree.body[-1].body
        if isinstance(n, ast.Assign) and n.targets[0].id == "dependencies"
    )
    dependency.value = ast.parse("[('orders', '0001_initial')]", mode="eval").body
    (tmp_path / "shipping/migrations/0001_initial.py").write_text(ast.unparse(tree))
    return config


def test_crossapp_foreign_key_and_initial_migrations(tmp_path):
    config = crossapp_project(tmp_path)
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    child = next(m for m in result["models"] if m["table"] == "shipping_orderitem")
    assert (
        next(f for f in child["fields"] if f["name"] == "order_id")["references"]["table"]
        == "orders_order"
    )
    assert result["drf_project"]["views"][0]["serializer"]["nested"]


def test_crossapp_missing_migration_dependency_rejected(tmp_path):
    config = crossapp_project(tmp_path)
    path = tmp_path / "shipping/migrations/0001_initial.py"
    path.write_text(path.read_text().replace("[('orders', '0001_initial')]", "[]"))
    assert capture(tmp_path, config)["gaps"]


def test_initial_dependency_cannot_reference_package_initializer(tmp_path):
    config = crossapp_project(tmp_path)
    path = tmp_path / "shipping/migrations/0001_initial.py"
    path.write_text(path.read_text().replace("'0001_initial'", "'__init__'"))
    assert capture(tmp_path, config)["gaps"]


def test_nested_router_cannot_shadow_a_prior_detail_route(tmp_path):
    config = multiapp_project(tmp_path)
    path = tmp_path / "shop_config/urls.py"
    path.write_text(path.read_text().replace("'sales/'", "'shop/orders/'"))
    assert capture(tmp_path, config)["gaps"]


def postgres_project(tmp_path):
    config = crossapp_project(tmp_path)
    path = tmp_path / "shop_config/settings.py"
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "DATABASES":
            node.value = ast.parse(
                "{'default': {'ENGINE': 'django.db.backends.postgresql', "
                + ", ".join(
                    repr(k) + ": os.environ[" + repr("FIXTURE_PG_" + k) + "]"
                    for k in ("NAME", "USER", "PASSWORD", "HOST", "PORT")
                )
                + "}}",
                mode="eval",
            ).body
    path.write_text(ast.unparse(tree))
    serializer = tmp_path / "orders/serializers.py"
    serializer.write_text(
        serializer.read_text().replace("    quantity = serializers.IntegerField(min_value=1)\n", "")
    )
    path = tmp_path / "sanka-verify.json"
    document = json.loads(path.read_text())
    document["scenarios"].append(
        {
            "id": "integer-overflow",
            "method": "POST",
            "path": "/api/orders/",
            "expected_status": 400,
            "body": {
                "reference": "overflow",
                "items": [{"sku": "a", "quantity": 2147483648, "price": "1.00"}],
            },
        }
    )
    huge = json.loads(json.dumps(document["scenarios"][-1]))
    huge["id"] = "integer-huge"
    huge["body"]["items"][0]["quantity"] = 2**70
    document["scenarios"].append(huge)
    path.write_text(json.dumps(document))
    return config


def test_postgres_conventional_capture_preserves_native_sequences(tmp_path):
    result = capture(tmp_path, postgres_project(tmp_path))
    assert result["gaps"] == []
    assert result["drf_project"]["database"]["engine"] == "postgresql"
    generated = render(result)
    assert "migration_identity" not in generated["migrations/00001_initial.sql"]
    assert "GENERATED BY DEFAULT AS IDENTITY" in generated["migrations/00001_initial.sql"]
    assert "nextval" in generated["drf.go"]


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
@pytest.mark.parametrize(
    "factory",
    [multiapp_project, crossapp_project, postgres_project, readonly_project, authenticated_project],
)
def test_general_project_native_replay(tmp_path, monkeypatch, target, factory):
    dsn = os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN")
    if os.getenv("SANKA_GO_TESTS") != "1" or not dsn:
        pytest.skip("requires native Go and isolated PostgreSQL fixture")
    import uuid

    import psycopg
    from psycopg import sql
    from test_golang_schema import schema_dsn

    config = factory(tmp_path) | {"target_framework": target}
    scenario = tmp_path / "sanka-verify.json"
    if factory is multiapp_project:
        document = json.loads(scenario.read_text().replace("/api/", "/shop/"))
        sales = json.loads(json.dumps(document["scenarios"]).replace("/shop/", "/sales/"))
        for case in sales:
            case["id"] = "sales-" + case["id"]
        document["scenarios"] += sales
        document["scenarios"].append(
            {
                "id": "missing-order",
                "method": "GET",
                "path": "/shop/orders/999/",
                "expected_status": 404,
            }
        )
        scenario.write_text(json.dumps(document))
    elif factory is postgres_project:
        document = json.loads(scenario.read_text())
        document.pop("db_env", None)
        scenario.write_text(json.dumps(document))
    captured = capture(tmp_path, config)
    assert not captured["gaps"], captured["gaps"]
    output = tmp_path / ".sanka/candidate"
    for name, contents in render(captured).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    name = "general_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", schema_dsn(dsn, name))
            monkeypatch.setenv("SANKA_GO_SOURCE_TEST_DATABASE_URL", dsn)
            report = replay_project(tmp_path, output, captured, "verify")
            assert report["ok"], report["failures"]
            assert report["candidate"] == report["source"]
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def project(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return configuration(
        {
            "source_framework": "drf",
            "source_file": "shop_config/urls.py",
            "models_file": "orders/models.py",
            "database_layer": "pgx",
        }
    )


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_capture(tmp_path, target):
    config = project(tmp_path) | {"target_framework": target}
    result = capture(tmp_path, config)
    assert result["gaps"] == []
    assert result["generation_ready"]
    assert result == capture(tmp_path, config)
    assert {model["table"] for model in result["models"]} == {"orders_order", "orders_orderitem"}
    contract = result["drf_project"]
    assert contract["settings_module"] == "shop_config.settings"
    assert contract["database"]["engine"] == "sqlite"
    view = contract["views"][0]
    assert view["path"] == "/api/orders/"
    assert view["serializer"]["nested"]["aggregate"]["limit"] == 100
    assert view["serializer"]["nested"]["update"] == "ignore"


def test_readonly_viewset_capture_restricts_methods(tmp_path):
    result = capture(tmp_path, readonly_project(tmp_path))
    assert result["gaps"] == []
    views = result["drf_project"]["views"]
    assert [v["read_only"] for v in views] == [False, True]
    assert {
        (route["path"], route["method"])
        for route in result["routes"]
        if "readonly-orders" in route["path"]
    } == {
        ("/api/readonly-orders/", "GET"),
        ("/api/readonly-orders/:id/", "GET"),
    }


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_signed_project_capture_and_render(tmp_path, target):
    config = authenticated_project(tmp_path) | {"target_framework": target}
    captured = capture(tmp_path, config)
    assert captured["gaps"] == []
    assert captured == capture(tmp_path, config)
    assert captured["drf_project"]["authentication"] == "jwt-hs256-roles"
    assert all(
        view["authentication"] == "jwt-hs256-roles" for view in captured["drf_project"]["views"]
    )
    generated = render(captured)
    assert "github.com/golang-jwt/jwt/v5" in generated["go.mod"]
    assert "AUTH_JWT_SECRET=\n" in generated[".env.example"]
    assert "accessStatus(authorization,method,path)" in generated["drf.go"]


@pytest.mark.parametrize(
    "filename,old,new",
    [
        (
            "orders/auth.py",
            'default_detail = "authentication unavailable"',
            'default_detail = "okay"',
        ),
        ("orders/views.py", "permission_classes = [IsAuthenticated]", "permission_classes = []"),
        (
            "orders/views.py",
            "authentication_classes = [JWTAuthentication]",
            "authentication_classes = []",
        ),
    ],
)
def test_conventional_drf_auth_changes_fail_closed(tmp_path, filename, old, new):
    config = authenticated_project(tmp_path)
    path = tmp_path / filename
    path.write_text(path.read_text().replace(old, new, 1))
    assert capture(tmp_path, config)["gaps"]


def test_conventional_drf_auth_replay_covers_writer_and_denied_state(tmp_path):
    from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV

    captured = capture(tmp_path, authenticated_project(tmp_path))
    groups = scenario_groups(tmp_path, captured)
    first = groups[0]
    assert first[0]["headers"]["authorization"].startswith("Bearer ")
    assert {case["id"].split(":")[-1] for case in first[1:]} >= {
        "writer",
        "missing",
        "invalid",
        "reader",
    }
    assert [case["expected_status"] for case in first[1:5]] == [200, 401, 401, 200]
    assert REPLAY_JWT_ENV["AUTH_JWT_SECRET"] not in str(captured)


@pytest.mark.parametrize(
    "filename, old, new",
    [
        (
            "orders/views.py",
            "queryset = Order.objects.all()",
            "queryset = Order.objects.filter(status__contains='paid')",
        ),
        ("orders/serializers.py", "return order", "order.memo = 'changed'\n        return order"),
        (
            "shop_config/settings.py",
            "MIDDLEWARE: list[str] = []",
            "MIDDLEWARE = ['custom.Middleware']",
        ),
    ],
)
def test_unknown_drf_behavior_blocks_generation(tmp_path, filename, old, new):
    config = project(tmp_path)
    path = tmp_path / filename
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))
    result = capture(tmp_path, config)
    assert result["gaps"]
    assert not result["generation_ready"]


@pytest.mark.parametrize(
    "filename,old,new",
    [
        (
            "orders/migrations/0001_initial.py",
            "max_length=30, unique=True",
            "max_length=31, unique=True",
        ),
        (
            "orders/migrations/0001_initial.py",
            "    initial = True",
            "    initial = True\n    atomic = False",
        ),
        (
            "orders/serializers.py",
            "from orders.models import Order, OrderItem",
            "from orders.models import Order",
        ),
        (
            "orders/serializers.py",
            "    items = OrderItemSerializer(many=True)",
            "    items = OrderItemSerializer(many=True)\n    items = OrderItemSerializer(many=True)",
        ),
    ],
)
def test_conventional_project_rejects_schema_and_binding_drift(tmp_path, filename, old, new):
    config = project(tmp_path)
    path = tmp_path / filename
    path.write_text(path.read_text().replace(old, new))
    assert capture(tmp_path, config)["gaps"]


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_renders_backend(tmp_path, target):
    result = capture(tmp_path, project(tmp_path) | {"target_framework": target})
    generated = render(result)
    assert "drf.go" in generated
    assert "cmd/api/main.go" in generated
    assert "migration_identity" in generated["migrations/00001_initial.sql"]
    assert "ON DELETE CASCADE" in generated["migrations/00001_initial.sql"]


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
@pytest.mark.parametrize("factory", [project, authenticated_project])
def test_conventional_drf_native_validation(tmp_path, target, factory):
    if os.getenv("SANKA_GO_TESTS") != "1":
        pytest.skip("requires native Go")
    result = capture(tmp_path, factory(tmp_path) | {"target_framework": target})
    output = tmp_path / ".sanka/candidate"
    for name, contents in render(result).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (output / "drf_validation_test.go").write_text(r"""package backend
import "testing"
func TestDRFValidation(t *testing.T) {
    schema := drfSchema.Project.Views[0].Serializer
    values, invalid := drfValidate([]byte(`{"reference":" A ","items":[{"sku":"x","quantity":"2","price":"1.00"}]}`),schema,false)
    if len(invalid)>0 || values["reference"]!="A" || len(values)!=2 { t.Fatalf("valid/defaults: %#v %#v", values,invalid) }
    _, invalid = drfValidate([]byte(`{"reference":"","items":[{"sku":"x","quantity":0,"price":"1.00"}]}`),schema,false)
    if len(invalid)!=2 { t.Fatalf("nested errors: %#v",invalid) }
    values, invalid = drfValidate([]byte(`{}`),schema,true)
    if len(invalid)>0 || len(values)>0 { t.Fatalf("partial defaults: %#v %#v",values,invalid) }
}
""")
    if factory is authenticated_project:
        from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV, replay_token

        response = (
            'reply,err:=app.Test(request);if err!=nil { t.Fatal(err) };defer reply.Body.Close();status:=reply.StatusCode;challenge:=reply.Header.Get("WWW-Authenticate")'
            if target == "fiber"
            else 'reply:=httptest.NewRecorder();app.ServeHTTP(reply,request);status:=reply.Code;challenge:=reply.Header().Get("WWW-Authenticate")'
        )
        duplicate = (
            "reply2,err:=app.Test(request2);if err!=nil { t.Fatal(err) };defer reply2.Body.Close();status2:=reply2.StatusCode"
            if target == "fiber"
            else "reply2:=httptest.NewRecorder();app.ServeHTTP(reply2,request2);status2:=reply2.Code"
        )
        token = replay_token(
            {
                "iss": REPLAY_JWT_ENV["AUTH_JWT_ISSUER"],
                "aud": REPLAY_JWT_ENV["AUTH_JWT_AUDIENCE"],
                "sub": "fixture-user",
                "tenant": "fixture-tenant",
                "role": "writer",
                "exp": 4102444800,
            }
        )
        (output / "drf_auth_test.go").write_text(
            "package backend\n"
            'import ("net/http/httptest"; "testing"; "github.com/jackc/pgx/v5/pgxpool")\n'
            "func TestDRFAuthChallenge(t *testing.T) {\n"
            ' t.Setenv("AUTH_JWT_SECRET", "sanka-isolated-replay-signing-key-32-bytes")\n'
            ' t.Setenv("AUTH_JWT_ISSUER", "sanka-replay")\n'
            ' t.Setenv("AUTH_JWT_AUDIENCE", "sanka-replay-backend")\n'
            ' app:=NewApp(&pgxpool.Pool{});request:=httptest.NewRequest("GET","http://testserver/api/orders/",nil)\n'
            + response
            + '\n if status!=401 || challenge!="Bearer" { t.Fatalf("status=%d challenge=%q",status,challenge) }\n'
            + ' request2:=httptest.NewRequest("POST","http://testserver/api/readonly-orders/",nil)\n'
            + f' request2.Header.Add("Authorization",{json.dumps(token)})\n'
            + ' request2.Header.Add("Authorization","Bearer invalid")\n'
            + duplicate
            + '\n if status2!=401 { t.Fatalf("duplicate Authorization status=%d",status2) }\n}\n'
        )
    completed = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "./..."],
        cwd=output,
        env=os.environ | {"GOMAXPROCS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_independent_scenario_setup_is_observed(tmp_path):
    captured = capture(tmp_path, project(tmp_path))
    groups = scenario_groups(tmp_path, captured)
    assert len(groups) == 7
    assert [len(group) for group in groups] == [2, 2, 1, 2, 2, 2, 2]
    assert groups[0][0]["method"] == "POST"
    assert groups[0][0]["expected_status"] == 201


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_conventional_drf_sqlite_postgresql_parity(tmp_path, monkeypatch, target):
    dsn = os.getenv("SANKA_MIGRATE_TEST_POSTGRES_DSN")
    if os.getenv("SANKA_GO_TESTS") != "1" or not dsn:
        pytest.skip("requires native Go and isolated PostgreSQL fixture")
    import uuid

    import psycopg
    from psycopg import sql
    from test_golang_schema import schema_dsn

    name = "drf_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv("SANKA_GO_TARGET_TEST_DATABASE_URL", schema_dsn(dsn, name))
            config = project(tmp_path) | {"target_framework": target}
            scenario_file = tmp_path / "sanka-verify.json"
            document = json.loads(scenario_file.read_text())
            for i, price in enumerate(
                ["1.234", "1234567.00", "123456789", "bad", "NaN", "0.001", None]
            ):
                document["scenarios"].append(
                    {
                        "id": f"decimal-{i}",
                        "method": "POST",
                        "path": "/api/orders/",
                        "expected_status": 400,
                        "body": {
                            "reference": "invalid",
                            "items": [{"sku": "a", "quantity": 1, "price": price}],
                        },
                    }
                )
            document["scenarios"].append(
                {
                    "id": "choice-whitespace",
                    "method": "POST",
                    "path": "/api/orders/",
                    "expected_status": 400,
                    "body": {"reference": "invalid", "status": " paid ", "items": []},
                }
            )
            setup = {
                "method": "POST",
                "path": "/api/orders/",
                "body": {"reference": "original", "status": "paid", "memo": "kept", "items": []},
            }
            document["scenarios"].append(
                {
                    "id": "put-optional-absent",
                    "setup": [setup],
                    "method": "PUT",
                    "path": "/api/orders/1/",
                    "expected_status": 200,
                    "body": {"reference": "replaced", "items": []},
                }
            )
            for i, body in enumerate(
                [
                    {},
                    {"reference": "x"},
                    {"reference": "x", "items": None},
                    {"reference": "x", "items": [None]},
                    {"reference": "x", "items": [{}, {"sku": "x", "quantity": 0, "price": "bad"}]},
                    {"reference": "x", "status": "", "items": []},
                ]
            ):
                document["scenarios"].append(
                    {
                        "id": f"validation-{i}",
                        "method": "POST",
                        "path": "/api/orders/",
                        "expected_status": 400,
                        "body": body,
                    }
                )
            rollback = dict(document["scenarios"][6])
            rollback.pop("setup")
            rollback.pop("id")
            document["scenarios"].append(
                {
                    "id": "create-after-rollback",
                    "setup": [setup, rollback],
                    "method": "POST",
                    "path": "/api/orders/",
                    "expected_status": 201,
                    "body": {
                        "reference": "after",
                        "items": [{"sku": "a", "quantity": 1, "price": "0.00"}],
                    },
                }
            )
            for i, choice in enumerate([True, {}, [], {"key": [True, None, 1.0]}, 1.0]):
                document["scenarios"].append(
                    {
                        "id": f"choice-type-{i}",
                        "method": "POST",
                        "path": "/api/orders/",
                        "expected_status": 400,
                        "body": {"reference": "choice", "status": choice, "items": []},
                    }
                )
            scenario_file.write_text(json.dumps(document))
            captured = capture(tmp_path, config)
            output = tmp_path / ".sanka/candidate"
            for filename, contents in render(captured).items():
                path = output / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            report = replay_project(tmp_path, output, captured, "verify")
            assert report["ok"], report["failures"]
            assert report["source"] == report["candidate"]
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.mark.parametrize(
    "case", ["annotation", "id-override", "nested-unique", "optional-aggregate"]
)
def test_review_capture_boundaries(tmp_path, case):
    config = project(tmp_path)
    model = tmp_path / "orders/models.py"
    serializer = tmp_path / "orders/serializers.py"
    migration = tmp_path / "orders/migrations/0001_initial.py"
    if case == "annotation":
        model.write_text(
            model.read_text().replace(
                "reference =", 'reference: setattr(reference, "max_length", 1) ='
            )
        )
    elif case == "id-override":
        serializer.write_text(
            serializer.read_text().replace(
                "class OrderSerializer(serializers.ModelSerializer):",
                "class OrderSerializer(serializers.ModelSerializer):\n    id = serializers.IntegerField(min_value=1)",
            )
        )
    elif case == "nested-unique":
        model.write_text(
            model.read_text().replace(
                "sku = models.CharField(max_length=30)",
                "sku = models.CharField(max_length=30, unique=True)",
            )
        )
        migration.write_text(
            migration.read_text().replace(
                '("sku", models.CharField(max_length=30))',
                '("sku", models.CharField(max_length=30, unique=True))',
            )
        )
    else:
        for path in [model, migration]:
            path.write_text(
                path.read_text().replace(
                    "models.PositiveIntegerField()", "models.PositiveIntegerField(default=1)"
                )
            )
        serializer.write_text(
            serializer.read_text().replace(
                "    quantity = serializers.IntegerField(min_value=1)\n", ""
            )
        )
    result = capture(tmp_path, config)
    assert result["gaps"] and not result["generation_ready"]


def test_explicit_integer_override_replaces_model_validation(tmp_path):
    config = project(tmp_path)
    for name in ["orders/models.py", "orders/migrations/0001_initial.py"]:
        path = tmp_path / name
        path.write_text(
            path.read_text().replace(
                "models.PositiveIntegerField()",
                "models.PositiveIntegerField(default=1, unique=True)",
            )
        )
    captured = capture(tmp_path, config)
    assert not captured["gaps"]
    fields = captured["drf_project"]["views"][0]["serializer"]["nested"]["serializer"]["fields"]
    field = next(f for f in fields if f["name"] == "quantity")
    assert field["required"] is True and field["unique"] is False and "default" not in field
