# SPDX-License-Identifier: Apache-2.0
"""A deliberately closed PHP syntax profile. Customer code is never imported."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import tree_sitter_php
from tree_sitter import Language, Node, Parser

from .database import schema_tables, used_tables

VERSION = "0.1.0a1"
SCHEMA = "sanka.php-capture/v1"
MANIFEST = "sanka-php.json"
PARSER = "tree-sitter/0.25.2+tree-sitter-php/0.24.1"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
IGNORED = {"vendor", ".git", ".sanka", "node_modules", ".venv"}
IMPORTS = {
    "Illuminate\\Support\\Facades\\Route",
    "Illuminate\\Support\\Facades\\DB",
    "Illuminate\\Http\\Request",
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def safe_file(root: Path, name: str) -> Path:
    relative = Path(name)
    if (
        not name
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in name
        or name != relative.as_posix()
    ):
        raise ValueError("project paths must be normalized relative paths")
    path = root / relative
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("project paths must not contain symlinks")
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError(f"missing or oversized source: {name}")
    return path


def text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="strict")


def field(node: Node, name: str) -> Node:
    found = node.child_by_field_name(name)
    if found is None:
        raise ValueError(f"missing {name} in {node.type}")
    return found


def children(node: Node) -> list[Node]:
    return [child for child in node.named_children if child.type != "comment"]


def arguments(node: Node) -> list[Node]:
    result = []
    for arg in children(field(node, "arguments")):
        if arg.type != "argument" or len(children(arg)) != 1:
            raise ValueError("named, unpacked and reference arguments require adaptation")
        result.append(children(arg)[0])
    return result


def literal_string(node: Node) -> str:
    raw = text(node)
    if node.type not in {"string", "encapsed_string"} or len(raw) < 2:
        raise ValueError("a literal string is required")
    if raw[0] == "'":
        # PHP single quotes only interpret escaped backslash and quote.
        return re.sub(r"\\([\\'])", r"\1", raw[1:-1])
    if any(n.type not in {"string_content", "escape_sequence"} for n in children(node)):
        raise ValueError("interpolated PHP strings require adaptation")
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "e": "\x1b",
        "f": "\f",
        "\\": "\\",
        '"': '"',
        "$": "$",
    }

    def decode(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in escapes:
            raise ValueError("numeric or unknown PHP escape requires adaptation")
        return escapes[token]

    return re.sub(r"\\(.)", decode, raw[1:-1])


def literal(value: Any) -> dict[str, Any]:
    return {"op": "literal", "value": value}


class Compiler:
    """Capture expression semantics once for the two native generators."""

    def __init__(
        self,
        *,
        kind: str,
        dialect: str,
        parameters: list[str],
        transaction: bool = False,
        request_name: str | None = None,
    ) -> None:
        self.kind = kind
        self.dialect = dialect
        self.variables = set(parameters)
        self.request = request_name or (parameters[0] if parameters else "request")
        self.transaction = transaction

    def expression(self, node: Node) -> dict[str, Any]:
        kind = node.type
        value = text(node)
        if kind == "parenthesized_expression":
            return self.expression(children(node)[0])
        if kind in {"string", "encapsed_string"}:
            return literal(literal_string(node))
        if kind == "integer":
            if not re.fullmatch(r"0|[1-9][0-9]*", value) or int(value) > 2**63 - 1:
                raise ValueError("only signed 64-bit decimal PHP integers are supported")
            return literal(int(value))
        if kind == "boolean":
            return literal(value.lower() == "true")
        if kind == "null":
            return literal(None)
        if kind == "variable_name":
            name = value[1:]
            if name not in self.variables:
                raise ValueError(f"unbound or global variable ${name}")
            return {"op": "variable", "name": name}
        if kind == "array_creation_expression":
            entries = [children(item) for item in children(node)]
            if not entries or all(len(item) == 1 for item in entries):
                return {"op": "array", "items": [self.expression(item[0]) for item in entries]}
            if not all(len(item) == 2 for item in entries):
                raise ValueError("mixed keyed and sequential PHP arrays require adaptation")
            pairs = []
            keys = set()
            for key_node, item in entries:
                name = literal_string(key_node)
                if re.fullmatch(r"[+-]?[0-9]+", name) or name in keys:
                    raise ValueError("numeric or duplicate PHP array keys require adaptation")
                keys.add(name)
                pairs.append([name, self.expression(item)])
            return {"op": "object", "items": pairs}
        if kind == "subscript_expression":
            nodes = children(node)
            if len(nodes) != 2:
                raise ValueError("PHP array append requires adaptation")
            return {
                "op": "index",
                "object": self.expression(nodes[0]),
                "key": self.expression(nodes[1]),
            }
        if kind == "binary_expression":
            operator = text(field(node, "operator"))
            if operator not in {"===", "!==", "??", "&&", "||"}:
                raise ValueError(f"PHP operator {operator} requires semantic adaptation")
            return {
                "op": "binary",
                "operator": operator,
                "left": self.expression(field(node, "left")),
                "right": self.expression(field(node, "right")),
            }
        if kind == "unary_op_expression" and text(field(node, "operator")) == "!":
            return {"op": "not", "value": self.expression(field(node, "argument"))}
        if kind == "function_call_expression":
            name = text(field(node, "function"))
            args = arguments(node)
            if name == "env" and self.kind == "laravel" and len(args) == 1:
                key = literal_string(args[0])
                if not IDENTIFIER.fullmatch(key):
                    raise ValueError("environment variable must be a literal identifier")
                return {"op": "env", "name": key}
            if name == "getenv" and self.kind == "custom" and len(args) == 1:
                key = literal_string(args[0])
                if not IDENTIFIER.fullmatch(key):
                    raise ValueError("environment variable must be a literal identifier")
                return {"op": "getenv", "name": key}
            if name == "hash_equals" and len(args) == 2:
                return {
                    "op": "hash_equals",
                    "left": self.expression(args[0]),
                    "right": self.expression(args[1]),
                }
            raise ValueError(f"function {name} is outside the captured dependency closure")
        if kind == "member_call_expression":
            owner = field(node, "object")
            name = text(field(node, "name"))
            args = arguments(node)
            if (
                self.kind == "laravel"
                and text(owner) == "$" + self.request
                and self.request in self.variables
            ):
                if name in {"input", "query", "header", "route"} and 1 <= len(args) <= 2:
                    key = literal_string(args[0])
                    if "." in key:
                        raise ValueError("nested Laravel request keys require adaptation")
                    return {
                        "op": "request",
                        "kind": name,
                        "key": key,
                        "default": self.expression(args[1]) if len(args) == 2 else literal(None),
                    }
                if name == "bearerToken" and not args:
                    return {"op": "bearer"}
                if name == "validate" and len(args) == 1:
                    rules = self.expression(args[0])
                    validate_rules(rules)
                    return {"op": "validate", "rules": rules}
            if (
                self.kind == "laravel"
                and owner.type == "function_call_expression"
                and text(field(owner, "function")) == "response"
                and not arguments(owner)
                and name == "json"
                and 1 <= len(args) <= 2
            ):
                return {
                    "op": "response",
                    "body": self.expression(args[0]),
                    "status": self.expression(args[1]) if len(args) == 2 else literal(200),
                }
            raise ValueError(f"method {name} is outside the captured dependency closure")
        if kind == "scoped_call_expression":
            scope, name, args = (
                text(field(node, "scope")),
                text(field(node, "name")),
                arguments(node),
            )
            if scope == "DB" and self.kind == "laravel" and self.dialect != "none":
                if (
                    name == "transaction"
                    and len(args) == 1
                    and args[0].type == "anonymous_function"
                ):
                    if self.transaction:
                        raise ValueError("nested transactions require a separate profile")
                    closure = args[0]
                    if (
                        children(field(closure, "parameters"))
                        or closure.child_by_field_name("return_type") is not None
                        or any(
                            n.type in {"reference_modifier", "attribute_list"}
                            for n in children(closure)
                        )
                    ):
                        raise ValueError("transaction callback signature requires adaptation")
                    use = [
                        n for n in children(closure) if n.type == "anonymous_function_use_clause"
                    ]
                    captured = [] if not use else children(use[0])
                    if any(
                        n.type != "variable_name" or text(n)[1:] not in self.variables
                        for n in captured
                    ):
                        raise ValueError("transaction closure references an unsupported capture")
                    child = Compiler(
                        kind=self.kind,
                        dialect=self.dialect,
                        parameters=[text(n)[1:] for n in captured],
                        transaction=True,
                        request_name=self.request,
                    )
                    body = child.block(field(closure, "body"))
                    if not body or body[-1]["op"] not in {"return", "throw"}:
                        body.append({"op": "return", "value": literal(None)})
                    return {
                        "op": "transaction",
                        "body": body,
                        "captures": [text(n)[1:] for n in captured],
                    }
                if name in {"select", "insert", "update", "delete"} and 1 <= len(args) <= 2:
                    sql = literal_string(args[0])
                    table = validate_sql(sql, name)
                    params = (
                        self.expression(args[1]) if len(args) == 2 else {"op": "array", "items": []}
                    )
                    if params["op"] != "array" or sql.count("?") != len(params["items"]):
                        raise ValueError("SQL needs one bound value per positional placeholder")
                    return {"op": "sql", "kind": name, "sql": sql, "table": table, "params": params}
            raise ValueError(f"static call {scope}::{name} requires adaptation")
        raise ValueError(f"unsupported PHP expression: {kind}")

    def block(self, node: Node) -> list[dict[str, Any]]:
        return [
            self.statement(n)
            for n in (children(node) if node.type == "compound_statement" else [node])
        ]

    def statement(self, node: Node) -> dict[str, Any]:
        if node.type == "return_statement" and len(children(node)) == 1:
            return {"op": "return", "value": self.expression(children(node)[0])}
        if node.type == "expression_statement" and len(children(node)) == 1:
            item = children(node)[0]
            if item.type == "throw_expression":
                created = children(item)[0]
                parts = children(created)
                if (
                    created.type != "object_creation_expression"
                    or len(parts) != 2
                    or text(parts[0]) != "RuntimeException"
                    or parts[1].type != "arguments"
                    or len(children(parts[1])) != 1
                ):
                    raise ValueError("only literal RuntimeException is supported")
                message = literal_string(children(children(parts[1])[0])[0])
                return {"op": "throw", "value": literal(message)}
            if item.type == "assignment_expression":
                left = field(item, "left")
                if left.type != "variable_name" or not IDENTIFIER.fullmatch(text(left)[1:]):
                    raise ValueError("only local scalar assignments are supported")
                name = text(left)[1:]
                if self.kind == "laravel" and name == self.request:
                    raise ValueError("reassigning the Laravel request requires adaptation")
                if name.startswith("_") or name == "this":
                    raise ValueError("reserved PHP variable")
                value = self.expression(field(item, "right"))
                self.variables.add(name)
                return {"op": "assign", "name": name, "value": value}
            return {"op": "expression", "value": self.expression(item)}
        if node.type == "if_statement":
            condition = self.expression(field(node, "condition"))
            before = self.variables.copy()
            yes = self.block(field(node, "body"))
            yes_vars = self.variables.copy()
            self.variables = before.copy()
            alternate = node.child_by_field_name("alternative")
            no: list[dict[str, Any]] = []
            if alternate:
                if alternate.type != "else_clause":
                    raise ValueError("elseif requires an explicit nested if in this profile")
                no = self.block(field(alternate, "body"))
            self.variables &= yes_vars
            return {"op": "if", "condition": condition, "then": yes, "else": no}
        raise ValueError(f"unsupported PHP statement: {node.type}")


def validate_rules(value: dict[str, Any]) -> None:
    if value["op"] != "object":
        raise ValueError("Laravel validation rules must be a literal object")
    for key, spec in value["items"]:
        if (
            not IDENTIFIER.fullmatch(key)
            or spec["op"] != "literal"
            or not isinstance(spec["value"], str)
        ):
            raise ValueError("only flat literal Laravel rules are supported")
        rules = spec["value"].split("|")
        if "string" not in rules or not any(
            r in rules for r in ("required", "nullable", "sometimes")
        ):
            raise ValueError("validation requires an explicit string and presence rule")
        if any(
            not re.fullmatch(r"required|nullable|sometimes|string|max:[1-9][0-9]{0,3}", r)
            for r in rules
        ):
            raise ValueError("unsupported Laravel validation rule")


def validate_sql(sql: str, operation: str) -> str:
    """Accept one flat, parameterized statement; no identifiers from request data."""
    identifier = r"[A-Za-z_][A-Za-z0-9_]*"
    columns = rf"{identifier}(?:\s*,\s*{identifier})*"
    where = rf"(?:\s+WHERE\s+{identifier}\s*=\s*\?(?:\s+AND\s+{identifier}\s*=\s*\?)*)?"
    patterns = {
        "select": (
            rf"SELECT\s+{columns}\s+FROM\s+({identifier}){where}"
            rf"(?:\s+ORDER\s+BY\s+{identifier}(?:\s+(?:ASC|DESC))?)?"
            r"(?:\s+LIMIT\s+[1-9][0-9]{0,3})?"
        ),
        "insert": (
            rf"INSERT\s+INTO\s+({identifier})\s*\(\s*{columns}\s*\)"
            r"\s+VALUES\s*\(\s*\?(?:\s*,\s*\?)*\s*\)"
        ),
        "update": (
            rf"UPDATE\s+({identifier})\s+SET\s+{identifier}\s*=\s*\?"
            rf"(?:\s*,\s*{identifier}\s*=\s*\?)*{where}"
        ),
        "delete": rf"DELETE\s+FROM\s+({identifier}){where}",
    }
    match = re.fullmatch(patterns[operation], sql.strip(), flags=re.IGNORECASE)
    if match is None:
        raise ValueError("SQL is outside the single-table parameterized profile")
    if operation in {"update", "delete"} and not re.search(r"\bWHERE\b", sql, flags=re.IGNORECASE):
        raise ValueError("unbounded SQL writes require a separate profile")
    return match.group(1)


def parameters(node: Node, kind: str) -> list[str]:
    if node.child_by_field_name("return_type") is not None:
        raise ValueError("declared return types require a separately qualified profile")
    if any(child.type in {"attribute_list", "reference_modifier"} for child in children(node)):
        raise ValueError("attributes and reference returns require adaptation")
    names: list[str] = []
    for parameter in children(field(node, "parameters")):
        if (
            parameter.type != "simple_parameter"
            or parameter.child_by_field_name("default_value")
            or any(n.type in {"reference_modifier", "attribute_list"} for n in children(parameter))
        ):
            raise ValueError("default, reference and variadic parameters require adaptation")
        name = text(field(parameter, "name"))[1:]
        if not IDENTIFIER.fullmatch(name):
            raise ValueError("unsupported parameter name")
        annotation = parameter.child_by_field_name("type")
        if annotation is not None and text(annotation) not in {"array", "Request", "string"}:
            raise ValueError("unsupported parameter type")
        if kind == "custom" and annotation is not None and text(annotation) != "array":
            raise ValueError("custom PHP handlers receive an array request")
        if kind == "laravel" and names and annotation is not None and text(annotation) != "string":
            raise ValueError("Laravel path parameters must be strings")
        if (
            not names
            and kind == "laravel"
            and (annotation is None or text(annotation) != "Request")
        ):
            raise ValueError("Laravel route must declare its Illuminate Request parameter")
        names.append(name)
    if not names or (kind == "custom" and len(names) != 1):
        raise ValueError("the route must accept its request explicitly")
    return names


def _route(
    method: str,
    path: str,
    file: str,
    handler: Node | None,
    kind: str,
    dialect: str,
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    if method not in METHODS or not re.fullmatch(
        r"/(?:[A-Za-z0-9_/-]|\{[A-Za-z_][A-Za-z0-9_]*\})*", path
    ):
        raise ValueError("unsupported route method or path")
    result: dict[str, Any] = {
        "method": method,
        "path": path,
        "file": file,
        "line": handler.start_point.row + 1 if handler else 0,
        "gaps": list(gaps or []),
    }
    try:
        if handler is None:
            raise ValueError("route handler is unresolved")
        names = parameters(handler, kind)
        path_parameters = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
        if len(set(path_parameters)) != len(path_parameters):
            raise ValueError("duplicate path parameter names")
        if kind == "laravel" and names[1:] != path_parameters:
            raise ValueError("Laravel handler parameters must match the declared path in order")
        result["parameters"] = names
        compiler = Compiler(kind=kind, dialect=dialect, parameters=names)
        body = compiler.block(field(handler, "body"))
        if not body or body[-1]["op"] != "return":
            raise ValueError("route must end in an explicit return")
        if not result["gaps"]:
            result["body"] = body
    except ValueError as error:
        result["gaps"].append(str(error))
    return result


def _laravel_routes(tree: Node, name: str, dialect: str, gaps: list[str]) -> list[dict[str, Any]]:
    result = []
    imports_found = {
        text(n)
        for item in children(tree)
        if item.type == "namespace_use_declaration"
        for n in children(item)
    }
    if not {"Illuminate\\Support\\Facades\\Route", "Illuminate\\Http\\Request"} <= imports_found:
        gaps.append(f"{name}: Route and Request require explicit qualified imports")
    pending = [tree]
    while pending:
        node = pending.pop()
        if (
            node.type == "scoped_call_expression"
            and text(field(node, "scope")) == "DB"
            and "Illuminate\\Support\\Facades\\DB" not in imports_found
        ):
            gaps.append(f"{name}: DB requires an explicit qualified import")
            break
        pending.extend(children(node))
    for item in children(tree):
        if item.type in {"php_tag", "namespace_use_declaration"}:
            if item.type == "namespace_use_declaration":
                imports = [text(n) for n in children(item)]
                if any(n not in IMPORTS for n in imports):
                    gaps.append(f"{name}: unsupported import or import alias")
            continue
        if item.type != "expression_statement" or len(children(item)) != 1:
            gaps.append(f"{name}:{item.start_point.row + 1}: dynamic route declaration")
            continue
        call = children(item)[0]
        route_gaps = []
        while call.type == "member_call_expression":
            route_gaps.append(f"route modifier {text(field(call, 'name'))} requires adaptation")
            call = field(call, "object")
        try:
            if call.type != "scoped_call_expression" or text(field(call, "scope")) != "Route":
                raise ValueError("dynamic route declaration")
            method = text(field(call, "name")).upper()
            args = arguments(call)
            if len(args) != 2:
                raise ValueError("route needs a literal path and explicit handler")
            path = literal_string(args[0])
            handler = args[1] if args[1].type == "anonymous_function" else None
            result.append(_route(method, path, name, handler, "laravel", dialect, route_gaps))
        except ValueError as error:
            gaps.append(f"{name}:{item.start_point.row + 1}: {error}")
    return result


def capture(root: Path) -> dict[str, Any]:
    if root != root.resolve() or not root.is_dir():
        raise ValueError("project root must be a canonical directory")
    assessment_only = not (root / MANIFEST).exists()
    if assessment_only:
        declared_composer = (
            json.loads(safe_file(root, "composer.json").read_text())
            if (root / "composer.json").exists()
            else {}
        )
        inferred_kind = (
            "laravel" if "laravel/framework" in declared_composer.get("require", {}) else "custom"
        )
        manifest = {
            "schema": "sanka.php-project/v1",
            "kind": inferred_kind,
            "database": {"dialect": "none"},
            "routes": [],
            "route_files": ["routes/api.php"] if (root / "routes/api.php").is_file() else [],
        }
    else:
        manifest = json.loads(safe_file(root, MANIFEST).read_text())
    if not isinstance(manifest, dict) or manifest.get("schema") != "sanka.php-project/v1":
        raise ValueError("sanka-php.json must use sanka.php-project/v1")
    if set(manifest) - {"schema", "kind", "routes", "route_files", "database"}:
        raise ValueError("unknown PHP project settings")
    kind = manifest.get("kind")
    if kind not in {"custom", "laravel"}:
        raise ValueError("source kind must be custom or laravel")
    database = manifest.get("database", {"dialect": "none"})
    if not isinstance(database, dict) or set(database) - {"dialect", "schema_file", "tables"}:
        raise ValueError("unsupported database settings")
    dialect = database.get("dialect")
    if dialect not in {"none", "sqlite", "mysql"}:
        raise ValueError("preserve none, SQLite or MySQL in this profile")
    inventory: dict[str, str] = {}
    sources = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if set(relative.parts) & IGNORED:
            continue
        if path.is_symlink():
            raise ValueError("source tree contains a symlink")
        if path.is_file() and (
            path.suffix in {".php", ".sql"}
            or relative.as_posix()
            in {
                MANIFEST,
                "composer.json",
                "composer.lock",
                "sanka-verify.json",
            }
        ):
            data = safe_file(root, relative.as_posix()).read_bytes()
            total += len(data)
            if total > 10_000_000 or len(inventory) >= 1000:
                raise ValueError("PHP source exceeds capture limits")
            inventory[relative.as_posix()] = hashlib.sha256(data).hexdigest()
            if path.suffix == ".php":
                sources[relative.as_posix()] = data
    gaps: list[str] = (
        ["assessment only: define sanka-php.json with the route and data closure"]
        if assessment_only
        else []
    )
    composer: dict[str, Any] = {}
    if (root / "composer.json").exists():
        composer = json.loads(safe_file(root, "composer.json").read_text())
        requirements = composer.get("require", {})
        allowed = {"php", "ext-json", "ext-pdo", "ext-pdo_mysql", "ext-pdo_sqlite"}
        if kind == "laravel":
            allowed.add("laravel/framework")
        if set(requirements) - allowed:
            gaps.append("Composer dependencies outside the qualified source profile")
        if (
            composer.get("scripts")
            or composer.get("repositories")
            or composer.get("config", {}).get("allow-plugins")
            or composer.get("autoload")
        ):
            gaps.append(
                "Composer autoloaders, scripts, custom repositories or plugins require adaptation"
            )
        if kind == "laravel" and "laravel/framework" in requirements:
            lock = (
                json.loads(safe_file(root, "composer.lock").read_text())
                if (root / "composer.lock").exists()
                else {}
            )
            framework = [
                p.get("version", "").removeprefix("v")
                for p in lock.get("packages", [])
                if p.get("name") == "laravel/framework"
            ]
            if framework != ["12.70.1"]:
                gaps.append("source Composer lock must match qualified Laravel 12.70.1")
    models = []
    if dialect != "none":
        schema_file = database.get("schema_file")
        tables = database.get("tables")
        if (
            not isinstance(schema_file, str)
            or not isinstance(tables, list)
            or not tables
            or any(
                not isinstance(table, str) or not IDENTIFIER.fullmatch(table) for table in tables
            )
            or len(set(tables)) != len(tables)
        ):
            raise ValueError("database replay needs an explicit schema and unique table names")
        if not schema_file.endswith(".sql"):
            raise ValueError("schema_file must be a SQL source file")
        models = schema_tables(safe_file(root, schema_file).read_text(), dialect, tables)
    elif set(database) != {"dialect"}:
        raise ValueError("stateless sources cannot declare database fixtures")
    parser = Parser(Language(tree_sitter_php.language_php()))
    trees = {name: parser.parse(value).root_node for name, value in sources.items()}
    for name, tree in trees.items():
        if tree.has_error:
            gaps.append(f"{name}: PHP parse error")
    routes: list[dict[str, Any]] = []
    if kind == "custom":
        declared = manifest.get("routes")
        if (
            not isinstance(declared, list)
            or (not declared and not assessment_only)
            or len(declared) > 256
        ):
            raise ValueError("custom PHP requires 1 to 256 explicit routes")
        functions: dict[tuple[str, str], Node] = {}
        function_names: set[str] = set()
        for name, tree in trees.items():
            for node in children(tree):
                if node.type == "function_definition":
                    key = (name, text(field(node, "name")))
                    if key in functions:
                        gaps.append(f"{name}: duplicate function")
                    if key[1].lower() in function_names:
                        gaps.append(f"{name}: PHP global function name collision")
                    function_names.add(key[1].lower())
                    functions[key] = node
                elif node.type != "php_tag":
                    gaps.append(f"{name}:{node.start_point.row + 1}: top-level PHP execution")
        for route in declared:
            if not isinstance(route, dict) or set(route) != {"method", "path", "file", "handler"}:
                raise ValueError("route requires exactly method, path, file and handler")
            safe_file(root, route["file"])
            handler = functions.get((route["file"], route["handler"]))
            record = _route(route["method"], route["path"], route["file"], handler, kind, dialect)
            record["handler"] = route["handler"]
            routes.append(record)
    else:
        files = manifest.get("route_files")
        if (
            not isinstance(files, list)
            or (not files and not assessment_only)
            or len(set(files)) != len(files)
        ):
            raise ValueError("Laravel requires explicit route_files")
        for name in files:
            safe_file(root, name)
            if name not in trees:
                raise ValueError("route file must contain PHP")
            routes.extend(_laravel_routes(trees[name], name, dialect, gaps))
        # Bootstraps and providers can add auth, scopes and observers. Their presence
        # is explicit; a later qualified Laravel runtime profile must admit them.
        for name in sources.keys() - set(files):
            gaps.append(f"{name}: application bootstrap/dependency closure requires qualification")
    identities = [(r["method"], r["path"]) for r in routes]
    for route in routes:
        if used_tables(route) - set(database.get("tables", [])):
            route["gaps"].append("SQL accesses a table outside the declared data closure")
            route.pop("body", None)
    if len(set(identities)) != len(identities):
        gaps.append("duplicate route identities")
    for index, (method, path) in enumerate(identities):
        segments = path.split("/")
        for other_method, other_path in identities[index + 1 :]:
            other_segments = other_path.split("/")
            if (
                method == other_method
                and len(segments) == len(other_segments)
                and all(
                    left == right or "{" in left or "{" in right
                    for left, right in zip(segments, other_segments, strict=True)
                )
            ):
                gaps.append(f"overlapping route precedence requires adaptation: {method} {path}")
    if not routes:
        gaps.append("no statically discovered routes")
    return {
        "schema": SCHEMA,
        "parser": PARSER,
        "kind": kind,
        "database": database,
        "composer": composer,
        "models": models,
        "routes": routes,
        "gaps": gaps,
        "discovered_routes": len(routes),
        "assessment_only": assessment_only,
        "route_inventory_complete": not assessment_only and not gaps,
        "source_inventory": inventory,
        "source_digest": digest(inventory),
    }
