# SPDX-License-Identifier: Apache-2.0
"""Structural parity replay for the generated SwiftUI package.

``test`` builds the applied candidate with ``swift build`` and runs its
``sanka-tree-dump`` executable, which prints the normalized tree of every native screen
and scenario without rendering SwiftUI. ``verify`` additionally renders the React Native
source screens in Node with ``react-test-renderer`` against the in-repo stubs and
compares canonical JSON per screen and scenario. Screens with network effects or
AsyncStorage take their fixture responses from ``verify-cases.json`` in the artifact
root, on both sides. Nothing opens a network listener.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from sanka_ts_capture import node_executable, node_version, parse_sources, transpile_sources
from sanka_ts_capture import tree as t

from .capture import canonical, capture, digest
from .render_swiftui import Rendered
from .screens import default_export
from .swift_expressions import js_number
from .tree import COMPONENT_ROLES, PROBE_TEXT, SCHEMA, index_documents, sample_params

REPLAY_SCHEMA = "sanka.react-native-to-native.replay/v1"
CASES_SCHEMA = "sanka.react-native-to-native.verify-cases/v1"
VERIFY_CASES = "verify-cases.json"
NODE_MAJOR = 22
NODE_PACKAGES = ("react", "react-test-renderer")
PINNED_FILES = ("Package.swift", "contract.json", "App/project.yml")
HARNESS = Path(__file__).resolve().parent / "node" / "rn-tree-run.js"
SWIFT_TIMEOUT = 1800
NODE_TIMEOUT = 300
BUILD_DIRECTORY = "native-swiftui-build"
MAX_CANDIDATE_BYTES = 10_000_000
MAX_CANDIDATE_FILES = 1000
NAVIGATION_INTENTS = ("navigate", "push", "back")


def _swift(command: list[str], cwd: Path) -> str:
    environment = os.environ | {"TERM": "dumb"}
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=SWIFT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"swift toolchain unavailable ({error}); install Xcode or the Command Line Tools "
            "and, without an accepted Xcode license, set DEVELOPER_DIR to the Command Line Tools"
        ) from error
    output = result.stdout + result.stderr
    if result.returncode:
        if "Xcode license" in output or "xcodebuild -license" in output:
            raise ValueError(
                "swift refused to run because the Xcode license is not accepted; run "
                "`sudo xcodebuild -license` or set "
                "DEVELOPER_DIR=/Library/Developer/CommandLineTools"
            )
        raise ValueError("replay process failed: " + output[-4000:])
    return result.stdout


def _toolchain(cwd: Path) -> str:
    version = _swift(["swift", "--version"], cwd).strip().splitlines()
    if not version or "Swift version" not in " ".join(version):
        raise ValueError("swift --version did not report a Swift toolchain")
    return next(line for line in version if "Swift version" in line).strip()


def _node(command: list[str], cwd: Path) -> None:
    environment = {"PATH": os.environ.get("PATH", ""), "NODE_OPTIONS": ""}
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=NODE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"source replay failed: {error}") from error
    if result.returncode:
        raise ValueError("source replay failed: " + (result.stdout + result.stderr)[-4000:])


def _node_modules(root: Path) -> Path:
    explicit = os.environ.get("SANKA_NODE_TOOLS")
    modules = Path(explicit) if explicit else root / "node_modules"
    for package in NODE_PACKAGES:
        if not (modules / package / "package.json").is_file():
            raise ValueError(
                f"verify requires the source project's {package} installation under "
                "node_modules, or SANKA_NODE_TOOLS pointing at a directory that contains it"
            )
    return modules.resolve()


def _snapshot(output: Path) -> dict[str, bytes]:
    if output.is_symlink() or not output.is_dir():
        raise ValueError("candidate must be a regular directory")
    if (output / ".build").exists():
        raise ValueError("remove the .build directory from the candidate before replay")
    snapshot: dict[str, bytes] = {}
    size = 0
    for directory, names, filenames in os.walk(output, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("candidate symlinks are unsupported")
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("candidate must contain only regular files")
            size += path.stat().st_size
            if size > MAX_CANDIDATE_BYTES or len(snapshot) >= MAX_CANDIDATE_FILES:
                raise ValueError("candidate exceeds replay limits")
            snapshot[path.relative_to(output).as_posix()] = path.read_bytes()
    return snapshot


# -- verify cases -------------------------------------------------------------------------


def _fetch_effects(screen: dict[str, Any]) -> list[dict[str, Any]]:
    return [effect for effect in screen.get("effects", []) if effect["kind"] == "fetch"]


def _uses_fixtures(screen: dict[str, Any]) -> bool:
    if screen.get("effects"):
        return True

    def stores(node: Any) -> bool:
        if isinstance(node, dict):
            return any(
                "store" in action
                for actions in (node.get("events") or {}).values()
                for action in actions
            ) or any(stores(value) for value in node.values())
        if isinstance(node, list):
            return any(stores(item) for item in node)
        return False

    return stores(screen.get("tree"))


def load_verify_cases(
    artifacts: Path, captured: dict[str, Any], native: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the validated ``verify-cases.json`` document, or None when no screen needs it."""
    path = artifacts / VERIFY_CASES
    needed = [screen["module"] for screen in native if _uses_fixtures(screen)]
    if path.is_symlink() or not path.is_file():
        if needed:
            raise ValueError(
                f"{VERIFY_CASES} is required under the artifact root because these screens "
                f"load data, submit forms or use AsyncStorage: {', '.join(sorted(needed))}; "
                "see the package README for the format"
            )
        return None
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"{VERIFY_CASES}: invalid JSON ({error})") from error
    validate_verify_cases(document, captured, native)
    return document


def validate_verify_cases(
    document: Any, captured: dict[str, Any], native: list[dict[str, Any]]
) -> None:
    """Raise ValueError with a precise message unless the document covers every effect."""
    where = VERIFY_CASES
    if not isinstance(document, dict) or set(document) - {"schema", "storage", "screens"}:
        raise ValueError(f"{where}: an object with schema, storage and screens")
    if document.get("schema") != CASES_SCHEMA:
        raise ValueError(f"{where}: schema must be {CASES_SCHEMA!r}")
    storage = document.get("storage", {})
    if not isinstance(storage, dict) or not all(type(value) is str for value in storage.values()):
        raise ValueError(f"{where}: storage must map keys to strings")
    screens = document.get("screens", {})
    if not isinstance(screens, dict):
        raise ValueError(f"{where}: screens must be an object keyed by screen module")
    modules = {screen["module"]: screen for screen in native}
    models = {model["name"]: model for model in captured.get("models", [])}
    for module, entry in screens.items():
        if module not in modules:
            raise ValueError(f"{where}: {module!r} is not a native screen")
        if not isinstance(entry, dict) or set(entry) - {"effects"}:
            raise ValueError(f"{where}: screens.{module} carries only effects")
        effects = entry.get("effects", {})
        if not isinstance(effects, dict):
            raise ValueError(f"{where}: screens.{module}.effects must be an object")
        known = {effect["id"]: effect for effect in _fetch_effects(modules[module])}
        for effect_id, fixture in effects.items():
            if effect_id not in known:
                raise ValueError(f"{where}: {module} has no fetch effect {effect_id!r}")
            _validate_fixture(fixture, known[effect_id], models, f"{where}: {module}.{effect_id}")
    for module, screen in modules.items():
        for effect in _fetch_effects(screen):
            if effect["id"] not in (screens.get(module) or {}).get("effects", {}):
                raise ValueError(
                    f"{where}: screens.{module}.effects.{effect['id']} needs a response and a "
                    "failure fixture"
                )


def _validate_fixture(
    fixture: Any, effect: dict[str, Any], models: dict[str, dict[str, Any]], where: str
) -> None:
    if not isinstance(fixture, dict) or set(fixture) != {"response", "failure"}:
        raise ValueError(f"{where}: fixtures carry exactly response and failure")
    response = fixture["response"]
    if not isinstance(response, dict) or set(response) != {"status", "body"}:
        raise ValueError(f"{where}.response: needs status and body")
    _validate_status(response["status"], f"{where}.response")
    if not 200 <= int(response["status"]) < 300 and effect["checks_ok"]:
        raise ValueError(f"{where}.response: status must be 2xx for an effect checking ok")
    if effect["method"] == "GET":
        _validate_body(response["body"], effect["response"], models, f"{where}.response.body")
    failure = fixture["failure"]
    if isinstance(failure, dict) and set(failure) == {"error"} and type(failure["error"]) is str:
        return
    if not isinstance(failure, dict) or set(failure) != {"status", "body"}:
        raise ValueError(f'{where}.failure: either {{"error": "network"}} or status and body')
    _validate_status(failure["status"], f"{where}.failure")
    if 200 <= int(failure["status"]) < 300:
        raise ValueError(f"{where}.failure: status must not be 2xx")
    if not effect["checks_ok"]:
        raise ValueError(
            f"{where}.failure: effect {effect['id']} does not check response.ok, so only a "
            'network failure ({"error": "network"}) can fail it'
        )


def _validate_status(value: Any, where: str) -> None:
    if type(value) is not int or not 100 <= value <= 599:
        raise ValueError(f"{where}: status must be an integer HTTP status")


def _validate_body(value: Any, ir: Any, models: dict[str, dict[str, Any]], where: str) -> None:
    """Check a fixture body against a captured response type; optional fields are absent."""
    if isinstance(ir, str):
        kinds: dict[str, tuple[type, ...]] = {
            "string": (str,),
            "number": (int, float),
            "boolean": (bool,),
        }
        if (isinstance(value, bool) and ir != "boolean") or not isinstance(value, kinds[ir]):
            raise ValueError(f"{where}: expected {ir}")
        return
    if "array" in ir:
        if not isinstance(value, list):
            raise ValueError(f"{where}: expected an array")
        for index, item in enumerate(value):
            _validate_body(item, ir["array"], models, f"{where}[{index}]")
        return
    if "nullable" in ir:
        _validate_body(value, ir["nullable"], models, where)
        return
    model = models[str(ir["model"])]
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected a {model['name']} object")
    fields = {field["name"]: field for field in model["fields"]}
    extra = sorted(set(value) - set(fields))
    if extra:
        raise ValueError(f"{where}: {model['name']} has no fields {extra}")
    for name, field in fields.items():
        if name not in value:
            if not field["optional"]:
                raise ValueError(f"{where}: missing required field {name!r}")
            continue
        if value[name] is None:
            raise ValueError(f"{where}.{name}: optional fields are absent, never null")
        _validate_body(value[name], field["type"], models, f"{where}.{name}")


# -- source instrumentation --------------------------------------------------------------


def instrument(text: str, module: str, effect_ids: list[str], node: str) -> str:
    """Wrap each captured effect function so the harness can attribute its intents."""
    parsed = parse_sources({module: text}, node=node)[module]
    _, function = default_export(parsed)
    if function is None:
        raise ValueError(f"{module}: default-exported component not found")
    body = t.field(function, "body")
    edits: list[tuple[int, int, str]] = []
    for statement in t.field_list(body, "statements") if body is not None else []:
        name: str | None = None
        target: t.Node | None = None
        if t.kind(statement) == "FunctionDeclaration":
            name = t.identifier_name(t.field(statement, "name"))
            target = statement
        elif t.kind(statement) == "VariableStatement":
            declarations = t.field(statement, "declarationList")
            items = t.field_list(declarations, "declarations") if declarations else []
            if len(items) == 1:
                name = t.identifier_name(t.field(items[0], "name"))
                target = t.field(items[0], "initializer")
        if name is None or target is None or name not in effect_ids:
            continue
        block = t.field(target, "body")
        if block is None or t.kind(block) != "Block":
            continue
        edits.append((t.start(block), int(block["e"]), name))
    for start, end, name in sorted(edits, reverse=True):
        original = text[start:end]
        wrapped = (
            "{ return globalThis.__sankaRun("
            + json.dumps(name)
            + ", async () => "
            + original
            + "); }"
        )
        text = text[:start] + wrapped + text[end:]
    return text


def _concrete_url(parts: list[dict[str, Any]], params: dict[str, Any]) -> str:
    rendered = []
    for part in parts:
        if "lit" in part:
            rendered.append(str(part["lit"]))
        else:
            value = params[str(part["param"])]
            rendered.append(
                js_number(value)
                if isinstance(value, int | float) and not isinstance(value, bool)
                else ("true" if value is True else "false" if value is False else str(value))
            )
    return "".join(rendered)


# -- replay -------------------------------------------------------------------------------


def replay(
    root: Path,
    output: Path,
    captured: dict[str, Any],
    rendered: Rendered,
    command: str,
) -> dict[str, Any]:
    if captured["gaps"] or rendered.gaps or not rendered.files:
        raise ValueError("cannot replay unsupported source behavior")
    if not output.is_dir():
        raise ValueError("apply the reviewed plan before testing")
    snapshot = _snapshot(output)
    # Bind evidence to the exact bytes tested, including manually repaired screens.
    candidate_hash = digest(
        {key: hashlib.sha256(value).hexdigest() for key, value in snapshot.items()}
    )
    for name in PINNED_FILES:
        if snapshot.get(name) != rendered.files[name].encode():
            raise ValueError(f"candidate {name} differs from the applied plan")
    for name, info in rendered.assets.items():
        content = snapshot.get(name)
        if content is None or hashlib.sha256(content).hexdigest() != info["sha256"]:
            raise ValueError(f"candidate {name} differs from the applied plan")
    config = captured["configuration"]
    if capture(root, config) != captured:
        raise ValueError("source changed before replay")
    by_module = {screen["module"]: screen for screen in captured["screens"]}
    native = [
        by_module[item["module"]]
        for item in rendered.dispositions
        if item["disposition"] == "native-screen"
    ]
    pending = [
        item["module"] for item in rendered.dispositions if item["disposition"] != "native-screen"
    ]
    names = sorted(screen["name"] for screen in native)
    cases = load_verify_cases(output.parent, captured, native)
    if sys.platform != "darwin":
        raise ValueError(
            f"SwiftUI replay requires macOS with a Swift toolchain (found {sys.platform})"
        )
    with tempfile.TemporaryDirectory(prefix="sanka-swiftui-replay-") as temporary:
        workspace = Path(temporary)
        candidate = workspace / "candidate"
        candidate.mkdir()
        for name, content in snapshot.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        cases_path = workspace / VERIFY_CASES
        cases_path.write_text(canonical(cases) + "\n", encoding="utf-8")
        # Keep build products beside the output so test and verify share one build.
        scratch = output.parent / BUILD_DIRECTORY
        scratch.mkdir(exist_ok=True)
        swift_version = _toolchain(candidate)
        _swift(["swift", "build", "--scratch-path", str(scratch.resolve())], candidate)
        observed = workspace / "candidate-observed.json"
        _swift(
            [
                "swift",
                "run",
                "--scratch-path",
                str(scratch.resolve()),
                "--skip-build",
                "sanka-tree-dump",
                str(observed),
                *([str(cases_path)] if cases is not None else []),
            ],
            candidate,
        )
        actual = json.loads(observed.read_text(encoding="utf-8"))
        candidate_index = index_documents(actual, names)
        failures = _effect_checks(candidate_index)
        result: dict[str, Any] = {
            "schema": REPLAY_SCHEMA,
            "tree_schema": SCHEMA,
            "command": command,
            "platform": "macos",
            "swift_version": swift_version,
            "source_digest": captured["source_digest"],
            "candidate_digest": candidate_hash,
            "verify_cases": None
            if cases is None
            else {"path": str(output.parent / VERIFY_CASES), "sha256": digest(cases)},
            "scope": (
                "normalized screen trees per scenario (roles, text, labels, enabled/checked "
                "state, typed intents, raised intents and requests); layout, colors and "
                "pixels are not compared"
                if command == "verify"
                else "swift build and the normalized tree dump of every native screen"
            ),
            "complete_app": False,
            "screens": names,
            "pending": pending,
            "candidate": actual,
            "failures": failures,
            "ok": not failures,
        }
        if command == "verify":
            node = node_executable()
            version = node_version(node)
            if version[0] != NODE_MAJOR:
                raise ValueError(
                    f"verify requires Node.js {NODE_MAJOR}.x to render the source "
                    f"(found {version[0]}); set SANKA_NODE"
                )
            modules = _node_modules(root)
            react_version = str(
                json.loads((modules / "react" / "package.json").read_text(encoding="utf-8"))[
                    "version"
                ]
            )
            source_directory = workspace / "source"
            source_directory.mkdir()
            texts = {}
            for screen in native:
                text = (root / screen["module"]).read_text(encoding="utf-8")
                effect_ids = [
                    str(effect["id"])
                    for effect in screen.get("effects", [])
                    if effect["kind"] == "fetch"
                ]
                texts[screen["module"]] = (
                    instrument(text, screen["module"], effect_ids, node) if effect_ids else text
                )
            transpiled = transpile_sources(texts, node=node) if texts else {}
            screens = []
            for screen in sorted(native, key=lambda item: str(item["module"])):
                relative = PurePosixPath(screen["module"]).with_suffix(".js").as_posix()
                target = source_directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(transpiled[screen["module"]], encoding="utf-8")
                params = sample_params(screen["params"])
                screens.append(
                    {
                        "name": screen["name"],
                        "module": screen["module"],
                        "file": relative,
                        "navigation": captured["navigation"],
                        "params": params,
                        "state": [entry["name"] for entry in screen["state"]],
                        "appear": list(screen.get("on_appear", [])),
                        "submit_effects": [
                            effect["id"]
                            for effect in _fetch_effects(screen)
                            if effect["method"] == "POST"
                        ],
                        "effects": [
                            {
                                "id": effect["id"],
                                "method": effect["method"],
                                "url": _concrete_url(effect["url"], params),
                            }
                            for effect in _fetch_effects(screen)
                        ],
                    }
                )
            routes = sorted(
                {
                    str(screen["route"])
                    for navigator in captured["navigators"]
                    for screen in navigator["screens"]
                    if "route" in screen
                }
            )
            spec = {
                "source_root": str(source_directory),
                "modules": str(modules),
                "roles": COMPONENT_ROLES,
                "probe_text": PROBE_TEXT,
                "routes": routes,
                "fixtures": cases or {"storage": {}, "screens": {}},
                "screens": screens,
            }
            spec_path = workspace / "spec.json"
            spec_path.write_text(canonical(spec), encoding="utf-8")
            source_observed = workspace / "source-observed.json"
            _node(
                [node, str(HARNESS), str(spec_path), str(source_observed)],
                source_directory,
            )
            expected = json.loads(source_observed.read_text(encoding="utf-8"))
            source_index = index_documents(expected, names)
            failures.extend(
                {"screen": screen, "scenario": scenario, "reason": "source and candidate differ"}
                for screen in names
                for scenario in sorted(set(candidate_index[screen]) | set(source_index[screen]))
                if canonical(candidate_index[screen].get(scenario))
                != canonical(source_index[screen].get(scenario))
            )
            failures.extend(
                {"module": module, "reason": "needs manual adaptation"} for module in pending
            )
            result.update(
                source=expected,
                node_version="v" + ".".join(str(item) for item in version),
                react_version=react_version,
                failures=failures,
                ok=not failures,
            )
        if capture(root, config) != captured:
            raise ValueError("source changed during replay; discard observations")
        if _snapshot(output) != snapshot:
            raise ValueError("candidate changed during replay; discard observations")
        return result


def _effect_checks(indexed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Submissions must navigate or reset on success and do nothing on failure."""
    failures: list[dict[str, Any]] = []
    for screen, scenarios in indexed.items():
        base = scenarios.get("loaded") or scenarios["initial"]
        for scenario, document in sorted(scenarios.items()):
            raised = document["raised"]
            navigations = [
                intent for intent in raised if any(k in intent for k in NAVIGATION_INTENTS)
            ]
            if scenario.startswith("submit-failed:"):
                if navigations:
                    failures.append(
                        {
                            "screen": screen,
                            "scenario": scenario,
                            "reason": "failed submission navigated",
                        }
                    )
                if canonical(document["tree"]) != canonical(base["tree"]):
                    failures.append(
                        {
                            "screen": screen,
                            "scenario": scenario,
                            "reason": "failed submission changed state",
                        }
                    )
            elif (
                scenario.startswith("submit:")
                and not navigations
                and not any("set" in intent for intent in raised)
            ):
                failures.append(
                    {"screen": screen, "scenario": scenario, "reason": "submission had no effect"}
                )
    return failures
