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
    from .drf_modules import startup

    manage = startup(ast.parse((root / "manage.py").read_text()))
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
    tree = startup(tree)
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    allowed = {"from __future__ import annotations", "import os", "from pathlib import Path"}
    if not {ast.unparse(n) for n in imports} <= allowed or not {
        "import os",
        "from pathlib import Path",
    } <= {ast.unparse(n) for n in imports}:
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
    secret_environment = None
    secret = values["SECRET_KEY"]
    if isinstance(secret, ast.Subscript) and ast.unparse(secret.value) == "os.environ":
        secret_environment = _literal(secret.slice)
        if (
            not isinstance(secret_environment, str)
            or not re.fullmatch(r"[A-Z][A-Z_0-9]*", secret_environment)
            or secret_environment in {"HOME", "PATH", "PYTHONPATH", "DJANGO_SETTINGS_MODULE"}
        ):
            raise ValueError("unsafe secret environment variable")
    elif not isinstance(_literal(secret), str):
        raise ValueError("SECRET_KEY must be a literal or explicit environment lookup")
    if type(_literal(values["DEBUG"])) is not bool:
        raise ValueError("DEBUG must be static")
    if _literal(values["MIDDLEWARE"]) != []:
        raise ValueError("custom Django middleware requires capture")
    rest = _literal(values["REST_FRAMEWORK"])
    if (
        not isinstance(rest, dict)
        or set(rest) - {"UNAUTHENTICATED_USER", "PAGE_SIZE"}
        or rest.get("UNAUTHENTICATED_USER", False) is not None
    ):
        raise ValueError("DRF settings require separate capture")
    page_size = rest.get("PAGE_SIZE")
    if page_size is not None and (type(page_size) is not int or not 0 < page_size <= 1000):
        raise ValueError("PAGE_SIZE must be a positive integer up to 1000")
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
    if not local or any(
        not isinstance(a, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", a) for a in local
    ):
        raise ValueError("installed apps require explicit local package names")
    db = values["DATABASES"]
    if (
        not isinstance(db, ast.Dict)
        or any(k is None for k in db.keys)
        or [_literal(k) for k in db.keys] != ["default"]
        or not isinstance(db.values[0], ast.Dict)
    ):
        raise ValueError("one explicit source database is required")
    opts = {_literal(k): v for k, v in zip(db.values[0].keys, db.values[0].values, strict=True)}
    if _literal(opts.get("ENGINE")) == "django.db.backends.postgresql":
        if set(opts) != {"ENGINE", "NAME", "USER", "PASSWORD", "HOST", "PORT"}:
            raise ValueError("PostgreSQL requires explicit environment-backed connection fields")
        environments = {}
        for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
            node = opts[key]
            if not isinstance(node, ast.Subscript) or ast.unparse(node.value) != "os.environ":
                raise ValueError("PostgreSQL connection fields must use os.environ names")
            env = _literal(node.slice)
            if (
                not isinstance(env, str)
                or not re.fullmatch(r"[A-Z][A-Z_0-9]*", env)
                or env in {"HOME", "PATH", "PYTHONPATH", "DJANGO_SETTINGS_MODULE"}
            ):
                raise ValueError("unsafe database environment variable")
            environments[key] = env
        if len(set(environments.values())) != len(environments):
            raise ValueError("database environment names must be independent")
        database: dict[str, Any] = {"engine": "postgresql", "environments": environments}
    else:
        if (
            set(opts) != {"ENGINE", "NAME"}
            or _literal(opts["ENGINE"]) != "django.db.backends.sqlite3"
        ):
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
        database = {"engine": "sqlite", "environment": env}
    if secret_environment and secret_environment in {
        database.get("environment"),
        *database.get("environments", {}).values(),
    }:
        raise ValueError("secret and database environment names must be independent")
    hosts = _literal(values["ALLOWED_HOSTS"])
    if not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) for h in hosts):
        raise ValueError("ALLOWED_HOSTS must be static strings")
    return (
        name,
        {
            "app": local[0],
            "apps": local,
            "urls": _literal(values["ROOT_URLCONF"]),
            "auto": auto,
            "hosts": hosts,
            "database": database,
            "secret_environment": secret_environment,
            "page_size": page_size,
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
        from .drf_modules import check_imports, normalize, urls

        apps = settings["apps"]
        qualified = len(apps) > 1
        trees: dict[str, ast.Module] = {}
        symbols: dict[str, dict[str, str]] = {
            "django.db": {"models": "models", "transaction": "transaction"},
            "rest_framework": {"serializers": "serializers"},
            "rest_framework.viewsets": {
                "ModelViewSet": "ModelViewSet",
                "ReadOnlyModelViewSet": "ReadOnlyModelViewSet",
            },
            "rest_framework.filters": {"OrderingFilter": "OrderingFilter"},
            "rest_framework.pagination": {"LimitOffsetPagination": "LimitOffsetPagination"},
        }
        for app in apps:
            for role in ("models", "serializers", "views"):
                module = app + "." + role
                filename = module.replace(".", "/") + ".py"
                if not (root / filename).exists() and role != "models":
                    continue
                filename, tree = _module(root, module)
                trees[module] = tree
                consumed.add(filename)
                symbols[module] = {
                    n.name: app + "__" + role + "__" + n.name if qualified else n.name
                    for n in tree.body
                    if isinstance(n, ast.ClassDef)
                }
        check_imports(trees)
        if config["models_file"] not in {a + "/models.py" for a in apps}:
            raise ValueError("models_file must name an installed Django application")
        models: list[dict[str, Any]] = []
        # Resolve initial migration dependencies before parsing foreign keys.
        migration_trees: dict[str, tuple[str, ast.Module]] = {}
        dependencies: dict[str, set[str]] = {}
        for app in apps:
            files = sorted(
                p for p in (root / app / "migrations").glob("*.py") if p.name != "__init__.py"
            )
            if len(files) != 1:
                raise ValueError(app + "/migrations: exactly one initial migration is required")
            relative = files[0].relative_to(root).as_posix()
            tree = ast.parse(files[0].read_text())
            migration_trees[app] = (relative, tree)
            migration_classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
            if len(migration_classes) != 1:
                raise ValueError(relative + ":1: migration class required")
            deps = _literal(_assignments(migration_classes[0].body).get("dependencies"))
            if not isinstance(deps, list) or any(
                not isinstance(d, tuple) or len(d) != 2 or not all(isinstance(x, str) for x in d)
                for d in deps
            ):
                raise ValueError(relative + ":1: explicit migration dependencies required")
            if len(set(deps)) != len(deps):
                raise ValueError(relative + ":1: duplicate migration dependency")
            dependencies[app] = {d[0] for d in deps}
            for other, revision in deps:
                if (
                    other not in apps
                    or not (root / other / "migrations" / (revision + ".py")).is_file()
                ):
                    raise ValueError(relative + ":1: unresolved initial migration dependency")
        for app, (_, tree) in migration_trees.items():
            migration_class = next(n for n in tree.body if isinstance(n, ast.ClassDef))
            for other, revision in _literal(_assignments(migration_class.body)["dependencies"]):
                if Path(migration_trees[other][0]).stem != revision:
                    raise ValueError(
                        migration_trees[app][0]
                        + ":1: dependency must name the captured initial revision"
                    )
        ordered: list[str] = []
        pending = set(apps)
        while pending:
            ready = sorted(a for a in pending if dependencies[a] <= set(ordered))
            if not ready:
                raise ValueError("initial migration dependency cycle")
            ordered.extend(ready)
            pending.difference_update(ready)
        for app in ordered:
            module = app + ".models"
            try:
                tree = normalize(trees[module], module, symbols)
                parsed = _models(
                    tree,
                    app,
                    settings["auto"],
                    models,
                    symbols,
                    symbols[module],
                    settings["database"]["engine"],
                )
                foreign_apps = {
                    m["source_app"]
                    for m in models
                    if any(
                        f.get("reference_model") == m["name"]
                        for item in parsed
                        for f in item["fields"]
                    )
                }
                if not foreign_apps <= dependencies[app]:
                    raise ValueError("cross-app foreign key requires initial migration dependency")
                models.extend(parsed)
                relative, migration = migration_trees[app]
                _migration(
                    migration,
                    parsed,
                    app,
                    settings["auto"],
                    models,
                    symbols[module],
                    dependencies[app],
                    settings["database"]["engine"],
                )
            except (ValueError, TypeError, KeyError) as error:
                raise ValueError(f"{app}/models.py or {app}/migrations: {error}") from error
        if len({m["table"] for m in models}) != len(models):
            raise ValueError("duplicate model database table")
        model_map = {m["name"]: m for m in models}
        serializers: dict[str, ast.ClassDef] = {}
        views: dict[str, ast.ClassDef] = {}
        allowed = {k: set(v.values()) for k, v in symbols.items()}
        for module, original in trees.items():
            role = module.rsplit(".", 1)[1]
            if role == "models":
                continue
            try:
                tree = normalize(original, module, symbols)
                classes = _classes(
                    tree,
                    allowed,
                    "serializers.ModelSerializer"
                    if role == "serializers"
                    else ("ModelViewSet", "ReadOnlyModelViewSet"),
                )
                (serializers if role == "serializers" else views).update(classes)
            except (ValueError, TypeError) as error:
                raise ValueError(f"{module.replace('.', '/')}.py: {error}") from error
        if config["source_file"] != settings["urls"].replace(".", "/") + ".py":
            raise ValueError("source_file must match ROOT_URLCONF")
        registrations, url_files = urls(
            root, settings["urls"], {k: v for k, v in symbols.items() if k.endswith(".views")}
        )
        for index, route in enumerate(registrations):
            for earlier in registrations[:index]:
                suffix = route["path"].removeprefix(earlier["path"])
                if suffix != route["path"] and suffix.endswith("/") and "/" not in suffix[:-1]:
                    raise ValueError(
                        "URL collection overlaps an earlier detail route: " + route["path"]
                    )
        consumed.update(url_files)
        contracts: list[dict[str, Any]] = []
        used = set()
        for registration in registrations:
            view_name = registration["view"]
            if view_name in used:
                raise ValueError("duplicate router view")
            used.add(view_name)
            attrs = _assignments(views[view_name].body)
            if set(attrs) - {
                "queryset",
                "serializer_class",
                "filter_backends",
                "ordering_fields",
                "ordering",
                "pagination_class",
            } or not isinstance(attrs.get("serializer_class"), ast.Name):
                raise ValueError("viewset overrides require capture")
            serializer_node = attrs["serializer_class"]
            assert isinstance(serializer_node, ast.Name)
            serializer_name = serializer_node.id
            if serializer_name not in serializers:
                raise ValueError("unresolved serializer")
            contract = _serializer(serializers[serializer_name], model_map, serializers)
            query = _view_query(attrs, model_map[contract["model"]], settings["page_size"])
            contracts.append(
                {
                    "path": registration["path"],
                    "basename": registration["basename"],
                    "serializer": contract,
                    "query": query,
                    "read_only": ast.unparse(views[view_name].bases[0]) == "ReadOnlyModelViewSet",
                }
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
        tests: list[str] = []
        migrations: list[str] = []
        for relative in records:
            if not relative.endswith(".py") or relative in consumed:
                continue
            tree = ast.parse((root / relative).read_text())
            path = Path(relative)
            if path.name == "__init__.py" and not tree.body:
                consumed.add(relative)
            elif relative in {a + "/apps.py" for a in apps}:
                app = path.parts[0]
                classes = _classes(tree, {"django.apps": {"AppConfig"}}, "AppConfig")
                if len(classes) != 1 or {
                    k: _literal(v)
                    for k, v in _assignments(next(iter(classes.values())).body).items()
                } != {"name": app, "default_auto_field": settings["auto"]}:
                    raise ValueError("custom AppConfig requires capture")
                consumed.add(relative)
            elif path.name == "tests.py" or path.name.startswith("test_"):
                tests.append(relative)
            elif relative in {v[0] for v in migration_trees.values()}:
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
                "secret_environment": settings["secret_environment"],
                "hosts": settings["hosts"],
                "views": contracts,
                "router_prefix": "/",
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
            for method in ["GET"] if view["read_only"] else ["GET", "POST"]:
                result["routes"].append(
                    {
                        "path": view["path"],
                        "method": method,
                        "status": 201 if method == "POST" else 200,
                    }
                )
            for method in ["GET"] if view["read_only"] else ["GET", "PUT", "PATCH", "DELETE"]:
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


def _view_query(
    attrs: dict[str, ast.expr], model: dict[str, Any], page_size: int | None
) -> dict[str, Any]:
    fields = {f["name"]: f for f in model["fields"]}
    node = attrs.get("queryset")
    if not isinstance(node, ast.Call) or node.args:
        raise ValueError("queryset requires all() or exact scalar filter()")
    filters = {}
    if ast.unparse(node.func) == model["name"] + ".objects.filter":
        for keyword in node.keywords:
            if keyword.arg not in fields or keyword.arg in filters:
                raise ValueError("queryset filter requires distinct model fields")
            value = _literal(keyword.value)
            field = fields[keyword.arg]
            kind = field["go_type"]
            if not (
                (kind == "string" and type(value) is str)
                or (kind == "bool" and type(value) is bool)
                or (kind in {"int32", "int64"} and type(value) is int and -(2**31) <= value < 2**31)
            ):
                raise ValueError("queryset filter requires a literal matching the field type")
            filters[keyword.arg] = value
    elif ast.unparse(node.func) != model["name"] + ".objects.all" or node.keywords:
        raise ValueError("queryset requires all() or exact scalar filter()")
    ordering_fields = []
    ordering: Any = ["id"]
    if "filter_backends" in attrs:
        if ast.unparse(attrs["filter_backends"]) != "[OrderingFilter]":
            raise ValueError("only the stock OrderingFilter backend is qualified")
        ordering_fields = _literal(attrs.get("ordering_fields"))
        if (
            not isinstance(ordering_fields, list)
            or not ordering_fields
            or any(type(f) is not str or f not in fields for f in ordering_fields)
        ):
            raise ValueError("OrderingFilter requires explicit scalar ordering_fields")
        ordering = _literal(attrs["ordering"]) if "ordering" in attrs else None
        if isinstance(ordering, str):
            ordering = [ordering]
        if ordering is None:
            ordering = ["id"]
        if (
            not isinstance(ordering, list)
            or not ordering
            or any(type(f) is not str or f.removeprefix("-") not in fields for f in ordering)
        ):
            raise ValueError("ordering requires captured scalar fields")
    elif {"ordering_fields", "ordering"} & attrs.keys():
        raise ValueError("ordering declarations require OrderingFilter")
    pagination = "pagination_class" in attrs
    if pagination and ast.unparse(attrs["pagination_class"]) != "LimitOffsetPagination":
        raise ValueError("only stock LimitOffsetPagination is qualified")
    return {
        "filters": filters,
        "ordering_fields": ordering_fields,
        "ordering": ordering,
        "pagination": pagination,
        "page_size": page_size,
    }


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


def _classes(
    tree: ast.Module, imports: dict[str, set[str]], base: str | tuple[str, ...]
) -> dict[str, ast.ClassDef]:
    bases = (base,) if isinstance(base, str) else base
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
                or len(node.bases) != 1
                or ast.unparse(node.bases[0]) not in bases
            ):
                raise ValueError("unsupported class inheritance or decorators")
            if ast.unparse(node.bases[0]).split(".")[0] not in seen:
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


def _models(
    tree: ast.Module,
    app: str,
    auto: str,
    external: list[dict[str, Any]] | None = None,
    symbols: dict[str, dict[str, str]] | None = None,
    identities: dict[str, str] | None = None,
    engine: str = "sqlite",
) -> list[dict[str, Any]]:
    imports = {"django.db": {"models"}}
    imports.update(
        {k: set(v.values()) for k, v in (symbols or {}).items() if k.endswith(".models")}
    )
    classes = _classes(tree, imports, "models.Model")
    result: list[dict[str, Any]] = []
    for name, cls in classes.items():
        fields: list[dict[str, Any]] = []
        constants = {}
        ordering = ["id"]
        original_name = next((k for k, v in (identities or {}).items() if v == name), name)
        table = identifier(app + "_" + original_name.lower())
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
                    or not isinstance(value.args[0], (ast.Name, ast.Constant))
                    or set(options) - {"on_delete", "related_name"}
                ):
                    raise ValueError("only explicit nonnullable foreign keys are qualified")
                target = value.args[0]
                parent_name = target.id if isinstance(target, ast.Name) else None
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    owner, _, class_name = target.value.rpartition(".")
                    if not owner:
                        owner, class_name = app, target.value
                    candidates = (symbols or {}).get(owner + ".models", {})
                    parent_name = next(
                        (v for k, v in candidates.items() if k.lower() == class_name.lower()), None
                    )
                parent = next(
                    (m for m in [*(external or []), *result] if m["name"] == parent_name), None
                )
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
                if engine == "sqlite" and go_type in {"int32", "int64"}:
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
                if engine == "postgresql" and go_type in {"int32", "int64"} and not field["auto"]:
                    bits = 32 if go_type == "int32" else 64
                    field.update(minimum=-(2 ** (bits - 1)), maximum=2 ** (bits - 1) - 1)
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
            if engine == "sqlite":
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
        result.append(
            {
                "name": name,
                "table": table,
                "fields": fields,
                "ordering": ordering,
                "source_app": app,
                "source_name": original_name,
            }
        )
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
                field.pop("maximum", None)
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


def _migration(
    tree: ast.Module,
    models: list[dict[str, Any]],
    app: str,
    auto: str,
    external: list[dict[str, Any]] | None = None,
    identities: dict[str, str] | None = None,
    dependencies: set[str] | None = None,
    engine: str = "sqlite",
) -> None:
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
        or {d[0] for d in _literal(attrs["dependencies"])} != (dependencies or set())
        or not isinstance(attrs["operations"], ast.List)
    ):
        raise ValueError("only an independent initial schema migration is qualified")
    declarations = ["from django.db import models"]
    known = {
        k.lower(): v for k, v in (identities or {m["name"]: m["name"] for m in models}).items()
    }
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
        if not isinstance(name, str) or name.lower() not in known:
            raise ValueError("migration model is not captured")
        lines = [f"class {known[name.lower()]}(models.Model):"]
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
                references = {
                    m["source_app"] + "." + m["source_name"].lower(): m
                    for m in (external or models)
                }
                parent = references.get(target.lower()) if isinstance(target, str) else None
                if (
                    parent is None
                    or ast.unparse(keywords.pop("on_delete", ast.Constant(None)))
                    != "django.db.models.deletion.CASCADE"
                ):
                    raise ValueError("unresolved migration foreign key")
                args.append(repr(parent["source_app"] + "." + parent["source_name"]))
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
    migration_symbols: dict[str, dict[str, str]] = {}
    for model in external or models:
        migration_symbols.setdefault(model["source_app"] + ".models", {})[model["source_name"]] = (
            model["name"]
        )
    baseline = _models(
        ast.parse("\n".join(declarations)),
        app,
        auto,
        external,
        migration_symbols,
        identities,
        engine,
    )

    def normalized(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(m, fields=sorted(m["fields"], key=lambda f: f["name"])) for m in items]

    if normalized(baseline) != normalized(models):
        raise ValueError("initial migration differs from the captured model schema")
