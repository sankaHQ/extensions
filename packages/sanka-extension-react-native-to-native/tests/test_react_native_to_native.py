# SPDX-License-Identifier: Apache-2.0
"""Static capture of React Navigation and Expo Router apps; nothing is executed."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extension_react_native_to_native.adapter import handle
from sanka_extension_react_native_to_native.capture import capture, configuration
from sanka_extensions.code import ExtensionRequest
from sanka_ts_capture import MIN_NODE_MAJOR, TypeScriptDriverError, node_executable, node_version

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REACT_IMPORT = 'import React, { useState } from "react";'


def _node_ready() -> bool:
    try:
        return node_version(node_executable())[0] >= MIN_NODE_MAJOR
    except TypeScriptDriverError:
        return False


needs_node = pytest.mark.skipif(not _node_ready(), reason="requires Node.js 20 or later")


def fixture(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)
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


def screen(captured: dict, module: str) -> dict:
    return next(item for item in captured["screens"] if item["module"] == module)


def test_configuration_boundaries() -> None:
    assert configuration({})["target_framework"] == "swiftui"
    assert configuration({"target": "compose"})["target_framework"] == "compose"
    assert configuration({"navigation": "expo-router"})["navigation"] == "expo-router"
    for config in (
        {"target_framework": "uikit"},
        {"target": "swiftui", "target_framework": "compose"},
        {"navigation": "drawer"},
        {"entry": "../App.tsx"},
        {"entry": "App.js"},
        {"bundle_id": "not an id"},
        {"min_ios": "15.0"},
        {"min_sdk": "21"},
        {"unknown": "x"},
    ):
        with pytest.raises(ValueError):
            configuration(config)


@needs_node
def test_react_navigation_todo_app_is_fully_native(tmp_path: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    captured = capture(root, configuration({}))
    assert captured["gaps"] == []
    assert captured["navigation"] == "react-navigation"
    assert captured["readiness"] == 1.0
    assert captured["app"]["display_name"] == "Todo"
    assert captured["navigators"] == [
        {
            "name": "root",
            "kind": "stack",
            "initial": "Home",
            "screens": [
                {
                    "name": "Home",
                    "component": "HomeScreen",
                    "title": "Todos",
                    "header": True,
                    "params": {},
                    "module": "src/screens/HomeScreen.tsx",
                },
                {
                    "name": "Detail",
                    "component": "DetailScreen",
                    "title": "Todo",
                    "header": True,
                    "params": {"id": "number", "title": "string"},
                    "module": "src/screens/DetailScreen.tsx",
                },
            ],
        }
    ]
    home = screen(captured, "src/screens/HomeScreen.tsx")
    assert home["disposition"] == "native-screen"
    assert home["state"] == [
        {"name": "title", "setter": "setTitle", "initial": {"lit": ""}},
        {
            "name": "todos",
            "setter": "setTodos",
            "initial": {
                "array": [
                    {
                        "object": {
                            "id": {"lit": 1},
                            "title": {"lit": "Buy milk"},
                            "done": {"lit": False},
                        }
                    }
                ]
            },
        },
    ]
    assert home["styles"]["button"]["backgroundColor"] == "#0a84ff"
    view = home["tree"]
    assert view["kind"] == "View" and view["props"] == {"style": ["container"]}
    heading, text_input, add, todo_list = view["children"]
    assert heading["children"] == [
        {"text": [{"lit": "Todos ("}, {"length": {"ref": ["todos"]}}, {"lit": ")"}]}
    ]
    assert text_input["events"] == {"onChangeText": [{"set": "title", "value": {"arg": 0}}]}
    assert add["events"]["onPress"] == [
        {
            "set": "todos",
            "value": {
                "append": {"ref": ["todos"]},
                "value": {
                    "object": {
                        "id": {
                            "binary": "+",
                            "left": {"length": {"ref": ["todos"]}},
                            "right": {"lit": 1},
                        },
                        "title": {"ref": ["title"]},
                        "done": {"lit": False},
                    }
                },
            },
        },
        {"set": "title", "value": {"lit": ""}},
    ]
    assert todo_list["props"]["keyExtractor"] == {"string": {"item": ["id"]}}
    row = todo_list["item"]["element"]
    assert row["events"]["onPress"] == [
        {"navigate": "Detail", "params": {"id": {"item": ["id"]}, "title": {"item": ["title"]}}}
    ]
    assert row["children"][1] == {
        "when": {"item": ["done"]},
        "then": {"kind": "Text", "props": {}, "children": [{"text": [{"lit": "done"}]}]},
        "else": None,
    }
    detail = screen(captured, "src/screens/DetailScreen.tsx")
    assert detail["disposition"] == "native-screen"
    assert detail["params"] == {"id": "number", "title": "string"}
    image, title, number, row, status, back = detail["tree"]["children"]
    assert image["props"]["source"] == {"asset": "assets/logo.png"}
    assert title["children"] == [{"text": [{"param": "title"}]}]
    assert number["children"] == [{"text": [{"lit": "Todo #"}, {"param": "id"}]}]
    assert row["children"][1]["events"] == {"onValueChange": [{"set": "done", "value": {"arg": 0}}]}
    assert status["children"] == [
        {
            "text": [
                {"cond": {"ref": ["done"]}, "then": {"lit": "Completed"}, "else": {"lit": "Open"}}
            ]
        }
    ]
    assert back["events"] == {"onPress": [{"back": True}]}
    assert captured["inventory"]["native_modules"] == []


@needs_node
def test_expo_router_notes_app_is_fully_native(tmp_path: Path) -> None:
    root = fixture(tmp_path, "expo-notes")
    captured = capture(root, configuration({}))
    assert captured["gaps"] == []
    assert captured["navigation"] == "expo-router"
    assert captured["readiness"] == 1.0
    assert captured["app"]["bundle_id"] == "com.example.notes"
    assert captured["app"]["package_name"] == "com.example.notes"
    root_navigator, tabs = captured["navigators"]
    assert root_navigator["name"] == "app" and root_navigator["kind"] == "stack"
    assert [
        (item["name"], item.get("route"), item.get("navigator"))
        for item in root_navigator["screens"]
    ] == [
        ("(tabs)", None, "app/(tabs)"),
        ("note/[id]", "/note/[id]", None),
    ]
    assert root_navigator["screens"][0]["header"] is False
    assert tabs["kind"] == "tabs"
    assert [(item["name"], item["route"], item["title"]) for item in tabs["screens"]] == [
        ("index", "/", "Notes"),
        ("settings", "/settings", "Settings"),
    ]
    notes = screen(captured, "app/(tabs)/index.tsx")
    assert notes["disposition"] == "native-screen"
    status_bar, notes_list, empty, link = notes["tree"]["children"]
    assert status_bar["kind"] == "StatusBar"
    assert notes_list["item"]["element"]["events"]["onPress"] == [
        {"push": "/note/[id]", "params": {"id": {"item": ["id"]}}}
    ]
    assert empty == {
        "when": {"binary": "===", "left": {"length": {"ref": ["notes"]}}, "right": {"lit": 0}},
        "then": {"kind": "Text", "props": {}, "children": [{"text": [{"lit": "No notes yet"}]}]},
        "else": None,
    }
    assert link["kind"] == "Link"
    assert link["events"] == {"onPress": [{"push": "/settings", "params": {}}]}
    assert link["children"] == [{"text": [{"lit": "Open settings"}]}]
    note = screen(captured, "app/note/[id].tsx")
    assert note["params"] == {"id": "string"}
    assert note["tree"]["children"][0]["children"] == [
        {"text": [{"lit": "Note "}, {"param": "id"}]}
    ]
    assert note["tree"]["children"][1]["events"] == {"onPress": [{"back": True}]}


@needs_node
@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (
            (
                'import React, { useState } from "react";',
                REACT_IMPORT + '\nimport Animated from "react-native-reanimated";',
            ),
            "SANKA_RN_NATIVE_MODULE",
        ),
        (
            (
                'import React, { useState } from "react";',
                REACT_IMPORT + '\nimport { useSelector } from "react-redux";',
            ),
            "SANKA_RN_THIRD_PARTY_COMPONENT",
        ),
        (("StyleSheet, Text,", "Platform, StyleSheet, Text,"), "SANKA_RN_UNSUPPORTED_HOOK"),
        (
            ("flex: 1, padding: 16,", 'flex: 1, padding: 16, width: "50%",'),
            "SANKA_RN_DYNAMIC_STYLE",
        ),
        (
            ("<Text style={styles.heading}>", "<Text style={styles.heading} selectable>"),
            "SANKA_RN_UNSUPPORTED_PROP",
        ),
        (
            (
                'const [title, setTitle] = useState("");',
                "const [title, setTitle] = useState(computeTitle());",
            ),
            "SANKA_RN_EXPRESSION",
        ),
        (
            ("onChangeText={setTitle}", "onChangeText={(text) => console.log(text)}"),
            "SANKA_RN_EVENT",
        ),
    ],
)
def test_unsupported_screen_constructs_become_dispositions(
    tmp_path: Path, replacement: tuple[str, str], code: str
) -> None:
    root = fixture(tmp_path, "rn-todo")
    home = root / "src" / "screens" / "HomeScreen.tsx"
    original = home.read_text()
    assert replacement[0] in original
    home.write_text(original.replace(replacement[0], replacement[1]))
    captured = capture(root, configuration({}))
    entry = screen(captured, "src/screens/HomeScreen.tsx")
    assert entry["disposition"] == "needs-manual-adaptation"
    assert code in {reason["code"] for reason in entry["adaptation_reasons"]}, entry[
        "adaptation_reasons"
    ]
    assert captured["readiness"] == 0.5
    assert screen(captured, "src/screens/DetailScreen.tsx")["disposition"] == "native-screen"
    assert captured["gaps"] == []


@needs_node
def test_expo_router_gaps(tmp_path: Path) -> None:
    root = fixture(tmp_path, "expo-notes")
    (root / "app" / "[...rest].tsx").write_text(
        'import { Text } from "react-native";\n'
        "export default function Missing() { return <Text>?</Text>; }\n"
    )
    captured = capture(root, configuration({}))
    assert any("catch-all" in gap for gap in captured["gaps"])
    (root / "app" / "[...rest].tsx").unlink()
    index = root / "app" / "(tabs)" / "index.tsx"
    index.write_text(index.read_text().replace('href="/settings"', 'href="/archive"'))
    captured = capture(root, configuration({}))
    entry = screen(captured, "app/(tabs)/index.tsx")
    assert entry["disposition"] == "needs-manual-adaptation"
    assert {reason["code"] for reason in entry["adaptation_reasons"]} == {
        "SANKA_RN_EXPO_DYNAMIC_ROUTE"
    }
    note = root / "app" / "note" / "[id].tsx"
    note.write_text(
        note.read_text().replace(
            'import { useLocalSearchParams, useRouter } from "expo-router";',
            'import { Redirect, useLocalSearchParams, useRouter } from "expo-router";',
        )
    )
    captured = capture(root, configuration({}))
    assert "SANKA_RN_UNSUPPORTED_HOOK" in {
        reason["code"] for reason in screen(captured, "app/note/[id].tsx")["adaptation_reasons"]
    }


@needs_node
def test_project_level_gaps(tmp_path: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    (root / "src" / "util.ts").write_text("export const answer = 42;\n")
    captured = capture(root, configuration({}))
    assert any("src/util.ts" in gap for gap in captured["gaps"])
    (root / "src" / "util.ts").unlink()
    package = json.loads((root / "package.json").read_text())
    package["dependencies"]["react-native"] = "0.72.0"
    (root / "package.json").write_text(json.dumps(package))
    assert any("0.76 or later" in gap for gap in capture(root, configuration({}))["gaps"])
    (root / "package.json").unlink()
    captured = capture(root, configuration({}))
    assert any("package.json" in gap for gap in captured["gaps"])
    assert captured["navigation"] is None
    assert captured["readiness"] == 0.0


@needs_node
def test_scan_and_plan_artifacts_and_unsupported_stages(tmp_path: Path) -> None:
    root = fixture(tmp_path, "rn-todo")
    scanned = handle(request(root, "scan"))
    assert scanned.outcome == "success", scanned.error
    assert (root / ".sanka" / "native" / "scan.json").is_file()
    planned = handle(request(root))
    assert planned.outcome == "success", planned.error
    assert planned.data["files"] == {}
    assert planned.data["generated"] is False
    assert planned.data["readiness"] == 1.0
    assert [item["disposition"] for item in planned.data["dispositions"]] == [
        "native-screen",
        "native-screen",
    ]
    assert planned.data["plan_hash"].startswith("sha256:")
    assert handle(request(root)).data == planned.data
    other = fixture(tmp_path / "elsewhere", "rn-todo")
    assert handle(request(other)).data == planned.data
    for command in ("apply", "test", "verify"):
        response = handle(
            dataclasses.replace(
                request(root, command),
                reviewed_plan_hash="runtime-review",
                configuration={
                    "target_framework": "swiftui",
                    "extension_plan_hash": planned.data["plan_hash"],
                },
            )
        )
        assert response.outcome == "error"
        assert response.error is not None
        assert response.error.code == "SANKA_EXTENSION_UNSUPPORTED_COMMAND"
    (root / "src" / "link.tsx").symlink_to(root / "App.tsx")
    assert handle(request(root)).outcome == "error"


@needs_node
def test_subprocess_protocol(tmp_path: Path) -> None:
    from sanka_extensions.code import encode_request

    root = fixture(tmp_path, "expo-notes")
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_react_native_to_native"],
        input=json.dumps(encode_request(request(root, "plan", navigation="expo-router"))),
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["outcome"] == "success"
    assert response["data"]["capture"]["navigation"] == "expo-router"
    assert response["data"]["readiness"] == 1.0
