# SPDX-License-Identifier: Apache-2.0
"""Reviewed native SQLAlchemy profile; legacy Django plans remain unchanged."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from sanka_code_migration.drf.model import FrameworkScan
from sanka_code_migration.ir import (
    BackendIR,
    EffectiveInputs,
    OperationFact,
    SourceSetting,
    VersionPin,
)
from sanka_code_migration.policy import resolve_profile

from .database import render_database

# Validated target profile, not versions resolved from the source application's environment.
DEPENDENCIES = {
    "flask": "3.1.3",
    "gunicorn": "26.2.0",
    "sqlalchemy": "2.0.52",
    "alembic": "1.20.0",
    "blinker": "1.9.0",
    "click": "8.5.0",
    "itsdangerous": "2.2.0",
    "jinja2": "3.1.6",
    "markupsafe": "3.0.3",
    "werkzeug": "3.1.8",
    "mako": "1.4.1",
    "typing-extensions": "4.16.0",
    "greenlet": "3.5.5",
}
PROFILE_KEYS = {
    "orm",
    "generation",
    "strategy",
    "package_manager",
    "database",
    "layers",
    "completion_policy",
}
CAPTURE_KEYS = (
    "backend_scan",
    "database_schema",
    "sqlalchemy_overrides",
    "source_dependency_digest",
    "behavior_inventory",
)


def _capture_hash(payload: dict[str, Any]) -> str:
    captured = {key: payload[key] for key in CAPTURE_KEYS}
    canonical = json.dumps(captured, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _service_operations(
    backend: FrameworkScan, schema: dict[str, Any]
) -> tuple[OperationFact, ...]:
    serializer_tables = {item.name: item.db_table for item in backend.serializer_details}
    destroy_tables = {
        serializer_tables[route.serializer]
        for route in backend.routes
        if route.operation == "destroy" and route.serializer in serializer_tables
    }
    incoming: dict[str, list[str]] = {}
    for table in schema["tables"]:
        for column in table["columns"]:
            reference = column.get("references")
            if reference and reference.get("on_delete") != "DO_NOTHING":
                incoming.setdefault(reference["table"], []).append(
                    f"{table['name']}.{column['name']}:{reference['on_delete']}"
                )
    deletes = tuple(
        OperationFact(
            name="delete:" + table,
            coordinates_multiple_writes=True,
            evidence=tuple(sorted(incoming[table])),
        )
        for table in sorted(destroy_tables & incoming.keys())
    )
    create_serializers = {
        route.serializer for route in backend.routes if route.operation == "create"
    }
    creates = tuple(
        OperationFact(
            name="create:" + serializer.name,
            coordinates_multiple_writes=True,
            evidence=(serializer.name + ":atomic-parent-and-child-writes",),
        )
        for serializer in backend.serializer_details
        if serializer.name in create_serializers
        and serializer.create_contract
        and serializer.create_contract.get("style") == "nested"
        and serializer.create_contract.get("atomic") is True
    )
    membership = []
    for view in backend.view_details:
        creator = view.access.get("creator_membership")
        action = view.access.get("member_action")
        if creator and any(
            route.view == view.name and route.operation == "create" for route in backend.routes
        ):
            membership.append(
                OperationFact(
                    name="create-membership:" + view.name,
                    coordinates_multiple_writes=True,
                    evidence=(creator["table"] + ":creator-membership",),
                )
            )
        if action and action.get("touches"):
            membership.append(
                OperationFact(
                    name="member-" + action["action"] + ":" + view.name,
                    coordinates_multiple_writes=True,
                    evidence=(action["relation"]["table"] + ":membership-and-parent-update",),
                )
            )
    return deletes + creates + tuple(membership)


def capture(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    from sanka_code_migration.drf.inventory import inventory_behavior
    from sanka_code_migration.drf.models import capture_schema
    from sanka_code_migration.drf.scan import scan_django

    from .sqlalchemy import capture_sqlalchemy_overrides

    scan = scan_django(
        root,
        settings_module=config.get("settings_module"),
        artifact_dir=root / ".sanka" / "backend",
    )
    lock_inputs: dict[str, Any] = {}
    for name in ("uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml"):
        path = root / name
        if path.is_file():
            lock_inputs[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Framework versions are semantic inputs even when the project has no lock yet.
    lock_inputs["source_versions"] = [scan.python_version, scan.django_version, scan.drf_version]
    digest = hashlib.sha256(json.dumps(lock_inputs, sort_keys=True).encode()).hexdigest()
    result = {
        "backend_scan": json.loads(json.dumps(scan.to_dict(), allow_nan=False)),
        "database_schema": capture_schema(),
        "sqlalchemy_overrides": capture_sqlalchemy_overrides(scan),
        "source_dependency_digest": "sha256:" + digest,
        "behavior_inventory": inventory_behavior(root),
    }
    return {**result, "backend_capture_hash": _capture_hash(result)}


def plan_native(
    root: Path, output: Path, scan: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    from .sqlalchemy import qualify_routes, render_sqlalchemy

    if scan.get("backend_capture_error") or "backend_scan" not in scan:
        raise ValueError(
            "native backend capture unavailable; resolve the capture error and rescan: "
            + str(scan.get("backend_capture_error", "missing scan"))
        )
    if scan.get("backend_capture_hash") != _capture_hash(scan):
        raise ValueError("native backend capture hash does not match; scan again")
    backend = FrameworkScan.from_dict(scan["backend_scan"])
    if scan.get("behavior_inventory"):
        raise ValueError(
            "backend behavior requires explicit migration: "
            + json.dumps(scan["behavior_inventory"], sort_keys=True)
        )
    schema = scan["database_schema"]
    operations = _service_operations(backend, schema)
    facts = BackendIR(
        source_content_digest=scan["source_hash"],
        source_dependency_lock_digest=scan["source_dependency_digest"],
        source_settings=(SourceSetting.from_value("http_security", backend.http_security),),
        persistence=bool(schema["tables"]),
        database_dialect=schema["dialect"],
        route_groups=tuple(sorted({view.name.rpartition(".")[0] for view in backend.view_details})),
        operations=operations,
    )
    profile = resolve_profile(facts, {k: v for k, v in config.items() if k in PROFILE_KEYS})
    supported_services = {operation.name for operation in operations}
    if set(profile.services) - supported_services or profile.repositories:
        raise ValueError("captured service and repository boundaries require migration support")
    dependencies = dict(DEPENDENCIES)
    if schema["dialect"] == "postgresql":
        dependencies.update({"psycopg": "3.3.4", "psycopg-binary": "3.3.4"})
    lock_root = Path(__file__).with_name("target_locks")
    dependency_lock = (lock_root / (schema["dialect"] + ".lock")).read_text()
    locked_packages = tomllib.loads(dependency_lock)["package"]
    profile = profile.with_locks(
        dependency_locks=tuple(
            VersionPin(package["name"], package["version"])
            for package in locked_packages
            if package["name"] != "sanka-generated-flask"
        ),
        toolchain_locks=(VersionPin("python", "3.12.13"), VersionPin("uv", "0.11.18")),
    )
    routes = qualify_routes(backend, scan["sqlalchemy_overrides"])
    gaps = [r for r in routes if not r["native"]]
    if gaps:
        raise ValueError("native SQLAlchemy has route gaps: " + json.dumps(gaps, sort_keys=True))
    if not routes:
        raise ValueError("native SQLAlchemy has no generatable routes")
    module_prefix = "backend" if profile.layout == "modular" else ""
    files = render_database(schema, module_prefix=module_prefix)
    files.update(
        render_sqlalchemy(
            backend, schema, overrides=scan["sqlalchemy_overrides"], module_prefix=module_prefix
        )
    )
    requirements = "\n".join(k + "==" + v for k, v in sorted(dependencies.items())) + "\n"
    files["requirements.txt"] = (lock_root / (schema["dialect"] + ".requirements.txt")).read_text()
    files["uv.lock"] = dependency_lock
    files[".python-version"] = "3.12.13\n"
    files["pyproject.toml"] = (
        '[project]\nname = "sanka-generated-flask"\nversion = "0.0.0"\n'
        'requires-python = "==3.12.*"\ndependencies = [\n'
        + "".join("  " + json.dumps(line) + ",\n" for line in requirements.splitlines())
        + "]\n\n[tool.uv]\npackage = false\n"
    )
    files[".env.example"] = "SANKA_DATABASE_URL=\n"
    files[".gitignore"] = ".env\n.venv/\n__pycache__/\n*.db\n"
    files["tests/test_generated_backend.py"] = _TEST.replace(
        "__DIALECT__", schema["dialect"]
    ).replace(
        "from database import",
        "from " + (module_prefix + "." if module_prefix else "") + "database import",
    )
    files["migration-gaps.json"] = "[]\n"
    files["architecture.json"] = json.dumps(profile.to_dict(), sort_keys=True, indent=2) + "\n"
    contract_name = (module_prefix + "/" if module_prefix else "") + "native_contract.json"
    facts = replace(
        facts,
        source_settings=(
            *facts.source_settings,
            SourceSetting.from_value("database_schema_digest", schema["schema_hash"]),
            SourceSetting.from_value(
                "target_dependency_lock_digest",
                "sha256:" + hashlib.sha256(dependency_lock.encode()).hexdigest(),
            ),
            SourceSetting.from_value(
                "http_contract_digest",
                "sha256:" + hashlib.sha256(files[contract_name].encode()).hexdigest(),
            ),
        ),
    )
    inputs = EffectiveInputs(facts, profile, "drf-to-flask/0.1.0a9")
    files["migration-inputs.json"] = inputs.to_json() + "\n"
    files["README.md"] = _README.replace(
        "database.", (module_prefix + "." if module_prefix else "") + "database."
    )
    generated_hashes = {
        name: hashlib.sha256(content.encode()).hexdigest()
        for name, content in sorted(files.items())
    }
    files["generated-files.json"] = json.dumps(generated_hashes, sort_keys=True, indent=2) + "\n"
    return {
        **scan,
        "target": "flask",
        "mode": "native",
        "orm": "sqlalchemy",
        "output": str(output),
        "native_eligible_routes": len(routes),
        "native_routes": len(routes),
        "needs_adaptation_routes": 0,
        "readiness": 1.0,
        "architecture": profile.to_dict(),
        "effective_inputs_hash": inputs.digest(),
        "generated_content_hash": "sha256:"
        + hashlib.sha256(json.dumps(generated_hashes, sort_keys=True).encode()).hexdigest(),
        "files": files,
        "reviewed_configuration": {k: config[k] for k in sorted(PROFILE_KEYS) if k in config},
    }


_README = """# Generated Flask backend

This is the native SQLAlchemy profile. Run `uv sync --locked` using the recorded
Python and uv versions, or `pip install --require-hashes -r requirements.txt`,
and configure `SANKA_DATABASE_URL` explicitly. No source framework is required.
On a POSIX deployment, serve the application with
`uv run --no-sync gunicorn 'target_app:create_app()'` after configuring the bind address,
worker count and timeout for your deployment. Validate startup without serving traffic
with `uv run --no-sync gunicorn --check-config 'target_app:create_app()'`.
Importing the application never migrates a database.

For an empty, explicitly selected database run `alembic upgrade head`.
For an existing database first run `database.check_schema(engine)` and review every
difference. Do not stamp an unverified schema or rerun historical data migrations.
The initial migration is an immutable schema snapshot; later changes need new revisions.
Destructive baseline downgrade deliberately fails and requires a reviewed rollback plan.

The architecture and input schema are recorded in `architecture.json` and `schema.json`.
Generation and successful boot are not behavioral or production qualification.
Run `python -m unittest discover -s tests` to check the factory and initial migration
against a disposable database. Add application contract tests to this suite.
Run independent source/target HTTP, authorization and database-effect verification
before adopting the target. Keep the original database backup and reviewed cutover plan.
"""

_TEST = """# Generated by Sanka. Uses only a disposable SQLite database.
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from database import check_schema
from target_app import create_app


class GeneratedBackendTest(unittest.TestCase):
    @unittest.skipUnless("__DIALECT__" == "sqlite", "requires a disposable SQLite database")
    def test_factory_does_not_create_schema(self):
        import sqlalchemy as sa
        app = create_app({"DATABASE_URL": "sqlite:///:memory:", "TESTING": True})
        engine = app.extensions["sanka_engine"]
        try:
            self.assertEqual(sa.inspect(engine).get_table_names(), [])
        finally:
            engine.dispose()

    @unittest.skipUnless("__DIALECT__" == "sqlite", "requires a dedicated PostgreSQL test database")
    def test_initial_schema_matches_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            url = "sqlite:///" + str(Path(temporary) / "test.db")
            with patch.dict(os.environ, {"SANKA_DATABASE_URL": url}):
                config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                command.upgrade(config, "head")
                app = create_app({"DATABASE_URL": url, "TESTING": True})
                engine = app.extensions["sanka_engine"]
                try:
                    self.assertEqual(check_schema(engine), [])
                    command.upgrade(config, "head")
                    self.assertEqual(check_schema(engine), [])
                finally:
                    engine.dispose()
"""
