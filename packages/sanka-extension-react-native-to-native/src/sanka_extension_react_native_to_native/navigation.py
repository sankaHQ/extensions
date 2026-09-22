# SPDX-License-Identifier: Apache-2.0
"""React Navigation recognition: navigators, screens, titles and typed parameters.

Only literal ``<X.Navigator>`` / ``<X.Screen>`` trees inside ``NavigationContainer``
and literal object-type parameter lists are recognized. Everything else is a gap.
"""

from __future__ import annotations

from typing import Any

from sanka_ts_capture import ParsedFile
from sanka_ts_capture import tree as t

from .expressions import Unsupported
from .screens import default_export

FACTORIES: dict[str, tuple[str, str]] = {
    "createNativeStackNavigator": ("stack", "@react-navigation/native-stack"),
    "createBottomTabNavigator": ("tabs", "@react-navigation/bottom-tabs"),
}
SCREEN_OPTIONS: dict[str, type] = {"title": str, "headerShown": bool}
SCALARS = {"StringKeyword": "string", "NumberKeyword": "number", "BooleanKeyword": "boolean"}
MAX_DEPTH = 2


def capture_navigation(
    parsed: ParsedFile, source: str, rel: str, imported: dict[str, str]
) -> dict[str, Any]:
    """Return navigators and screen component references declared in the entry module."""
    param_lists: dict[str, dict[str, dict[str, str] | None]] = {}
    navigators: dict[str, dict[str, Any]] = {}
    functions: dict[str, t.Node] = {}
    for statement in t.field_list(parsed.tree, "statements"):
        kind = t.kind(statement)
        line = t.line_of(source, statement)
        if kind == "TypeAliasDeclaration":
            name = t.identifier_name(t.field(statement, "name"))
            body = t.field(statement, "type")
            if name is not None and body is not None and t.kind(body) == "TypeLiteral":
                param_lists[name] = _param_list(body, rel, line)
        elif kind == "VariableStatement":
            declarations = t.field(statement, "declarationList")
            items = t.field_list(declarations, "declarations") if declarations else []
            for item in items:
                name = t.identifier_name(t.field(item, "name"))
                initializer = t.field(item, "initializer")
                if name is None or initializer is None:
                    continue
                if t.kind(initializer) in {"ArrowFunction", "FunctionExpression"}:
                    functions[name] = initializer
                    continue
                parts = t.call_parts(initializer, allow_type_arguments=True)
                factory = t.identifier_name(t.unparenthesize(parts[0])) if parts else None
                if factory in FACTORIES and parts is not None:
                    navigator_kind, module = FACTORIES[factory]
                    if imported.get(factory) != module:
                        raise Unsupported(
                            "SANKA_RN_NAVIGATION",
                            f"{rel}:{line}: {factory} must come from {module}",
                        )
                    if parts[1]:
                        raise Unsupported(
                            "SANKA_RN_NAVIGATION",
                            f"{rel}:{line}: navigator factories take no arguments",
                        )
                    type_arguments = t.field_list(initializer, "typeArguments")
                    params_name = (
                        t.identifier_name(t.field(type_arguments[0], "typeName"))
                        if len(type_arguments) == 1
                        else None
                    )
                    navigators[name] = {"kind": navigator_kind, "params": params_name}
        elif kind == "FunctionDeclaration":
            name = t.identifier_name(t.field(statement, "name"))
            if name is not None:
                functions[name] = statement
    root_name, root_function = default_export(parsed)
    if root_name is None or root_function is None:
        raise Unsupported(
            "SANKA_RN_NAVIGATION", f"{rel}: the entry module must default-export the app component"
        )
    for navigator in navigators.values():
        params_name = navigator["params"]
        if params_name is not None and params_name not in param_lists:
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{rel}: parameter list type {params_name!r} must be a literal object type",
            )
    context = _Context(rel, source, imported, navigators, param_lists, functions)
    root = _returned_jsx(root_function, rel)
    container = _single_element(root, rel)
    if (
        t.property_chain(t.field(_opening(container), "tagName") or {}) != ("NavigationContainer",)
        or imported.get("NavigationContainer") != "@react-navigation/native"
    ):
        raise Unsupported(
            "SANKA_RN_NAVIGATION", f"{rel}: the app must render one NavigationContainer"
        )
    if t.field_list(t.field(_opening(container), "attributes") or {}, "properties"):
        raise Unsupported(
            "SANKA_RN_NAVIGATION", f"{rel}: NavigationContainer props require additional capture"
        )
    inner = [
        child
        for child in t.field_list(container, "children")
        if t.kind(child) != "JsxText" or (t.text(child) or "").strip()
    ]
    if len(inner) != 1:
        raise Unsupported(
            "SANKA_RN_NAVIGATION", f"{rel}: NavigationContainer must wrap exactly one navigator"
        )
    result: list[dict[str, Any]] = []
    context.navigator(inner[0], "root", result, depth=0)
    return {"navigators": result, "param_lists": param_lists}


def _param_list(body: t.Node, rel: str, line: int) -> dict[str, dict[str, str] | None]:
    screens: dict[str, dict[str, str] | None] = {}
    for member in t.field_list(body, "members"):
        name = t.identifier_name(t.field(member, "name")) or t.string_value(t.field(member, "name"))
        value = t.field(member, "type")
        if t.kind(member) != "PropertySignature" or name is None or value is None:
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{rel}:{line}: parameter lists must map screen names to object types",
            )
        if t.kind(value) == "UndefinedKeyword":
            screens[name] = None
            continue
        if t.kind(value) != "TypeLiteral":
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{rel}:{line}: screen {name!r} parameters must be an object type or undefined",
            )
        params: dict[str, str] = {}
        for entry in t.field_list(value, "members"):
            param = t.identifier_name(t.field(entry, "name"))
            scalar = SCALARS.get(t.kind(t.field(entry, "type") or {}))
            if t.kind(entry) != "PropertySignature" or param is None or scalar is None:
                raise Unsupported(
                    "SANKA_RN_NAVIGATION",
                    f"{rel}:{line}: screen {name!r} parameters must be string, number or boolean",
                )
            params[param] = scalar + ("?" if t.field(entry, "questionToken") is not None else "")
        screens[name] = params
    return screens


def _returned_jsx(function: t.Node, rel: str) -> t.Node:
    body = t.field(function, "body")
    if body is None:
        raise Unsupported("SANKA_RN_NAVIGATION", f"{rel}: malformed component")
    if t.kind(body) != "Block":
        return t.unparenthesize(body)
    statements = t.field_list(body, "statements")
    returned = (
        t.field(statements[0], "expression")
        if len(statements) == 1 and t.kind(statements[0]) == "ReturnStatement"
        else None
    )
    if returned is None:
        raise Unsupported(
            "SANKA_RN_NAVIGATION", f"{rel}: navigator components must only return JSX"
        )
    return t.unparenthesize(returned)


def _single_element(node: t.Node, rel: str) -> t.Node:
    node = t.unparenthesize(node)
    if t.kind(node) not in {"JsxElement", "JsxSelfClosingElement"}:
        raise Unsupported("SANKA_RN_NAVIGATION", f"{rel}: expected a JSX element")
    return node


def _opening(node: t.Node) -> t.Node:
    return t.field(node, "openingElement") or node


class _Context:
    def __init__(
        self,
        rel: str,
        source: str,
        imported: dict[str, str],
        navigators: dict[str, dict[str, Any]],
        param_lists: dict[str, dict[str, dict[str, str] | None]],
        functions: dict[str, t.Node],
    ) -> None:
        self.rel = rel
        self.source = source
        self.imported = imported
        self.navigators = navigators
        self.param_lists = param_lists
        self.functions = functions

    def navigator(
        self, node: t.Node, name: str, result: list[dict[str, Any]], *, depth: int
    ) -> None:
        element = _single_element(node, self.rel)
        opening = _opening(element)
        tag = t.property_chain(t.field(opening, "tagName") or {})
        if tag is None or len(tag) != 2 or tag[1] != "Navigator" or tag[0] not in self.navigators:
            raise Unsupported(
                "SANKA_RN_NAVIGATION", f"{self.rel}: expected <Navigator> from a navigator factory"
            )
        variable = tag[0]
        declared = self.navigators[variable]
        initial: str | None = None
        for attribute in t.field_list(t.field(opening, "attributes") or {}, "properties"):
            key = (
                t.identifier_name(t.field(attribute, "name"))
                if t.kind(attribute) == "JsxAttribute"
                else None
            )
            value = t.field(attribute, "initializer")
            if key == "initialRouteName" and value is not None:
                initial = t.string_value(value)
                if initial is not None:
                    continue
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{self.rel}: navigator prop {key!r} requires additional capture",
            )
        params_list = (
            self.param_lists.get(declared["params"] or "", {}) if declared["params"] else {}
        )
        screens: list[dict[str, Any]] = []
        for child in t.field_list(element, "children"):
            if t.kind(child) == "JsxText":
                if (t.text(child) or "").strip():
                    raise Unsupported("SANKA_RN_NAVIGATION", f"{self.rel}: text inside a navigator")
                continue
            screens.append(self.screen(child, variable, params_list, result, depth))
        if not screens:
            raise Unsupported(
                "SANKA_RN_NAVIGATION", f"{self.rel}: navigator {variable} declares no screens"
            )
        names = [screen["name"] for screen in screens]
        if len(set(names)) != len(names):
            raise Unsupported(
                "SANKA_RN_NAVIGATION", f"{self.rel}: duplicate screen names in {variable}"
            )
        if initial is not None and initial not in names:
            raise Unsupported(
                "SANKA_RN_NAVIGATION", f"{self.rel}: initialRouteName {initial!r} is not a screen"
            )
        result.insert(
            0,
            {
                "name": name,
                "kind": declared["kind"],
                "initial": initial or names[0],
                "screens": screens,
            },
        )

    def screen(
        self,
        node: t.Node,
        variable: str,
        params_list: dict[str, dict[str, str] | None],
        result: list[dict[str, Any]],
        depth: int,
    ) -> dict[str, Any]:
        element = _single_element(node, self.rel)
        opening = _opening(element)
        if t.property_chain(t.field(opening, "tagName") or {}) != (variable, "Screen"):
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{self.rel}: navigators may only contain <{variable}.Screen>",
            )
        if t.kind(element) == "JsxElement" and any(
            t.kind(child) != "JsxText" or (t.text(child) or "").strip()
            for child in t.field_list(element, "children")
        ):
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{self.rel}: screen children (render callbacks) require additional capture",
            )
        name: str | None = None
        component: str | None = None
        options: dict[str, Any] = {}
        for attribute in t.field_list(t.field(opening, "attributes") or {}, "properties"):
            key = (
                t.identifier_name(t.field(attribute, "name"))
                if t.kind(attribute) == "JsxAttribute"
                else None
            )
            value = t.field(attribute, "initializer")
            expression = (
                t.field(value, "expression")
                if value is not None and t.kind(value) == "JsxExpression"
                else value
            )
            if key == "name" and value is not None:
                name = t.string_value(value)
            elif key == "component" and expression is not None:
                component = t.identifier_name(t.unparenthesize(expression))
            elif key == "options" and expression is not None:
                options = _options(expression, self.rel)
            else:
                raise Unsupported(
                    "SANKA_RN_NAVIGATION",
                    f"{self.rel}: screen prop {key!r} requires additional capture",
                )
            if key in {"name", "component"} and (
                name is None if key == "name" else component is None
            ):
                raise Unsupported(
                    "SANKA_RN_NAVIGATION", f"{self.rel}: screen {key} must be literal"
                )
        if name is None or component is None:
            raise Unsupported(
                "SANKA_RN_NAVIGATION", f"{self.rel}: screens need a literal name and a component"
            )
        if params_list and name not in params_list:
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{self.rel}: screen {name!r} is missing from the parameter list type",
            )
        params = params_list.get(name) if params_list else {}
        screen: dict[str, Any] = {
            "name": name,
            "component": component,
            "title": options.get("title"),
            "header": options.get("headerShown", True),
            "params": params or {},
        }
        module = self.imported.get(component)
        if module is not None and module.startswith("."):
            screen["import"] = module
        elif component in self.functions:
            if depth + 1 > MAX_DEPTH:
                raise Unsupported(
                    "SANKA_RN_NAVIGATION",
                    f"{self.rel}: navigators nested deeper than {MAX_DEPTH} levels",
                )
            nested = _returned_jsx(self.functions[component], self.rel)
            self.navigator(nested, component, result, depth=depth + 1)
            screen["navigator"] = component
        else:
            raise Unsupported(
                "SANKA_RN_NAVIGATION",
                f"{self.rel}: screen component {component!r} must be imported from a "
                "project module",
            )
        return screen


def _options(node: t.Node, rel: str) -> dict[str, Any]:
    node = t.unparenthesize(node)
    encoded = node.get("json")
    if t.kind(node) != "ObjectLiteralExpression" or not isinstance(encoded, str):
        raise Unsupported("SANKA_RN_NAVIGATION", f"{rel}: screen options must be a literal object")
    import json

    options = json.loads(encoded)
    for key, value in options.items():
        expected = SCREEN_OPTIONS.get(key)
        if expected is None or type(value) is not expected:
            raise Unsupported(
                "SANKA_RN_NAVIGATION", f"{rel}: screen option {key!r} requires additional capture"
            )
    return dict(options)
