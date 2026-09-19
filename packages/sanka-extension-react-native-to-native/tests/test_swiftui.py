# SPDX-License-Identifier: Apache-2.0
"""SwiftUI generation, review-bound apply and the structural parity replay."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extension_react_native_to_native.adapter import handle
from sanka_extension_react_native_to_native.capture import canonical
from sanka_extension_react_native_to_native.tree import NODE_KEYS, ROLES
from sanka_extensions.code import ExtensionRequest, encode_request
from sanka_ts_capture import MIN_NODE_MAJOR, TypeScriptDriverError, node_executable, node_version

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMMON_FILES = {
    "App/project.yml",
    "Package.swift",
    "README.md",
    "Sources/AppUI/Models.swift",
    "Sources/AppUI/Navigation.swift",
    "Sources/AppUI/Styles.swift",
    "Sources/AppUI/Tree.swift",
    "Sources/SankaTreeDump/main.swift",
    "contract.json",
}
EXPECTED_FILES = {
    "rn-todo": COMMON_FILES
    | {
        "App/TodoApp.swift",
        "Sources/AppUI/Screens/DetailScreen.swift",
        "Sources/AppUI/Screens/HomeScreen.swift",
    },
    "expo-notes": COMMON_FILES
    | {
        "App/NotesApp.swift",
        "Sources/AppUI/Screens/NoteScreen.swift",
        "Sources/AppUI/Screens/NotesScreen.swift",
        "Sources/AppUI/Screens/SettingsScreen.swift",
    },
}
EXPECTED_SCENARIOS = {
    "rn-todo": {
        "DetailScreen": {"initial", "toggle:3.1"},
        "HomeScreen": {"initial", "press:2", "type:1"},
    },
    "expo-notes": {
        "NoteScreen": {"initial"},
        "NotesScreen": {"initial"},
        "SettingsScreen": {"initial", "toggle:0.1"},
    },
}


def _node_ready() -> bool:
    try:
        return node_version(node_executable())[0] >= MIN_NODE_MAJOR
    except TypeScriptDriverError:
        return False


needs_node = pytest.mark.skipif(not _node_ready(), reason="requires Node.js 20 or later")
needs_swift = pytest.mark.skipif(
    os.getenv("SANKA_SWIFT_TESTS") != "1", reason="set SANKA_SWIFT_TESTS=1 for the swift replay"
)


def fixture(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root, ignore=shutil.ignore_patterns(".sanka"))
    return root


def request(root: Path, command: str = "plan", **config: str) -> ExtensionRequest:
    return ExtensionRequest(
        "test",
        command,
        str(root),
        str(root / ".sanka" / "native"),
        "sanka/react-native-to-native",
        "0.1.0a1",
        "0" * 64,
        {},
        {"target_framework": "swiftui", **config},
        (),
        None,
    )


def approved(root: Path, plan_hash: str, command: str = "apply") -> ExtensionRequest:
    return dataclasses.replace(
        request(root, command),
        reviewed_plan_hash="runtime-review",
        configuration={"target_framework": "swiftui", "extension_plan_hash": plan_hash},
    )


def apply(root: Path) -> Path:
    planned = handle(request(root))
    assert planned.outcome == "success", planned.error
    assert handle(request(root)).data == planned.data
    result = handle(approved(root, planned.data["plan_hash"]))
    assert result.outcome == "success", result.error
    return Path(str(result.data["output"]))


def snapshot(output: Path) -> dict[str, bytes]:
    return {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }


def walk(node: dict, found: list[dict]) -> list[dict]:
    found.append(node)
    for child in node["children"]:
        walk(child, found)
    return found


@needs_node
@pytest.mark.parametrize("name", ["rn-todo", "expo-notes"])
def test_plan_renders_the_swiftui_package(tmp_path: Path, name: str) -> None:
    root = fixture(tmp_path, name)
    planned = handle(request(root))
    assert planned.outcome == "success", planned.error
    data = planned.data
    assert data["generated"] is True
    assert data["gaps"] == []
    assert data["readiness"] == 1.0
    assert set(data["files"]) == EXPECTED_FILES[name]
    assert {item["disposition"] for item in data["dispositions"]} == {"native-screen"}
    package = data["files"]["Package.swift"]
    assert package.startswith("// swift-tools-version: 5.10")
    assert "platforms: [.iOS(.v17), .macOS(.v14)]" in package
    assert "dependencies:" not in package.split("targets:")[0]
    assert json.loads(data["files"]["contract.json"]) == data["capture"]
    assert "import SwiftUI" in data["files"]["Sources/AppUI/Navigation.swift"]
    assert "public enum Route: Hashable" in data["files"]["Sources/AppUI/Navigation.swift"]
    assert "XcodeGen 2.46.0" in data["files"]["App/project.yml"]
    for file in data["files"].values():
        assert "/private/" not in file and str(root) not in file
    if name == "rn-todo":
        assert data["assets"] == {
            "Sources/AppUI/Resources/assets/logo.png": {
                "source": "assets/logo.png",
                "sha256": hashlib.sha256((root / "assets" / "logo.png").read_bytes()).hexdigest(),
            }
        }
        home = data["files"]["Sources/AppUI/Screens/HomeScreen.swift"]
        assert "public struct HomeScreenState: Equatable" in home
        assert "public enum HomeScreenAction: Equatable" in home
        assert "case setTodos([HomeScreenTodosItem])" in home
        assert "case navigate(Route)" in home
        assert "public static func apply(_ action: HomeScreenAction" in home
        assert "public static func tree(state: HomeScreenState" in home
        assert "TextField(" in home and "List {" in home and "Toggle(" not in home
        assert (
            "case detail(id: Double, title: String)"
            in data["files"]["Sources/AppUI/Navigation.swift"]
        )
    else:
        assert data["assets"] == {}
        navigation = data["files"]["Sources/AppUI/Navigation.swift"]
        assert "TabView {" in navigation
        assert "case note(id: String)" in navigation
        assert '.push("/note/[id]", ["id": .string(id)])' in navigation
    compose = handle(request(root, target_framework="compose"))
    assert compose.outcome == "success", compose.error
    assert compose.data["files"] == {} and compose.data["generated"] is False
    assert compose.data["readiness"] == 1.0


@needs_node
def test_emitter_gaps_become_dispositions(tmp_path: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    detail = root / "src" / "screens" / "DetailScreen.tsx"
    text = detail.read_text()
    # A text run outside <Text> is a React Native runtime error; the emitter reports it.
    detail.write_text(text.replace("<Text>Done</Text>", "<View>Done</View>"))
    planned = handle(request(root))
    assert planned.outcome == "success", planned.error
    detail_entry = next(
        item for item in planned.data["dispositions"] if item["module"].endswith("DetailScreen.tsx")
    )
    assert detail_entry["disposition"] == "needs-manual-adaptation"
    assert {reason["code"] for reason in detail_entry["adaptation_reasons"]} == {
        "SANKA_RN_SWIFTUI_TREE"
    }
    assert planned.data["readiness"] == 0.5
    assert planned.data["generated"] is True
    placeholder = planned.data["files"]["Sources/AppUI/Screens/DetailScreen.swift"]
    assert "Needs manual adaptation" in placeholder
    assert "DetailScreen.replay()" not in planned.data["files"]["Sources/SankaTreeDump/main.swift"]
    assert "HomeScreen.replay()" in planned.data["files"]["Sources/SankaTreeDump/main.swift"]


@needs_node
def test_plan_bytes_are_independent_of_location_and_hash_seed(tmp_path: Path) -> None:
    documents = []
    for name, seed in (("first", "1"), ("second", "4242")):
        root = fixture(tmp_path / name, "rn-todo")
        result = subprocess.run(
            [sys.executable, "-m", "sanka_extension_react_native_to_native"],
            input=json.dumps(encode_request(request(root))),
            text=True,
            capture_output=True,
            timeout=180,
            env=os.environ | {"PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert response["outcome"] == "success"
        documents.append((root / ".sanka" / "native" / "plan.json").read_bytes())
        assert response["data"]["plan_hash"].startswith("sha256:")
    assert documents[0] == documents[1]


@needs_node
def test_apply_refuses_drift_and_existing_output(tmp_path: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    planned = handle(request(root))
    assert planned.outcome == "success", planned.error
    plan_hash = planned.data["plan_hash"]
    unreviewed = handle(dataclasses.replace(request(root, "apply")))
    assert unreviewed.outcome == "error"
    home = root / "src" / "screens" / "HomeScreen.tsx"
    original = home.read_text()
    home.write_text(original.replace('placeholder="New todo"', 'placeholder="Todo"'))
    blocked = handle(approved(root, plan_hash))
    assert blocked.outcome == "error"
    assert not (root / ".sanka" / "native" / "native-swiftui").exists()
    home.write_text(original)
    tampered = root / ".sanka" / "native" / "plan.json"
    tampered.write_text("{}")
    assert handle(approved(root, plan_hash)).outcome == "error"
    handle(request(root))
    output = apply(root)
    assert output == root / ".sanka" / "native" / "native-swiftui"
    files = snapshot(output)
    assert set(files) == EXPECTED_FILES["rn-todo"] | {"Sources/AppUI/Resources/assets/logo.png"}
    assert (
        files["Sources/AppUI/Resources/assets/logo.png"]
        == (root / "assets" / "logo.png").read_bytes()
    )
    assert files["contract.json"].decode() == canonical(planned.data["capture"]) + "\n"
    again = handle(approved(root, plan_hash))
    assert again.outcome == "error"
    assert again.error is not None and "already exists" in again.error.message
    assert snapshot(output) == files


@needs_node
def test_replay_requires_the_current_applied_plan(tmp_path: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    before = handle(request(root, "verify"))
    assert before.outcome == "error"
    output = apply(root)
    extra = output / "extra.swift"
    extra.symlink_to(root / "App.tsx")
    response = handle(request(root, "test"))
    assert response.error is not None and "regular files" in response.error.message
    extra.unlink()
    home = root / "src" / "screens" / "HomeScreen.tsx"
    home.write_text(home.read_text() + "\n// changed\n")
    response = handle(request(root, "verify"))
    assert response.error is not None
    assert "differs from the applied plan" in response.error.message
    assert not (root / ".sanka" / "native" / "verify.json").exists()


@needs_node
@pytest.mark.parametrize(
    ("name", "edit"),
    [
        ("Package.swift", b"\n// edited\n"),
        ("contract.json", b" "),
        ("App/project.yml", b"\n# edited\n"),
        ("Sources/AppUI/Resources/assets/logo.png", b"\x00"),
    ],
)
def test_hand_edits_to_pinned_files_are_refused(tmp_path: Path, name: str, edit: bytes) -> None:
    root = fixture(tmp_path, "rn-todo")
    output = apply(root)
    target = output / name
    target.write_bytes(target.read_bytes() + edit)
    for command in ("test", "verify"):
        response = handle(request(root, command))
        assert response.outcome == "error"
        assert response.error is not None
        assert f"{name} differs from the applied plan" in response.error.message
        assert not (root / ".sanka" / "native" / f"{command}.json").exists()


@needs_node
def test_compose_stages_stay_refused(tmp_path: Path) -> None:
    root = fixture(tmp_path, "expo-notes")
    planned = handle(request(root, target_framework="compose"))
    assert planned.outcome == "success", planned.error
    for command in ("apply", "test", "verify"):
        response = handle(
            dataclasses.replace(
                request(root, command),
                reviewed_plan_hash="runtime-review",
                configuration={
                    "target_framework": "compose",
                    "extension_plan_hash": planned.data["plan_hash"],
                },
            )
        )
        assert response.outcome == "error"
        assert response.error is not None
        assert response.error.code == "SANKA_EXTENSION_UNSUPPORTED_COMMAND"


@needs_node
@needs_swift
@pytest.mark.parametrize("name", ["rn-todo", "expo-notes"])
def test_source_swiftui_parity(tmp_path: Path, node_tools: Path, name: str) -> None:
    root = fixture(tmp_path, name)
    output = apply(root)
    before = snapshot(output)
    tested = handle(request(root, "test"))
    assert tested.outcome == "success", tested.error
    assert tested.data["ok"] is True
    assert "Swift version" in tested.data["swift_version"]
    assert tested.data["platform"] == "macos"
    assert tested.data["pending"] == []
    assert (root / ".sanka" / "native" / "test.json").is_file()
    verified = handle(request(root, "verify"))
    assert verified.outcome == "success", verified.error
    report = verified.data
    assert report["ok"] is True and report["failures"] == []
    assert report["node_version"].startswith("v22.")
    assert report["react_version"] == "19.3.0"
    assert report["complete_app"] is False
    observed = {
        (document["screen"], document["scenario"]): document for document in report["candidate"]
    }
    assert {
        screen: {scenario for (name_, scenario) in observed if name_ == screen}
        for screen in EXPECTED_SCENARIOS[name]
    } == EXPECTED_SCENARIOS[name]
    assert canonical(sorted(report["source"], key=canonical)) == canonical(
        sorted(report["candidate"], key=canonical)
    )
    for document in report["candidate"]:
        for node in walk(document["tree"], []):
            assert set(node) == set(NODE_KEYS) and node["role"] in ROLES
    if name == "rn-todo":
        initial = observed[("HomeScreen", "initial")]["tree"]
        heading, field, add, todo_list = initial["children"]
        assert heading == {
            "role": "text",
            "text": "Todos (1)",
            "label": None,
            "enabled": None,
            "checked": None,
            "action": None,
            "children": [],
        }
        assert field["role"] == "textfield" and field["label"] == "New todo"
        assert field["action"] == [{"bind": "title"}]
        assert add["role"] == "button" and add["enabled"] is True
        assert add["action"] == [
            {
                "set": "todos",
                "value": [
                    {"done": False, "id": 1, "title": "Buy milk"},
                    {"done": False, "id": 2, "title": ""},
                ],
            },
            {"set": "title", "value": ""},
        ]
        assert todo_list["role"] == "list"
        assert todo_list["children"][0]["text"] == "1"
        assert todo_list["children"][0]["children"][0]["action"] == [
            {"navigate": "Detail", "params": {"id": 1, "title": "Buy milk"}}
        ]
        pressed = observed[("HomeScreen", "press:2")]["tree"]
        assert pressed["children"][0]["text"] == "Todos (2)"
        assert len(pressed["children"][3]["children"]) == 2
        typed = observed[("HomeScreen", "type:1")]["tree"]
        assert typed["children"][1]["text"] == "Sanka"
        detail = observed[("DetailScreen", "initial")]
        assert detail["params"] == {"id": 1, "title": "sample-title"}
        assert detail["tree"]["children"][0]["text"] == "assets/logo.png"
        assert detail["tree"]["children"][2]["text"] == "Todo #1"
        assert detail["tree"]["children"][4]["text"] == "Open"
        toggled = observed[("DetailScreen", "toggle:3.1")]["tree"]
        assert toggled["children"][3]["children"][1]["checked"] is True
        assert toggled["children"][4]["text"] == "Completed"
    else:
        notes = observed[("NotesScreen", "initial")]["tree"]
        status_bar, note_list, link = notes["children"]
        assert status_bar == {
            "role": "container",
            "text": None,
            "label": None,
            "enabled": None,
            "checked": None,
            "action": None,
            "children": [],
        }
        assert [item["text"] for item in note_list["children"]] == ["a", "b"]
        assert note_list["children"][1]["children"][0]["action"] == [
            {"push": "/note/[id]", "params": {"id": "b"}}
        ]
        assert link["action"] == [{"push": "/settings", "params": {}}]
        assert link["children"][0]["text"] == "Open settings"
        note = observed[("NoteScreen", "initial")]["tree"]
        assert note["children"][0]["text"] == "Note sample-id"
        assert note["children"][1]["action"] == [{"back": True}]
    assert snapshot(output) == before


@needs_node
@needs_swift
def test_replay_detects_candidate_changes(tmp_path: Path, node_tools: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    output = apply(root)
    screen = output / "Sources" / "AppUI" / "Screens" / "HomeScreen.swift"
    text = screen.read_text()
    assert text.count('Node(role: "text", text: "Add")') == 1
    screen.write_text(
        text.replace('Node(role: "text", text: "Add")', 'Node(role: "text", text: "Add!")')
    )
    response = handle(request(root, "verify"))
    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_PARITY_FAILED"
    report = json.loads((root / ".sanka" / "native" / "verify.json").read_text())
    assert report["ok"] is False
    assert {(item["screen"], item["scenario"]) for item in report["failures"]} == {
        ("HomeScreen", "initial"),
        ("HomeScreen", "press:2"),
        ("HomeScreen", "type:1"),
    }
    assert report["candidate_digest"].startswith("sha256:")
