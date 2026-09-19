# SPDX-License-Identifier: Apache-2.0
"""Response models: exported TypeScript interfaces of scalar fields in project modules.

Screens reference them through type-only imports (``useState<Item[]>``,
``const data: Item = await response.json()``). Only the referenced interfaces are
captured; every member must be ``string``, ``number`` or ``boolean``, optionally
marked ``?``. Anything else is a ``SANKA_RN_MODEL`` reason code.
"""

from __future__ import annotations

from typing import Any

from sanka_ts_capture import ParsedFile
from sanka_ts_capture import tree as t

from .expressions import Unsupported

SCALARS = {"StringKeyword": "string", "NumberKeyword": "number", "BooleanKeyword": "boolean"}
CODE = "SANKA_RN_MODEL"


def capture_model(parsed: ParsedFile, rel: str, name: str) -> dict[str, Any]:
    """Return ``{"name", "module", "fields"}`` for the exported interface ``name``."""
    for statement in t.field_list(parsed.tree, "statements"):
        kind = t.kind(statement)
        if kind not in {"InterfaceDeclaration", "TypeAliasDeclaration"}:
            continue
        if t.identifier_name(t.field(statement, "name")) != name:
            continue
        if "ExportKeyword" not in t.modifier_kinds(statement):
            raise Unsupported(CODE, f"{rel}: model {name} must be exported")
        if t.field_list(statement, "typeParameters") or t.field_list(statement, "heritageClauses"):
            raise Unsupported(CODE, f"{rel}: model {name} must not be generic or extend others")
        body = statement if kind == "InterfaceDeclaration" else t.field(statement, "type")
        if body is None or (kind == "TypeAliasDeclaration" and t.kind(body) != "TypeLiteral"):
            raise Unsupported(CODE, f"{rel}: model {name} must be an object type")
        fields: list[dict[str, Any]] = []
        seen: set[str] = set()
        for member in t.field_list(body, "members"):
            field = t.identifier_name(t.field(member, "name"))
            value = t.field(member, "type")
            if (
                t.kind(member) != "PropertySignature"
                or field is None
                or value is None
                or t.modifier_kinds(member)
            ):
                raise Unsupported(CODE, f"{rel}: model {name} members must be plain properties")
            scalar = SCALARS.get(t.kind(value))
            if scalar is None:
                raise Unsupported(
                    CODE, f"{rel}: model {name}.{field} must be string, number or boolean"
                )
            if field in seen:
                raise Unsupported(CODE, f"{rel}: model {name} declares {field!r} twice")
            seen.add(field)
            fields.append(
                {
                    "name": field,
                    "type": scalar,
                    "optional": t.field(member, "questionToken") is not None,
                }
            )
        if not fields:
            raise Unsupported(CODE, f"{rel}: model {name} declares no members")
        return {"name": name, "module": rel, "fields": sorted(fields, key=lambda f: str(f["name"]))}
    raise Unsupported(CODE, f"{rel}: no exported interface or object type named {name}")
