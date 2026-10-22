# SPDX-License-Identifier: Apache-2.0
"""Closed, syntax-checked flat JPA CRUD profile; no general Java transpilation.

JavaParser supplies syntax tokens, including complete method bodies. Each admitted
operation must match a documented repository idiom in full. Extra statements,
annotations, fields, inheritance and build configuration become global gaps.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .capture import (
    ANNOTATIONS,
    IMPORTS,
    JAVA,
    PARSER_SHA256,
    PARSER_VERSION,
    digest,
    identifier,
    mapping,
    parse,
)

PROPERTIES = (
    "spring.datasource.url=${SANKA_JAVA_DATABASE_URL}\n"
    "spring.datasource.username=${SANKA_JAVA_DATABASE_USER}\n"
    "spring.datasource.password=${SANKA_JAVA_DATABASE_PASSWORD}\n"
    "spring.jpa.hibernate.ddl-auto=validate\n"
    "spring.jpa.open-in-view=false\n"
)
NAMES = {
    **ANNOTATIONS,
    **{
        n: "jakarta.persistence." + n
        for n in ("Entity", "Table", "Id", "GeneratedValue", "GenerationType", "Column")
    },
    **{
        n: "jakarta.validation.constraints." + n
        for n in ("NotBlank", "NotNull", "Size", "DecimalMin", "Digits")
    },
    **{
        n: "org.springframework.web.bind.annotation." + n
        for n in (
            "PostMapping",
            "PutMapping",
            "DeleteMapping",
            "RequestBody",
            "RestControllerAdvice",
            "ExceptionHandler",
        )
    },
    "Valid": "jakarta.validation.Valid",
    "Transactional": "org.springframework.transaction.annotation.Transactional",
    "JsonFormat": "com.fasterxml.jackson.annotation.JsonFormat",
    "JpaRepository": "org.springframework.data.jpa.repository.JpaRepository",
    "BigDecimal": "java.math.BigDecimal",
    "ResponseEntity": "org.springframework.http.ResponseEntity",
    "HttpStatus": "org.springframework.http.HttpStatus",
    "MethodArgumentNotValidException": (
        "org.springframework.web.bind.MethodArgumentNotValidException"
    ),
    "HttpMessageNotReadableException": (
        "org.springframework.http.converter.HttpMessageNotReadableException"
    ),
    "MethodArgumentTypeMismatchException": (
        "org.springframework.web.method.annotation.MethodArgumentTypeMismatchException"
    ),
}
INVALID = {
    "MethodArgumentNotValidException",
    "HttpMessageNotReadableException",
    "MethodArgumentTypeMismatchException",
}


def tokens(text: str) -> list[str]:
    """Tokenize trusted grammar templates; application tokens come from JavaParser."""
    return re.findall(r'"(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|::|\S', text)


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def qualified_annotations(node: dict[str, Any], imports: set[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for annotation in node["annotations"]:
        name = annotation["name"]
        require(
            name not in result and name in NAMES and NAMES[name] in imports,
            f"unresolved or duplicate annotation {name}",
        )
        result[name] = annotation
    return result


def exact(annotation: dict[str, Any], source: str) -> bool:
    return bool(annotation["tokens"] == tokens(source))


def scalar(node: dict[str, Any]) -> Any:
    kind = node["kind"]
    if kind in {"string", "boolean"}:
        return node["value"]
    if kind == "integer" and re.fullmatch(r"0|[1-9][0-9]*", str(node["value"])):
        return int(node["value"])
    raise ValueError("annotation requires a literal scalar")


def values(annotation: dict[str, Any]) -> dict[str, Any]:
    return {k: scalar(v) for k, v in annotation["values"].items()}


def plain_method(method: dict[str, Any], *, abstract: bool = False) -> None:
    require(
        method["modifiers"] == ([] if abstract else ["public"])
        and not method["throws"]
        and not method["type_parameters"]
        and not method["type_annotations"]
        and not method["receiver"],
        "method modifiers, type annotations or signature require adaptation",
    )


def sql_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name))


def entity(typ: dict[str, Any], imports: set[str]) -> dict[str, Any]:
    ann = qualified_annotations(typ, imports)
    require(
        set(ann) == {"Entity", "Table"} and exact(ann["Entity"], "@Entity"),
        "entity requires only @Entity and an explicit @Table",
    )
    table = values(ann["Table"])
    require(set(table) == {"name"} and sql_name(table["name"]), "unsupported table name")
    require(
        not typ["methods"]
        and not typ["constructors"]
        and all(m == "FieldDeclaration" for m in typ["other_members"]),
        "entity must use plain public fields without custom behavior",
    )
    fields = []
    for field in typ["fields"]:
        require(
            field["modifiers"] == ["public"] and len(field["variables"]) == 1,
            "entity requires one public field per declaration",
        )
        variable = field["variables"][0]
        name, kind = variable["name"], variable["type"]
        require(
            identifier(name)
            and name not in {"metadata", "registry", "schema"}
            and not variable["initializer"]
            and not variable["type_annotations"],
            "entity field name, initializer or type requires adaptation",
        )
        fa = qualified_annotations(field, imports)
        require("Column" in fa, "every entity field requires an explicit @Column")
        column = values(fa["Column"])
        require("name" in column and sql_name(column["name"]), "unsupported column name")
        nullable = column.get("nullable", True)
        require(type(nullable) is bool, "nullable must be boolean")
        captured = {"name": name, "column": column["name"], "type": kind, "nullable": nullable}
        if name == "id":
            require(
                kind == "Long"
                and column == {"name": "id", "nullable": False}
                and set(fa) == {"Id", "GeneratedValue", "Column"}
                and exact(fa["Id"], "@Id")
                and exact(fa["GeneratedValue"], "@GeneratedValue(strategy=GenerationType.IDENTITY)")
                and NAMES["GenerationType"] in imports,
                "requires an identity Long id",
            )
        elif kind == "String":
            require(
                set(fa) == {"Column"}
                and set(column) <= {"name", "nullable", "length"}
                and type(column.get("length")) is int
                and 1 <= column["length"] <= 1000,
                "String columns require a bounded explicit length",
            )
            captured["length"] = column["length"]
        elif kind == "BigDecimal":
            require(
                NAMES["BigDecimal"] in imports
                and set(fa) == {"Column", "JsonFormat"}
                and exact(fa["JsonFormat"], "@JsonFormat(shape=JsonFormat.Shape.STRING)")
                and set(column) == {"name", "nullable", "precision", "scale"}
                and nullable is False
                and type(column["precision"]) is int
                and type(column["scale"]) is int
                and 1 <= column["precision"] <= 38
                and 0 <= column["scale"] < column["precision"],
                "decimal columns require bounded precision/scale and JSON STRING format",
            )
            captured.update(precision=column["precision"], scale=column["scale"])
        else:
            raise ValueError("only Long identity, String and BigDecimal fields are qualified")
        fields.append(captured)
    require(
        2 <= len(fields) <= 12
        and sum(f["name"] == "id" for f in fields) == 1
        and len({f["name"] for f in fields}) == len(fields)
        and len({f["column"] for f in fields}) == len(fields),
        "entity needs one id and unique bounded fields/columns",
    )
    return {"name": typ["name"], "table": table["name"], "fields": fields}


def request_model(typ: dict[str, Any], imports: set[str], model: dict[str, Any]) -> str:
    require(
        not typ["annotations"] and not typ["methods"] and not typ["other_members"],
        "input must be a plain validated record",
    )
    fields = [f for f in model["fields"] if f["name"] != "id"]
    require(len(typ["components"]) == len(fields), "input must contain all writable fields")
    for component, field in zip(typ["components"], fields, strict=True):
        require(
            component["name"] == field["name"]
            and component["type"] == field["type"]
            and component["plain"],
            "input components must match writable entity fields in order",
        )
        ann = qualified_annotations(component, imports)
        if field["type"] == "String":
            required = {"Size"} | (set() if field["nullable"] else {"NotBlank"})
            require(
                set(ann) == required
                and values(ann["Size"]) == {"max": field["length"]}
                and (field["nullable"] or exact(ann["NotBlank"], "@NotBlank")),
                "String validation must match column length and nullability",
            )
        else:
            require(
                NAMES["BigDecimal"] in imports
                and set(ann) == {"NotNull", "DecimalMin", "Digits"}
                and exact(ann["NotNull"], "@NotNull")
                and values(ann["DecimalMin"]) == {"value": "0.00"}
                and values(ann["Digits"])
                == {"integer": field["precision"] - field["scale"], "fraction": field["scale"]},
                "decimal validation requires nonnegative values and schema-matching digits",
            )
    return str(typ["name"])


def operation_bodies(model: dict[str, Any], missing: str, rejected: str) -> dict[str, list[str]]:
    name = model["name"]
    assignments = "".join(
        f"entity.{f['name']} = input.{f['name']}();" for f in model["fields"] if f["name"] != "id"
    )
    find = f"repository.findById(id).orElseThrow({missing}::new)"
    new = f"{name} entity = new {name}();{assignments}"
    bodies = {
        "list": "return repository.findAllByOrderByIdAsc();",
        "get": f"return {find};",
        "create": new
        + "return ResponseEntity.status(HttpStatus.CREATED).body(repository.saveAndFlush(entity));",
        "update": f"{name} entity = {find};"
        + assignments
        + "return ResponseEntity.ok(repository.saveAndFlush(entity));",
        "delete": f"{name} entity = {find};repository.delete(entity);"
        "return ResponseEntity.noContent().build();",
        "rollback": new + f"repository.saveAndFlush(entity);throw new {rejected}();",
    }
    return {kind: tokens("{" + body + "}") for kind, body in bodies.items()}


def capture_crud(root: Path, files: dict[str, str], profile: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "sanka.java-capture/v1",
        "profile": profile | {"contract": "flat-jpa-crud/v1", "postgresql": "17.11"},
        "parser": {"version": PARSER_VERSION, "sha256": PARSER_SHA256},
        "analyzer_digest": hashlib.sha256((JAVA / "Analyzer.java").read_bytes()).hexdigest(),
        "source_inventory": files,
        "source_digest": digest(files),
        "application": None,
        "records": {},
        "endpoints": [],
        "gaps": [],
        "ready": False,
    }
    try:
        for name in files:
            if name.startswith("src/main/") and not (
                name.startswith("src/main/java/") and name.endswith(".java")
            ):
                require(
                    name == "src/main/resources/application.properties",
                    f"unsupported runtime resource {name}",
                )
            require(
                not name.startswith((".mvn/", "config/"))
                and not name.endswith((".gradle", ".gradle.kts"))
                and not re.fullmatch(r"application(?:-[^.]+)?\.(?:properties|yaml|yml)", name),
                f"unsupported build/runtime configuration {name}",
            )
        require(
            (root / "src/main/resources/application.properties").read_text() == PROPERTIES,
            "datasource configuration must use the qualified environment placeholders "
            "and validate-only schema",
        )
        units = parse(
            root, sorted(n for n in files if n.startswith("src/main/java/") and n.endswith(".java"))
        )
        require(len({u["package"] for u in units}) == 1, "all profile types must share one package")
        declarations = {}
        for unit in units:
            require(
                unit["plain"]
                and len(unit["types"]) == 1
                and re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", unit["package"]),
                "one public type per explicit lowercase package file is required",
            )
            imports = {i["name"] for i in unit["imports"]}
            require(
                all(
                    not i["static"]
                    and not i["wildcard"]
                    and i["name"] in IMPORTS | set(NAMES.values())
                    for i in unit["imports"]
                ),
                "unresolved, static or wildcard import",
            )
            typ = unit["types"][0]
            name = typ["name"]
            require(
                typ["kind"] in {"ClassOrInterfaceDeclaration", "RecordDeclaration"}
                and (typ["kind"] != "RecordDeclaration" or typ["plain"]),
                "unsupported declaration or record inheritance",
            )
            require(
                identifier(name)
                and name not in declarations
                and name
                not in set(NAMES)
                | {"String", "Long", "List", "Object", "RuntimeException", "Void"},
                "duplicate or shadowed profile type",
            )
            require(
                typ["modifiers"] == ["public"]
                and not typ["implements"]
                and not typ["type_parameters"],
                "type modifiers, generics or interfaces require adaptation",
            )
            qualified_annotations(typ, imports)
            declarations[name] = (unit, typ, imports)
        roles = {}
        exceptions = set()
        for name, (unit, typ, imports) in declarations.items():
            ann = qualified_annotations(typ, imports)
            if "Entity" in ann:
                role = "entity"
            elif "SpringBootApplication" in ann:
                role = "application"
            elif "RestController" in ann:
                role = "controller"
            elif "RestControllerAdvice" in ann:
                role = "errors"
            elif typ["interface"]:
                role = "repository"
            elif typ["kind"] == "RecordDeclaration":
                role = (
                    "error_record"
                    if len(typ["components"]) == 1 and typ["components"][0]["name"] == "code"
                    else "input"
                )
            elif typ["extends"] == ["RuntimeException"]:
                require(
                    not ann and not typ["methods"] and not typ["other_members"],
                    "exception has custom behavior",
                )
                exceptions.add(name)
                continue
            else:
                raise ValueError(f"{name}: unsupported dependency in CRUD application")
            require(role not in roles, f"multiple {role} types require adaptation")
            if role != "repository":
                require(
                    not typ["extends"] and not typ["interface"],
                    f"{name}: inheritance requires adaptation",
                )
            roles[role] = (unit, typ, imports)
        require(
            set(roles)
            == {
                "entity",
                "application",
                "controller",
                "errors",
                "repository",
                "input",
                "error_record",
            }
            and len(exceptions) == 2,
            "requires one complete CRUD dependency closure and two plain domain exceptions",
        )
        _, app, ai = roles["application"]
        require(
            set(qualified_annotations(app, ai)) == {"SpringBootApplication"}
            and exact(
                qualified_annotations(app, ai)["SpringBootApplication"], "@SpringBootApplication"
            )
            and len(app["methods"]) == 1
            and not app["other_members"],
            "custom application bootstrap",
        )
        main = app["methods"][0]
        require(
            main["main"]
            and main["parameters"][0]["plain"]
            and not main["parameters"][0]["annotations"]
            and main["body_tokens"]
            == tokens(
                "{SpringApplication.run("
                + app["name"]
                + ".class,"
                + main["parameters"][0]["name"]
                + ");}"
            )
            and "org.springframework.boot.SpringApplication" in ai,
            "custom application main",
        )
        result["application"] = roles["application"][0]["package"] + "." + app["name"]
        _, et, ei = roles["entity"]
        model = entity(et, ei)
        _, it, ii = roles["input"]
        input_name = request_model(it, ii, model)
        _, error, _ = roles["error_record"]
        require(
            not error["annotations"]
            and not error["methods"]
            and not error["other_members"]
            and error["components"][0]["type"] == "String"
            and error["components"][0]["plain"]
            and not error["components"][0]["annotations"],
            "error response requires a plain String code record",
        )
        _, repo, ri = roles["repository"]
        require(
            not repo["annotations"]
            and not repo["other_members"]
            and [t.replace(" ", "") for t in repo["extends"]]
            == [f"JpaRepository<{model['name']},Long>"]
            and NAMES["JpaRepository"] in ri
            and "java.util.List" in ri
            and len(repo["methods"]) == 1,
            "repository must extend JpaRepository and declare only stable id ordering",
        )
        listing = repo["methods"][0]
        plain_method(listing, abstract=True)
        require(
            listing["name"] == "findAllByOrderByIdAsc"
            and not listing["parameters"]
            and not listing["annotations"]
            and not listing["body_tokens"]
            and listing["type"].replace(" ", "") == f"List<{model['name']}>",
            "unsupported repository operation",
        )
        _, advice, ad_i = roles["errors"]
        require(
            set(qualified_annotations(advice, ad_i)) == {"RestControllerAdvice"}
            and exact(
                qualified_annotations(advice, ad_i)["RestControllerAdvice"], "@RestControllerAdvice"
            )
            and not advice["other_members"]
            and NAMES["ResponseEntity"] in ad_i
            and NAMES["HttpStatus"] in ad_i,
            "unsupported global error advice",
        )
        errors = {}
        for method in advice["methods"]:
            plain_method(method)
            ann = qualified_annotations(method, ad_i)
            require(
                set(ann) == {"ExceptionHandler"}
                and not method["parameters"]
                and method["type"].replace(" ", "") == f"ResponseEntity<{error['name']}>",
                "unsupported error handler signature",
            )
            at = ann["ExceptionHandler"]["tokens"]
            require(
                len(at) == 7 and at == tokens("@ExceptionHandler(" + at[3] + ".class)"),
                "one explicit exception class required",
            )
            exception = at[3]
            require(
                exception in exceptions | INVALID
                and exception not in errors
                and (exception in exceptions or NAMES[exception] in ad_i),
                "unresolved error dependency",
            )
            body = method["body_tokens"]
            strings = [t for t in body if t.startswith('"')]
            require(len(strings) == 1, "error body requires one literal code")
            code = json.loads(strings[0])
            require(re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code), "unsupported error code")
            statuses = {"BAD_REQUEST": 400, "NOT_FOUND": 404, "CONFLICT": 409}
            matched = [
                status
                for status in statuses
                if body
                == tokens(
                    "{return ResponseEntity.status(HttpStatus."
                    + status
                    + ").body(new "
                    + error["name"]
                    + "("
                    + strings[0]
                    + "));}"
                )
            ]
            require(len(matched) == 1, "unsupported error handler body")
            errors[exception] = {"status": statuses[matched[0]], "code": code}
        require(
            set(errors) == exceptions | INVALID
            and len({(errors[e]["status"], errors[e]["code"]) for e in INVALID}) == 1
            and all(errors[e]["status"] == 400 for e in INVALID),
            "complete consistent explicit validation handlers required",
        )
        missing = [e for e in exceptions if errors[e]["status"] == 404]
        rejected = [e for e in exceptions if errors[e]["status"] == 409]
        require(
            len(missing) == len(rejected) == 1,
            "requires distinct not-found and rejected-write handlers",
        )
        model["errors"] = {
            "invalid": errors[next(iter(INVALID))],
            "missing": errors[missing[0]],
            "rejected": errors[rejected[0]],
        }
        unit, controller, ci = roles["controller"]
        ca = qualified_annotations(controller, ci)
        require(
            set(ca) == {"RestController", "RequestMapping"}
            and exact(ca["RestController"], "@RestController")
            and not any(
                m not in {"FieldDeclaration", "ConstructorDeclaration"}
                for m in controller["other_members"]
            ),
            "unsupported controller annotations or members",
        )
        prefix = mapping(ca["RequestMapping"]["values"])
        require("{" not in prefix, "controller prefix must be literal")
        require(
            len(controller["fields"]) == len(controller["constructors"]) == 1,
            "one constructor-injected repository required",
        )
        field = controller["fields"][0]
        require(
            field["modifiers"] == ["final", "private"]
            and not field["annotations"]
            and field["variables"]
            == [
                {
                    "name": "repository",
                    "type": repo["name"],
                    "type_annotations": 0,
                    "initializer": [],
                }
            ],
            "controller must inject only its repository",
        )
        constructor = controller["constructors"][0]
        require(
            constructor["modifiers"] == ["public"]
            and not constructor["annotations"]
            and not constructor["throws"]
            and not constructor["type_parameters"]
            and constructor["parameters"]
            == [{"name": "repository", "type": repo["name"], "annotations": [], "plain": True}]
            and constructor["body_tokens"] == tokens("{this.repository=repository;}"),
            "custom constructor behavior",
        )
        bodies = operation_bodies(model, missing[0], rejected[0])
        expected_methods = {
            "list": "GetMapping",
            "get": "GetMapping",
            "create": "PostMapping",
            "update": "PutMapping",
            "delete": "DeleteMapping",
            "rollback": "PostMapping",
        }
        return_types = {
            "list": f"List<{model['name']}>",
            "get": model["name"],
            "create": f"ResponseEntity<{model['name']}>",
            "update": f"ResponseEntity<{model['name']}>",
            "delete": "ResponseEntity<Void>",
            "rollback": model["name"],
        }
        used = set()
        for method in controller["methods"]:
            plain_method(method)
            kinds = [kind for kind, body in bodies.items() if method["body_tokens"] == body]
            require(
                len(kinds) == 1 and kinds[0] not in used,
                "controller body is outside the qualified repository grammar",
            )
            kind = kinds[0]
            used.add(kind)
            ma = qualified_annotations(method, ci)
            mapping_name = expected_methods[kind]
            mutation = kind in {"create", "update", "delete", "rollback"}
            require(
                set(ma) == {mapping_name} | ({"Transactional"} if mutation else set())
                and (not mutation or exact(ma["Transactional"], "@Transactional"))
                and method["type"].replace(" ", "") == return_types[kind],
                "mapping, transaction or response type requires adaptation",
            )
            require(
                "java.util.List" in ci
                and NAMES["ResponseEntity"] in ci
                and NAMES["HttpStatus"] in ci,
                "unresolved controller return type",
            )
            path = prefix + (
                mapping(ma[mapping_name]["values"]) if ma[mapping_name]["values"] else ""
            )
            needs_id = kind in {"get", "update", "delete"}
            needs_body = kind in {"create", "update", "rollback"}
            require(
                re.findall(r"\{([^}]+)\}", path) == (["id"] if needs_id else []),
                "CRUD path must bind only id",
            )
            require(
                len(method["parameters"]) == int(needs_id) + int(needs_body),
                "unsupported CRUD parameter",
            )
            for param in method["parameters"]:
                pa = qualified_annotations(param, ci)
                require(param["plain"], "parameter modifiers require adaptation")
                if param["name"] == "id" and needs_id:
                    require(
                        param["type"] == "long"
                        and set(pa) == {"PathVariable"}
                        and exact(pa["PathVariable"], '@PathVariable("id")'),
                        "id requires explicit long binding",
                    )
                elif param["name"] == "input" and needs_body:
                    require(
                        param["type"] == input_name
                        and set(pa) == {"Valid", "RequestBody"}
                        and exact(pa["Valid"], "@Valid")
                        and exact(pa["RequestBody"], "@RequestBody"),
                        "body requires a validated input record",
                    )
                else:
                    raise ValueError("only named id/input parameters are qualified")
            require(
                [p["name"] for p in method["parameters"]]
                == (["id"] if needs_id else []) + (["input"] if needs_body else []),
                "parameter order or duplicate binding requires adaptation",
            )
            result["endpoints"].append(
                {
                    "method": mapping_name.removesuffix("Mapping").upper(),
                    "path": path,
                    "operation": kind,
                    "file": str(Path(unit["file"]).relative_to(root)),
                    "line": method["line"],
                    "gaps": [],
                    "disposition": "supported",
                }
            )
        require(
            used == set(bodies),
            "complete CRUD and rollback operations are required for this profile",
        )
        require(
            len({(e["method"], re.sub(r"\{[^}]+\}", "{}", e["path"])) for e in result["endpoints"]})
            == len(result["endpoints"]),
            "ambiguous CRUD route mappings",
        )
        result.update(persistence=model, ready=True)
    except (ValueError, OSError, KeyError, TypeError) as error:
        result["gaps"].append(str(error))
    return result


def schema_sql(model: dict[str, Any]) -> str:
    columns = []
    for field in model["fields"]:
        if field["name"] == "id":
            sql = "bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        elif field["type"] == "String":
            sql = f"varchar({field['length']})"
        else:
            sql = f"numeric({field['precision']},{field['scale']})"
        if field["name"] != "id" and not field["nullable"]:
            sql += " NOT NULL"
        columns.append(f'"{field["column"]}" {sql}')
    return f'CREATE TABLE "{model["table"]}" (' + ", ".join(columns) + ");\n"


def snapshot_sql(model: dict[str, Any]) -> str:
    columns = [
        f'"{f["column"]}"' + ("::text" if f["type"] == "BigDecimal" else "") + f' AS "{f["name"]}"'
        for f in model["fields"]
    ]
    return f'SELECT {", ".join(columns)} FROM "{model["table"]}" ORDER BY "id"'
