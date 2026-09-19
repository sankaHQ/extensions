# SPDX-License-Identifier: Apache-2.0
"""Typed lowering of captured screen expressions to Swift.

JavaScript numbers become ``Double``, strings ``String``, booleans ``Bool``; arrays and
object literals become generated value types named after the state that holds them.
Every construct the capture grammar allows but Swift cannot express with one static
type is a gap with a stable reason code, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .expressions import Unsupported

Expr = dict[str, Any]

GAP_TYPE = "SANKA_RN_SWIFTUI_TYPE"
GAP_IDENTIFIER = "SANKA_RN_SWIFTUI_IDENTIFIER"
GAP_EXPRESSION = "SANKA_RN_SWIFTUI_EXPRESSION"
SWIFT_KEYWORDS = frozenset(
    {
        "Any",
        "Self",
        "Type",
        "as",
        "associatedtype",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "continue",
        "default",
        "defer",
        "deinit",
        "do",
        "else",
        "enum",
        "extension",
        "fallthrough",
        "false",
        "fileprivate",
        "for",
        "func",
        "guard",
        "if",
        "import",
        "in",
        "init",
        "inout",
        "internal",
        "is",
        "let",
        "nil",
        "open",
        "operator",
        "precedencegroup",
        "private",
        "protocol",
        "public",
        "repeat",
        "rethrows",
        "return",
        "self",
        "static",
        "struct",
        "subscript",
        "super",
        "switch",
        "throw",
        "throws",
        "true",
        "try",
        "typealias",
        "var",
        "where",
        "while",
    }
)
# Members every generated value type defines; a captured field with one of these names
# would shadow them.
RESERVED_MEMBERS = frozenset({"json"})
BINARY_OPERATORS = {
    "+": "+",
    "-": "-",
    "===": "==",
    "!==": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "&&": "&&",
    "||": "||",
}
SCALARS = ("string", "number", "boolean")
SAFE_INTEGER = 2**53


@dataclass(frozen=True)
class Ty:
    """A static type: a scalar kind, ``array`` with ``element``, or ``object`` with ``fields``."""

    kind: str
    element: Ty | None = None
    fields: tuple[tuple[str, Ty], ...] = ()
    name: str = ""

    @property
    def field_types(self) -> dict[str, Ty]:
        return dict(self.fields)


STRING = Ty("string")
NUMBER = Ty("number")
BOOLEAN = Ty("boolean")
NULL = Ty("null")
UNKNOWN = Ty("unknown")
PARAM_TYPES = {"string": STRING, "number": NUMBER, "boolean": BOOLEAN}


def array_of(element: Ty) -> Ty:
    return Ty("array", element=element)


def object_of(fields: dict[str, Ty]) -> Ty:
    return Ty("object", fields=tuple(sorted(fields.items())))


def describe(ty: Ty) -> str:
    if ty.kind == "array":
        return f"array<{describe(ty.element or UNKNOWN)}>"
    if ty.kind == "object":
        return "{" + ", ".join(f"{name}: {describe(item)}" for name, item in ty.fields) + "}"
    return ty.kind


def unify(left: Ty, right: Ty, where: str) -> Ty:
    """Return the common type of two inferred types or raise a typing gap."""
    if left.kind == "unknown":
        return right
    if right.kind == "unknown":
        return left
    if left.kind != right.kind:
        raise Unsupported(
            GAP_TYPE, f"{where}: {describe(left)} and {describe(right)} do not share a Swift type"
        )
    if left.kind == "array":
        element = unify(left.element or UNKNOWN, right.element or UNKNOWN, where)
        return Ty("array", element=element, name=left.name or right.name)
    if left.kind == "object":
        if [name for name, _ in left.fields] != [name for name, _ in right.fields]:
            raise Unsupported(
                GAP_TYPE, f"{where}: object shapes {describe(left)} and {describe(right)} differ"
            )
        fields = {
            name: unify(item, right.field_types[name], f"{where}.{name}")
            for name, item in left.fields
        }
        return Ty("object", fields=tuple(sorted(fields.items())), name=left.name or right.name)
    return left


def is_complete(ty: Ty) -> bool:
    if ty.kind == "unknown" or ty.kind == "null":
        return False
    if ty.kind == "array":
        return is_complete(ty.element or UNKNOWN)
    if ty.kind == "object":
        return all(is_complete(item) for _, item in ty.fields)
    return True


def bind_names(ty: Ty, prefix: str, where: str) -> Ty:
    """Assign generated Swift struct names to every object type below ``ty``."""
    if ty.kind == "array":
        return replace(ty, element=bind_names(ty.element or UNKNOWN, prefix + "Item", where))
    if ty.kind == "object":
        fields = {}
        for name, item in ty.fields:
            identifier(name, f"{where} field", members=True)
            fields[name] = bind_names(item, prefix + name[:1].upper() + name[1:], f"{where}.{name}")
        return Ty("object", fields=tuple(sorted(fields.items())), name=prefix)
    return ty


def collect_objects(ty: Ty, into: dict[str, Ty]) -> None:
    """Collect every named object type below ``ty`` (structural conflicts are gaps)."""
    if ty.kind == "array":
        collect_objects(ty.element or UNKNOWN, into)
    elif ty.kind == "object":
        existing = into.get(ty.name)
        if existing is not None and existing.fields != ty.fields:
            raise Unsupported(GAP_TYPE, f"value type {ty.name} would need two different shapes")
        into[ty.name] = ty
        for _, item in ty.fields:
            collect_objects(item, into)


def swift_type(ty: Ty) -> str:
    if ty.kind == "string":
        return "String"
    if ty.kind == "number":
        return "Double"
    if ty.kind == "boolean":
        return "Bool"
    if ty.kind == "array":
        return "[" + swift_type(ty.element or UNKNOWN) + "]"
    if ty.kind == "object" and ty.name:
        return ty.name
    raise Unsupported(GAP_TYPE, f"{describe(ty)} has no Swift type")


def identifier(name: str, where: str, *, members: bool = False) -> str:
    """Validate a captured name as a Swift identifier; reserved words are gaps."""
    if not name.isidentifier() or not name.isascii():
        raise Unsupported(GAP_IDENTIFIER, f"{where}: {name!r} is not a Swift identifier")
    if name in SWIFT_KEYWORDS or (members and name in RESERVED_MEMBERS):
        raise Unsupported(GAP_IDENTIFIER, f"{where}: {name!r} is reserved in generated Swift")
    return name


def swift_string(value: str) -> str:
    escaped = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif ord(character) < 0x20 or character == "\x7f":
            escaped.append(f"\\u{{{ord(character):x}}}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def swift_number(value: int | float) -> str:
    if isinstance(value, bool):
        raise Unsupported(GAP_EXPRESSION, "booleans are not numbers")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise Unsupported(GAP_EXPRESSION, "non-finite numbers are not qualified")
    return repr(number)


def js_number(value: int | float) -> str:
    """Format a literal the way JavaScript's ``String(number)`` does for common values."""
    number = float(value)
    if number == int(number) and abs(number) < SAFE_INTEGER:
        return str(int(number))
    return repr(number)


@dataclass(frozen=True)
class Env:
    """Types of the symbols an expression may reference, plus how to spell them."""

    state: dict[str, Ty]
    params: dict[str, Ty]
    items: tuple[Ty, ...] = ()
    item_access: str = ""

    def with_item(self, ty: Ty) -> Env:
        return replace(self, items=(*self.items, ty))


def infer(expr: Expr, env: Env) -> Ty:
    """Return the static type of ``expr`` without generating code."""
    if "lit" in expr:
        return _literal_type(expr["lit"])
    if "ref" in expr:
        chain = list(expr["ref"])
        if chain[0] not in env.state:
            raise Unsupported(GAP_EXPRESSION, f"unknown state value {chain[0]!r}")
        return _walk(env.state[chain[0]], chain[1:], chain[0])
    if "param" in expr:
        if expr["param"] not in env.params:
            raise Unsupported(GAP_EXPRESSION, f"unknown route parameter {expr['param']!r}")
        return env.params[expr["param"]]
    if "item" in expr:
        if not env.items:
            raise Unsupported(GAP_EXPRESSION, "list item referenced outside a list")
        return _walk(env.items[-1], list(expr["item"]), "item")
    if "arg" in expr:
        raise Unsupported(
            GAP_EXPRESSION, "event arguments may only be assigned directly to a state value"
        )
    if "length" in expr:
        inner = infer(expr["length"], env)
        if inner.kind not in {"array", "string"}:
            raise Unsupported(GAP_TYPE, f".length of {describe(inner)} is not qualified")
        return NUMBER
    if "string" in expr:
        inner = infer(expr["string"], env)
        if inner.kind not in SCALARS:
            raise Unsupported(GAP_TYPE, f"String() of {describe(inner)} is not qualified")
        return STRING
    if "not" in expr:
        inner = infer(expr["not"], env)
        if inner.kind != "boolean":
            raise Unsupported(GAP_TYPE, f"! of {describe(inner)} needs JavaScript truthiness")
        return BOOLEAN
    if "binary" in expr:
        return _binary_type(expr, env)
    if "cond" in expr:
        condition = infer(expr["cond"], env)
        if condition.kind != "boolean":
            raise Unsupported(GAP_TYPE, "conditions must be boolean, not JavaScript truthiness")
        return unify(infer(expr["then"], env), infer(expr["else"], env), "conditional branches")
    if "template" in expr:
        for part in expr["template"]:
            if infer(part, env).kind not in {*SCALARS, "null"}:
                raise Unsupported(GAP_TYPE, "template literals interpolate scalars only")
        return STRING
    if "array" in expr:
        element = UNKNOWN
        for item in expr["array"]:
            element = unify(element, infer(item, env), "array elements")
        return array_of(element)
    if "object" in expr:
        return object_of({key: infer(value, env) for key, value in expr["object"].items()})
    if "append" in expr:
        base = infer(expr["append"], env)
        if base.kind != "array":
            raise Unsupported(GAP_TYPE, "array appends must spread an array state value")
        element = unify(base.element or UNKNOWN, infer(expr["value"], env), "appended value")
        return Ty("array", element=element, name=base.name)
    raise Unsupported(GAP_EXPRESSION, f"unsupported expression {sorted(expr)}")


def _literal_type(value: Any) -> Ty:
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, int | float):
        return NUMBER
    if isinstance(value, str):
        return STRING
    if value is None:
        return NULL
    raise Unsupported(GAP_EXPRESSION, f"unsupported literal {value!r}")


def _walk(ty: Ty, fields: list[str], where: str) -> Ty:
    current = ty
    for name in fields:
        if current.kind != "object" or name not in current.field_types:
            raise Unsupported(GAP_TYPE, f"{where}: {describe(current)} has no field {name!r}")
        current = current.field_types[name]
    return current


def _binary_type(expr: Expr, env: Env) -> Ty:
    operator = expr["binary"]
    left = infer(expr["left"], env)
    right = infer(expr["right"], env)
    if operator not in BINARY_OPERATORS:
        raise Unsupported(GAP_EXPRESSION, f"operator {operator!r} is not qualified")
    if operator == "+":
        if left.kind == right.kind and left.kind in {"number", "string"}:
            return left
        raise Unsupported(GAP_TYPE, f"+ of {describe(left)} and {describe(right)} would coerce")
    if operator == "-":
        if left.kind == right.kind == "number":
            return NUMBER
        raise Unsupported(GAP_TYPE, "- requires two numbers")
    if operator in {"===", "!=="}:
        if left.kind == right.kind and left.kind in SCALARS:
            return BOOLEAN
        raise Unsupported(
            GAP_TYPE, f"{operator} of {describe(left)} and {describe(right)} is not qualified"
        )
    if operator in {"&&", "||"}:
        if left.kind == right.kind == "boolean":
            return BOOLEAN
        raise Unsupported(GAP_TYPE, f"{operator} of non-boolean operands needs truthiness")
    if left.kind == right.kind == "number":
        return BOOLEAN
    raise Unsupported(GAP_TYPE, f"{operator} requires two numbers")


def emit(expr: Expr, env: Env, expected: Ty | None = None) -> str:
    """Return Swift code for ``expr``; ``expected`` names the value type of object literals."""
    if "lit" in expr:
        value = expr["lit"]
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return swift_number(value)
        if isinstance(value, str):
            return swift_string(value)
        raise Unsupported(GAP_EXPRESSION, "null is only qualified inside rendered text")
    if "ref" in expr:
        chain = list(expr["ref"])
        infer(expr, env)
        return "state." + ".".join(chain)
    if "param" in expr:
        infer(expr, env)
        return "params." + str(expr["param"])
    if "item" in expr:
        infer(expr, env)
        return "".join(
            [f"item{len(env.items) - 1}", env.item_access, *(f".{name}" for name in expr["item"])]
        )
    if "length" in expr:
        inner = infer(expr["length"], env)
        code = emit(expr["length"], env)
        return f"Double({code}.utf16.count)" if inner.kind == "string" else f"Double({code}.count)"
    if "string" in expr:
        return emit_template(expr["string"], env)
    if "not" in expr:
        infer(expr, env)
        return f"!({emit(expr['not'], env)})"
    if "binary" in expr:
        infer(expr, env)
        left = emit(expr["left"], env)
        right = emit(expr["right"], env)
        return f"({left} {BINARY_OPERATORS[expr['binary']]} {right})"
    if "cond" in expr:
        infer(expr, env)
        condition = emit(expr["cond"], env)
        then_code = emit(expr["then"], env, expected)
        else_code = emit(expr["else"], env, expected)
        return f"({condition} ? {then_code} : {else_code})"
    if "template" in expr:
        return emit_template(expr, env)
    if "array" in expr:
        ty = infer(expr, env)
        target = expected if expected is not None and expected.kind == "array" else ty
        element = target.element or UNKNOWN
        if not expr["array"]:
            return f"{swift_type(target)}()"
        return "[" + ", ".join(emit(item, env, element) for item in expr["array"]) + "]"
    if "object" in expr:
        return _emit_object(expr, env, expected)
    if "append" in expr:
        ty = infer(expr, env)
        target = expected if expected is not None and expected.kind == "array" else ty
        base = emit(expr["append"], env)
        return f"({base} + [{emit(expr['value'], env, target.element or UNKNOWN)}])"
    if "arg" in expr or "local" in expr:
        infer(expr, env)
    raise Unsupported(GAP_EXPRESSION, f"unsupported expression {sorted(expr)}")


def _emit_object(expr: Expr, env: Env, expected: Ty | None) -> str:
    inferred = infer(expr, env)
    if expected is None or expected.kind != "object" or not expected.name:
        raise Unsupported(GAP_TYPE, "object literals need a declared state shape")
    if [name for name, _ in inferred.fields] != [name for name, _ in expected.fields]:
        raise Unsupported(
            GAP_TYPE,
            f"object literal {describe(inferred)} does not match {expected.name} "
            f"{describe(expected)}",
        )
    unify(expected, inferred, expected.name)
    arguments = ", ".join(
        f"{name}: {emit(expr['object'][name], env, item)}" for name, item in expected.fields
    )
    return f"{expected.name}({arguments})"


def emit_template(expr: Expr, env: Env) -> str:
    """Return a Swift ``String`` expression following JavaScript string coercion."""
    if "lit" in expr:
        value = expr["lit"]
        if isinstance(value, bool):
            return swift_string("true" if value else "false")
        if isinstance(value, int | float):
            return swift_string(js_number(value))
        if value is None:
            return swift_string("null")
        return swift_string(str(value))
    if "cond" in expr:
        infer(expr["cond"], env)
        condition = emit(expr["cond"], env)
        then_code = emit_template(expr["then"], env)
        else_code = emit_template(expr["else"], env)
        return f"({condition} ? {then_code} : {else_code})"
    if "template" in expr:
        parts = [emit_template(part, env) for part in expr["template"]]
        return "(" + " + ".join(parts) + ")" if parts else '""'
    ty = infer(expr, env)
    code = emit(expr, env)
    if ty.kind == "string":
        return code
    if ty.kind in {"number", "boolean"}:
        return f"jsString({code})"
    raise Unsupported(GAP_TYPE, f"{describe(ty)} cannot be rendered as text")


def emit_child_text(expr: Expr, env: Env) -> str | None:
    """Return Swift code for a JSX text child, or None when React renders nothing."""
    if "lit" in expr:
        value = expr["lit"]
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int | float):
            return swift_string(js_number(value))
        return swift_string(str(value)) if value else None
    if "cond" in expr:
        condition_type = infer(expr["cond"], env)
        if condition_type.kind != "boolean":
            raise Unsupported(GAP_TYPE, "conditions must be boolean, not JavaScript truthiness")
        then_code = emit_child_text(expr["then"], env)
        else_code = emit_child_text(expr["else"], env)
        if then_code is None and else_code is None:
            return None
        condition = emit(expr["cond"], env)
        return f"({condition} ? {then_code or '""'} : {else_code or '""'})"
    if "template" in expr:
        return emit_template(expr, env)
    ty = infer(expr, env)
    if ty.kind in {"boolean", "null"}:
        return None
    if ty.kind == "string":
        return emit(expr, env)
    if ty.kind == "number":
        return f"jsString({emit(expr, env)})"
    raise Unsupported(GAP_TYPE, f"{describe(ty)} cannot be rendered as text")


def emit_run(segments: list[Expr], env: Env) -> str:
    """Concatenate the segments of a captured text run into one Swift ``String``."""
    parts = [code for code in (emit_child_text(segment, env) for segment in segments) if code]
    if not parts:
        return '""'
    return parts[0] if len(parts) == 1 else "(" + " + ".join(parts) + ")"


def emit_json(code: str, ty: Ty) -> str:
    """Return a ``JSONValue`` expression for a Swift value of type ``ty``."""
    if ty.kind == "string":
        return f".string({code})"
    if ty.kind == "number":
        return f".number({code})"
    if ty.kind == "boolean":
        return f".bool({code})"
    if ty.kind == "array":
        return f".array({code}.map {{ element in {emit_json('element', ty.element or UNKNOWN)} }})"
    if ty.kind == "object" and ty.name:
        return f"{code}.json"
    raise Unsupported(GAP_TYPE, f"{describe(ty)} has no JSON encoding")


def emit_decode(code: str, ty: Ty, *, optional: bool = True) -> str:
    """Return an optional Swift value of type ``ty`` decoded from a ``JSONValue`` expression.

    ``optional`` says whether ``code`` is a ``JSONValue?`` (a dictionary lookup) or a
    plain ``JSONValue``.
    """
    chain = "?." if optional else "."
    if ty.kind == "string":
        return f"{code}{chain}stringValue"
    if ty.kind == "number":
        return f"{code}{chain}numberValue"
    if ty.kind == "boolean":
        return f"{code}{chain}boolValue"
    if ty.kind == "array":
        inner = emit_decode("element", ty.element or UNKNOWN, optional=False)
        return f"decodeArray({code}, {{ element in {inner} }})"
    if ty.kind == "object" and ty.name:
        if optional:
            return f"{code}.flatMap {{ {ty.name}(json: $0) }}"
        return f"{ty.name}(json: {code})"
    raise Unsupported(GAP_TYPE, f"{describe(ty)} has no JSON decoding")
