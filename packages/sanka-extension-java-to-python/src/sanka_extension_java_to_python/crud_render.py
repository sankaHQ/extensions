# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sanka_java_migration.crud import schema_sql

from .render import LOCK


def render_crud(captured: dict[str, Any], scenarios: list[dict[str, Any]]) -> dict[str, str]:
    model = captured["persistence"]
    lines = []
    for field in model["fields"]:
        if field["name"] == "id":
            continue
        kind = (
            "Decimal"
            if field["type"] == "BigDecimal"
            else "str | None"
            if field["nullable"]
            else "str"
        )
        constraint = (
            f"ge=0, max_digits={field['precision']}, decimal_places={field['scale']}"
            if field["type"] == "BigDecimal"
            else f"max_length={field['length']}"
        )
        default = "default=None, " if field["nullable"] else ""
        lines.append(f"    {field['name']}: {kind} = Field({default}{constraint})")
    source = (Path(__file__).parent / "runners/crud_app.py.txt").read_text()
    source = source.replace("CAPTURED_MODEL", repr(json.dumps(model, sort_keys=True)))
    source = source.replace("INPUT_FIELDS", "\n".join(lines))
    for endpoint in sorted(
        captured["endpoints"], key=lambda e: (e["path"].count("{"), e["path"], e["method"])
    ):
        operation = endpoint["operation"]
        method = endpoint["method"].lower()
        response = (
            "list[_Response]"
            if operation == "list"
            else "None"
            if operation == "delete"
            else "_Response"
        )
        status = 201 if operation == "create" else 204 if operation == "delete" else 200
        options = (
            ", openapi_extra=_body_schema" if operation in {"create", "update", "rollback"} else ""
        )
        source += (
            f"\n@app.{method}({endpoint['path']!r}, response_model={response}, "
            f"status_code={status}{options})\n"
            f"async def endpoint_{operation}(request: Request):\n"
            f"    return await _dispatch(request, {operation!r})\n"
        )
    return {
        "app.py": source,
        "requirements.lock": LOCK.read_text(),
        "schema.sql": schema_sql(model),
        "scenarios.json": json.dumps(scenarios, indent=2) + "\n",
        "test_app.py": (Path(__file__).parent / "runners/test_candidate.py.txt").read_text(),
        "README.md": (
            "# Generated FastAPI PostgreSQL candidate\n\n"
            "Install `uv pip sync --require-hashes requirements.lock` "
            "in an isolated Python environment. "
            "Set `DATABASE_URL` to your PostgreSQL database "
            "using the `postgresql+psycopg://` scheme. "
            "The existing database must match `schema.sql`; "
            "startup never creates or migrates a schema. "
            "For a new empty fixture only, apply that SQL separately before running tests. "
            "Run `python -m uvicorn app:app`. No Java or Sanka runtime is required.\n\n"
            "`python -m pytest` runs ordered scenarios and mutates that fixture database. "
            "Use disposable empty data only. The migration verifier allocates "
            "separate native/source/target databases and compares HTTP values, "
            "every table row and identity sequence after each request. "
            "This profile preserves explicit string decimal JSON, validation, "
            "not-found responses and single-resource transaction rollback. "
            "It does not qualify authentication, relationships, "
            "custom queries, concurrent writes, live transport or production cutover.\n"
        ),
    }
