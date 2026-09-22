# SPDX-License-Identifier: Apache-2.0
"""Deterministic SwiftUI emitter for the captured screen IR.

Every native screen becomes a state value type, an action enum, a reducer, a SwiftUI
body and a ``tree(state:params:)`` function that returns the normalized screen tree.
The body and the tree are emitted from the same IR by the same walk, so what the
replay compares is what the view renders. Screens outside the envelope become
placeholder views that compile and display their reason codes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .capture import canonical
from .expressions import Unsupported
from .swift_expressions import (
    BOOLEAN,
    PARAM_TYPES,
    STRING,
    UNKNOWN,
    Env,
    Ty,
    accepts,
    bind_names,
    collect_objects,
    describe,
    emit,
    emit_condition,
    emit_decode,
    emit_json,
    emit_run,
    emit_template,
    identifier,
    infer,
    is_complete,
    narrowed_state,
    optional_of,
    required,
    swift_number,
    swift_string,
    swift_type,
    type_from_ir,
    unify,
)
from .swift_runtime import (
    API_CLIENT_SWIFT,
    ASSET_IMAGE_SWIFT,
    HEADER,
    NAVIGATION_RUNTIME,
    STYLE_FIELDS,
    TREE_SWIFT,
    styles_runtime,
)
from .tree import COMPONENT_ROLES, sample_params

SWIFT_TOOLS_VERSION = "5.10"
XCODEGEN_VERSION = "2.46.0"
MIN_XCODE = "16.0"
OUTPUT = "native-swiftui"
GAP_NAVIGATION = "SANKA_RN_SWIFTUI_NAVIGATION"
GAP_TREE = "SANKA_RN_SWIFTUI_TREE"
GAP_ASSET = "SANKA_RN_SWIFTUI_ASSET"
GAP_IDENTIFIER = "SANKA_RN_SWIFTUI_IDENTIFIER"
RUNTIME_TYPES = frozenset(
    {
        "AppRoot",
        "Intent",
        "JSONValue",
        "Node",
        "Route",
        "SankaAPIError",
        "SankaFixtureMode",
        "SankaFixtureTransport",
        "SankaFixtures",
        "SankaKeyed",
        "SankaMemoryStorage",
        "SankaNavigator",
        "SankaRequest",
        "SankaResponse",
        "SankaScreen",
        "SankaStorage",
        "SankaStyle",
        "SankaStyleModifier",
        "SankaTransport",
        "URLSessionTransport",
        "UserDefaultsStorage",
    }
)
GAP_EFFECT = "SANKA_RN_SWIFTUI_EFFECT"
IOS_VERSIONS = {"16.0": ".v16", "17.0": ".v17", "18.0": ".v18"}
STYLE_KINDS = dict(STYLE_FIELDS)
_ASSET_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_PARAM_SEGMENT = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]\Z")
_GROUP_SEGMENT = re.compile(r"\([A-Za-z0-9_-]+\)\Z")
BIND_EVENTS = {"onChangeText": STRING, "onValueChange": BOOLEAN}
MAX_STACK_CHILDREN = 10


@dataclass
class Rendered:
    files: dict[str, str]
    assets: dict[str, dict[str, str]]
    dispositions: list[dict[str, Any]]
    gaps: list[str]


@dataclass(frozen=True)
class RouteInfo:
    case: str
    name: str
    pattern: str | None
    params: dict[str, Ty]
    module: str
    title: str | None
    header: bool


@dataclass
class Handler:
    index: int
    event: str
    actions: list[dict[str, Any]]
    items: tuple[Ty, ...]


@dataclass
class ScreenOutput:
    source: str
    styles: str
    native: bool


@dataclass
class Project:
    """Facts shared by every screen emitter: routes, screen types and value types."""

    navigation: str
    routes: list[RouteInfo]
    screen_types: dict[str, str]
    params_by_module: dict[str, dict[str, Ty]]
    models: dict[str, Ty] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)
    used_assets: dict[str, str] = field(default_factory=dict)

    def route_for_screen(self, name: str) -> RouteInfo:
        matches = [route for route in self.routes if route.name == name]
        if len(matches) != 1:
            raise Unsupported(
                GAP_NAVIGATION, f"navigate({name!r}) does not name exactly one screen route"
            )
        return matches[0]

    def route_for_href(self, href: str) -> tuple[RouteInfo, dict[str, Any]]:
        exact = [route for route in self.routes if route.pattern == href]
        if len(exact) == 1:
            return exact[0], {}
        segments = href.strip("/").split("/") if href.strip("/") else []
        found: list[tuple[RouteInfo, dict[str, Any]]] = []
        for route in self.routes:
            if route.pattern is None:
                continue
            expected = route.pattern.strip("/").split("/") if route.pattern.strip("/") else []
            if len(expected) != len(segments):
                continue
            values: dict[str, Any] = {}
            for want, got in zip(expected, segments, strict=True):
                match = _PARAM_SEGMENT.fullmatch(want)
                if match is not None:
                    values[match.group(1)] = {"lit": got}
                elif want != got:
                    break
            else:
                found.append((route, values))
        if len(found) != 1:
            raise Unsupported(GAP_NAVIGATION, f"href {href!r} does not resolve to one route")
        return found[0]


def render_swiftui(captured: dict[str, Any]) -> Rendered:
    """Render the SwiftUI package for a capture without project-level gaps."""
    if captured["gaps"]:
        raise ValueError("resolve source capture gaps before generation")
    config = captured["configuration"]
    if config["target_framework"] != "swiftui":
        raise ValueError(f"unsupported target framework: {config['target_framework']}")
    screens: list[dict[str, Any]] = sorted(captured["screens"], key=lambda item: item["module"])
    dispositions = [
        {
            "module": screen["module"],
            "disposition": screen["disposition"],
            "adaptation_reasons": list(screen["adaptation_reasons"]),
        }
        for screen in screens
    ]
    gaps: list[str] = []
    try:
        project = _project(captured, screens)
    except Unsupported as error:
        gaps.append(f"{error.code}: {error.message}")
        return Rendered({}, {}, dispositions, gaps)
    outputs: dict[str, ScreenOutput] = {}
    for screen, disposition in zip(screens, dispositions, strict=True):
        type_name = project.screen_types[screen["module"]]
        output: ScreenOutput | None = None
        if disposition["disposition"] == "native-screen":
            try:
                output = ScreenEmitter(screen, type_name, project).render()
            except Unsupported as error:
                disposition["disposition"] = "needs-manual-adaptation"
                disposition["adaptation_reasons"].append(
                    {"code": error.code, "message": f"{screen['module']}: {error.message}"}
                )
        if output is None:
            output = _placeholder(screen, type_name, project, disposition["adaptation_reasons"])
        outputs[type_name] = output
    files: dict[str, str] = {}
    for type_name in sorted(outputs):
        files[f"Sources/AppUI/Screens/{type_name}.swift"] = outputs[type_name].source
    files["Sources/AppUI/Styles.swift"] = _styles_file(outputs, project.used_assets)
    files["Sources/AppUI/Tree.swift"] = TREE_SWIFT
    files["Sources/AppUI/APIClient.swift"] = API_CLIENT_SWIFT
    files["Sources/AppUI/Navigation.swift"] = _navigation_file(captured, project)
    if project.models:
        files["Sources/AppUI/Models.swift"] = _models_file(project.models)
    native_types = sorted(name for name, output in outputs.items() if output.native)
    files["Sources/SankaTreeDump/main.swift"] = _dump_file(native_types)
    app_name = _app_identifier(captured["app"])
    files["Package.swift"] = _package_file(config["min_ios"], project.used_assets)
    files[f"App/{app_name}App.swift"] = _app_file(app_name)
    files["App/project.yml"] = _project_yml(captured, app_name, config["min_ios"])
    files["contract.json"] = canonical(captured) + "\n"
    files["README.md"] = _readme(captured, dispositions, native_types)
    assets = {
        f"Sources/AppUI/Resources/{path}": {"source": path, "sha256": digest}
        for path, digest in sorted(project.used_assets.items())
    }
    return Rendered(dict(sorted(files.items())), assets, dispositions, gaps)


def _project(captured: dict[str, Any], screens: list[dict[str, Any]]) -> Project:
    navigation = captured["navigation"]
    navigators = captured["navigators"]
    by_name = {navigator["name"]: navigator for navigator in navigators}
    referenced = {
        screen["navigator"]
        for navigator in navigators
        for screen in navigator["screens"]
        if "navigator" in screen
    }
    for name in referenced:
        if name not in by_name:
            raise Unsupported(GAP_NAVIGATION, f"nested navigator {name!r} was not captured")
    roots = [navigator["name"] for navigator in navigators if navigator["name"] not in referenced]
    if len(roots) != 1:
        raise Unsupported(GAP_NAVIGATION, "the app must have exactly one root navigator")
    screen_types: dict[str, str] = {}
    for screen in screens:
        type_name = _screen_type(screen["name"])
        if type_name in screen_types.values() or type_name in RUNTIME_TYPES:
            raise Unsupported(
                GAP_IDENTIFIER, f"screen type {type_name} collides with another screen or runtime"
            )
        screen_types[screen["module"]] = type_name
    routes: list[RouteInfo] = []
    for navigator in navigators:
        for screen in navigator["screens"]:
            if "module" not in screen:
                continue
            if screen["module"] not in screen_types:
                raise Unsupported(
                    GAP_NAVIGATION, f"route {screen['name']!r} has no captured screen"
                )
            case = _case_name(screen["name"], navigation)
            if any(route.case == case for route in routes):
                raise Unsupported(
                    GAP_IDENTIFIER, f"route {screen['name']!r} collides with another route case"
                )
            params: dict[str, Ty] = {}
            for name in sorted(screen["params"]):
                kind = screen["params"][name]
                if kind.endswith("?"):
                    raise Unsupported(
                        GAP_NAVIGATION,
                        f"route {screen['name']!r}: optional parameter {name!r} requires "
                        "additional capture",
                    )
                identifier(name, f"route {screen['name']!r} parameter", members=True)
                params[name] = PARAM_TYPES[kind]
            routes.append(
                RouteInfo(
                    case=case,
                    name=screen["name"],
                    pattern=screen.get("route"),
                    params=params,
                    module=screen["module"],
                    title=screen.get("title"),
                    header=bool(screen.get("header", True)),
                )
            )
    params_by_module: dict[str, dict[str, Ty]] = {}
    for route in routes:
        declared = params_by_module.setdefault(route.module, route.params)
        if declared != route.params:
            raise Unsupported(
                GAP_NAVIGATION, f"{route.module} is registered with different parameter lists"
            )
    for screen in screens:
        if screen["module"] not in params_by_module:
            raise Unsupported(GAP_NAVIGATION, f"{screen['module']} is not reachable by a route")
        if "params" in screen:
            captured_params = {
                name: PARAM_TYPES.get(kind.rstrip("?"), UNKNOWN)
                for name, kind in screen["params"].items()
            }
            if captured_params != params_by_module[screen["module"]]:
                raise Unsupported(
                    GAP_NAVIGATION,
                    f"{screen['module']}: screen parameters differ from the route declaration",
                )
    models: dict[str, Ty] = {}
    for model in captured.get("models", []):
        name = identifier(str(model["name"]), "model name")
        if name in RUNTIME_TYPES or name in screen_types.values() or name in models:
            raise Unsupported(GAP_IDENTIFIER, f"model {name} collides with another type")
        fields: dict[str, Ty] = {}
        for field_entry in model["fields"]:
            field_name = identifier(str(field_entry["name"]), f"model {name} field", members=True)
            scalar = PARAM_TYPES[str(field_entry["type"])]
            fields[field_name] = optional_of(scalar) if field_entry["optional"] else scalar
        models[name] = Ty("object", fields=tuple(sorted(fields.items())), name=name, model=True)
    return Project(
        navigation=navigation,
        routes=routes,
        screen_types=screen_types,
        params_by_module=params_by_module,
        models=models,
        assets=dict(captured.get("assets", {})),
    )


def _screen_type(name: str) -> str:
    type_name = name if name.endswith("Screen") else name + "Screen"
    return identifier(type_name, "screen name")


def _case_name(name: str, navigation: str) -> str:
    if navigation == "react-navigation":
        words = [name]
    else:
        words = [
            segment
            for segment in name.split("/")
            if segment
            and _PARAM_SEGMENT.fullmatch(segment) is None
            and _GROUP_SEGMENT.fullmatch(segment) is None
        ]
    if not words:
        raise Unsupported(GAP_IDENTIFIER, f"route {name!r} has no static segment to name")
    joined = "".join(word[:1].upper() + word[1:] for word in words)
    case = joined[:1].lower() + joined[1:]
    return identifier(case, f"route {name!r}")


def _app_identifier(app: dict[str, Any]) -> str:
    words = re.findall(r"[A-Za-z0-9]+", str(app.get("display_name") or app.get("name") or ""))
    joined = "".join(word[:1].upper() + word[1:] for word in words)
    if not joined or joined[0].isdigit():
        joined = "Migrated" + joined
    return joined


def _bundle_id(app: dict[str, Any]) -> str:
    if app.get("bundle_id"):
        return str(app["bundle_id"])
    slug = re.sub(r"[^a-z0-9]", "", str(app.get("name") or "app").lower()) or "app"
    return f"com.example.{slug}"


class ScreenEmitter:
    """Emit one native screen: state, actions, reducer, body and normalized tree."""

    def __init__(self, screen: dict[str, Any], type_name: str, project: Project) -> None:
        self.screen = screen
        self.type = type_name
        self.project = project
        self.state_type = type_name + "State"
        self.params_type = type_name + "Params"
        self.action_type = type_name + "Action"
        self.styles_type = type_name + "Styles"
        self.params = project.params_by_module[screen["module"]]
        self.state: dict[str, Ty] = {}
        self.setters: dict[str, str] = {}
        self.initial: dict[str, dict[str, Any]] = {}
        self.handlers: list[Handler] = []
        self.handler_index: dict[tuple[int, str], int] = {}
        self.uses_navigation = False
        self.uses_back = False
        self.uses_run = False
        self.uses_store = False
        self.set_names: list[str] = []
        self.effects: dict[str, dict[str, Any]] = {
            str(effect["id"]): effect for effect in screen.get("effects", [])
        }
        self.appear: list[str] = [str(item) for item in screen.get("on_appear", [])]
        self.submit_effects: list[str] = [
            effect_id
            for effect_id, effect in self.effects.items()
            if effect["kind"] == "fetch" and effect["method"] == "POST"
        ]

    # -- analysis ---------------------------------------------------------------------

    def render(self) -> ScreenOutput:
        self._state_types()
        self._collect_handlers(self.screen["tree"], ())
        for handler in self.handlers:
            for action in handler.actions:
                self._analyze_action(action, handler)
        for effect in self.effects.values():
            self._analyze_effect(effect)
        styles = self._styles()
        body_env = Env(self.state, self.params, item_access=".element")
        tree_env = Env(self.state, self.params)
        body = self._body(self.screen["tree"], body_env, root=True)
        tree = self._tree(self.screen["tree"], tree_env, root=True)
        handlers = [self._handler_source(handler) for handler in self.handlers]
        effects = [
            self._effect_source(index, effect) for index, effect in enumerate(self.effects.values())
        ]
        source = self._screen_source(body, tree, handlers, effects)
        return ScreenOutput(source=source, styles=styles, native=True)

    def _state_types(self) -> None:
        empty = Env({}, {})
        declared: set[str] = set()
        for entry in self.screen["state"]:
            name = identifier(entry["name"], "state value", members=True)
            identifier(entry["setter"], "state setter")
            self.setters[name] = entry["setter"]
            self.initial[name] = entry["initial"]
            if "type" in entry:
                ty = type_from_ir(entry["type"], self.project.models, f"state {name!r}")
                accepts(ty, infer(entry["initial"], empty), f"state {name!r} initial value")
                self.state[name] = ty
                declared.add(name)
            else:
                self.state[name] = infer(entry["initial"], empty)
        assignments = list(self._assignments(self.screen["tree"], ()))
        for _ in range(8):
            changed = False
            for name, value, items, data in assignments:
                if name in declared:
                    continue
                env = Env(self.state, self.params, items=items, data=data)
                try:
                    inferred = infer(value, env)
                except Unsupported as error:
                    if error.code == "SANKA_RN_SWIFTUI_TYPE" and not is_complete(self.state[name]):
                        continue
                    raise
                unified = unify(self.state[name], inferred, f"state {name!r}")
                if unified != self.state[name]:
                    self.state[name] = unified
                    changed = True
            if not changed:
                break
        for name, ty in self.state.items():
            if not is_complete(ty):
                raise Unsupported(
                    "SANKA_RN_SWIFTUI_TYPE",
                    f"state {name!r} has no complete Swift type ({describe(ty)}); "
                    "declare it with useState<T>",
                )
            if name not in declared:
                self.state[name] = bind_names(ty, self.type + name[:1].upper() + name[1:], name)
            collect_objects(self.state[name], self.project.models)

    def _assignments(self, node: Any, items: tuple[Ty, ...]) -> Any:
        """Yield ``(state, value, item types, data type)`` for every set action."""
        if not isinstance(node, dict):
            return
        if "when" in node:
            yield from self._assignments(node["then"], items)
            yield from self._assignments(node["else"], items)
            return
        if "each" in node:
            yield from self._assignments(node["element"], (*items, self._element_type(node, items)))
            return
        if "kind" not in node:
            return
        for actions in (node.get("events") or {}).values():
            for action in actions:
                if "set" in action and action["value"] != {"arg": 0}:
                    if action["set"] not in self.state:
                        raise Unsupported(
                            GAP_TREE, f"setter targets unknown state {action['set']!r}"
                        )
                    yield action["set"], action["value"], items, None
        if node["kind"] == "FlatList":
            element = self._element_type(node, items)
            yield from self._assignments(node["item"]["element"], (*items, element))
        for child in node.get("children", []):
            yield from self._assignments(child, items)

    def _element_type(self, node: dict[str, Any], items: tuple[Ty, ...]) -> Ty:
        source = node["each"] if "each" in node else node["props"]["data"]
        try:
            ty = infer(source, Env(self.state, self.params, items=items))
        except Unsupported:
            return UNKNOWN
        if ty.kind != "array":
            raise Unsupported("SANKA_RN_SWIFTUI_TYPE", "lists must iterate over an array value")
        return ty.element or UNKNOWN

    def _collect_handlers(self, node: Any, items: tuple[Ty, ...]) -> None:
        if "when" in node:
            self._collect_handlers(node["then"], items)
            if node["else"] is not None:
                self._collect_handlers(node["else"], items)
            return
        if "each" in node:
            self._collect_handlers(node["element"], (*items, self._element_type(node, items)))
            return
        if "kind" not in node:
            return
        for event in sorted(node.get("events") or {}):
            index = len(self.handlers)
            self.handlers.append(Handler(index, event, node["events"][event], items))
            self.handler_index[(id(node), event)] = index
        if node["kind"] == "FlatList":
            element = self._element_type(node, items)
            self._collect_handlers(node["item"]["element"], (*items, element))
        for child in node.get("children", []):
            self._collect_handlers(child, items)

    def _analyze_action(self, action: dict[str, Any], handler: Handler) -> None:
        if "set" in action:
            name = action["set"]
            if name not in self.state:
                raise Unsupported(GAP_TREE, f"setter targets unknown state {name!r}")
            if name not in self.set_names:
                self.set_names.append(name)
            if action["value"] == {"arg": 0}:
                expected = BIND_EVENTS.get(handler.event)
                if expected is None:
                    raise Unsupported(
                        GAP_TREE, f"{handler.event} has no argument to assign to {name!r}"
                    )
                if self.state[name] != expected:
                    raise Unsupported(
                        "SANKA_RN_SWIFTUI_TYPE",
                        f"{handler.event} assigns {describe(expected)} to state {name!r} "
                        f"of type {describe(self.state[name])}",
                    )
        elif "navigate" in action or "push" in action:
            self.uses_navigation = True
        elif "back" in action:
            self.uses_back = True
        elif "run" in action:
            if action["run"] not in self.effects:
                raise Unsupported(GAP_EFFECT, f"handler runs unknown effect {action['run']!r}")
            if handler.event == "onRefresh" and handler.actions != [action]:
                raise Unsupported(GAP_EFFECT, "onRefresh must run exactly one loader")
            self.uses_run = True
        elif "store" in action:
            self.uses_store = True
        else:
            raise Unsupported(GAP_TREE, f"unsupported action {sorted(action)}")

    def _analyze_effect(self, effect: dict[str, Any]) -> None:
        """Record the state names effects assign and validate their shapes."""
        for phase in ("before", "success", "failure", "after"):
            for action in effect.get(phase, []):
                if "set" in action:
                    if action["set"] not in self.state:
                        raise Unsupported(
                            GAP_EFFECT,
                            f"effect {effect['id']} sets unknown state {action['set']!r}",
                        )
                    if action["set"] not in self.set_names:
                        self.set_names.append(action["set"])
                elif "navigate" in action or "push" in action:
                    self.uses_navigation = True
                elif "back" in action:
                    self.uses_back = True
                else:
                    raise Unsupported(
                        GAP_EFFECT,
                        f"effect {effect['id']} has an unsupported action {sorted(action)}",
                    )
        if effect["kind"] == "fetch":
            for name in effect["headers"]:
                if not name.isascii() or not name.strip() or any(c in name for c in ":\r\n"):
                    raise Unsupported(GAP_EFFECT, f"header name {name!r} is not qualified")

    def _data_type(self, effect: dict[str, Any]) -> Ty | None:
        if effect["kind"] == "storage-read":
            return optional_of(STRING)
        if effect.get("response") is None:
            return None
        return type_from_ir(effect["response"], self.project.models, f"effect {effect['id']}")

    def _styles(self) -> str:
        lines = [f"public enum {self.styles_type} {{"]
        styles = self.screen["styles"]
        for name in sorted(styles):
            identifier(name, "style name")
            arguments = []
            for prop, kind in STYLE_FIELDS:
                if prop not in styles[name]:
                    continue
                value = styles[name][prop]
                if kind == "Double":
                    arguments.append(f"{prop}: {swift_number(value)}")
                else:
                    arguments.append(f"{prop}: {swift_string(str(value))}")
            unknown = sorted(set(styles[name]) - set(STYLE_KINDS))
            if unknown:
                raise Unsupported(
                    "SANKA_RN_DYNAMIC_STYLE", f"style {name!r} uses unsupported {unknown}"
                )
            lines.append(f"    public static let {name} = SankaStyle({', '.join(arguments)})")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # -- helpers ----------------------------------------------------------------------

    def _style_expr(self, names: list[str] | None) -> str | None:
        if not names:
            return None
        parts = [f"{self.styles_type}.{name}" for name in names]
        expression = parts[0]
        for part in parts[1:]:
            expression = f"{expression}.merging({part})"
        return expression

    def _merged_style(self, names: list[str] | None) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for name in names or []:
            merged.update(self.screen["styles"][name])
        return merged

    def _literal(self, node: dict[str, Any], prop: str) -> Any:
        value = node["props"].get(prop)
        if value is None:
            return None
        if "lit" not in value:
            raise Unsupported(GAP_TREE, f"<{node['kind']} {prop}> must be a literal")
        return value["lit"]

    def _label(self, node: dict[str, Any]) -> str | None:
        label = self._literal(node, "accessibilityLabel")
        return None if label is None else str(label)

    def _modifiers(self, node: dict[str, Any]) -> list[str]:
        lines = []
        style = self._style_expr(node["props"].get("style"))
        if style is not None:
            lines.append(f"    .sankaStyle({style})")
        label = self._label(node)
        if label is not None:
            lines.append(f"    .accessibilityLabel(Text(verbatim: {swift_string(label)}))")
        test_id = self._literal(node, "testID")
        if test_id is not None:
            lines.append(f"    .accessibilityIdentifier({swift_string(str(test_id))})")
        return lines

    def _handler_call(
        self, node: dict[str, Any], event: str, env: Env, prefix: str, argument: str = ""
    ) -> str | None:
        index = self.handler_index.get((id(node), event))
        if index is None:
            return None
        arguments = ["state", "params"]
        arguments.extend(f"item{depth}{env.item_access}" for depth in range(len(env.items)))
        if argument:
            arguments.append(argument)
        return f"Self.{prefix}{index}({', '.join(arguments)})"

    def _bound_value(self, node: dict[str, Any], env: Env, expected: Ty) -> str:
        value = node["props"].get("value")
        if value is None:
            raise Unsupported(GAP_TREE, f"<{node['kind']}> must bind value to state")
        ty = infer(value, env)
        if ty != expected:
            raise Unsupported(
                "SANKA_RN_SWIFTUI_TYPE",
                f"<{node['kind']} value> must be {describe(expected)}, found {describe(ty)}",
            )
        return emit(value, env)

    def _enabled(self, node: dict[str, Any], env: Env) -> str:
        disabled = node["props"].get("disabled")
        if disabled is None:
            return "true"
        if infer(disabled, env) != BOOLEAN:
            raise Unsupported("SANKA_RN_SWIFTUI_TYPE", "disabled must be a boolean expression")
        return f"!({emit(disabled, env)})"

    def _text_run(self, node: dict[str, Any], env: Env) -> str:
        children = node.get("children", [])
        if not children:
            return '""'
        if len(children) != 1 or "text" not in children[0]:
            raise Unsupported(GAP_TREE, "<Text> may only contain text, not nested elements")
        return emit_run(children[0]["text"], env)

    def _button_children(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """Expo Router's Link renders string children inside an implicit Text."""
        children: list[dict[str, Any]] = node.get("children", [])
        if node["kind"] == "Link":
            return [
                {"kind": "Text", "props": {}, "children": [child]} if "text" in child else child
                for child in children
            ]
        return children

    def _image_source(self, node: dict[str, Any], env: Env) -> tuple[str, str]:
        source = node["props"].get("source")
        if source is None:
            raise Unsupported(GAP_TREE, "<Image> needs a source")
        if "asset" in source:
            path = str(source["asset"])
            for segment in path.split("/"):
                if not _ASSET_SEGMENT.fullmatch(segment) or segment.startswith("."):
                    raise Unsupported(GAP_ASSET, f"asset path {path!r} is not a bundle resource")
            digest = self.project.assets.get(path)
            if digest is None:
                raise Unsupported(GAP_ASSET, f"asset {path!r} was not captured")
            self.project.used_assets[path] = digest
            return "asset", swift_string(path)
        uri = source["uri"]
        if infer(uri, env) != STRING:
            raise Unsupported("SANKA_RN_SWIFTUI_TYPE", "image uri must be a string")
        return "uri", emit(uri, env)

    def _key(self, node: dict[str, Any], env: Env) -> str | None:
        extractor = node["props"].get("keyExtractor")
        if extractor is None:
            return None
        return emit_template(extractor, env)

    def _data(self, node: dict[str, Any], env: Env) -> tuple[str, Ty]:
        source = node["each"] if "each" in node else node["props"]["data"]
        ty = infer(source, env)
        if ty.kind != "array":
            raise Unsupported("SANKA_RN_SWIFTUI_TYPE", "lists must iterate over an array value")
        return emit(source, env), ty.element or UNKNOWN

    # -- SwiftUI body -----------------------------------------------------------------

    def _body(self, node: dict[str, Any], env: Env, *, root: bool = False) -> list[str]:
        if "when" in node:
            lines = [f"if {self._condition(node['when'], env)} {{"]
            lines.extend(_indent(self._body(node["then"], self._narrow(node["when"], env))))
            if node["else"] is not None:
                lines.append("} else {")
                lines.extend(_indent(self._body(node["else"], env)))
            lines.append("}")
            return lines
        if "each" in node:
            data, element = self._data(node, env)
            inner = env.with_item(element)
            depth = len(env.items)
            lines = [f"ForEach(Array({data}.enumerated()), id: \\.offset) {{ item{depth} in"]
            lines.extend(_indent(self._body(node["element"], inner)))
            lines.append("}")
            return lines
        if "text" in node:
            raise Unsupported(GAP_TREE, "text outside <Text> is not rendered by React Native")
        kind = node["kind"]
        if kind == "Fragment":
            if root:
                return self._stack({"flexDirection": "column"}, node["children"], env, [])
            return [line for child in node["children"] for line in self._body(child, env)]
        if kind == "StatusBar":
            return ["EmptyView()"]
        if kind in {"View", "SafeAreaView"}:
            style = self._merged_style(node["props"].get("style"))
            return self._stack(style, node["children"], env, self._modifiers(node))
        if kind == "ScrollView":
            content = self._merged_style(node["props"].get("contentContainerStyle"))
            content_style = self._style_expr(node["props"].get("contentContainerStyle"))
            inner_modifiers = [f"    .sankaStyle({content_style})"] if content_style else []
            lines = ["ScrollView {"]
            lines.extend(_indent(self._stack(content, node["children"], env, inner_modifiers)))
            lines.append("}")
            return lines + self._modifiers(node)
        if kind == "Text":
            lines = [f"Text(verbatim: {self._text_run(node, env)})"]
            limit = self._literal(node, "numberOfLines")
            if limit is not None:
                lines.append(f"    .lineLimit({int(limit)})")
            return lines + self._modifiers(node)
        if kind == "Image":
            mode, source = self._image_source(node, env)
            resize = self._literal(node, "resizeMode")
            content_mode = ".fit" if resize in {None, "contain", "center"} else ".fill"
            image = f"sankaImage({mode}: {source}, contentMode: {content_mode})"
            return [image, *self._modifiers(node)]
        if kind in {"Pressable", "TouchableOpacity", "Link"}:
            call = self._handler_call(node, "onPress", env, "handler")
            action = f"dispatch({call})" if call else ""
            lines = [f"Button(action: {{ {action} }}) {{"]
            children = self._button_children(node)
            lines.extend(_indent(self._stack({"flexDirection": "column"}, children, env, [])))
            lines.append("}")
            lines.append("    .buttonStyle(.plain)")
            if node["props"].get("disabled") is not None:
                lines.append(f"    .disabled(!({self._enabled(node, env)}))")
            return lines + self._modifiers(node)
        if kind == "TextInput":
            value = self._bound_value(node, env, STRING)
            call = self._handler_call(node, "onChangeText", env, "handler", argument="value")
            setter = f"{{ value in dispatch({call}) }}" if call else "{ _ in }"
            binding = f"Binding(get: {{ {value} }}, set: {setter})"
            placeholder = self._literal(node, "placeholder")
            title = swift_string("" if placeholder is None else str(placeholder))
            control = "SecureField" if self._literal(node, "secureTextEntry") else "TextField"
            lines = [f"{control}({title}, text: {binding})", "    .textFieldStyle(.plain)"]
            return lines + self._modifiers(node)
        if kind == "Switch":
            value = self._bound_value(node, env, BOOLEAN)
            call = self._handler_call(node, "onValueChange", env, "handler", argument="value")
            setter = f"{{ value in dispatch({call}) }}" if call else "{ _ in }"
            binding = f"Binding(get: {{ {value} }}, set: {setter})"
            lines = [f"Toggle(isOn: {binding}) {{ EmptyView() }}", "    .labelsHidden()"]
            return lines + self._modifiers(node)
        if kind == "FlatList":
            data, element = self._data(node, env)
            inner = env.with_item(element)
            depth = len(env.items)
            key = self._key(node, Env(self.state, self.params, items=inner.items))
            if key is None:
                collection = f"Array({data}.enumerated()), id: \\.offset"
            else:
                collection = f"sankaKeyed({data}) {{ item{depth} in {key} }}"
            lines = ["List {", f"    ForEach({collection}) {{ item{depth} in"]
            lines.extend(_indent(self._body(node["item"]["element"], inner), 2))
            lines.append("    }")
            lines.append("}")
            lines.append("    .listStyle(.plain)")
            refresh = (node.get("events") or {}).get("onRefresh")
            if refresh:
                effect = swift_string(str(refresh[0]["run"]))
                lines.append(f"    .refreshable {{ await perform(effect: {effect}) }}")
            if node["props"].get("refreshing") is not None:
                infer(node["props"]["refreshing"], env)
            return lines + self._modifiers(node)
        if kind == "ActivityIndicator":
            return ["ProgressView()", *self._modifiers(node)]
        raise Unsupported(GAP_TREE, f"<{kind}> has no SwiftUI rendering")

    def _condition(self, expr: dict[str, Any], env: Env) -> str:
        return emit_condition(expr, env)

    def _narrow(self, expr: dict[str, Any], env: Env) -> Env:
        """Inside a null check's then-branch the checked state value is non-null."""
        name = narrowed_state(expr, env)
        return env.with_narrowed(name) if name is not None else env

    def _stack(
        self,
        style: dict[str, Any],
        children: list[dict[str, Any]],
        env: Env,
        modifiers: list[str],
    ) -> list[str]:
        horizontal = str(style.get("flexDirection", "column")).startswith("row")
        align = str(style.get("alignItems", "stretch"))
        if horizontal:
            alignment = {"flex-start": ".top", "flex-end": ".bottom"}.get(align, ".center")
            gap = style.get("columnGap", style.get("gap", 0))
        else:
            alignment = {"center": ".center", "flex-end": ".trailing"}.get(align, ".leading")
            gap = style.get("rowGap", style.get("gap", 0))
        justify = str(style.get("justifyContent", "flex-start"))
        units: list[list[str]] = [self._body(child, env) for child in children]
        spaced: list[list[str]] = []
        if units and justify in {"center", "flex-end", "space-around", "space-evenly"}:
            spaced.append(["Spacer()"])
        for index, unit in enumerate(units):
            if index and justify in {"space-between", "space-around", "space-evenly"}:
                spaced.append(["Spacer()"])
            spaced.append(unit)
        if units and justify in {"center", "space-around", "space-evenly"}:
            spaced.append(["Spacer()"])
        stack = "HStack" if horizontal else "VStack"
        lines = [f"{stack}(alignment: {alignment}, spacing: {swift_number(gap)}) {{"]
        lines.extend(_indent(_grouped(spaced)))
        lines.append("}")
        return lines + modifiers

    # -- normalized tree --------------------------------------------------------------

    def _tree(self, node: dict[str, Any], env: Env, *, root: bool = False) -> str:
        """Return a Swift ``Node`` expression for an element node."""
        if "text" in node:
            raise Unsupported(GAP_TREE, "text outside <Text> is not rendered by React Native")
        kind = node["kind"]
        if kind == "Fragment":
            if not root:
                raise Unsupported(GAP_TREE, "fragments are spliced into their parent")
            return _node("container", children=self._tree_children(node["children"], env))
        role = COMPONENT_ROLES.get(kind)
        if role is None:
            raise Unsupported(GAP_TREE, f"<{kind}> has no tree role")
        label = self._label(node)
        label_code = None if label is None else swift_string(label)
        if kind == "StatusBar":
            return _node("container")
        if kind in {"View", "SafeAreaView", "ScrollView"}:
            return _node(
                "container", label=label_code, children=self._tree_children(node["children"], env)
            )
        if kind == "Text":
            return _node("text", text=self._text_run(node, env), label=label_code)
        if kind == "Image":
            _, source = self._image_source(node, env)
            return _node("image", text=source, label=label_code)
        if kind in {"Pressable", "TouchableOpacity", "Link"}:
            return _node(
                "button",
                label=label_code,
                enabled=self._enabled(node, env),
                action=self._handler_call(node, "onPress", env, "intents"),
                children=self._tree_children(self._button_children(node), env),
            )
        if kind == "TextInput":
            placeholder = self._literal(node, "placeholder")
            fallback = None if placeholder is None else swift_string(str(placeholder))
            return _node(
                "textfield",
                text=self._bound_value(node, env, STRING),
                label=label_code or fallback,
                action=self._handler_call(node, "onChangeText", env, "intents"),
            )
        if kind == "Switch":
            return _node(
                "switch",
                label=label_code,
                checked=self._bound_value(node, env, BOOLEAN),
                action=self._handler_call(node, "onValueChange", env, "intents"),
            )
        if kind == "FlatList":
            data, element = self._data(node, env)
            inner = env.with_item(element)
            depth = len(env.items)
            key = self._key(node, inner)
            item = self._tree(node["item"]["element"], inner)
            if key is None:
                closure = (
                    f"Array({data}.enumerated()).map {{ (index, item{depth}) in "
                    f"{_node('listitem', text='jsString(Double(index))', children=f'[{item}]')} }}"
                )
            else:
                closure = (
                    f"{data}.map {{ item{depth} in "
                    f"{_node('listitem', text=key, children=f'[{item}]')} }}"
                )
            return _node("list", label=label_code, children=closure)
        if kind == "ActivityIndicator":
            return _node("indicator", label=label_code)
        raise Unsupported(GAP_TREE, f"<{kind}> has no tree rendering")

    def _tree_children(self, children: list[dict[str, Any]], env: Env) -> str:
        if not children:
            return "[]"
        if all(
            "kind" in child and child["kind"] != "Fragment" and not child.get("children")
            for child in children
        ):
            return "[" + ", ".join(self._tree(child, env) for child in children) + "]"
        statements = self._tree_statements(children, env)
        return (
            "{ () -> [Node] in\n    var nodes: [Node] = []\n"
            + "\n".join(_indent(statements))
            + "\n    return nodes\n}()"
        )

    def _tree_statements(self, children: list[dict[str, Any]], env: Env) -> list[str]:
        lines: list[str] = []
        for child in children:
            if "when" in child:
                lines.append(f"if {self._condition(child['when'], env)} {{")
                narrowed = self._narrow(child["when"], env)
                lines.extend(_indent(self._tree_statements([child["then"]], narrowed)))
                if child["else"] is not None:
                    lines.append("} else {")
                    lines.extend(_indent(self._tree_statements([child["else"]], env)))
                lines.append("}")
            elif "each" in child:
                data, element = self._data(child, env)
                inner = env.with_item(element)
                lines.append(f"for item{len(env.items)} in {data} {{")
                lines.extend(_indent(self._tree_statements([child["element"]], inner)))
                lines.append("}")
            elif "text" in child:
                raise Unsupported(GAP_TREE, "text outside <Text> is not rendered by React Native")
            elif child["kind"] == "Fragment":
                lines.extend(self._tree_statements(child["children"], env))
            else:
                first, *rest = f"nodes.append({self._tree(child, env)})".split("\n")
                lines.append(first)
                lines.extend(_indent(rest))
        return lines

    # -- handlers and actions ---------------------------------------------------------

    def _handler_source(self, handler: Handler) -> str:
        env = Env(self.state, self.params, items=handler.items)
        parameters = [f"_ state: {self.state_type}", f"_ params: {self.params_type}"]
        parameters.extend(
            f"_ item{depth}: {swift_type(ty)}" for depth, ty in enumerate(handler.items)
        )
        bound = BIND_EVENTS.get(handler.event)
        typed_parameters = list(parameters)
        if bound is not None:
            typed_parameters.append(f"_ value: {swift_type(bound)}")
        typed: list[str] = []
        intents: list[str] = []
        for action in handler.actions:
            action_code, intent_code = self._action(action, env)
            typed.append(action_code)
            intents.append(intent_code)
        return (
            f"    static func handler{handler.index}({', '.join(typed_parameters)}) "
            f"-> [{self.action_type}] {{\n"
            f"        [{', '.join(typed)}]\n"
            "    }\n\n"
            f"    static func intents{handler.index}({', '.join(parameters)}) -> [Intent] {{\n"
            f"        [{', '.join(intents)}]\n"
            "    }\n"
        )

    def _action(self, action: dict[str, Any], env: Env) -> tuple[str, str]:
        if "set" in action:
            name = action["set"]
            setter = self.setters[name]
            if action["value"] == {"arg": 0}:
                return f".{setter}(value)", f".bind({swift_string(name)})"
            code = emit(action["value"], env, self.state[name])
            accepts(self.state[name], infer(action["value"], env), f"state {name!r}")
            return f".{setter}({code})", f"{self.action_type}.{setter}({code}).intent"
        if "back" in action:
            return ".back", "Intent.back"
        if "run" in action:
            effect = swift_string(str(action["run"]))
            return f".run({effect})", f"Intent.run({effect})"
        if "store" in action:
            key = swift_string(str(action["store"]))
            if infer(action["value"], env) != STRING:
                raise Unsupported("SANKA_RN_SWIFTUI_TYPE", "stored values must be strings")
            value = emit(action["value"], env)
            return f".store({key}, {value})", f"{self.action_type}.store({key}, {value}).intent"
        if "navigate" in action:
            route = self.project.route_for_screen(str(action["navigate"]))
            values = dict(action.get("params") or {})
        else:
            route, values = self.project.route_for_href(str(action["push"]))
            values.update(action.get("params") or {})
        if set(values) != set(route.params):
            raise Unsupported(
                GAP_NAVIGATION,
                f"navigation to {route.name!r} passes {sorted(values)} but the route declares "
                f"{sorted(route.params)}",
            )
        arguments = []
        for name in sorted(route.params):
            expected = route.params[name]
            if infer(values[name], env) != expected:
                raise Unsupported(
                    "SANKA_RN_SWIFTUI_TYPE",
                    f"navigation parameter {name!r} must be {describe(expected)}",
                )
            arguments.append(f"{name}: {emit(values[name], env, expected)}")
        constructed = f"Route.{route.case}" + (f"({', '.join(arguments)})" if arguments else "")
        return f".navigate({constructed})", f"{self.action_type}.navigate({constructed}).intent"

    def _effect_source(self, index: int, effect: dict[str, Any]) -> str:
        """Emit one effect as a static async function over a state reader and an emitter."""
        env = Env(self.state, self.params)
        signature = (
            f"    @MainActor\n"
            f"    static func effect{index}(\n"
            f"        _ read: () -> {self.state_type},\n"
            f"        _ params: {self.params_type},\n"
            f"        _ emit: ({self.action_type}) -> Void,\n"
            "        transport: SankaTransport,\n"
            "        storage: SankaStorage\n"
            "    ) async {"
        )
        lines = [signature]

        def emits(actions: list[dict[str, Any]], scope: Env, level: int) -> list[str]:
            return [
                "    " * level + f"emit({self._action(action, scope)[0]})" for action in actions
            ]

        data_type = self._data_type(effect)
        if effect["kind"] == "storage-read":
            lines.append("        do {")
            lines.append(
                f"            let data = try await storage.get({swift_string(effect['key'])})"
            )
            lines.extend(emits(effect["success"], env.with_data(data_type), 3))
            lines.append("        } catch {")
            lines.append("            return")
            lines.append("        }")
            lines.append("    }")
            return "\n".join(lines) + "\n"
        lines.extend(emits(effect["before"], env, 2))
        lines.append("        do {")
        lines.append("            var state = read()")
        url = emit_template({"template": effect["url"]}, env)
        headers = ", ".join(
            f"{swift_string(name)}: {emit_template(value, env)}"
            for name, value in sorted(effect["headers"].items())
        )
        if effect["body"] is None:
            body = "nil"
        else:
            fields = ", ".join(
                f"{swift_string(name)}: {emit_json(emit(value, env), infer(value, env))}"
                for name, value in sorted(effect["body"].items())
            )
            body = f".object([{fields}])"
        lines.append(
            f"            let request = SankaRequest(effect: {swift_string(effect['id'])}, "
            f"method: {swift_string(effect['method'])}, url: {url}, "
            f"headers: [{headers or ':'}], body: {body})"
        )
        lines.append("            let response = try await transport.send(request)")
        if effect["checks_ok"]:
            lines.append("            guard (200..<300).contains(response.status) else {")
            lines.append("                throw SankaAPIError.status(response.status)")
            lines.append("            }")
        if data_type is not None:
            decode = emit_decode("json", data_type, optional=False)
            lines.append(
                f"            let data = try decodeResponse(response) {{ json in {decode} }}"
            )
        lines.append("            state = read()")
        lines.append("            _ = state")
        lines.extend(emits(effect["success"], env.with_data(data_type), 3))
        lines.append("        } catch SankaAPIError.pending {")
        lines.append("            return")
        lines.append("        } catch {")
        lines.extend(emits(effect["failure"], env, 3))
        lines.append("        }")
        lines.extend(emits(effect["after"], env, 2))
        lines.append("    }")
        return "\n".join(lines) + "\n"

    # -- assembly ---------------------------------------------------------------------

    def _screen_source(
        self, body: list[str], tree: str, handlers: list[str], effects: list[str]
    ) -> str:
        state_lines = []
        empty = Env({}, {})
        for name, ty in self.state.items():
            initial = emit(self.initial[name], empty, ty)
            state_lines.append(f"    public var {name}: {swift_type(ty)} = {initial}")
        params_lines = [
            f"    public var {name}: {swift_type(ty)}" for name, ty in self.params.items()
        ]
        params_init = ", ".join(f"{name}: {swift_type(ty)}" for name, ty in self.params.items())
        params_assign = "".join(f"\n        self.{name} = {name}" for name in self.params)
        cases = [
            f"    case {self.setters[name]}({swift_type(self.state[name])})"
            for name in self.set_names
        ]
        if self.uses_navigation:
            cases.append("    case navigate(Route)")
        if self.uses_back:
            cases.append("    case back")
        if self.uses_run:
            cases.append("    case run(String)")
        if self.uses_store:
            cases.append("    case store(String, String)")
        intent_cases = []
        decode_cases = []
        reduce_cases = []
        for name in self.set_names:
            setter = self.setters[name]
            ty = self.state[name]
            decoded = emit_decode("value", ty, optional=False)
            intent_cases.append(
                f"        case .{setter}(let value):\n"
                f"            return .set({swift_string(name)}, {emit_json('value', ty)})"
            )
            decode_cases.append(
                f"        case .set({swift_string(name)}, let value):\n"
                f"            guard let decoded = {decoded} else {{\n"
                "                return nil\n"
                "            }\n"
                f"            self = .{setter}(decoded)"
            )
            reduce_cases.append(
                f"        case .{setter}(let value):\n            state.{name} = value"
            )
        if self.uses_navigation:
            intent_cases.append(
                "        case .navigate(let route):\n            return route.intent"
            )
        if self.uses_back:
            intent_cases.append("        case .back:\n            return .back")
        if self.uses_run:
            intent_cases.append("        case .run(let effect):\n            return .run(effect)")
        if self.uses_store:
            intent_cases.append(
                "        case .store(let key, let value):\n            return .store(key, value)"
            )
        ignored = [
            case
            for case, used in (
                (".navigate", self.uses_navigation),
                (".back", self.uses_back),
                (".run", self.uses_run),
                (".store", self.uses_store),
            )
            if used
        ]
        if ignored:
            reduce_cases.append(f"        case {', '.join(ignored)}:\n            break")
        dispatch_cases = []
        if self.uses_navigation:
            dispatch_cases.append(
                "            case .navigate(let route):\n                navigator.navigate(route)"
            )
        if self.uses_back:
            dispatch_cases.append("            case .back:\n                navigator.back()")
        if self.uses_run:
            dispatch_cases.append(
                "            case .run(let effect):\n"
                "                Task { await perform(effect: effect) }"
            )
        if self.uses_store:
            dispatch_cases.append(
                "            case .store(let key, let value):\n"
                "                Task { await storage.set(key, value) }"
            )
        if cases:
            intent_body = "        switch self {\n" + "\n".join(intent_cases) + "\n        }"
            decode_body = (
                "        switch intent {\n"
                + "\n".join(decode_cases)
                + ("\n" if decode_cases else "")
                + "        default:\n            return nil\n        }"
            )
            reduce_body = "        switch action {\n" + "\n".join(reduce_cases) + "\n        }"
            if dispatch_cases:
                fallback = (
                    "\n            default:\n                var next = state\n"
                    "                Self.apply(action, to: &next)\n                state = next"
                    if self.set_names
                    else ""
                )
                dispatch_body = (
                    "        for action in actions {\n            switch action {\n"
                    + "\n".join(dispatch_cases)
                    + fallback
                    + "\n            }\n        }"
                )
            else:
                dispatch_body = (
                    "        for action in actions {\n            var next = state\n"
                    "            Self.apply(action, to: &next)\n            state = next\n        }"
                )
        else:
            intent_body = "        switch self {}"
            decode_body = "        return nil"
            reduce_body = "        switch action {}"
            dispatch_body = (
                "        for action in actions {\n            var next = state\n"
                "            Self.apply(action, to: &next)\n            state = next\n        }"
            )
        sample = sample_params({name: ty.kind for name, ty in self.params.items()})
        sample_arguments = ", ".join(
            f"{name}: {emit({'lit': sample[name]}, empty, ty)}" for name, ty in self.params.items()
        )
        sample_json = ", ".join(
            f"{swift_string(name)}: {emit_json(emit({'lit': sample[name]}, empty, ty), ty)}"
            for name, ty in self.params.items()
        )
        params_struct = (
            f"public struct {self.params_type}: Equatable {{\n"
            + ("\n".join(params_lines) + "\n\n" if params_lines else "")
            + (
                f"    public init({params_init}) {{{params_assign}\n    }}\n"
                if params_lines
                else "    public init() {}\n"
            )
            + "}\n"
        )
        effect_ids = list(self.effects)
        run_cases = "".join(
            f"        case {swift_string(effect_id)}:\n"
            f"            await effect{index}(read, params, emit, transport: transport, "
            "storage: storage)\n"
            for index, effect_id in enumerate(effect_ids)
        )
        appear = ", ".join(swift_string(item) for item in self.appear)
        submits = ", ".join(swift_string(item) for item in self.submit_effects)
        task = "" if not self.appear else "        .task { await appear() }\n"
        return (
            HEADER + f"// Screen {self.screen['name']} from {self.screen['module']}.\n"
            "import SwiftUI\n\n"
            f"public struct {self.state_type}: Equatable {{\n"
            + ("\n".join(state_lines) + "\n\n" if state_lines else "")
            + "    public init() {}\n}\n\n"
            + params_struct
            + f"\npublic enum {self.action_type}: Equatable {{\n"
            + ("\n".join(cases) + "\n\n" if cases else "")
            + "    public var intent: Intent {\n"
            + intent_body
            + "\n    }\n\n"
            "    public init?(intent: Intent) {\n" + decode_body + "\n    }\n}\n\n"
            f"public struct {self.type}: View, SankaScreen {{\n"
            f"    public typealias ScreenState = {self.state_type}\n"
            f"    public typealias ScreenParams = {self.params_type}\n\n"
            f"    public static let screenName = {swift_string(self.screen['name'])}\n"
            f"    public static let module = {swift_string(self.screen['module'])}\n"
            f"    public static let appearEffects: [String] = [{appear}]\n"
            f"    public static let submitEffects: [String] = [{submits}]\n"
            f"    public static var initialState: {self.state_type} {{ {self.state_type}() }}\n"
            f"    public static var sampleParams: {self.params_type} {{\n"
            f"        {self.params_type}({sample_arguments})\n    }}\n"
            "    public static var sampleParamsJSON: JSONValue {\n"
            f"        .object([{sample_json or ':'}])\n    }}\n\n"
            f"    @State private var state = {self.state_type}()\n"
            "    @Environment(\\.sankaNavigator) private var navigator\n"
            "    @Environment(\\.sankaTransport) private var transport\n"
            "    @Environment(\\.sankaStorage) private var storage\n"
            f"    private let params: {self.params_type}\n\n"
            f"    public init(params: {self.params_type}) {{\n"
            "        self.params = params\n    }\n\n"
            f"    public static func apply(_ action: {self.action_type}, "
            f"to state: inout {self.state_type}) {{\n" + reduce_body + "\n    }\n\n"
            f"    public static func apply(intent: Intent, to state: inout {self.state_type})"
            " -> Bool {\n"
            f"        guard let action = {self.action_type}(intent: intent) else {{\n"
            "            return false\n        }\n"
            "        apply(action, to: &state)\n        return true\n    }\n\n"
            "    @MainActor\n"
            "    static func runEffect(\n"
            "        _ effect: String,\n"
            f"        read: () -> {self.state_type},\n"
            f"        params: {self.params_type},\n"
            f"        emit: ({self.action_type}) -> Void,\n"
            "        transport: SankaTransport,\n"
            "        storage: SankaStorage\n"
            "    ) async {\n"
            "        switch effect {\n" + run_cases + "        default:\n            break\n"
            "        }\n    }\n\n"
            "    @MainActor\n"
            "    public static func run(\n"
            "        effect: String,\n"
            f"        state: inout {self.state_type},\n"
            f"        params: {self.params_type},\n"
            "        transport: SankaTransport,\n"
            "        storage: SankaStorage\n"
            "    ) async -> [Intent] {\n"
            "        var current = state\n"
            "        var raised: [Intent] = []\n"
            "        await runEffect(\n"
            "            effect,\n"
            "            read: { current },\n"
            "            params: params,\n"
            "            emit: { action in\n"
            "                raised.append(action.intent)\n"
            "                apply(action, to: &current)\n"
            "            },\n"
            "            transport: transport,\n"
            "            storage: storage\n"
            "        )\n"
            "        state = current\n"
            "        return raised\n    }\n\n"
            "    @MainActor\n"
            "    private func perform(effect: String) async {\n"
            "        await Self.runEffect(\n"
            "            effect,\n"
            "            read: { state },\n"
            "            params: params,\n"
            "            emit: { action in dispatch([action]) },\n"
            "            transport: transport,\n"
            "            storage: storage\n"
            "        )\n    }\n\n"
            "    @MainActor\n"
            "    private func appear() async {\n"
            "        for effect in Self.appearEffects {\n"
            "            await perform(effect: effect)\n"
            "        }\n    }\n\n"
            f"    private func dispatch(_ actions: [{self.action_type}]) {{\n"
            + dispatch_body
            + "\n    }\n\n"
            + "\n".join(handlers)
            + ("\n" if handlers else "")
            + "\n".join(effects)
            + ("\n" if effects else "")
            + "    public var body: some View {\n"
            + "\n".join(_indent(body, 2))
            + "\n"
            + task
            + "    }\n\n"
            f"    public static func tree(state: {self.state_type}, "
            f"params: {self.params_type}) -> Node {{\n"
            + "\n".join(_indent(tree.split("\n"), 2))
            + "\n    }\n}\n"
        )


def _node(role: str, **fields: str | None) -> str:
    arguments = [f"role: {swift_string(role)}"]
    for key in ("text", "label", "enabled", "checked", "action", "children"):
        value = fields.get(key)
        if value is not None:
            arguments.append(f"{key}: {value}")
    return f"Node({', '.join(arguments)})"


def _indent(lines: list[str], levels: int = 1) -> list[str]:
    prefix = "    " * levels
    return [prefix + line if line else line for line in lines]


def _grouped(units: list[list[str]]) -> list[str]:
    """Wrap more than ten stack children in ``Group`` blocks (the ViewBuilder limit)."""
    if len(units) <= MAX_STACK_CHILDREN:
        return [line for unit in units for line in unit]
    groups: list[list[str]] = []
    for start in range(0, len(units), MAX_STACK_CHILDREN):
        inner = [line for unit in units[start : start + MAX_STACK_CHILDREN] for line in unit]
        groups.append(["Group {", *_indent(inner), "}"])
    return _grouped(groups)


def _placeholder(
    screen: dict[str, Any], type_name: str, project: Project, reasons: list[dict[str, str]]
) -> ScreenOutput:
    params = project.params_by_module[screen["module"]]
    params_lines = [f"    public var {name}: {swift_type(ty)}" for name, ty in params.items()]
    params_init = ", ".join(f"{name}: {swift_type(ty)}" for name, ty in params.items())
    params_assign = "".join(f"\n        self.{name} = {name}" for name in params)
    message = "; ".join(f"{reason['code']}: {reason['message']}" for reason in reasons)
    source = (
        HEADER + f"// Screen {screen['name']} from {screen['module']} needs manual adaptation.\n"
        "import SwiftUI\n\n"
        f"public struct {type_name}Params: Equatable {{\n"
        + ("\n".join(params_lines) + "\n\n" if params_lines else "")
        + (
            f"    public init({params_init}) {{{params_assign}\n    }}\n"
            if params_lines
            else "    public init() {}\n"
        )
        + "}\n\n"
        f"public struct {type_name}: View {{\n"
        f"    public static let adaptationReasons = {swift_string(message)}\n\n"
        f"    public init(params: {type_name}Params) {{}}\n\n"
        "    public var body: some View {\n"
        '        Text(verbatim: "Needs manual adaptation: " + Self.adaptationReasons)\n'
        "            .padding()\n"
        "    }\n}\n"
    )
    return ScreenOutput(source=source, styles="", native=False)


def _styles_file(outputs: dict[str, ScreenOutput], assets: dict[str, str]) -> str:
    sections = [outputs[name].styles for name in sorted(outputs) if outputs[name].styles]
    runtime = styles_runtime() + (ASSET_IMAGE_SWIFT if assets else "")
    return runtime + ("\n" + "\n".join(sections) if sections else "")


def _models_file(models: dict[str, Ty]) -> str:
    blocks = []
    for name in sorted(models):
        ty = models[name]
        conformance = "Codable, Equatable" if ty.model else "Equatable"
        fields = [
            f"    public var {field_name}: {swift_type(item)}" for field_name, item in ty.fields
        ]
        init_params = ", ".join(
            f"{field_name}: {swift_type(item)}" for field_name, item in ty.fields
        )
        assigns = "".join(
            f"\n        self.{field_name} = {field_name}" for field_name, _ in ty.fields
        )
        json_required = ", ".join(
            f"{swift_string(field_name)}: {emit_json(field_name, item)}"
            for field_name, item in ty.fields
            if not item.optional
        )
        json_optional = "".join(
            f"        if let {field_name} = {field_name} {{\n"
            f"            fields[{swift_string(field_name)}] = "
            f"{emit_json(field_name, required(item))}\n        }}\n"
            for field_name, item in ty.fields
            if item.optional
        )
        decodes = ""
        for field_name, item in ty.fields:
            key = swift_string(field_name)
            if item.optional:
                decoded = emit_decode("raw", required(item), optional=False)
                decodes += (
                    f"        var {field_name}: {swift_type(item)} = nil\n"
                    f"        if let raw = fields[{key}] {{\n"
                    f"            guard let value = {decoded} else {{ return nil }}\n"
                    f"            {field_name} = value\n        }}\n"
                )
            else:
                decoded = emit_decode(f"fields[{key}]", item)
                decodes += (
                    f"        guard let {field_name} = {decoded} else {{\n"
                    "            return nil\n        }\n"
                )
        init_call = ", ".join(f"{field_name}: {field_name}" for field_name, _ in ty.fields)
        blocks.append(
            f"public struct {name}: {conformance} {{\n"
            + ("\n".join(fields) + "\n\n" if fields else "")
            + f"    public init({init_params}) {{{assigns}\n    }}\n\n"
            "    public var json: JSONValue {\n"
            f"        var fields: [String: JSONValue] = [{json_required or ':'}]\n"
            + json_optional
            + "        return .object(fields)\n"
            "    }\n\n"
            "    public init?(json: JSONValue) {\n"
            "        guard let fields = json.objectValue else { return nil }\n"
            + ("        _ = fields\n" if not ty.fields else decodes)
            + f"        self.init({init_call})\n"
            "    }\n}\n"
        )
    return (
        HEADER + "// Value types: declared response models (Codable) and shapes inferred from "
        "literal state.\n\n" + "\n".join(blocks)
    )


def _navigation_file(captured: dict[str, Any], project: Project) -> str:
    cases = []
    intents = []
    destinations = []
    for route in project.routes:
        if route.params:
            signature = ", ".join(f"{name}: {swift_type(ty)}" for name, ty in route.params.items())
            binding = ", ".join(f"let {name}" for name in route.params)
            cases.append(f"    case {route.case}({signature})")
            pattern = f".{route.case}({binding})"
            values = ", ".join(
                f"{swift_string(name)}: {emit_json(name, ty)}" for name, ty in route.params.items()
            )
            params_code = f"[{values}]"
            arguments = ", ".join(f"{name}: {name}" for name in route.params)
        else:
            cases.append(f"    case {route.case}")
            pattern = f".{route.case}"
            params_code = "[:]"
            arguments = ""
        if project.navigation == "react-navigation":
            intent = f".navigate({swift_string(route.name)}, {params_code})"
        else:
            intent = f".push({swift_string(route.pattern or '/')}, {params_code})"
        intents.append(f"        case {pattern}:\n            return {intent}")
        view = _screen_view(route, project, arguments)
        destinations.append(f"        case {pattern}:\n            {view}")
    root_name = next(
        navigator["name"]
        for navigator in captured["navigators"]
        if not any(
            screen.get("navigator") == navigator["name"]
            for other in captured["navigators"]
            for screen in other["screens"]
        )
    )
    root_view = _navigator_view(root_name, captured["navigators"], project)
    return (
        HEADER + "// Navigation graph: one NavigationStack whose typed routes cover every screen.\n"
        "import SwiftUI\n\n"
        "public enum Route: Hashable {\n"
        + "\n".join(cases)
        + "\n\n    public var intent: Intent {\n        switch self {\n"
        + "\n".join(intents)
        + "\n        }\n    }\n}\n"
        + NAVIGATION_RUNTIME
        + "\npublic struct AppRoot: View {\n"
        "    @State private var path: [Route] = []\n\n"
        "    public init() {}\n\n"
        "    public var body: some View {\n"
        "        NavigationStack(path: $path) {\n"
        + "\n".join(_indent(root_view, 3))
        + "\n                .navigationDestination(for: Route.self) { route in\n"
        "                    destination(route)\n"
        "                }\n"
        "        }\n"
        "        .environment(\n"
        "            \\.sankaNavigator,\n"
        "            SankaNavigator(\n"
        "                navigate: { route in path.append(route) },\n"
        "                back: { if !path.isEmpty { path.removeLast() } }\n"
        "            )\n"
        "        )\n"
        "    }\n\n"
        "    @ViewBuilder\n"
        "    private func destination(_ route: Route) -> some View {\n"
        "        switch route {\n" + "\n".join(destinations) + "\n        }\n    }\n}\n"
    )


def _screen_view(route: RouteInfo, project: Project, arguments: str) -> str:
    type_name = project.screen_types[route.module]
    title = "nil" if route.title is None else swift_string(route.title)
    visible = "true" if route.header else "false"
    return (
        f"{type_name}(params: {type_name}Params({arguments}))"
        f".sankaHeader(title: {title}, visible: {visible})"
    )


def _navigator_view(name: str, navigators: list[dict[str, Any]], project: Project) -> list[str]:
    navigator = next(item for item in navigators if item["name"] == name)
    screens = navigator["screens"]
    if navigator["kind"] == "stack":
        initial = next(screen for screen in screens if screen["name"] == navigator["initial"])
        return _entry_view(initial, navigators, project)
    lines = ["TabView {"]
    for screen in screens:
        entry = _entry_view(screen, navigators, project)
        title = screen.get("title") or screen["name"]
        lines.extend(_indent(entry))
        lines.append(f"        .tabItem {{ Text(verbatim: {swift_string(str(title))}) }}")
    lines.append("}")
    return lines


def _entry_view(
    screen: dict[str, Any], navigators: list[dict[str, Any]], project: Project
) -> list[str]:
    if "navigator" in screen:
        return _navigator_view(screen["navigator"], navigators, project)
    route = next(route for route in project.routes if route.module == screen["module"])
    sample = sample_params({name: ty.kind for name, ty in route.params.items()})
    arguments = ", ".join(
        f"{name}: {emit({'lit': sample[name]}, Env({}, {}), ty)}"
        for name, ty in route.params.items()
    )
    return [_screen_view(route, project, arguments)]


def _dump_file(native_types: list[str]) -> str:
    calls = "".join(
        f"documents.append(contentsOf: await {name}.replay(fixtures: fixtures))\n"
        for name in native_types
    )
    return (
        HEADER + "// Prints the normalized tree of every native screen and scenario as JSON. The\n"
        "// optional second argument is verify-cases.json with fixture responses and storage.\n"
        "import AppUI\n"
        "import Foundation\n\n"
        "let arguments = CommandLine.arguments\n"
        "guard arguments.count == 2 || arguments.count == 3 else {\n"
        "    FileHandle.standardError.write(\n"
        '        Data("usage: sanka-tree-dump <output.json> [verify-cases.json]\\n".utf8)\n'
        "    )\n"
        "    exit(2)\n"
        "}\n"
        "var fixtures = SankaFixtures.empty\n"
        "if arguments.count == 3 {\n"
        "    do {\n"
        "        fixtures = try SankaFixtures.load(path: arguments[2])\n"
        "    } catch {\n"
        '        FileHandle.standardError.write(Data("sanka-tree-dump: \\(error)\\n".utf8))\n'
        "        exit(1)\n"
        "    }\n"
        "}\n"
        "var documents: [JSONValue] = []\n"
        + calls
        + 'let encoded = JSONValue.array(documents).encoded + "\\n"\n'
        "do {\n"
        "    try Data(encoded.utf8).write(to: URL(fileURLWithPath: arguments[1]))\n"
        "} catch {\n"
        '    FileHandle.standardError.write(Data("sanka-tree-dump: \\(error)\\n".utf8))\n'
        "    exit(1)\n"
        "}\n"
    )


def _package_file(min_ios: str, assets: dict[str, str]) -> str:
    tops = sorted({path.split("/", 1)[0] for path in assets})
    resources = ", ".join(f'.copy("Resources/{top}")' for top in tops)
    target = (
        f'        .target(name: "AppUI", resources: [{resources}]),'
        if resources
        else '        .target(name: "AppUI"),'
    )
    return (
        f"// swift-tools-version: {SWIFT_TOOLS_VERSION}\n"
        "// Generated by sanka/react-native-to-native; no external dependencies.\n"
        "import PackageDescription\n\n"
        "let package = Package(\n"
        '    name: "AppUI",\n'
        f"    platforms: [.iOS({IOS_VERSIONS[min_ios]}), .macOS(.v14)],\n"
        "    products: [\n"
        '        .library(name: "AppUI", targets: ["AppUI"]),\n'
        '        .executable(name: "sanka-tree-dump", targets: ["SankaTreeDump"]),\n'
        "    ],\n"
        "    targets: [\n"
        + target
        + '\n        .executableTarget(name: "SankaTreeDump", dependencies: ["AppUI"]),\n'
        "    ]\n"
        ")\n"
    )


def _app_file(app_name: str) -> str:
    return (
        HEADER + "import AppUI\nimport SwiftUI\n\n"
        "@main\n"
        f"struct {app_name}App: App {{\n"
        "    var body: some Scene {\n"
        "        WindowGroup {\n"
        "            AppRoot()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )


def _project_yml(captured: dict[str, Any], app_name: str, min_ios: str) -> str:
    return (
        f"# Generated by sanka/react-native-to-native for XcodeGen {XCODEGEN_VERSION}:\n"
        "#   xcodegen generate --spec App/project.yml\n"
        "# No pbxproj is generated; the spec is the deterministic source of the iOS shell.\n"
        f"name: {app_name}\n"
        "options:\n"
        "  deploymentTarget:\n"
        f'    iOS: "{min_ios}"\n'
        f'  xcodeVersion: "{MIN_XCODE}"\n'
        "packages:\n"
        "  AppUI:\n"
        "    path: ..\n"
        "targets:\n"
        f"  {app_name}:\n"
        "    type: application\n"
        "    platform: iOS\n"
        "    sources:\n"
        "      - path: .\n"
        '        excludes: ["project.yml"]\n'
        "    dependencies:\n"
        "      - package: AppUI\n"
        "        product: AppUI\n"
        "    settings:\n"
        "      base:\n"
        f"        PRODUCT_BUNDLE_IDENTIFIER: {_bundle_id(captured['app'])}\n"
        "        GENERATE_INFOPLIST_FILE: YES\n"
    )


def _readme(
    captured: dict[str, Any], dispositions: list[dict[str, Any]], native_types: list[str]
) -> str:
    pending = [
        f"- `{item['module']}`: "
        + "; ".join(
            f"{reason['code']}: {reason['message']}" for reason in item["adaptation_reasons"]
        )
        for item in dispositions
        if item["disposition"] != "native-screen"
    ]
    return (
        f"# {captured['app']['display_name']} (experimental SwiftUI migration)\n\n"
        "Generated by `sanka/react-native-to-native` from a captured React Native application\n"
        f"({captured['navigation']}). `contract.json` is the exact capture. This is not a\n"
        "complete app migration: only slice-1 screens (navigation, static trees, literal\n"
        "styles, local state) are reproduced, and structural parity is verified per\n"
        "screen, not pixels.\n\n"
        "## Layout\n\n"
        "- `Package.swift`: library `AppUI` (iOS and macOS) and the `sanka-tree-dump`\n"
        "  executable; no external dependencies.\n"
        "- `Sources/AppUI/Navigation.swift`: `enum Route` with typed parameters, the\n"
        "  `NavigationStack` root (`AppRoot`) and the navigator environment.\n"
        "- `Sources/AppUI/Screens/<Name>.swift`: per screen a `<Name>State` value, a\n"
        "  `<Name>Params` value, an `enum <Name>Action`, the reducer `apply(_:to:)`, the\n"
        "  SwiftUI `body` and `tree(state:params:)`, the normalized tree of any state.\n"
        "- `Sources/AppUI/Styles.swift`: `StyleSheet` entries as `SankaStyle` values applied\n"
        "  by one bounded modifier; `Sources/AppUI/Tree.swift`: tree, intent and JSON types.\n"
        "- `Sources/AppUI/Models.swift`: declared response models (Codable) and value types\n"
        "  inferred from literal state, when any.\n"
        "- `Sources/AppUI/APIClient.swift`: `SankaTransport` (URLSession) and `SankaStorage`\n"
        "  (UserDefaults) protocols with fixture implementations; screens run their effects\n"
        "  through the environment values `sankaTransport` and `sankaStorage`.\n"
        "- `Sources/AppUI/Resources/`: image assets copied byte for byte.\n"
        "- `App/`: the iOS app shell as an XcodeGen spec (`project.yml`, pinned to XcodeGen\n"
        f"  {XCODEGEN_VERSION}, Xcode {MIN_XCODE} or later); run `xcodegen generate --spec\n"
        "  App/project.yml` to produce the project. No `pbxproj` is generated.\n\n"
        "```sh\n"
        "swift build\n"
        "swift run sanka-tree-dump tree.json\n"
        "```\n\n"
        "## Parity harness\n\n"
        "`sanka-tree-dump` prints, for every native screen, the normalized tree of the initial\n"
        "state and of the state after each state-changing button press, switch toggle and\n"
        "text-field edit. The trees come from the generated `tree(state:params:)` functions\n"
        "and the reducers, not from rendering SwiftUI, so no XCTest, ViewInspector or\n"
        "simulator is required. `sanka verify` renders the React Native source screens with\n"
        "the same scenarios and compares canonical JSON. Layout, colors, fonts and pixels are\n"
        "not compared. Navigation is compared as typed intents, never by rendering the\n"
        "destination.\n\n"
        "## Not reproduced\n\n"
        "- Flexbox layout is approximated with stacks, spacers and frames; percentages,\n"
        "  `Dimensions` and platform branches are not captured.\n"
        "- `keyboardType`, `autoCapitalize`, `ActivityIndicator` size/color and\n"
        "  `resizeMode` beyond fit/fill are ignored.\n"
        "- Navigating to a screen that is already on the stack pushes it again.\n"
        "- The iOS app shell is not built by the replay; open it with Xcode after XcodeGen.\n"
        "- Effects apply state changes as they happen; a request in flight is not cancelled\n"
        "  when the screen disappears.\n"
        + (
            "\n## Screens needing manual adaptation\n\n" + "\n".join(pending) + "\n"
            if pending
            else ""
        )
        + f"\nNative screens: {', '.join(native_types) if native_types else 'none'}.\n"
    )
