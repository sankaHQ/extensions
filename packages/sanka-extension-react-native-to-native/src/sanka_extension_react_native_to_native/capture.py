# SPDX-License-Identifier: Apache-2.0
"""Static, fail-closed capture of a React Native application's screens and navigation.

Source files are parsed through the vendored TypeScript syntax driver and never
imported or executed. Every screen receives a disposition; every construct outside
the envelope is a reason code, never a silent omission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from sanka_ts_capture import TYPESCRIPT_SHA256, TYPESCRIPT_VERSION, ParsedFile, parse_sources

from .expo_router import capture_routes, href_matches, route_files, route_patterns
from .expressions import Unsupported
from .inventory import inventory
from .navigation import capture_navigation
from .screens import capture_screen, imports

SOURCES = ("react-native",)
TARGETS = ("swiftui", "compose")
NAVIGATION = ("react-navigation", "expo-router")
VERSION = "0.1.0a1"
IGNORED = frozenset({".git", ".sanka", "node_modules", ".expo", "dist", "build", "Pods", ".gradle"})
SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
TOOLING_FILES = frozenset(
    {
        "index.js",
        "babel.config.js",
        "metro.config.js",
        "jest.config.js",
        "eslint.config.js",
        ".eslintrc.js",
        ".prettierrc.js",
        "react-native.config.js",
        "app.config.ts",
        "app.config.js",
        "tailwind.config.js",
    }
)
MAX_SOURCE_BYTES = 20_000_000
MAX_SOURCE_FILES = 3000
MAX_MODULES = 200
MIN_IOS = ("16.0", "17.0", "18.0")
MIN_SDK = ("24", "26", "28")
_BUNDLE_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_RN_MAJOR = re.compile(r"\A\s*[\^~]?\s*v?0\.(\d+)")


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def configuration(raw: dict[str, Any]) -> dict[str, str]:
    allowed = {
        "source_framework",
        "target_framework",
        "target",
        "navigation",
        "entry",
        "bundle_id",
        "package_name",
        "min_ios",
        "min_sdk",
        "extension_plan_hash",
    }
    if set(raw) - allowed:
        raise ValueError("unknown configuration fields: " + ", ".join(sorted(set(raw) - allowed)))
    if "target" in raw and "target_framework" in raw and raw["target"] != raw["target_framework"]:
        raise ValueError("target and target_framework must agree")
    result = {
        "source_framework": raw.get("source_framework", "react-native"),
        "target_framework": raw.get("target_framework", raw.get("target", "swiftui")),
        "navigation": raw.get("navigation", ""),
        "entry": raw.get("entry", ""),
        "bundle_id": raw.get("bundle_id", ""),
        "package_name": raw.get("package_name", ""),
        "min_ios": raw.get("min_ios", "17.0"),
        "min_sdk": raw.get("min_sdk", "26"),
    }
    if any(type(value) is not str for value in result.values()):
        raise ValueError("configuration values must be strings")
    if result["source_framework"] not in SOURCES:
        raise ValueError("source_framework must be react-native")
    if result["target_framework"] not in TARGETS:
        raise ValueError("target_framework must be swiftui or compose")
    if result["navigation"] and result["navigation"] not in NAVIGATION:
        raise ValueError("navigation must be react-navigation or expo-router")
    if result["entry"] and not _relative_module(result["entry"]):
        raise ValueError("entry must be a relative .tsx or .ts module inside the project")
    for key in ("bundle_id", "package_name"):
        if result[key] and not _BUNDLE_ID.fullmatch(result[key]):
            raise ValueError(f"{key} must be a reverse-DNS identifier")
    if result["min_ios"] not in MIN_IOS:
        raise ValueError("min_ios must be one of " + ", ".join(MIN_IOS))
    if result["min_sdk"] not in MIN_SDK:
        raise ValueError("min_sdk must be one of " + ", ".join(MIN_SDK))
    return result


def _relative_module(value: str) -> bool:
    pure = PurePosixPath(value)
    return (
        not pure.is_absolute()
        and bool(pure.parts)
        and not any(part in {"", ".", ".."} for part in pure.parts)
        and pure.suffix in {".tsx", ".ts"}
        and value == pure.as_posix()
    )


def capture(root: Path, config: dict[str, str]) -> dict[str, Any]:
    records, modules = _walk(root)
    files = frozenset(records)
    gaps: list[str] = []
    package = _package(root, gaps)
    app = _app(root, package, config)
    profile = config["navigation"] or _detect(package, files)
    if profile is None:
        gaps.append(
            "navigation: neither an Expo Router app/ directory nor a React Navigation "
            "App.tsx was found"
        )
    dependencies = package.get("dependencies") or {}
    react_native = str(dependencies.get("react-native", ""))
    match = _RN_MAJOR.match(react_native)
    if match is None or int(match.group(1)) < 76:
        gaps.append(
            f"package.json: react-native {react_native!r} is not a qualified version "
            "(0.76 or later)"
        )
    result: dict[str, Any] = {
        "schema": "sanka.react-native.capture/v1",
        "source_digest": digest(records),
        "configuration": config,
        "typescript": {"version": TYPESCRIPT_VERSION, "sha256": TYPESCRIPT_SHA256},
        "app": app,
        "navigation": profile,
        "navigators": [],
        "screens": [],
        "inventory": inventory(root, package),
        "gaps": [],
        "readiness": 0.0,
        "scope": "navigation graph, screen trees, literal styles and local state (slice 1)",
        "complete_app": False,
    }
    entries: list[str] = []
    if profile == "react-navigation":
        entry = config["entry"] or "App.tsx"
        if entry not in files:
            gaps.append(f"{entry}: entry module not found")
        else:
            entries.append(entry)
    elif profile == "expo-router":
        if config["entry"]:
            gaps.append("entry: Expo Router apps derive their entry from the app/ directory")
        entries.extend(route_files(files))
        if not entries:
            gaps.append("app/: no route files found")
    if entries and not gaps:
        graph, texts = _load(root, entries, modules, gaps)
        try:
            if profile == "react-navigation":
                _react_navigation(result, graph, texts, entries[0], files, gaps)
            else:
                _expo_router(result, graph, texts, files, gaps)
        except Unsupported as error:
            gaps.append(f"{error.code}: {error.message}")
        referenced = set(graph)
        for module in sorted(modules):
            name = PurePosixPath(module).name
            if (
                module in referenced
                or name in TOOLING_FILES
                or "__tests__" in module
                or ".test." in name
                or ".spec." in name
            ):
                continue
            gaps.append(f"{module}: additional modules require whole-project capture")
    native = sum(screen["disposition"] == "native-screen" for screen in result["screens"])
    result["readiness"] = native / len(result["screens"]) if result["screens"] else 0.0
    if not result["screens"]:
        gaps.append("no qualified screens")
    result["gaps"] = sorted(set(gaps))
    return result


def _walk(root: Path) -> tuple[dict[str, str], list[str]]:
    records: dict[str, str] = {}
    modules: list[str] = []
    total = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED)
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("source symlinks are unsupported")
        for name in sorted(filenames):
            source = Path(directory) / name
            if source.is_symlink() or not source.is_file():
                raise ValueError("only regular source files are supported")
            rel = source.relative_to(root).as_posix()
            total += source.stat().st_size
            if total > MAX_SOURCE_BYTES or len(records) >= MAX_SOURCE_FILES:
                raise ValueError("source exceeds experimental capture limits")
            records[rel] = hashlib.sha256(source.read_bytes()).hexdigest()
            top = rel.split("/", 1)[0]
            if source.suffix.lower() in SOURCE_SUFFIXES and top not in {"ios", "android"}:
                modules.append(rel)
    return records, modules


def _package(root: Path, gaps: list[str]) -> dict[str, Any]:
    path = root / "package.json"
    if path.is_symlink() or not path.is_file():
        gaps.append("package.json: required to identify the React Native version")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        gaps.append("package.json: invalid JSON")
        return {}
    if not isinstance(data, dict):
        gaps.append("package.json: must be an object")
        return {}
    return data


def _app(root: Path, package: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    name = str(package.get("name", ""))
    display = ""
    bundle_id = config["bundle_id"]
    package_name = config["package_name"]
    path = root / "app.json"
    if path.is_file() and not path.is_symlink():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        expo = data.get("expo") if isinstance(data, dict) else None
        if isinstance(expo, dict):
            display = str(expo.get("name", display))
            ios = expo.get("ios") if isinstance(expo.get("ios"), dict) else {}
            android = expo.get("android") if isinstance(expo.get("android"), dict) else {}
            bundle_id = (
                bundle_id or str(ios.get("bundleIdentifier", ""))
                if isinstance(ios, dict)
                else bundle_id
            )
            package_name = (
                package_name or str(android.get("package", ""))
                if isinstance(android, dict)
                else package_name
            )
        elif isinstance(data, dict):
            display = str(data.get("displayName", data.get("name", display)))
    return {
        "name": name,
        "display_name": display or name,
        "bundle_id": bundle_id,
        "package_name": package_name,
        "react_native": str((package.get("dependencies") or {}).get("react-native", "")),
        "expo": str((package.get("dependencies") or {}).get("expo", "")) or None,
    }


def _detect(package: dict[str, Any], files: frozenset[str]) -> str | None:
    dependencies = package.get("dependencies") or {}
    if "expo-router" in dependencies and any(path.startswith("app/") for path in files):
        return "expo-router"
    if "@react-navigation/native" in dependencies and "App.tsx" in files:
        return "react-navigation"
    return None


def _resolve(importer: str, specifier: str, modules: list[str]) -> str | None:
    base = PurePosixPath(importer).parent
    parts = list(base.parts)
    for segment in specifier.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(segment)
    candidate = "/".join(parts)
    for suffix in ("", ".tsx", ".ts", ".jsx", ".js", "/index.tsx", "/index.ts", "/index.js"):
        if candidate + suffix in modules:
            return candidate + suffix
    return None


def _load(
    root: Path, entries: list[str], modules: list[str], gaps: list[str]
) -> tuple[dict[str, tuple[ParsedFile, dict[str, str]]], dict[str, str]]:
    """Parse the module graph reachable from the entries through relative imports."""
    texts: dict[str, str] = {}
    pending = list(entries)
    order: list[str] = []
    while pending:
        rel = pending.pop(0)
        if rel in texts:
            continue
        if len(texts) >= MAX_MODULES:
            gaps.append(f"{rel}: module graph exceeds {MAX_MODULES} modules")
            break
        texts[rel] = (root / rel).read_text(encoding="utf-8")
        order.append(rel)
        parsed_now = parse_sources({rel: texts[rel]})[rel]
        if parsed_now.diagnostics:
            first = parsed_now.diagnostics[0]
            gaps.append(f"{rel}: syntax error {first.code}: {first.message}")
            continue
        names, _ = imports(parsed_now, rel)
        for specifier in sorted(set(names.values())):
            if not specifier.startswith("."):
                continue
            resolved = _resolve(rel, specifier, modules)
            if resolved is None:
                gaps.append(f"{rel}: import {specifier!r} does not resolve to a project module")
                continue
            pending.append(resolved)
    parsed = parse_sources(texts) if texts else {}
    graph: dict[str, tuple[ParsedFile, dict[str, str]]] = {}
    for rel in order:
        item = parsed[rel]
        names, _ = imports(item, rel)
        graph[rel] = (item, names)
    return graph, texts


def _react_navigation(
    result: dict[str, Any],
    graph: dict[str, tuple[ParsedFile, dict[str, str]]],
    texts: dict[str, str],
    entry: str,
    files: frozenset[str],
    gaps: list[str],
) -> None:
    parsed, imported = graph[entry]
    _, import_gaps = imports(parsed, entry)
    for gap in import_gaps:
        gaps.append(f"{gap['code']}: {gap['message']}")
    captured = capture_navigation(parsed, texts[entry], entry, imported)
    modules_by_component: dict[str, str] = {}
    for navigator in captured["navigators"]:
        for screen in navigator["screens"]:
            specifier = screen.pop("import", None)
            if specifier is None:
                continue
            resolved = _resolve(entry, specifier, sorted(graph))
            if resolved is None:
                gaps.append(
                    f"{entry}: screen {screen['name']!r} imports an unresolved module {specifier!r}"
                )
                continue
            screen["module"] = resolved
            modules_by_component[screen["component"]] = resolved
    result["navigators"] = captured["navigators"]
    result["screens"] = _screens(result["navigators"], graph, texts, "react-navigation", files)


def _expo_router(
    result: dict[str, Any],
    graph: dict[str, tuple[ParsedFile, dict[str, str]]],
    texts: dict[str, str],
    files: frozenset[str],
    gaps: list[str],
) -> None:
    layouts = {
        rel: (parsed, texts[rel], imported)
        for rel, (parsed, imported) in graph.items()
        if PurePosixPath(rel).stem == "_layout" and rel.startswith("app/")
    }
    navigators, route_gaps = capture_routes(files, layouts)
    gaps.extend(route_gaps)
    result["navigators"] = navigators
    result["screens"] = _screens(navigators, graph, texts, "expo-router", files)
    patterns = route_patterns(navigators)
    for screen in result["screens"]:
        for target in _push_targets(screen.get("tree")):
            if not href_matches(target, patterns):
                screen["disposition"] = "needs-manual-adaptation"
                screen["adaptation_reasons"].append(
                    {
                        "code": "SANKA_RN_EXPO_DYNAMIC_ROUTE",
                        "message": f"{screen['module']}: href {target!r} matches no route",
                    }
                )


def _push_targets(node: Any) -> list[str]:
    targets: list[str] = []
    if isinstance(node, dict):
        for actions in (node.get("events") or {}).values():
            for action in actions:
                if isinstance(action, dict) and "push" in action:
                    targets.append(str(action["push"]))
        for value in node.values():
            targets.extend(_push_targets(value))
    elif isinstance(node, list):
        for item in node:
            targets.extend(_push_targets(item))
    return targets


def _screens(
    navigators: list[dict[str, Any]],
    graph: dict[str, tuple[ParsedFile, dict[str, str]]],
    texts: dict[str, str],
    navigation_kind: str,
    files: frozenset[str],
) -> list[dict[str, Any]]:
    screens: list[dict[str, Any]] = []
    seen: set[str] = set()
    for navigator in navigators:
        for screen in navigator["screens"]:
            module = screen.get("module")
            if module is None or module in seen:
                continue
            seen.add(module)
            reasons: list[dict[str, str]] = []
            entry: dict[str, Any] = {
                "module": module,
                "name": screen.get("component") or PurePosixPath(module).stem,
            }
            if module not in graph:
                reasons.append(
                    {"code": "SANKA_RN_SCREEN", "message": f"{module}: module was not parsed"}
                )
            else:
                parsed, imported = graph[module]
                _, import_gaps = imports(parsed, module)
                reasons.extend(import_gaps)
                declared = {
                    key: value.rstrip("?") for key, value in (screen.get("params") or {}).items()
                }
                try:
                    captured = capture_screen(
                        parsed,
                        texts[module],
                        module,
                        navigation_kind=navigation_kind,
                        declared_params=declared,
                        imported=imported,
                        project_root_files=files,
                    )
                except Unsupported as error:
                    reasons.append({"code": error.code, "message": error.message})
                else:
                    entry.update(captured)
                    undeclared = set(captured["params"]) - set(declared)
                    if undeclared and navigation_kind == "expo-router":
                        message = (
                            f"{module}: parameters {sorted(undeclared)} are not route segments"
                        )
                        reasons.append({"code": "SANKA_RN_PARAM", "message": message})
            entry["disposition"] = "native-screen" if not reasons else "needs-manual-adaptation"
            entry["adaptation_reasons"] = reasons
            screens.append(entry)
    return sorted(screens, key=lambda item: str(item["module"]))
