# SPDX-License-Identifier: Apache-2.0
"""Conservative, target-independent capture; source code is never executed here."""

from __future__ import annotations

import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

VERSION = "0.1.0a1"
PARSER_VERSION = "3.28.2"
PARSER_SHA256 = "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd"
BOOT_VERSION = "3.5.16"
JAVA = Path(__file__).parent / "java"
ANNOTATIONS = {
    "SpringBootApplication": "org.springframework.boot.autoconfigure.SpringBootApplication",
    **{
        name: "org.springframework.web.bind.annotation." + name
        for name in (
            "RestController",
            "RequestMapping",
            "GetMapping",
            "RequestParam",
            "PathVariable",
        )
    },
}
IMPORTS = {
    *ANNOTATIONS.values(),
    "org.springframework.boot.SpringApplication",
    "java.util.Map",
    "java.util.List",
}
SCALARS = {"String": "string", "int": "integer", "long": "integer", "boolean": "boolean"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value: str) -> bool:
    return (
        bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value))
        and not keyword.iskeyword(value)
        and not value.startswith("model_")
    )


def inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    size = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        ignored = {".git", ".sanka", "target", ".venv", "__pycache__", ".pytest_cache"}
        dirs[:] = sorted(d for d in dirs if base != root or d not in ignored)
        for name in [*dirs, *sorted(files)]:
            path = base / name
            if path.is_symlink():
                raise ValueError(f"source contains a symlink: {path.relative_to(root)}")
            if path.is_file():
                size += path.stat().st_size
                if size > 2_000_000 or len(result) >= 200:
                    raise ValueError("source exceeds first-profile inventory limit")
                result[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return result


def pom_profile(root: Path) -> dict[str, str]:
    text = (root / "pom.xml").read_text()
    if "<!" in text:
        raise ValueError("POM declarations and entities are unsupported")
    node = ET.fromstring(text)
    for element in node.iter():
        element.tag = element.tag.removeprefix("{http://maven.apache.org/POM/4.0.0}")
        if element is not node and element.attrib:
            raise ValueError("POM attributes are unsupported")

    def fields(parent: ET.Element, allowed: set[str]) -> dict[str, ET.Element]:
        children = {child.tag: child for child in parent}
        if len(children) != len(parent) or set(children) - allowed:
            raise ValueError(f"unsupported or duplicate POM element under {parent.tag}")
        return children

    def leaf(parent: ET.Element, name: str) -> str:
        child = parent.find(name)
        if child is None or len(child):
            raise ValueError(f"missing or nonliteral POM {name}")
        return (child.text or "").strip()

    fields(
        node,
        {
            "modelVersion",
            "parent",
            "groupId",
            "artifactId",
            "version",
            "name",
            "description",
            "properties",
            "dependencies",
            "build",
        },
    )
    if node.tag != "project" or leaf(node, "modelVersion") != "4.0.0":
        raise ValueError("unsupported Maven model")
    parent = node.find("parent")
    if parent is None:
        raise ValueError("Spring Boot parent required")
    fields(parent, {"groupId", "artifactId", "version", "relativePath"})
    if [leaf(parent, k) for k in ("groupId", "artifactId", "version", "relativePath")] != [
        "org.springframework.boot",
        "spring-boot-starter-parent",
        BOOT_VERSION,
        "",
    ]:
        raise ValueError("requires pinned Spring Boot parent and empty relativePath")
    properties = node.find("properties")
    if properties is None:
        raise ValueError("explicit java.version required")
    fields(properties, {"java.version"})
    version = leaf(properties, "java.version")
    if version not in {"17", "21"}:
        raise ValueError("only Java 17 and 21 are qualified")
    deps = node.find("dependencies")
    names = []
    if deps is None:
        raise ValueError("Spring Web dependency required")
    for dep in deps:
        fields(dep, {"groupId", "artifactId", "scope"})
        name = leaf(dep, "artifactId")
        if (
            dep.tag != "dependency"
            or leaf(dep, "groupId") != "org.springframework.boot"
            or name not in {"spring-boot-starter-web", "spring-boot-starter-test"}
        ):
            raise ValueError(
                "unsupported dependency; security, persistence and other starters need adaptation"
            )
        scope = dep.findtext("scope", "compile").strip()
        if scope != ("test" if name.endswith("-test") else "compile"):
            raise ValueError("unsupported dependency scope")
        names.append(name)
    if sorted(names) != ["spring-boot-starter-test", "spring-boot-starter-web"]:
        raise ValueError("requires exactly the Web and test starters")
    build = node.find("build")
    if build is not None:
        fields(build, {"plugins"})
        plugins = build.find("plugins")
        if plugins is None or len(plugins) != 1:
            raise ValueError("only the Spring Boot Maven plugin is supported")
        plugin = plugins[0]
        fields(plugin, {"groupId", "artifactId"})
        if plugin.tag != "plugin" or [leaf(plugin, k) for k in ("groupId", "artifactId")] != [
            "org.springframework.boot",
            "spring-boot-maven-plugin",
        ]:
            raise ValueError("custom build plugins require adaptation")
    for name in ("groupId", "artifactId", "version", "name", "description"):
        if node.find(name) is not None and ("${" in leaf(node, name) or not leaf(node, name)):
            raise ValueError("project coordinates must be literal")
    return {"java": version, "spring_boot": BOOT_VERSION, "build": "maven"}


def parse(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    jar = JAVA / "javaparser-core.jar"
    if not jar.is_file() or hashlib.sha256(jar.read_bytes()).hexdigest() != PARSER_SHA256:
        raise ValueError("pinned JavaParser is missing or has the wrong digest")
    executable = shutil.which("java")
    if not executable:
        raise ValueError("a Java 21 JDK is required for static analysis")
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "JAVA_HOME", "SYSTEMROOT"}
    }
    try:
        run = subprocess.run(
            [
                executable,
                "--class-path",
                str(jar),
                str(JAVA / "Analyzer.java"),
                *[str(root / p) for p in paths],
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise ValueError(f"JavaParser analysis failed: {error}") from error
    return list(json.loads(run.stdout))


def read_annotations(values: list[dict[str, Any]], imports: set[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for value in values:
        name = value["name"]
        if name not in ANNOTATIONS or ANNOTATIONS[name] not in imports or name in result:
            raise ValueError(f"unresolved or unsupported annotation {name}")
        result[name] = value["values"]
    return result


def literal(node: dict[str, Any]) -> str:
    if node.get("kind") != "string":
        raise ValueError("annotation values must be literal strings")
    return str(node["value"])


def mapping(values: dict[str, Any]) -> str:
    if set(values) not in ({"value"}, {"path"}):
        raise ValueError("mapping requires exactly one literal path")
    path = literal(next(iter(values.values())))
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+|/\{[A-Za-z][A-Za-z0-9_]*\})+", path):
        raise ValueError("mapping contains unsupported path syntax")
    return path


def expression(
    node: dict[str, Any], parameters: set[str], records: dict[str, Any], imports: set[str]
) -> dict[str, Any]:
    kind = node.get("kind")
    if kind in {"string", "boolean", "null"}:
        return node
    if kind == "integer":
        raw = node["value"]
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:[lL])?", raw):
            raise ValueError("only nonnegative decimal integer literals are supported")
        value = int(raw.rstrip("lL"))
        if value > (2**63 - 1 if raw[-1:] in {"l", "L"} else 2**31 - 1):
            raise ValueError("integer literal exceeds Java type range")
        return {"kind": kind, "value": value}
    if kind == "name" and node["value"] in parameters:
        return node
    if kind == "plus":
        left = expression(node["left"], parameters, records, imports)
        right = expression(node["right"], parameters, records, imports)
        if any(item["kind"] not in {"string", "name", "plus"} for item in (left, right)):
            raise ValueError("only string concatenation is supported")
        return {"kind": kind, "left": left, "right": right}
    if (
        kind == "call"
        and node["scope"] in {"Map", "List"}
        and node["name"] == "of"
        and "java.util." + node["scope"] in imports
    ):
        args = [expression(arg, parameters, records, imports) for arg in node["args"]]
        if any(arg["kind"] == "null" for arg in args):
            raise ValueError("Map.of/List.of cannot contain null")
        if node["scope"] == "Map":
            if len(args) % 2 or len(args) > 20 or any(key["kind"] != "string" for key in args[::2]):
                raise ValueError("Map.of requires at most ten literal string keys")
            if len({key["value"] for key in args[::2]}) != len(args) // 2:
                raise ValueError("duplicate Map.of keys")
        return {"kind": node["scope"].lower(), "args": args}
    if kind == "new" and node["type"] in records:
        args = [expression(arg, parameters, records, imports) for arg in node["args"]]
        components = records[node["type"]]
        if len(args) != len(components):
            raise ValueError("record constructor arity mismatch")
        for arg, component in zip(args, components, strict=True):
            expected = SCALARS[component["type"]]
            actual = "string" if arg["kind"] in {"name", "plus"} else arg["kind"]
            if actual != expected and not (actual == "null" and expected == "string"):
                raise ValueError("record constructor type mismatch")
            if component["type"] == "int" and arg.get("value", 0) > 2**31 - 1:
                raise ValueError("record int exceeds int32")
        return {"kind": "record", "type": node["type"], "args": args}
    raise ValueError(f"unsupported expression: {kind}")


def capture(root: Path) -> dict[str, Any]:
    files = inventory(root)
    gaps: list[str] = []
    profile: dict[str, str] = {}
    try:
        profile = pom_profile(root)
    except (ValueError, OSError, ET.ParseError) as error:
        gaps.append(f"pom.xml: {error}")
    for name in files:
        if name.startswith("src/main/") and not (
            name.startswith("src/main/java/") and name.endswith(".java")
        ):
            gaps.append(f"{name}: resources or non-Java source require adaptation")
        if (
            name.startswith((".mvn/", "config/"))
            or name.endswith((".gradle", ".gradle.kts"))
            or re.fullmatch(r"application(?:-[^.]+)?\.(?:properties|yaml|yml)", name)
        ):
            gaps.append(f"{name}: custom build configuration requires adaptation")
    paths = sorted(
        name for name in files if name.startswith("src/main/java/") and name.endswith(".java")
    )
    units = parse(root, paths) if paths else []
    records: dict[str, Any] = {}
    declarations: list[tuple[dict[str, Any], dict[str, Any], set[str]]] = []
    seen: set[str] = set()
    for unit in units:
        location = str(Path(unit["file"]).relative_to(root))
        unit["file"] = location
        try:
            imports = {i["name"] for i in unit["imports"]}
            if not unit["plain"] or any(
                i["static"] or i["wildcard"] or i["name"] not in IMPORTS for i in unit["imports"]
            ):
                raise ValueError("wildcard, static or unresolved imports/package declarations")
            if len(unit["types"]) != 1 or not re.fullmatch(
                r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", unit["package"]
            ):
                raise ValueError("one public type in an explicit lowercase package is required")
            typ = unit["types"][0]
            name = typ["name"]
            if (
                not identifier(name)
                or name in seen
                or name
                in {
                    *SCALARS,
                    "Map",
                    "List",
                    "Object",
                    *ANNOTATIONS,
                    "SpringApplication",
                    "Request",
                    "FastAPI",
                    "BaseModel",
                    "bool",
                    "str",
                    "app",
                }
            ):
                raise ValueError("unsupported, duplicate or shadowed type name")
            if name.startswith("endpoint_"):
                raise ValueError("type name overlaps generated handler names")
            seen.add(name)
            if not typ["plain"] or typ["other_members"]:
                raise ValueError(
                    "fields, constructors, nested types, inheritance or initializers "
                    "need adaptation"
                )
            declarations.append((unit, typ, imports))
            if typ["kind"] == "RecordDeclaration":
                if typ["annotations"] or typ["methods"]:
                    raise ValueError("custom record behavior requires adaptation")
                for component in typ["components"]:
                    if (
                        component["type"] not in SCALARS
                        or not component["plain"]
                        or component["annotations"]
                        or not identifier(component["name"])
                    ):
                        raise ValueError("record component requires adaptation")
                records[name] = typ["components"]
        except ValueError as error:
            gaps.append(f"{location}: {error}")
    endpoints = []
    applications = []
    packages = {unit["package"] for unit, _, _ in declarations}
    if len(packages) != 1:
        gaps.append("all types must share the application's package in this first profile")
    for unit, typ, imports in declarations:
        location = f"{unit['file']}:{typ['line']}"
        try:
            if typ["kind"] == "RecordDeclaration":
                continue
            ann = read_annotations(typ["annotations"], imports)
            if ann == {"SpringBootApplication": {}}:
                if len(typ["methods"]) != 1:
                    raise ValueError("application class must contain only the standard main method")
                method = typ["methods"][0]
                parameter = method["parameters"][0] if method["parameters"] else {}
                body = re.sub(r"\s+", "", method["body"])
                if (
                    not method["main"]
                    or parameter.get("annotations")
                    or not parameter.get("plain")
                    or body
                    != "{SpringApplication.run("
                    + typ["name"]
                    + ".class,"
                    + parameter["name"]
                    + ");}"
                    or "org.springframework.boot.SpringApplication" not in imports
                ):
                    raise ValueError("application bootstrap requires adaptation")
                applications.append(unit["package"] + "." + typ["name"])
                continue
            if set(ann) - {"RestController", "RequestMapping"} or ann.get("RestController") != {}:
                raise ValueError("only plain RestController classes are supported")
            prefix = mapping(ann["RequestMapping"]) if "RequestMapping" in ann else ""
            for method in typ["methods"]:
                route = {
                    "method": "GET",
                    "path": "",
                    "file": unit["file"],
                    "line": method["line"],
                    "gaps": [],
                }
                try:
                    annotations_ = read_annotations(method["annotations"], imports)
                    if (
                        set(annotations_) != {"GetMapping"}
                        or not method["plain"]
                        or method["expression"] is None
                    ):
                        raise ValueError("requires a public GET method with one return expression")
                    route["path"] = prefix + mapping(annotations_["GetMapping"])
                    parameters = []
                    for param in method["parameters"]:
                        pann = read_annotations(param["annotations"], imports)
                        if (
                            param["type"] != "String"
                            or not param["plain"]
                            or not identifier(param["name"])
                            or len(pann) != 1
                        ):
                            raise ValueError(
                                "only explicitly bound String parameters are supported"
                            )
                        binding, values = next(iter(pann.items()))
                        key = "name" if "name" in values else "value"
                        required = {key, "defaultValue"} if binding == "RequestParam" else {key}
                        if (
                            binding not in {"RequestParam", "PathVariable"}
                            or set(values) != required
                        ):
                            raise ValueError(
                                "query parameters require a literal default; "
                                "path parameters require an explicit name"
                            )
                        external = literal(values[key])
                        if not identifier(external):
                            raise ValueError("unsupported external parameter name")
                        if binding == "RequestParam" and any(
                            token in literal(values["defaultValue"]) for token in ("${", "#{")
                        ):
                            raise ValueError("dynamic parameter defaults require adaptation")
                        parameters.append(
                            {
                                "name": param["name"],
                                "external": external,
                                "binding": binding,
                                "default": literal(values["defaultValue"])
                                if binding == "RequestParam"
                                else None,
                            }
                        )
                    if len({p["name"] for p in parameters}) != len(parameters) or len(
                        {(p["binding"], p["external"]) for p in parameters}
                    ) != len(parameters):
                        raise ValueError("duplicate parameter binding")
                    variables = re.findall(r"\{([^}]+)\}", route["path"])
                    if sorted(variables) != sorted(
                        p["external"] for p in parameters if p["binding"] == "PathVariable"
                    ):
                        raise ValueError("path variables must bind exactly once")
                    value = expression(
                        method["expression"], {p["name"] for p in parameters}, records, imports
                    )
                    response_type = method["type"].replace(" ", "")
                    valid = (
                        (value["kind"] == "record" and response_type == value["type"])
                        or (value["kind"] == "map" and response_type == "Map<String,Object>")
                        or (value["kind"] == "list" and response_type == "List<Object>")
                    )
                    if not valid:
                        raise ValueError(
                            "return type must match a record, "
                            "Map<String,Object> or List<Object> JSON value"
                        )
                    route.update(parameters=parameters, response=value)
                except ValueError as error:
                    route["gaps"].append(str(error))
                route["disposition"] = "needs_adaptation" if route["gaps"] else "supported"
                endpoints.append(route)
        except ValueError as error:
            gaps.append(f"{location}: {error}")
    if len(applications) != 1:
        gaps.append("exactly one standard SpringBootApplication is required")
    patterns = [re.sub(r"\{[^}]+\}", "{}", e["path"]) for e in endpoints]
    if len(set(patterns)) != len(patterns):
        gaps.append("duplicate or ambiguous GET mappings")
    for index, endpoint in enumerate(endpoints):
        left = endpoint["path"].split("/")
        for other in endpoints[index + 1 :]:
            right = other["path"].split("/")
            if (
                "{" in endpoint["path"]
                and "{" in other["path"]
                and len(left) == len(right)
                and all(
                    a == b or a.startswith("{") or b.startswith("{")
                    for a, b in zip(left, right, strict=True)
                )
            ):
                gaps.append("overlapping variable mappings require adaptation")
    if not endpoints:
        gaps.append("no supported controller endpoints found")
    return {
        "schema": "sanka.java-capture/v1",
        "profile": profile,
        "parser": {"version": PARSER_VERSION, "sha256": PARSER_SHA256},
        "analyzer_digest": hashlib.sha256((JAVA / "Analyzer.java").read_bytes()).hexdigest(),
        "source_inventory": files,
        "source_digest": digest(files),
        "application": applications[0] if len(applications) == 1 else None,
        "records": records,
        "endpoints": endpoints,
        "gaps": gaps,
        "ready": not gaps and all(not e["gaps"] for e in endpoints),
    }
