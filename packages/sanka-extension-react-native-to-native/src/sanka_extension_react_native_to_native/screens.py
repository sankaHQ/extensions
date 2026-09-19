# SPDX-License-Identifier: Apache-2.0
"""Screen component capture: hooks, state, JSX element tree, events and styles."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from sanka_ts_capture import ParsedFile
from sanka_ts_capture import tree as t

from .expressions import Expr, Scope, Unsupported, href, is_literal, lower, lower_handler
from .styles import capture_styles

COMPONENTS: dict[str, frozenset[str]] = {
    "View": frozenset({"style"}),
    "SafeAreaView": frozenset({"style"}),
    "ScrollView": frozenset({"style", "contentContainerStyle"}),
    "Text": frozenset({"style", "numberOfLines"}),
    "Image": frozenset({"style", "source", "resizeMode"}),
    "Pressable": frozenset({"style", "onPress", "disabled"}),
    "TouchableOpacity": frozenset({"style", "onPress", "disabled"}),
    "TextInput": frozenset(
        {
            "style",
            "value",
            "onChangeText",
            "placeholder",
            "secureTextEntry",
            "keyboardType",
            "autoCapitalize",
        }
    ),
    "Switch": frozenset({"style", "value", "onValueChange"}),
    "FlatList": frozenset({"style", "contentContainerStyle", "data", "renderItem", "keyExtractor"}),
    "ActivityIndicator": frozenset({"style", "size", "color"}),
}
COMMON_PROPS = frozenset({"testID", "accessibilityLabel", "key"})
EVENT_PROPS = frozenset({"onPress", "onChangeText", "onValueChange"})
STYLE_PROPS = frozenset({"style", "contentContainerStyle"})
LITERAL_PROPS = frozenset(
    {
        "placeholder",
        "secureTextEntry",
        "keyboardType",
        "autoCapitalize",
        "numberOfLines",
        "resizeMode",
        "size",
        "color",
        "testID",
        "accessibilityLabel",
    }
)
REACT_NATIVE_IMPORTS = frozenset(COMPONENTS) | {"StyleSheet"}
REACT_IMPORTS = frozenset({"useState"})
NAVIGATION_IMPORTS = frozenset({"NavigationContainer", "useNavigation"})
EXPO_IMPORTS = frozenset({"Link", "useRouter", "useLocalSearchParams", "Stack", "Tabs"})
ALLOWED_MODULES: dict[str, frozenset[str]] = {
    "react": REACT_IMPORTS,
    "react-native": REACT_NATIVE_IMPORTS,
    "@react-navigation/native": NAVIGATION_IMPORTS,
    "@react-navigation/native-stack": frozenset({"createNativeStackNavigator"}),
    "@react-navigation/bottom-tabs": frozenset({"createBottomTabNavigator"}),
    "expo-router": EXPO_IMPORTS,
    "expo-status-bar": frozenset({"StatusBar"}),
}
ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def imports(parsed: ParsedFile, rel: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Map imported local names to modules; return blocking import gaps separately."""
    names: dict[str, str] = {}
    gaps: list[dict[str, str]] = []
    for statement in t.field_list(parsed.tree, "statements"):
        if t.kind(statement) != "ImportDeclaration":
            continue
        module = t.string_value(t.field(statement, "moduleSpecifier")) or ""
        clause = t.field(statement, "importClause")
        if clause is None or clause.get("typeOnly"):
            continue
        default_name = t.identifier_name(t.field(clause, "name"))
        bindings = t.field(clause, "namedBindings")
        named: list[str] = []
        if bindings is not None:
            if t.kind(bindings) != "NamedImports":
                gaps.append(
                    _gap("SANKA_RN_UNSUPPORTED_IMPORT", f"{rel}: namespace import of {module!r}")
                )
                continue
            for element in t.field_list(bindings, "elements"):
                if element.get("typeOnly"):
                    continue
                local = t.identifier_name(t.field(element, "name"))
                imported = t.identifier_name(t.field(element, "propertyName")) or local
                if local is None or imported is None:
                    continue
                if imported != local:
                    gaps.append(
                        _gap("SANKA_RN_UNSUPPORTED_IMPORT", f"{rel}: renamed import {imported!r}")
                    )
                    continue
                named.append(local)
        if module.startswith("."):
            for name in [default_name, *named]:
                if name is not None:
                    names[name] = module
            continue
        allowed = ALLOWED_MODULES.get(module)
        if allowed is None:
            code = "SANKA_RN_THIRD_PARTY_COMPONENT"
            if module.startswith(("expo-", "react-native-", "@react-native")):
                code = "SANKA_RN_NATIVE_MODULE"
            gaps.append(_gap(code, f"{rel}: import of {module!r} is outside the envelope"))
            continue
        if default_name is not None and module != "react":
            gaps.append(
                _gap("SANKA_RN_UNSUPPORTED_IMPORT", f"{rel}: default import from {module!r}")
            )
        for name in named:
            if name not in allowed:
                gaps.append(_gap("SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: '{name}' from {module!r}"))
                continue
            names[name] = module
        if default_name is not None and module == "react":
            names[default_name] = module
    return names, gaps


def _gap(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def default_export(parsed: ParsedFile) -> tuple[str | None, t.Node | None]:
    """Return the default-exported component name and its function node."""
    statements = t.field_list(parsed.tree, "statements")
    functions: dict[str, t.Node] = {}
    exported: str | None = None
    for statement in statements:
        kind = t.kind(statement)
        modifiers = t.modifier_kinds(statement)
        if kind == "FunctionDeclaration":
            name = t.identifier_name(t.field(statement, "name"))
            if name is None:
                continue
            functions[name] = statement
            if {"ExportKeyword", "DefaultKeyword"} <= modifiers:
                exported = name
        elif kind == "VariableStatement":
            declarations = t.field(statement, "declarationList")
            for declaration in t.field_list(declarations, "declarations") if declarations else []:
                name = t.identifier_name(t.field(declaration, "name"))
                initializer = t.field(declaration, "initializer")
                if (
                    name
                    and initializer is not None
                    and t.kind(initializer)
                    in {
                        "ArrowFunction",
                        "FunctionExpression",
                    }
                ):
                    functions[name] = initializer
        elif kind == "ExportAssignment" and not statement.get("exportEquals"):
            expression = t.field(statement, "expression")
            name = t.identifier_name(t.unparenthesize(expression)) if expression else None
            if name is not None:
                exported = name
    if exported is None:
        return None, None
    return exported, functions.get(exported)


def capture_screen(
    parsed: ParsedFile,
    source: str,
    rel: str,
    *,
    navigation_kind: str,
    declared_params: dict[str, str] | None,
    imported: dict[str, str],
    project_root_files: frozenset[str],
) -> dict[str, Any]:
    """Capture one screen module; raise Unsupported for anything outside the envelope."""
    name, function = default_export(parsed)
    if name is None or function is None:
        raise Unsupported("SANKA_RN_SCREEN", f"{rel}: no default-exported function component")
    for statement in t.field_list(parsed.tree, "statements"):
        kind = t.kind(statement)
        if kind in {
            "ImportDeclaration",
            "InterfaceDeclaration",
            "TypeAliasDeclaration",
            "ExportAssignment",
            "EmptyStatement",
        }:
            continue
        if kind == "FunctionDeclaration" and statement is function:
            continue
        if kind == "VariableStatement":
            declarations = t.field(statement, "declarationList")
            items = t.field_list(declarations, "declarations") if declarations else []
            if len(items) == 1:
                initializer = t.field(items[0], "initializer")
                if initializer is function:
                    continue
                if (
                    initializer is not None
                    and t.identifier_name(t.field(items[0], "name")) == "styles"
                ):
                    continue
        raise Unsupported(
            "SANKA_RN_LOGIC", f"{rel}:{t.line_of(source, statement)}: unsupported {kind}"
        )
    styles = _styles(parsed, rel)
    route_object, navigation_name, hook_params = _parameters(function, rel)
    body = t.field(function, "body")
    if body is None or t.kind(body) != "Block":
        raise Unsupported("SANKA_RN_SCREEN", f"{rel}: component needs a block body")
    state: dict[str, str] = {}
    setters: dict[str, str] = {}
    initial: dict[str, Any] = {}
    params: dict[str, str] = dict(declared_params or {})
    returned: t.Node | None = None
    scope_navigation = navigation_name
    for statement in t.field_list(body, "statements"):
        kind = t.kind(statement)
        if kind == "ReturnStatement":
            returned = t.field(statement, "expression")
            if returned is None:
                raise Unsupported("SANKA_RN_SCREEN", f"{rel}: the component must return JSX")
            break
        if kind != "VariableStatement":
            raise Unsupported("SANKA_RN_LOGIC", f"{rel}: only hooks and a JSX return are qualified")
        declarations = t.field(statement, "declarationList")
        items = t.field_list(declarations, "declarations") if declarations else []
        if declarations is None or declarations.get("decl") != "const" or len(items) != 1:
            raise Unsupported(
                "SANKA_RN_LOGIC", f"{rel}: only const hook declarations are qualified"
            )
        declaration = items[0]
        target = t.field(declaration, "name")
        initializer = t.field(declaration, "initializer")
        parts = t.call_parts(initializer, allow_type_arguments=True) if initializer else None
        if target is None or parts is None:
            raise Unsupported("SANKA_RN_LOGIC", f"{rel}: only hook declarations are qualified")
        hook = t.identifier_name(t.unparenthesize(parts[0]))
        if hook == "useState" and imported.get("useState") == "react":
            value_name, setter = _state_binding(target, rel)
            if len(parts[1]) != 1:
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: useState needs one initial value"
                )
            empty = Scope({}, {}, frozenset(), None, None, navigation_kind, None)
            init = lower(parts[1][0], empty)
            if not is_literal(init):
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: useState initial values must be literals"
                )
            state[value_name] = setter
            setters[setter] = value_name
            initial[value_name] = init
        elif hook == "useRouter" and imported.get("useRouter") == "expo-router":
            scope_navigation = _identifier(target, rel, "useRouter")
        elif (
            hook == "useNavigation" and imported.get("useNavigation") == "@react-navigation/native"
        ):
            scope_navigation = _identifier(target, rel, "useNavigation")
        elif (
            hook == "useLocalSearchParams" and imported.get("useLocalSearchParams") == "expo-router"
        ):
            params.update(_search_params(target, parts[0], initializer, rel))
        else:
            raise Unsupported("SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: hook {hook!r} is not qualified")
    if returned is None:
        raise Unsupported("SANKA_RN_SCREEN", f"{rel}: the component must return JSX")
    params.update(hook_params)
    scope = Scope(
        state=state,
        setters=setters,
        params=frozenset(params),
        route_object=route_object,
        navigation=scope_navigation,
        navigation_kind=navigation_kind,
        styles="styles" if styles else None,
    )
    context = _Context(rel, imported, styles, project_root_files)
    tree = context.element(returned, scope)
    return {
        "name": name,
        "module": rel,
        "params": params,
        "state": [{"name": key, "setter": state[key], "initial": initial[key]} for key in state],
        "styles": styles,
        "tree": tree,
    }


def _styles(parsed: ParsedFile, rel: str) -> dict[str, dict[str, Any]]:
    for statement in t.field_list(parsed.tree, "statements"):
        if t.kind(statement) != "VariableStatement":
            continue
        declarations = t.field(statement, "declarationList")
        items = t.field_list(declarations, "declarations") if declarations else []
        if len(items) != 1 or t.identifier_name(t.field(items[0], "name")) != "styles":
            continue
        initializer = t.field(items[0], "initializer")
        if initializer is None:
            raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"{rel}: styles must be initialized")
        return capture_styles(initializer)
    return {}


def _identifier(target: t.Node, rel: str, hook: str) -> str:
    name = t.identifier_name(target)
    if name is None:
        raise Unsupported(
            "SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: {hook}() must bind a plain identifier"
        )
    return name


def _state_binding(target: t.Node, rel: str) -> tuple[str, str]:
    if t.kind(target) != "ArrayBindingPattern":
        raise Unsupported(
            "SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: useState must destructure [value, setter]"
        )
    elements = t.field_list(target, "elements")
    names = [t.identifier_name(t.field(element, "name")) for element in elements]
    if (
        len(names) != 2
        or None in names
        or any(
            t.field(element, "initializer") is not None
            or t.field(element, "dotDotDotToken") is not None
            for element in elements
        )
    ):
        raise Unsupported(
            "SANKA_RN_UNSUPPORTED_HOOK", f"{rel}: useState must destructure [value, setter]"
        )
    value, setter = str(names[0]), str(names[1])
    if setter != "set" + value[:1].upper() + value[1:]:
        raise Unsupported(
            "SANKA_RN_UNSUPPORTED_HOOK",
            f"{rel}: useState setter must be set{value[:1].upper()}{value[1:]}",
        )
    return value, setter


def _search_params(target: t.Node, callee: t.Node, call: t.Node | None, rel: str) -> dict[str, str]:
    if t.kind(target) != "ObjectBindingPattern":
        raise Unsupported(
            "SANKA_RN_EXPO_LAYOUT", f"{rel}: useLocalSearchParams must destructure its params"
        )
    names: list[str] = []
    for element in t.field_list(target, "elements"):
        name = t.identifier_name(t.field(element, "name"))
        if (
            name is None
            or t.field(element, "propertyName") is not None
            or t.field(element, "initializer") is not None
        ):
            raise Unsupported(
                "SANKA_RN_EXPO_LAYOUT", f"{rel}: useLocalSearchParams destructuring must be plain"
            )
        names.append(name)
    typed = (
        _type_literal_members(t.field_list(call, "typeArguments")[0])
        if call and t.field_list(call, "typeArguments")
        else {}
    )
    for name in names:
        if typed and typed.get(name) != "string":
            raise Unsupported(
                "SANKA_RN_EXPO_LAYOUT", f"{rel}: route parameter '{name}' must be typed as string"
            )
    return dict.fromkeys(names, "string")


def _type_literal_members(node: t.Node) -> dict[str, str]:
    if t.kind(node) != "TypeLiteral":
        raise Unsupported(
            "SANKA_RN_EXPO_LAYOUT", "route parameter types must be an object type literal"
        )
    members: dict[str, str] = {}
    for member in t.field_list(node, "members"):
        name = t.identifier_name(t.field(member, "name"))
        kind = t.kind(t.field(member, "type") or {})
        scalar = {
            "StringKeyword": "string",
            "NumberKeyword": "number",
            "BooleanKeyword": "boolean",
        }.get(kind)
        if t.kind(member) != "PropertySignature" or name is None or scalar is None:
            raise Unsupported(
                "SANKA_RN_EXPO_LAYOUT", "route parameters must be string, number or boolean"
            )
        members[name] = scalar
    return members


def _parameters(function: t.Node, rel: str) -> tuple[str | None, str | None, dict[str, str]]:
    parameters = t.field_list(function, "parameters")
    if not parameters:
        return None, None, {}
    if len(parameters) != 1:
        raise Unsupported("SANKA_RN_SCREEN", f"{rel}: screen components take a single props object")
    pattern = t.field(parameters[0], "name")
    if pattern is None or t.kind(pattern) != "ObjectBindingPattern":
        raise Unsupported(
            "SANKA_RN_SCREEN", f"{rel}: destructure {{ navigation, route }} from props"
        )
    route_object: str | None = None
    navigation: str | None = None
    for element in t.field_list(pattern, "elements"):
        name = t.identifier_name(t.field(element, "name"))
        if (
            name is None
            or t.field(element, "propertyName") is not None
            or t.field(element, "initializer") is not None
        ):
            raise Unsupported("SANKA_RN_SCREEN", f"{rel}: props destructuring must be plain")
        if name == "navigation":
            navigation = name
        elif name == "route":
            route_object = name
        else:
            raise Unsupported(
                "SANKA_RN_SCREEN", f"{rel}: prop '{name}' requires additional capture"
            )
    return route_object, navigation, {}


class _Context:
    def __init__(
        self,
        rel: str,
        imported: dict[str, str],
        styles: dict[str, dict[str, Any]],
        project_files: frozenset[str],
    ) -> None:
        self.rel = rel
        self.imported = imported
        self.styles = styles
        self.project_files = project_files

    def element(self, node: t.Node, scope: Scope) -> dict[str, Any]:
        node = t.unparenthesize(node)
        kind = t.kind(node)
        if kind == "JsxFragment":
            return {"kind": "Fragment", "props": {}, "children": self.children(node, scope)}
        if kind not in {"JsxElement", "JsxSelfClosingElement"}:
            raise Unsupported("SANKA_RN_SCREEN", f"{self.rel}: expected a JSX element")
        opening = t.field(node, "openingElement") if kind == "JsxElement" else node
        if opening is None:
            raise Unsupported("SANKA_RN_SCREEN", f"{self.rel}: malformed JSX element")
        tag = t.property_chain(t.field(opening, "tagName") or {})
        if tag is None or len(tag) != 1:
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_COMPONENT", f"{self.rel}: member component tags"
            )
        name = tag[0]
        module = self.imported.get(name)
        if name == "StatusBar" and module == "expo-status-bar":
            return {"kind": "StatusBar", "props": {}, "children": []}
        if name == "Link" and module == "expo-router":
            return self.link(node, opening, scope)
        if name not in COMPONENTS:
            code = (
                "SANKA_RN_CUSTOM_COMPONENT"
                if module is None or module.startswith(".")
                else "SANKA_RN_THIRD_PARTY_COMPONENT"
            )
            raise Unsupported(code, f"{self.rel}: <{name}> is outside the slice-1 component set")
        if module != "react-native":
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_COMPONENT",
                f"{self.rel}: <{name}> must come from react-native",
            )
        props, events, item = self.attributes(name, opening, scope)
        element: dict[str, Any] = {"kind": name, "props": props}
        if events:
            element["events"] = events
        if name == "FlatList":
            if "data" not in props or item is None:
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: FlatList needs data and renderItem"
                )
            element["item"] = item
        children = self.children(node, scope) if kind == "JsxElement" else []
        if name in {"Image", "TextInput", "Switch", "ActivityIndicator", "FlatList"} and children:
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_COMPONENT", f"{self.rel}: <{name}> cannot have children"
            )
        element["children"] = children
        return element

    def link(self, node: t.Node, opening: t.Node, scope: Scope) -> dict[str, Any]:
        target: tuple[str, dict[str, Expr]] | None = None
        props: dict[str, Any] = {}
        for attribute in t.field_list(t.field(opening, "attributes") or {}, "properties"):
            key, value = self.attribute(attribute)
            if key == "href":
                target = href(value, scope) if value is not None else None
            elif key in STYLE_PROPS:
                props[key] = self.style(value)
            elif key not in COMMON_PROPS:
                raise Unsupported("SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: <Link {key}>")
        if target is None:
            raise Unsupported("SANKA_RN_EXPO_LAYOUT", f"{self.rel}: <Link> needs a literal href")
        children = self.children(node, scope) if t.kind(node) == "JsxElement" else []
        return {
            "kind": "Link",
            "props": props,
            "events": {"onPress": [{"push": target[0], "params": target[1]}]},
            "children": children,
        }

    def attribute(self, attribute: t.Node) -> tuple[str, t.Node | None]:
        if t.kind(attribute) != "JsxAttribute":
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: spread props require additional capture"
            )
        key = t.identifier_name(t.field(attribute, "name"))
        if key is None:
            raise Unsupported("SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: namespaced props")
        initializer = t.field(attribute, "initializer")
        if initializer is None:
            return key, None
        if t.kind(initializer) == "JsxExpression":
            expression = t.field(initializer, "expression")
            if expression is None:
                raise Unsupported("SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: empty JSX expression")
            return key, expression
        return key, initializer

    def attributes(
        self, component: str, opening: t.Node, scope: Scope
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
        allowed = COMPONENTS[component] | COMMON_PROPS
        props: dict[str, Any] = {}
        events: dict[str, list[dict[str, Any]]] = {}
        item: dict[str, Any] | None = None
        for attribute in t.field_list(t.field(opening, "attributes") or {}, "properties"):
            key, value = self.attribute(attribute)
            if key not in allowed:
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: <{component} {key}> is not qualified"
                )
            if key == "key":
                continue
            if value is None:
                if key in {"secureTextEntry", "disabled"}:
                    props[key] = {"lit": True}
                    continue
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: <{component} {key}> needs a value"
                )
            if key in EVENT_PROPS:
                events[key] = lower_handler(value, scope)
            elif key in STYLE_PROPS:
                props[key] = self.style(value)
            elif key == "renderItem":
                item = self.render_item(value, scope)
            elif key == "keyExtractor":
                props[key] = self.key_extractor(value, scope)
            elif key == "source":
                props[key] = self.source(value, scope)
            elif key in LITERAL_PROPS:
                lowered = lower(value, scope)
                if "lit" not in lowered:
                    raise Unsupported(
                        "SANKA_RN_UNSUPPORTED_PROP",
                        f"{self.rel}: <{component} {key}> must be a literal",
                    )
                props[key] = lowered
            else:
                props[key] = lower(value, scope)
        return props, events, item

    def style(self, value: t.Node | None) -> list[str]:
        if value is None:
            raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"{self.rel}: style needs a value")
        value = t.unparenthesize(value)
        nodes = (
            t.field_list(value, "elements")
            if t.kind(value) == "ArrayLiteralExpression"
            else [value]
        )
        names: list[str] = []
        for node in nodes:
            chain = t.property_chain(node)
            if (
                chain is None
                or len(chain) != 2
                or chain[0] != "styles"
                or chain[1] not in self.styles
            ):
                raise Unsupported(
                    "SANKA_RN_DYNAMIC_STYLE",
                    f"{self.rel}: styles must reference StyleSheet entries",
                )
            names.append(chain[1])
        return names

    def render_item(self, value: t.Node, scope: Scope) -> dict[str, Any]:
        value = t.unparenthesize(value)
        if t.kind(value) != "ArrowFunction" or t.modifier_kinds(value):
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_PROP",
                f"{self.rel}: renderItem must be an inline arrow function",
            )
        parameters = t.field_list(value, "parameters")
        pattern = t.field(parameters[0], "name") if len(parameters) == 1 else None
        if pattern is None or t.kind(pattern) != "ObjectBindingPattern":
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: renderItem must destructure {{ item }}"
            )
        elements = t.field_list(pattern, "elements")
        item_name: str | None = None
        for element in elements:
            local = t.identifier_name(t.field(element, "name"))
            exported = t.identifier_name(t.field(element, "propertyName")) or local
            if exported != "item" or local is None or t.field(element, "initializer") is not None:
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: renderItem may only destructure item"
                )
            item_name = local
        if item_name is None:
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: renderItem must destructure {{ item }}"
            )
        body = t.field(value, "body")
        if body is None:
            raise Unsupported("SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: renderItem needs a body")
        if t.kind(body) == "Block":
            statements = t.field_list(body, "statements")
            body = (
                t.field(statements[0], "expression")
                if len(statements) == 1 and t.kind(statements[0]) == "ReturnStatement"
                else None
            )
            if body is None:
                raise Unsupported(
                    "SANKA_RN_UNSUPPORTED_PROP", f"{self.rel}: renderItem must return JSX"
                )
        return {"item": item_name, "element": self.element(body, scope.with_item(item_name))}

    def key_extractor(self, value: t.Node, scope: Scope) -> Expr:
        value = t.unparenthesize(value)
        parameters = t.field_list(value, "parameters")
        name = (
            t.identifier_name(t.field(parameters[0], "name"))
            if t.kind(value) == "ArrowFunction" and parameters
            else None
        )
        body = t.field(value, "body")
        if name is None or body is None or t.kind(body) == "Block":
            raise Unsupported(
                "SANKA_RN_UNSUPPORTED_PROP",
                f"{self.rel}: keyExtractor must be (item) => expression",
            )
        return lower(body, scope.with_item(name))

    def source(self, value: t.Node, scope: Scope) -> Expr:
        value = t.unparenthesize(value)
        parts = t.call_parts(value)
        if (
            parts is not None
            and t.identifier_name(t.unparenthesize(parts[0])) == "require"
            and len(parts[1]) == 1
        ):
            asset = t.string_value(t.unparenthesize(parts[1][0]))
            if asset is None or (not asset.startswith("./") and not asset.startswith("../")):
                raise Unsupported(
                    "SANKA_RN_ASSET", f"{self.rel}: image sources must require a relative asset"
                )
            resolved = self.resolve(asset)
            if (
                PurePosixPath(resolved).suffix.lower() not in ASSET_SUFFIXES
                or resolved not in self.project_files
            ):
                raise Unsupported(
                    "SANKA_RN_ASSET", f"{self.rel}: asset {asset!r} is missing or not an image"
                )
            return {"asset": resolved}
        lowered = lower(value, scope)
        uri = lowered.get("object", {}).get("uri") if "object" in lowered else None
        if uri is None or set(lowered["object"]) != {"uri"}:
            raise Unsupported(
                "SANKA_RN_ASSET", f"{self.rel}: image sources must be require(...) or {{ uri }}"
            )
        return {"uri": uri}

    def resolve(self, relative: str) -> str:
        base = PurePosixPath(self.rel).parent
        parts: list[str] = list(base.parts)
        for segment in relative.split("/"):
            if segment in {"", "."}:
                continue
            if segment == "..":
                if not parts:
                    raise Unsupported(
                        "SANKA_RN_ASSET", f"{self.rel}: asset path escapes the project"
                    )
                parts.pop()
            else:
                parts.append(segment)
        return "/".join(parts)

    def children(self, node: t.Node, scope: Scope) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        pending: list[Expr] = []

        def flush() -> None:
            if pending:
                result.append({"text": list(pending)})
                pending.clear()

        for child in t.field_list(node, "children"):
            kind = t.kind(child)
            if kind == "JsxText":
                text = _jsx_text(t.text(child) or "")
                if text:
                    pending.append({"lit": text})
                continue
            if kind in {"JsxElement", "JsxSelfClosingElement", "JsxFragment"}:
                flush()
                result.append(self.element(child, scope))
                continue
            if kind != "JsxExpression":
                raise Unsupported("SANKA_RN_SCREEN", f"{self.rel}: unsupported JSX child {kind}")
            expression = t.field(child, "expression")
            if expression is None:
                continue
            lowered = self.child_expression(expression, scope)
            if lowered is None:
                continue
            if "text" in lowered:
                pending.extend(lowered["text"])
            else:
                flush()
                result.append(lowered)
        flush()
        return result

    def child_expression(self, node: t.Node, scope: Scope) -> dict[str, Any] | None:
        node = t.unparenthesize(node)
        kind = t.kind(node)
        if kind in {"JsxElement", "JsxSelfClosingElement", "JsxFragment"}:
            return self.element(node, scope)
        if kind == "NullKeyword":
            return None
        if kind == "ConditionalExpression":
            condition = t.field(node, "condition")
            when_true = t.field(node, "whenTrue")
            when_false = t.field(node, "whenFalse")
            if condition is None or when_true is None or when_false is None:
                raise Unsupported("SANKA_RN_SCREEN", f"{self.rel}: malformed conditional")
            branches = [
                self.child_expression(when_true, scope),
                self.child_expression(when_false, scope),
            ]
            if all(branch is None or "text" in branch for branch in branches):
                return {"text": [lower(node, scope)]}
            return {"when": lower(condition, scope), "then": branches[0], "else": branches[1]}
        if (
            kind == "BinaryExpression"
            and t.kind(t.field(node, "operatorToken") or {}) == "AmpersandAmpersandToken"
        ):
            left = t.field(node, "left")
            right = t.field(node, "right")
            if left is None or right is None:
                raise Unsupported("SANKA_RN_SCREEN", f"{self.rel}: malformed && expression")
            branch = self.child_expression(right, scope)
            if branch is None or "text" in branch:
                raise Unsupported("SANKA_RN_SCREEN", f"{self.rel}: && must guard a JSX element")
            return {"when": lower(left, scope), "then": branch, "else": None}
        if kind == "CallExpression":
            return self.each(node, scope)
        return {"text": [lower(node, scope)]}

    def each(self, node: t.Node, scope: Scope) -> dict[str, Any]:
        parts = t.call_parts(node)
        callee = t.unparenthesize(parts[0]) if parts else None
        if (
            parts is None
            or callee is None
            or t.kind(callee) != "PropertyAccessExpression"
            or t.identifier_name(t.field(callee, "name")) != "map"
            or len(parts[1]) != 1
        ):
            raise Unsupported(
                "SANKA_RN_SCREEN", f"{self.rel}: only array.map over a state value renders lists"
            )
        over = lower(t.field(callee, "expression") or {}, scope)
        callback = t.unparenthesize(parts[1][0])
        parameters = t.field_list(callback, "parameters")
        name = (
            t.identifier_name(t.field(parameters[0], "name"))
            if t.kind(callback) == "ArrowFunction" and len(parameters) == 1
            else None
        )
        body = t.field(callback, "body")
        if name is None or body is None or t.kind(body) == "Block":
            raise Unsupported(
                "SANKA_RN_SCREEN", f"{self.rel}: map callbacks must be (item) => <element>"
            )
        return {"each": over, "item": name, "element": self.element(body, scope.with_item(name))}


def _jsx_text(raw: str) -> str:
    if "\n" not in raw:
        return raw
    lines = [line.strip() for line in raw.split("\n")]
    return " ".join(line for line in lines if line)
