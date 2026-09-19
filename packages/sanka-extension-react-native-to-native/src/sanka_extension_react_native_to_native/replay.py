# SPDX-License-Identifier: Apache-2.0
"""Structural parity replay for the generated SwiftUI package.

``test`` builds the applied candidate with ``swift build`` and runs its
``sanka-tree-dump`` executable, which prints the normalized tree of every native screen
and scenario without rendering SwiftUI. ``verify`` additionally renders the React Native
source screens in Node with ``react-test-renderer`` against the in-repo stubs and
compares canonical JSON per screen and scenario. Nothing opens a network listener.
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

from sanka_ts_capture import node_executable, node_version, transpile_sources

from .capture import canonical, capture, digest
from .render_swiftui import Rendered
from .tree import COMPONENT_ROLES, PROBE_TEXT, SCHEMA, index_documents, sample_params

REPLAY_SCHEMA = "sanka.react-native-to-native.replay/v1"
NODE_MAJOR = 22
NODE_PACKAGES = ("react", "react-test-renderer")
PINNED_FILES = ("Package.swift", "contract.json", "App/project.yml")
HARNESS = Path(__file__).resolve().parent / "node" / "rn-tree-run.js"
SWIFT_TIMEOUT = 1800
NODE_TIMEOUT = 300
BUILD_DIRECTORY = "native-swiftui-build"
MAX_CANDIDATE_BYTES = 10_000_000
MAX_CANDIDATE_FILES = 1000


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
    if sys.platform != "darwin":
        raise ValueError(
            f"SwiftUI replay requires macOS with a Swift toolchain (found {sys.platform})"
        )
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
    with tempfile.TemporaryDirectory(prefix="sanka-swiftui-replay-") as temporary:
        workspace = Path(temporary)
        candidate = workspace / "candidate"
        candidate.mkdir()
        for name, content in snapshot.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
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
            ],
            candidate,
        )
        actual = json.loads(observed.read_text(encoding="utf-8"))
        candidate_index = index_documents(actual, names)
        result: dict[str, Any] = {
            "schema": REPLAY_SCHEMA,
            "tree_schema": SCHEMA,
            "command": command,
            "platform": "macos",
            "swift_version": swift_version,
            "source_digest": captured["source_digest"],
            "candidate_digest": candidate_hash,
            "scope": (
                "normalized screen trees per scenario (roles, text, labels, enabled/checked "
                "state, typed intents); layout, colors and pixels are not compared"
                if command == "verify"
                else "swift build and the normalized tree dump of every native screen"
            ),
            "complete_app": False,
            "screens": names,
            "pending": pending,
            "candidate": actual,
            "ok": True,
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
            texts = {
                screen["module"]: (root / screen["module"]).read_text(encoding="utf-8")
                for screen in native
            }
            transpiled = transpile_sources(texts, node=node) if texts else {}
            screens = []
            for screen in sorted(native, key=lambda item: str(item["module"])):
                relative = PurePosixPath(screen["module"]).with_suffix(".js").as_posix()
                target = source_directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(transpiled[screen["module"]], encoding="utf-8")
                screens.append(
                    {
                        "name": screen["name"],
                        "file": relative,
                        "navigation": captured["navigation"],
                        "params": sample_params(screen["params"]),
                        "state": [entry["name"] for entry in screen["state"]],
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
            failures = [
                {"screen": screen, "scenario": scenario}
                for screen in names
                for scenario in sorted(set(candidate_index[screen]) | set(source_index[screen]))
                if canonical(candidate_index[screen].get(scenario))
                != canonical(source_index[screen].get(scenario))
            ]
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
