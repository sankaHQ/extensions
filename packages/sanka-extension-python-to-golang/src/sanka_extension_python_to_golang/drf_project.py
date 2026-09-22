# SPDX-License-Identifier: Apache-2.0
"""Static, bounded conventional Django/DRF contracts; never import source code."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .models import DJANGO_TYPES, _field, identifier


def _same(node: ast.AST, source: str) -> bool:
    expected = ast.parse(source)
    return ast.dump(node) == ast.dump(expected.body[0])


def _assignments(body: list[ast.stmt]) -> dict[str, ast.expr]:
    result = {}
    for node in body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            if ast.unparse(node.annotation) not in {"str", "int", "bool", "list[str]"}:
                raise ValueError("unqualified declaration annotation")
            name, value = node.target.id, node.value
        else:
            raise ValueError("only explicit static declarations are qualified")
        if name in result:
            raise ValueError("reassigned declaration: " + name)
        result[name] = value
    return result


def _settings(root: Path) -> tuple[str, dict[str, Any], set[str]]:
    manage = ast.parse((root / "manage.py").read_text())
    calls = [
        n
        for n in ast.walk(manage)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.environ.setdefault"
    ]
    if (
        len(calls) != 1
        or len(calls[0].args) != 2
        or _literal(calls[0].args[0]) != "DJANGO_SETTINGS_MODULE"
    ):
        raise ValueError("manage.py requires a static settings module")
    name = _literal(calls[0].args[1])
    expected = ast.parse(f"""import os
import sys
if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", {name!r})
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
""")
    if ast.dump(manage) != ast.dump(expected):
        raise ValueError("manage.py contains uncaptured startup behavior")
    filename, tree = _module(root, name)
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    allowed = {"from __future__ import annotations", "import os", "from pathlib import Path"}
    if {ast.unparse(n) for n in imports} != allowed:
        raise ValueError("settings requires explicit os/Path imports")
    values = _assignments([n for n in tree.body if n not in imports])
    required = {
        "BASE_DIR",
        "SECRET_KEY",
        "DEBUG",
        "ALLOWED_HOSTS",
        "ROOT_URLCONF",
        "MIDDLEWARE",
        "INSTALLED_APPS",
        "DATABASES",
        "DEFAULT_AUTO_FIELD",
        "USE_TZ",
        "TIME_ZONE",
        "REST_FRAMEWORK",
    }
    if set(values) != required:
        raise ValueError("settings contains unqualified configuration")
    if ast.unparse(values["BASE_DIR"]) != "Path(__file__).resolve().parent.parent":
        raise ValueError("unsupported project base directory")
    if (
        not isinstance(_literal(values["SECRET_KEY"]), str)
        or type(_literal(values["DEBUG"])) is not bool
    ):
        raise ValueError("settings key/debug must be static")
    if _literal(values["MIDDLEWARE"]) != []:
        raise ValueError("custom Django middleware requires capture")
    if _literal(values["REST_FRAMEWORK"]) != {"UNAUTHENTICATED_USER": None}:
        raise ValueError("DRF settings require separate capture")
    if _literal(values["USE_TZ"]) is not True or _literal(values["TIME_ZONE"]) != "UTC":
        raise ValueError("only explicit UTC settings are qualified")
    auto = _literal(values["DEFAULT_AUTO_FIELD"])
    if auto not in {"django.db.models.AutoField", "django.db.models.BigAutoField"}:
        raise ValueError("unknown implicit primary key")
    apps = _literal(values["INSTALLED_APPS"])
    builtin = {"django.contrib.auth", "django.contrib.contenttypes", "rest_framework"}
    if not isinstance(apps, list) or len(set(apps)) != len(apps) or not builtin <= set(apps):
        raise ValueError("unsupported installed apps")
    local = [a for a in apps if a not in builtin]
    if len(local) != 1 or not re.fullmatch(r"[a-z][a-z0-9_]*", local[0]):
        raise ValueError("one conventional Django application is qualified")
    db = values["DATABASES"]
    if (
        not isinstance(db, ast.Dict)
        or any(k is None for k in db.keys)
        or [_literal(k) for k in db.keys] != ["default"]
        or not isinstance(db.values[0], ast.Dict)
    ):
        raise ValueError("one explicit source database is required")
    opts = {_literal(k): v for k, v in zip(db.values[0].keys, db.values[0].values, strict=True)}
    if set(opts) != {"ENGINE", "NAME"} or _literal(opts["ENGINE"]) != "django.db.backends.sqlite3":
        raise ValueError("this conventional profile requires a SQLite source")
    dbname = opts["NAME"]
    if (
        not isinstance(dbname, ast.Call)
        or ast.unparse(dbname.func) != "os.environ.get"
        or len(dbname.args) != 2
        or dbname.keywords
        or not isinstance(dbname.args[0], ast.Constant)
    ):
        raise ValueError("SQLite path must use an explicit environment override")
    env = _literal(dbname.args[0])
    if (
        not isinstance(env, str)
        or not re.fullmatch(r"[A-Z][A-Z_0-9]*", env)
        or env in {"HOME", "PATH", "PYTHONPATH", "DJANGO_SETTINGS_MODULE"}
    ):
        raise ValueError("unsafe database environment variable")
    fallback = dbname.args[1]
    if (
        not isinstance(fallback, ast.Call)
        or ast.unparse(fallback.func) != "str"
        or fallback.keywords
        or len(fallback.args) != 1
        or not isinstance(fallback.args[0], ast.BinOp)
        or not isinstance(fallback.args[0].op, ast.Div)
        or ast.unparse(fallback.args[0].left) != "BASE_DIR"
        or not isinstance(_literal(fallback.args[0].right), str)
    ):
        raise ValueError("unrecognized SQLite fallback path")
    hosts = _literal(values["ALLOWED_HOSTS"])
    if not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) for h in hosts):
        raise ValueError("ALLOWED_HOSTS must be static strings")
    return (
        name,
        {
            "app": local[0],
            "urls": _literal(values["ROOT_URLCONF"]),
            "auto": auto,
            "hosts": hosts,
            "database": {"engine": "sqlite", "environment": env},
        },
        {"manage.py", filename},
    )


def capture_project(
    root: Path, config: dict[str, str], records: dict[str, str], total: int
) -> dict[str, Any]:
    from .capture import digest

    result: dict[str, Any] = {
        "schema": "sanka.python-to-golang.capture/v1",
        "configuration": config,
        "source_digest": digest(records),
        "routes": [],
        "models": [],
        "gaps": [],
        "scope": "conventional DRF JSON CRUD with nested atomic writes",
        "complete_backend": False,
        "generation_ready": False,
        "source_inventory": {
            "files": len(records),
            "python_files": sum(p.endswith(".py") for p in records),
            "bytes": total,
            "module_roles": {},
        },
    }
    try:
        if config["database_layer"] != "pgx" or config["schema_mode"] != "empty":
            raise ValueError("conventional DRF migration requires pgx and an empty target schema")
        settings_name, settings, consumed = _settings(root)
        app = settings["app"]
        models_file, model_tree = _module(root, app + ".models")
        if config["models_file"] != models_file:
            raise ValueError("models_file must match the installed Django application")
        models = _models(model_tree, app, settings["auto"])
        model_map = {m["name"]: m for m in models}
        serializers_file, serializer_tree = _module(root, app + ".serializers")
        serializers = _classes(
            serializer_tree,
            {
                "django.db": {"transaction"},
                "rest_framework": {"serializers"},
                app + ".models": set(model_map),
            },
            "serializers.ModelSerializer",
        )
        views_file, view_tree = _module(root, app + ".views")
        views = _classes(
            view_tree,
            {
                "rest_framework.viewsets": {"ModelViewSet"},
                app + ".models": set(model_map),
                app + ".serializers": set(serializers),
            },
            "ModelViewSet",
        )
        url_file, urls = _module(root, settings["urls"])
        if url_file != config["source_file"]:
            raise ValueError("source_file must match ROOT_URLCONF")
        expected_imports = {
            "django.urls": {"include", "path"},
            "rest_framework.routers": {"DefaultRouter"},
            app + ".views": set(views),
        }
        imported: set[str] = set()
        statements: list[ast.stmt] = []
        for node in urls.body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in expected_imports
                and not node.level
                and not statements
            ):
                for alias in node.names:
                    if (
                        alias.asname
                        or alias.name not in expected_imports[node.module]
                        or alias.name in imported
                    ):
                        raise ValueError("unsupported URL imports")
                    imported.add(alias.name)
            else:
                statements.append(node)
        if (
            not {"include", "path", "DefaultRouter", *views} <= imported
            or len(statements) < 3
            or not _same(statements[0], "router = DefaultRouter()")
        ):
            raise ValueError("URLs require an explicit DefaultRouter")
        urlpatterns = _assignments([statements[-1]])
        if (
            set(urlpatterns) != {"urlpatterns"}
            or not isinstance(urlpatterns["urlpatterns"], ast.List)
            or len(urlpatterns["urlpatterns"].elts) != 1
        ):
            raise ValueError("one literal router prefix is qualified")
        prefix_call = urlpatterns["urlpatterns"].elts[0]
        if not isinstance(prefix_call, ast.Call) or len(prefix_call.args) != 2:
            raise ValueError("unsupported URL prefix")
        prefix = _literal(prefix_call.args[0])
        if (
            not isinstance(prefix, str)
            or not re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)*", prefix)
            or not _same(statements[-1], f"urlpatterns = [path({prefix!r}, include(router.urls))]")
        ):
            raise ValueError("URL prefix must be a literal path")
        used = set()
        contracts: list[dict[str, Any]] = []
        for registration in statements[1:-1]:
            if not isinstance(registration, ast.Expr) or not isinstance(
                registration.value, ast.Call
            ):
                raise ValueError("unsupported router configuration")
            call = registration.value
            if (
                ast.unparse(call.func) != "router.register"
                or len(call.args) != 2
                or not isinstance(call.args[1], ast.Name)
                or len(call.keywords) != 1
                or call.keywords[0].arg != "basename"
            ):
                raise ValueError("router registration requires prefix, viewset and basename")
            route, view_name, basename = (
                _literal(call.args[0]),
                call.args[1].id,
                _literal(call.keywords[0].value),
            )
            if (
                not isinstance(route, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]+", route)
                or not isinstance(basename, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]+", basename)
                or view_name not in views
                or view_name in used
            ):
                raise ValueError("unresolved or duplicate router view")
            used.add(view_name)
            attrs = _assignments(views[view_name].body)
            if set(attrs) != {"queryset", "serializer_class"} or not isinstance(
                attrs["serializer_class"], ast.Name
            ):
                raise ValueError("viewset overrides require capture")
            serializer_name = attrs["serializer_class"].id
            if serializer_name not in serializers:
                raise ValueError("unresolved serializer")
            contract = _serializer(serializers[serializer_name], model_map, serializers)
            if ast.unparse(attrs["queryset"]) != contract["model"] + ".objects.all()":
                raise ValueError("viewset queryset must be the complete captured model")
            contracts.append(
                {"path": "/" + prefix + route + "/", "basename": basename, "serializer": contract}
            )
        if (
            used != set(views)
            or not contracts
            or len({v["path"] for v in contracts}) != len(contracts)
        ):
            raise ValueError("unconsumed or duplicate viewsets")
        used_serializers = {v["serializer"]["name"] for v in contracts}
        used_serializers.update(
            v["serializer"]["nested"]["serializer"]["name"]
            for v in contracts
            if v["serializer"].get("nested")
        )
        if used_serializers != set(serializers):
            raise ValueError("unconsumed serializer classes require capture")
        consumed.update({models_file, serializers_file, views_file, url_file})
        tests: list[str] = []
        migrations: list[str] = []
        for relative in records:
            if not relative.endswith(".py") or relative in consumed:
                continue
            tree = ast.parse((root / relative).read_text())
            path = Path(relative)
            if path.name == "__init__.py" and not tree.body:
                consumed.add(relative)
            elif relative == app + "/apps.py":
                classes = _classes(tree, {"django.apps": {"AppConfig"}}, "AppConfig")
                if len(classes) != 1 or {
                    k: _literal(v)
                    for k, v in _assignments(next(iter(classes.values())).body).items()
                } != {"name": app, "default_auto_field": settings["auto"]}:
                    raise ValueError("custom AppConfig requires capture")
                consumed.add(relative)
            elif path.name == "tests.py" or path.name.startswith("test_"):
                tests.append(relative)
            elif path.parts[:2] == (app, "migrations"):
                if migrations:
                    raise ValueError("only one initial schema migration is qualified")
                _migration(tree, models, app, settings["auto"])
                migrations.append(relative)
            else:
                raise ValueError("unclassified project module: " + relative)
        if not migrations:
            raise ValueError("an initial schema migration is required")
        result.update(
            models=models,
            drf_project={
                "settings_module": settings_name,
                "drf_version": "3.18",
                "database": settings["database"],
                "hosts": settings["hosts"],
                "views": contracts,
                "router_prefix": "/" + prefix,
            },
            source_modules=sorted(consumed),
            generation_ready=True,
        )
        result["source_inventory"]["module_roles"] = {
            "application": sorted(consumed),
            "tests": sorted(tests),
            "migrations": sorted(migrations),
            "unclassified": [],
        }
        for view in contracts:
            for method in ["GET", "POST"]:
                result["routes"].append(
                    {
                        "path": view["path"],
                        "method": method,
                        "status": 201 if method == "POST" else 200,
                    }
                )
            for method in ["GET", "PUT", "PATCH", "DELETE"]:
                result["routes"].append(
                    {
                        "path": view["path"] + ":id/",
                        "method": method,
                        "status": 204 if method == "DELETE" else 200,
                    }
                )
    except (ValueError, TypeError, KeyError, SyntaxError, OSError) as error:
        result["gaps"] = ["DRF project: " + str(error)]
        result["generation_ready"] = False
    return result


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        raise ValueError("expanded dictionary entries are unsupported")
    return ast.literal_eval(node)


def _module(root: Path, name: str) -> tuple[str, ast.Module]:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*(?:\.[a-zA-Z_][a-zA-Z_0-9]*)*", name):
        raise ValueError("invalid Python module")
    relative = name.replace(".", "/") + ".py"
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError("missing regular module: " + relative)
    return relative, ast.parse(path.read_text())


def _classes(tree: ast.Module, imports: dict[str, set[str]], base: str) -> dict[str, ast.ClassDef]:
    seen: set[str] = set()
    classes: dict[str, ast.ClassDef] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and not node.level and node.module in imports:
            for alias in node.names:
                if alias.asname or alias.name not in imports[node.module] or alias.name in seen:
                    raise ValueError("unknown or shadowed import")
                seen.add(alias.name)
        elif isinstance(node, ast.ClassDef):
            if (
                node.name in seen
                or node.decorator_list
                or node.keywords
                or node.type_params
                or [ast.unparse(v) for v in node.bases] != [base]
            ):
                raise ValueError("unsupported class inheritance or decorators")
            if base.split(".")[0] not in seen:
                raise ValueError("base must be imported before use")
            external = set().union(*imports.values()) | set(classes)
            referenced = {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            if referenced & external - seen:
                raise ValueError("class references a missing or late import")
            classes[node.name] = node
            seen.add(node.name)
        else:
            raise ValueError("module contains unqualified execution")
    return classes


def _models(tree: ast.Module, app: str, auto: str) -> list[dict[str, Any]]:
    classes = _classes(tree, {"django.db": {"models"}}, "models.Model")
    result: list[dict[str, Any]] = []
    for name, cls in classes.items():
        fields: list[dict[str, Any]] = []
        constants = {}
        ordering = ["id"]
        table = identifier(app + "_" + name.lower())
        for node in cls.body:
            if isinstance(node, ast.ClassDef) and node.name == "Meta":
                if node.bases or node.decorator_list or node.keywords:
                    raise ValueError("unsupported model Meta")
                meta = _assignments(node.body)
                if set(meta) - {"ordering", "db_table", "app_label"}:
                    raise ValueError("unsupported model Meta setting")
                if "ordering" in meta:
                    ordering = _literal(meta["ordering"])
                    if ordering != ["id"]:
                        raise ValueError("only primary-key ordering is qualified")
                if "db_table" in meta:
                    table = identifier(_literal(meta["db_table"]))
                if "app_label" in meta and _literal(meta["app_label"]) != app:
                    raise ValueError("model app_label differs from installed app")
                continue
            values = _assignments([node])
            field_name, value = next(iter(values.items()))
            if field_name in constants or any(f["source_name"] == field_name for f in fields):
                raise ValueError("reassigned model field")
            if not isinstance(value, ast.Call):
                if not field_name.isupper():
                    raise ValueError("unsupported model attribute")
                constants[field_name] = _literal(value)
                continue
            kind = ast.unparse(value.func)
            if not kind.startswith("models."):
                raise ValueError("unknown model field")
            kind = kind.removeprefix("models.")
            options = {}
            for keyword in value.keywords:
                if keyword.arg is None or keyword.arg in options:
                    raise ValueError("duplicate or expanded field arguments")
                options[keyword.arg] = keyword.value
            if kind == "ForeignKey":
                if (
                    len(value.args) != 1
                    or not isinstance(value.args[0], ast.Name)
                    or set(options) - {"on_delete", "related_name"}
                ):
                    raise ValueError("only explicit nonnullable foreign keys are qualified")
                parent = next((m for m in result if m["name"] == value.args[0].id), None)
                if (
                    parent is None
                    or ast.unparse(options.get("on_delete", ast.Constant(None))) != "models.CASCADE"
                ):
                    raise ValueError("foreign key requires preceding model and CASCADE")
                related = identifier(_literal(options["related_name"]))
                pk = next(f for f in parent["fields"] if f["primary_key"])
                field = _field(
                    field_name + "_id", pk["sql_type"], pk["go_type"], {"index": True}, False
                )
                field.update(
                    references={
                        "table": parent["table"],
                        "column": pk["name"],
                        "on_delete": "CASCADE",
                        "deferrable": True,
                        "deferred": True,
                    },
                    related_name=related,
                    reference_model=parent["name"],
                )
            else:
                if value.args or kind not in {
                    "AutoField",
                    "BigAutoField",
                    "CharField",
                    "TextField",
                    "IntegerField",
                    "PositiveIntegerField",
                    "BooleanField",
                    "DecimalField",
                }:
                    raise ValueError("unsupported conventional model field: " + kind)
                if set(options) - {
                    "max_length",
                    "unique",
                    "blank",
                    "default",
                    "choices",
                    "max_digits",
                    "decimal_places",
                    "primary_key",
                }:
                    raise ValueError("unsupported conventional field option")
                opts = {
                    key: constants[v.id]
                    if isinstance(v, ast.Name) and v.id in constants
                    else _literal(v)
                    for key, v in options.items()
                }
                blank = opts.pop("blank", False)
                choices = opts.pop("choices", None)
                if type(blank) is not bool:
                    raise ValueError("blank must be boolean")
                sql_type, go_type = DJANGO_TYPES[
                    "IntegerField" if kind == "PositiveIntegerField" else kind
                ]
                if go_type in {"int32", "int64"}:
                    # SQLite integer storage has a signed 64-bit range, including
                    # Django AutoField and IntegerField declarations.
                    sql_type, go_type = "bigint", "int64"
                mapped = {
                    {
                        "max_length": "length",
                        "max_digits": "precision",
                        "decimal_places": "scale",
                    }.get(k, k): v
                    for k, v in opts.items()
                }
                field = _field(
                    field_name, sql_type, go_type, mapped, kind in {"AutoField", "BigAutoField"}
                )
                field.update(
                    blank=blank, required=not (blank or "default" in field or field["auto"])
                )
                if kind == "PositiveIntegerField":
                    field["minimum"] = 0
                if choices is not None:
                    if (
                        go_type != "string"
                        or not isinstance(choices, (list, tuple))
                        or not choices
                        or any(
                            not isinstance(v, (list, tuple))
                            or len(v) != 2
                            or any(type(x) is not str for x in v)
                            for v in choices
                        )
                    ):
                        raise ValueError("choices require static string pairs")
                    field["choices"] = [v[0] for v in choices]
            field.update(source_name=field_name, kind=kind)
            fields.append(field)
        if not any(f["primary_key"] for f in fields):
            kind = auto.rsplit(".", 1)[-1]
            sql_type, go_type = DJANGO_TYPES[kind]
            sql_type, go_type = "bigint", "int64"
            fields.insert(
                0,
                dict(
                    _field("id", sql_type, go_type, {"primary_key": True}, True),
                    source_name="id",
                    kind=kind,
                    required=False,
                    blank=False,
                ),
            )
        if (
            sum(f["primary_key"] for f in fields) != 1
            or fields[0]["name"] != "id"
            or not fields[0]["auto"]
        ):
            raise ValueError("conventional models require auto id primary keys")
        result.append({"name": name, "table": table, "fields": fields, "ordering": ordering})
    return result


def _serializer(
    cls: ast.ClassDef, models: dict[str, dict[str, Any]], classes: dict[str, ast.ClassDef]
) -> dict[str, Any]:
    meta = [n for n in cls.body if isinstance(n, ast.ClassDef) and n.name == "Meta"]
    if len(meta) != 1 or meta[0].bases or meta[0].decorator_list or meta[0].keywords:
        raise ValueError("serializer requires plain Meta")
    declarations = _assignments(meta[0].body)
    if set(declarations) != {"model", "fields", "read_only_fields"} or not isinstance(
        declarations["model"], ast.Name
    ):
        raise ValueError("serializer Meta requires model, fields and read_only_fields")
    model = models.get(declarations["model"].id)
    if model is None:
        raise ValueError("serializer model is unresolved")
    names = _literal(declarations["fields"])
    readonly = _literal(declarations["read_only_fields"])
    if (
        not isinstance(names, list)
        or len(names) != len(set(names))
        or "id" not in names
        or readonly != ["id"]
        or not all(isinstance(n, str) for n in names)
    ):
        raise ValueError("unsupported serializer field selection")
    selected = []
    nested = None
    methods = {}
    overrides: dict[str, ast.expr] = {}
    for node in cls.body:
        if node is meta[0]:
            continue
        if isinstance(node, ast.FunctionDef):
            if node.name in methods or node.decorator_list or node.returns or node.type_params:
                raise ValueError("unsupported serializer method")
            methods[node.name] = node
        else:
            declaration = _assignments([node])
            if overrides.keys() & declaration.keys():
                raise ValueError("reassigned serializer field")
            overrides.update(declaration)
    for name in names:
        field = next((dict(f) for f in model["fields"] if f["source_name"] == name), None)
        override = overrides.pop(name, None)
        if field is not None:
            if "references" in field:
                raise ValueError("direct writable foreign-key serializers require capture")
            if override is not None:
                if (
                    field["primary_key"]
                    or not isinstance(override, ast.Call)
                    or ast.unparse(override.func) != "serializers.IntegerField"
                    or override.args
                    or len(override.keywords) != 1
                    or override.keywords[0].arg != "min_value"
                    or field["go_type"] not in {"int32", "int64"}
                ):
                    raise ValueError("unsupported serializer field override")
                minimum = _literal(override.keywords[0].value)
                if type(minimum) is not int:
                    raise ValueError("min_value must be integer")
                field.update(minimum=minimum, required=True, unique=False, blank=False)
                field.pop("default", None)
            selected.append(field)
        else:
            if (
                nested
                or not isinstance(override, ast.Call)
                or not isinstance(override.func, ast.Name)
                or override.func.id not in classes
                or override.func.id == cls.name
                or override.args
                or len(override.keywords) != 1
                or override.keywords[0].arg != "many"
                or _literal(override.keywords[0].value) is not True
            ):
                raise ValueError("only one explicit nested serializer is qualified")
            child = _serializer(classes[override.func.id], models, {})
            if any(f["unique"] for f in child["fields"]):
                raise ValueError("nested uniqueness validators require separate capture")
            child_model = models[child["model"]]
            foreign = [
                f
                for f in child_model["fields"]
                if f.get("reference_model") == model["name"] and f.get("related_name") == name
            ]
            if len(foreign) != 1 or child.get("nested"):
                raise ValueError("nested serializer requires one reverse foreign key")
            nested = {"name": name, "serializer": child, "foreign_key": foreign[0]["name"]}
    if overrides or set(methods) - {"create", "update"}:
        raise ValueError("unconsumed serializer fields or hooks")
    result = {"name": cls.name, "model": model["name"], "fields": selected}
    if nested:
        if set(methods) != {"create", "update"}:
            raise ValueError("nested writes require explicit captured create/update")
        create: Any = methods["create"]
        # Bind local variable names from the recipe, then compare the entire AST.
        try:
            items = create.body[0].targets[0].id
            atomic = create.body[1]
            parent = atomic.body[0].targets[0].id
            loop = atomic.body[1]
            item = loop.target.id
            total = atomic.body[2].targets[0].id
            generator = atomic.body[2].value.args[0]
            variable = generator.generators[0].target.id
            quantity = generator.elt.attr
            guard = atomic.body[3]
            limit = _literal(guard.test.comparators[0])
            error = _literal(guard.body[0].exc.args[0])
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("unrecognized nested-create recipe") from exc
        if (
            type(limit) is not int
            or not 0 <= limit <= 9223372036854775807
            or quantity
            not in {
                f["name"]
                for f in nested["serializer"]["fields"]
                if f["go_type"] in {"int32", "int64"}
                and f.get("minimum", -1) >= 0
                and f.get("required")
            }
            or not isinstance(error, dict)
            or set(error) != {nested["name"]}
            or not isinstance(error[nested["name"]], list)
            or len(error[nested["name"]]) != 1
            or type(error[nested["name"]][0]) is not str
        ):
            raise ValueError("aggregate guard requires a literal integer limit and nested error")
        expected = f"""def create(self, validated_data):
    {items} = validated_data.pop({nested["name"]!r})
    with transaction.atomic():
        {parent} = {model["name"]}.objects.create(**validated_data)
        for {item} in {items}:
            {nested["serializer"]["model"]}.objects.create(
                {foreign[0]["source_name"]}={parent}, **{item})
        {total} = sum({variable}.{quantity} for {variable} in {parent}.{nested["name"]}.all())
        if {total} > {limit}:
            raise serializers.ValidationError({error!r})
    return {parent}
"""
        update = f"""def update(self, instance, validated_data):
    validated_data.pop({nested["name"]!r}, None)
    return super().update(instance, validated_data)
"""
        if not _same(create, expected) or not _same(methods["update"], update):
            raise ValueError("nested serializer hooks contain uncaptured behavior")
        nested.update(
            aggregate={"field": quantity, "limit": limit, "error": error}, update="ignore"
        )
        result["nested"] = nested
    elif methods:
        raise ValueError("custom scalar serializer methods require capture")
    return result


def _migration(tree: ast.Module, models: list[dict[str, Any]], app: str, auto: str) -> None:
    """Reconstruct the baseline through the same static model parser."""
    if (
        len(tree.body) != 3
        or not _same(tree.body[0], "import django.db.models.deletion")
        or not _same(tree.body[1], "from django.db import migrations, models")
    ):
        raise ValueError("unqualified schema migration imports or execution")
    cls = tree.body[2]
    if (
        not isinstance(cls, ast.ClassDef)
        or cls.name != "Migration"
        or cls.decorator_list
        or cls.keywords
        or cls.type_params
        or [ast.unparse(b) for b in cls.bases] != ["migrations.Migration"]
    ):
        raise ValueError("unqualified migration class")
    attrs = _assignments(cls.body)
    if (
        set(attrs) != {"initial", "dependencies", "operations"}
        or _literal(attrs["initial"]) is not True
        or _literal(attrs["dependencies"]) != []
        or not isinstance(attrs["operations"], ast.List)
    ):
        raise ValueError("only an independent initial schema migration is qualified")
    declarations = ["from django.db import models"]
    known = {m["name"].lower(): m["name"] for m in models}
    for call in attrs["operations"].elts:
        if (
            not isinstance(call, ast.Call)
            or ast.unparse(call.func) != "migrations.CreateModel"
            or call.args
        ):
            raise ValueError("only CreateModel baseline operations are qualified")
        options = {k.arg: k.value for k in call.keywords}
        if (
            len(options) != len(call.keywords)
            or set(options) != {"name", "fields", "options"}
            or not isinstance(options["fields"], ast.List)
        ):
            raise ValueError("unsupported CreateModel options")
        name = _literal(options["name"])
        if not isinstance(name, str) or known.get(name.lower()) != name:
            raise ValueError("migration model is not captured")
        lines = [f"class {name}(models.Model):"]
        for pair in options["fields"].elts:
            if (
                not isinstance(pair, ast.Tuple)
                or len(pair.elts) != 2
                or not isinstance(pair.elts[1], ast.Call)
            ):
                raise ValueError("migration fields must be literal named declarations")
            field_name, field = _literal(pair.elts[0]), pair.elts[1]
            identifier(field_name)
            keywords = {k.arg: k.value for k in field.keywords}
            if len(keywords) != len(field.keywords) or None in keywords or field.args:
                raise ValueError("expanded migration fields are unsupported")
            args = []
            if (
                ast.unparse(field.func) in {"models.AutoField", "models.BigAutoField"}
                and "serialize" in keywords
                and _literal(keywords.pop("serialize")) is not False
            ):
                raise ValueError("unsupported primary key serialization")
            if ast.unparse(field.func) == "models.ForeignKey":
                target = _literal(keywords.pop("to", None))
                if (
                    not isinstance(target, str)
                    or target not in {app + "." + n for n in known}
                    or ast.unparse(keywords.pop("on_delete", ast.Constant(None)))
                    != "django.db.models.deletion.CASCADE"
                ):
                    raise ValueError("unresolved migration foreign key")
                args.append(known[target.split(".")[1]])
                args.append("on_delete=models.CASCADE")
            args += [str(k) + "=" + ast.unparse(v) for k, v in keywords.items()]
            lines.append(f"    {field_name} = {ast.unparse(field.func)}({', '.join(args)})")
        meta = _literal(options["options"])
        if not isinstance(meta, dict) or set(meta) - {"ordering", "db_table"}:
            raise ValueError("unsupported migration model options")
        lines.append("    class Meta:")
        lines.extend("        " + str(k) + "=" + repr(v) for k, v in meta.items())
        if not meta:
            raise ValueError("explicit model ordering is required")
        declarations.append("\n".join(lines))
    baseline = _models(ast.parse("\n".join(declarations)), app, auto)

    def normalized(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(m, fields=sorted(m["fields"], key=lambda f: f["name"])) for m in items]

    if normalized(baseline) != normalized(models):
        raise ValueError("initial migration differs from the captured model schema")
