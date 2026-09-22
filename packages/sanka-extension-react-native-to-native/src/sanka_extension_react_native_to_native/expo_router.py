# SPDX-License-Identifier: Apache-2.0
"""Expo Router recognition: the ``app/`` file tree, layouts and typed parameters."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from sanka_ts_capture import ParsedFile
from sanka_ts_capture import tree as t

from .expressions import Unsupported
from .navigation import SCREEN_OPTIONS
from .screens import default_export

ROUTE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js")
LAYOUT_TAGS = {"Stack": "stack", "Tabs": "tabs"}
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+\Z")
_PARAM = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]\Z")


def route_files(files: frozenset[str]) -> list[str]:
    return sorted(
        path
        for path in files
        if path.startswith("app/") and path.endswith(ROUTE_SUFFIXES) and "/" in path
    )


def capture_routes(
    files: frozenset[str], layouts: dict[str, tuple[ParsedFile, str, dict[str, str]]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return navigators derived from ``app/`` and project-level gaps."""
    gaps: list[str] = []
    directories: dict[str, dict[str, Any]] = {}
    for path in route_files(files):
        pure = PurePosixPath(path)
        stem = pure.stem
        directory = pure.parent.as_posix()
        if stem.startswith("+"):
            continue
        if stem == "_layout":
            continue
        if stem.startswith("[...") or "[..." in path:
            gaps.append(f"{path}: catch-all routes require additional capture")
            continue
        for segment in [*pure.parent.parts[1:], stem]:
            if not (_SEGMENT.fullmatch(segment) or _PARAM.fullmatch(segment) or _group(segment)):
                gaps.append(f"{path}: route segment {segment!r} is not qualified")
                break
        else:
            directories.setdefault(directory, {"files": []})["files"].append(path)
    for path in route_files(files):
        if PurePosixPath(path).stem == "_layout":
            directories.setdefault(PurePosixPath(path).parent.as_posix(), {"files": []})[
                "layout"
            ] = path
    if "app" not in directories:
        directories["app"] = {"files": []}
    navigators: list[dict[str, Any]] = []
    owners = sorted(
        directory
        for directory, info in directories.items()
        if "layout" in info or directory == "app"
    )
    for owner in owners:
        info = directories[owner]
        layout = info.get("layout")
        kind = "stack"
        options: dict[str, dict[str, Any]] = {}
        initial: str | None = None
        if layout is not None:
            parsed, source, imported = layouts[layout]
            try:
                kind, options, initial = capture_layout(parsed, source, layout, imported)
            except Unsupported as error:
                gaps.append(f"{layout}: {error.message}")
                continue
        screens: list[dict[str, Any]] = []
        for directory in sorted(directories):
            if not _owned_by(directory, owner, owners):
                continue
            if directory != owner and "layout" in directories[directory]:
                name = _relative(directory, owner)
                screens.append(
                    {
                        "name": name,
                        "navigator": directory,
                        "title": options.get(name, {}).get("title"),
                        "header": options.get(name, {}).get("headerShown", True),
                        "params": {},
                    }
                )
                continue
            for path in sorted(directories[directory]["files"]):
                pure = PurePosixPath(path)
                name = _relative(pure.with_suffix("").as_posix(), owner)
                params = {
                    match.group(1): "string"
                    for segment in [*pure.parent.parts[1:], pure.stem]
                    if (match := _PARAM.fullmatch(segment))
                }
                screens.append(
                    {
                        "name": name,
                        "module": path,
                        "route": _route(pure),
                        "title": options.get(name, {}).get("title"),
                        "header": options.get(name, {}).get("headerShown", True),
                        "params": params,
                    }
                )
        for name in options:
            if name not in {screen["name"] for screen in screens}:
                gaps.append(f"{layout}: <Screen name={name!r}> does not match a route")
        if not screens:
            gaps.append(f"{owner}: layout declares no routes")
            continue
        names = [screen["name"] for screen in screens]
        navigators.append(
            {
                "name": owner,
                "kind": kind,
                "initial": initial if initial in names else names[0],
                "screens": screens,
            }
        )
    return navigators, gaps


def _group(segment: str) -> bool:
    return (
        segment.startswith("(")
        and segment.endswith(")")
        and bool(_SEGMENT.fullmatch(segment[1:-1]))
    )


def _owned_by(directory: str, owner: str, owners: list[str]) -> bool:
    if directory == owner:
        return True
    if not directory.startswith(owner + "/"):
        return False
    for other in owners:
        if other != owner and other.startswith(owner + "/") and directory.startswith(other + "/"):
            return False
    return True


def _relative(path: str, owner: str) -> str:
    return path[len(owner) + 1 :] if path.startswith(owner + "/") else path


def _route(pure: PurePosixPath) -> str:
    segments = [segment for segment in [*pure.parent.parts[1:], pure.stem] if not _group(segment)]
    if segments and segments[-1] == "index":
        segments.pop()
    return "/" + "/".join(segments)


def capture_layout(
    parsed: ParsedFile, source: str, rel: str, imported: dict[str, str]
) -> tuple[str, dict[str, dict[str, Any]], str | None]:
    """Return ``(kind, per-screen options, initial route)`` for a ``_layout`` module."""
    name, function = default_export(parsed)
    if name is None or function is None:
        raise Unsupported("SANKA_RN_EXPO_LAYOUT", "layouts must default-export a component")
    body = t.field(function, "body")
    statements = (
        t.field_list(body, "statements") if body is not None and t.kind(body) == "Block" else []
    )
    returned = (
        t.field(statements[0], "expression")
        if len(statements) == 1 and t.kind(statements[0]) == "ReturnStatement"
        else (body if body is not None and t.kind(body) != "Block" else None)
    )
    if returned is None:
        raise Unsupported("SANKA_RN_EXPO_LAYOUT", "layouts must only return <Stack> or <Tabs>")
    element = t.unparenthesize(returned)
    opening = t.field(element, "openingElement") or element
    tag = t.property_chain(t.field(opening, "tagName") or {})
    if (
        tag is None
        or len(tag) != 1
        or tag[0] not in LAYOUT_TAGS
        or imported.get(tag[0]) != "expo-router"
    ):
        raise Unsupported(
            "SANKA_RN_EXPO_LAYOUT", "layouts must return <Stack> or <Tabs> from expo-router"
        )
    kind = LAYOUT_TAGS[tag[0]]
    initial: str | None = None
    for attribute in t.field_list(t.field(opening, "attributes") or {}, "properties"):
        key = (
            t.identifier_name(t.field(attribute, "name"))
            if t.kind(attribute) == "JsxAttribute"
            else None
        )
        value = t.field(attribute, "initializer")
        if key == "initialRouteName" and value is not None and t.string_value(value) is not None:
            initial = t.string_value(value)
            continue
        raise Unsupported(
            "SANKA_RN_EXPO_LAYOUT", f"layout prop {key!r} requires additional capture"
        )
    options: dict[str, dict[str, Any]] = {}
    for child in t.field_list(element, "children") if t.kind(element) == "JsxElement" else []:
        if t.kind(child) == "JsxText":
            if (t.text(child) or "").strip():
                raise Unsupported("SANKA_RN_EXPO_LAYOUT", "text inside a layout navigator")
            continue
        child_opening = t.field(child, "openingElement") or child
        if t.property_chain(t.field(child_opening, "tagName") or {}) != (tag[0], "Screen"):
            raise Unsupported("SANKA_RN_EXPO_LAYOUT", f"layouts may only contain <{tag[0]}.Screen>")
        screen_name: str | None = None
        screen_options: dict[str, Any] = {}
        for attribute in t.field_list(t.field(child_opening, "attributes") or {}, "properties"):
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
            if key == "name" and value is not None and t.string_value(value) is not None:
                screen_name = t.string_value(value)
            elif key == "options" and expression is not None:
                literal = t.unparenthesize(expression)
                encoded = literal.get("json")
                if t.kind(literal) != "ObjectLiteralExpression" or not isinstance(encoded, str):
                    raise Unsupported(
                        "SANKA_RN_EXPO_LAYOUT", "screen options must be a literal object"
                    )
                screen_options = json.loads(encoded)
                for option, item in screen_options.items():
                    expected = SCREEN_OPTIONS.get(option)
                    if expected is None or type(item) is not expected:
                        raise Unsupported(
                            "SANKA_RN_EXPO_LAYOUT",
                            f"screen option {option!r} requires additional capture",
                        )
            else:
                raise Unsupported(
                    "SANKA_RN_EXPO_LAYOUT", f"screen prop {key!r} requires additional capture"
                )
        if screen_name is None:
            raise Unsupported("SANKA_RN_EXPO_LAYOUT", "layout screens need a literal name")
        options[screen_name] = screen_options
    return kind, options, initial


def route_patterns(navigators: list[dict[str, Any]]) -> set[str]:
    return {
        screen["route"]
        for navigator in navigators
        for screen in navigator["screens"]
        if "route" in screen
    }


def href_matches(path: str, patterns: set[str]) -> bool:
    if path in patterns:
        return True
    segments = path.strip("/").split("/") if path.strip("/") else []
    for pattern in patterns:
        expected = pattern.strip("/").split("/") if pattern.strip("/") else []
        if len(expected) == len(segments) and all(
            _PARAM.fullmatch(want) is not None or want == got
            for want, got in zip(expected, segments, strict=True)
        ):
            return True
    return False
