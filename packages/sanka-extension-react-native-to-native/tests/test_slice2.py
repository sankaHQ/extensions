# SPDX-License-Identifier: Apache-2.0
"""Slice 2: data loading, forms, AsyncStorage and pull-to-refresh, captured and replayed."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extension_react_native_to_native.adapter import handle
from sanka_extension_react_native_to_native.capture import canonical, capture, configuration
from sanka_extension_react_native_to_native.replay import (
    CASES_SCHEMA,
    load_verify_cases,
    validate_verify_cases,
)
from sanka_extensions.code import ExtensionRequest, encode_request
from sanka_ts_capture import MIN_NODE_MAJOR, TypeScriptDriverError, node_executable, node_version

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ITEMS = "src/screens/ItemsScreen.tsx"
DETAIL = "src/screens/ItemDetailScreen.tsx"
CREATE = "src/screens/CreateItemScreen.tsx"
SETTINGS = "src/screens/SettingsScreen.tsx"
CASES = {
    "schema": CASES_SCHEMA,
    "storage": {"token": "fixture-token"},
    "screens": {
        ITEMS: {
            "effects": {
                "load": {
                    "response": {
                        "status": 200,
                        "body": [
                            {"id": 1, "title": "Milk", "done": True},
                            {"id": 2, "title": "Bread"},
                        ],
                    },
                    "failure": {"error": "network"},
                }
            }
        },
        DETAIL: {
            "effects": {
                "load": {
                    "response": {"status": 200, "body": {"id": 1, "title": "Milk", "done": True}},
                    "failure": {"status": 404, "body": {"error": "not found"}},
                }
            }
        },
        CREATE: {
            "effects": {
                "submit": {
                    "response": {"status": 201, "body": {"id": 3}},
                    "failure": {"status": 401, "body": {"error": "unauthorized"}},
                }
            }
        },
    },
}
CHAINED_LOADER = (
    "  useEffect(() => {\n"
    "    fetch(`${API_BASE}/api/items`)\n"
    "      .then((r) => r.json())\n"
    "      .then(setItems)\n"
    "      .catch(() => setError(true));\n"
    "  }, []);\n"
)


def _node_ready() -> bool:
    try:
        return node_version(node_executable())[0] >= MIN_NODE_MAJOR
    except TypeScriptDriverError:
        return False


needs_node = pytest.mark.skipif(not _node_ready(), reason="requires Node.js 20 or later")
needs_swift = pytest.mark.skipif(
    os.getenv("SANKA_SWIFT_TESTS") != "1", reason="set SANKA_SWIFT_TESTS=1 for the swift replay"
)


def fixture(tmp_path: Path, name: str = "rn-catalog") -> Path:
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


def apply(root: Path) -> Path:
    planned = handle(request(root))
    assert planned.outcome == "success", planned.error
    result = handle(
        dataclasses.replace(
            request(root, "apply"),
            reviewed_plan_hash="runtime-review",
            configuration={
                "target_framework": "swiftui",
                "extension_plan_hash": planned.data["plan_hash"],
            },
        )
    )
    assert result.outcome == "success", result.error
    return Path(str(result.data["output"]))


def screen(captured: dict, module: str) -> dict:
    return next(item for item in captured["screens"] if item["module"] == module)


def rewrite(root: Path, module: str, old: str, new: str) -> None:
    path = root / module
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def chained(root: Path) -> None:
    """Turn the Items loader into the promise-chain form without pull-to-refresh."""
    path = root / ITEMS
    text = path.read_text()
    start = text.index("  const load = async () => {")
    end = text.index("  useEffect(() => {\n    load();\n  }, []);\n")
    end += len("  useEffect(() => {\n    load();\n  }, []);\n")
    text = text[:start] + CHAINED_LOADER + text[end:]
    text = text.replace("        refreshing={loading}\n        onRefresh={load}\n", "")
    path.write_text(text)


def native(captured: dict) -> list[dict]:
    return [item for item in captured["screens"] if item["disposition"] == "native-screen"]


@needs_node
def test_slice2_idioms_are_captured(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    captured = capture(root, configuration({}))
    assert captured["gaps"] == [] and captured["readiness"] == 1.0
    assert captured["models"] == [
        {
            "name": "Item",
            "module": "src/models.ts",
            "fields": [
                {"name": "done", "type": "boolean", "optional": True},
                {"name": "id", "type": "number", "optional": False},
                {"name": "title", "type": "string", "optional": False},
            ],
        }
    ]
    items = screen(captured, ITEMS)
    assert items["state"][0] == {
        "name": "items",
        "setter": "setItems",
        "initial": {"array": []},
        "type": {"array": {"model": "Item"}},
    }
    assert items["on_appear"] == ["load"]
    assert items["effects"] == [
        {
            "id": "load",
            "kind": "fetch",
            "method": "GET",
            "url": [{"lit": "https://catalog.example.com"}, {"lit": "/api/items"}],
            "headers": {},
            "body": None,
            "checks_ok": True,
            "before": [
                {"set": "loading", "value": {"lit": True}},
                {"set": "error", "value": {"lit": False}},
            ],
            "success": [{"set": "items", "value": {"data": []}}],
            "failure": [{"set": "error", "value": {"lit": True}}],
            "after": [{"set": "loading", "value": {"lit": False}}],
            "response": {"array": {"model": "Item"}},
        }
    ]
    flat = next(child for child in items["tree"]["children"] if child.get("kind") == "FlatList")
    assert flat["props"]["refreshing"] == {"ref": ["loading"]}
    assert flat["events"]["onRefresh"] == [{"run": "load"}]
    detail = screen(captured, DETAIL)
    assert detail["state"][0]["type"] == {"nullable": {"model": "Item"}}
    assert detail["effects"][0]["url"] == [
        {"lit": "https://catalog.example.com"},
        {"lit": "/api/items/"},
        {"param": "id"},
    ]
    assert detail["effects"][0]["response"] == {"model": "Item"}
    create = screen(captured, CREATE)
    assert create["on_appear"] == ["read-token"]
    read, submit = create["effects"]
    assert read == {
        "id": "read-token",
        "kind": "storage-read",
        "key": "token",
        "success": [{"set": "token", "value": {"nullish": {"data": []}, "default": {"lit": ""}}}],
    }
    assert submit["method"] == "POST" and submit["checks_ok"] is True
    assert submit["headers"] == {
        "Content-Type": {"lit": "application/json"},
        "Authorization": {"template": [{"lit": "Bearer "}, {"ref": ["token"]}]},
    }
    assert submit["body"] == {"title": {"ref": ["title"]}, "done": {"lit": False}}
    assert submit["before"] == [{"set": "saving", "value": {"lit": True}}]
    assert submit["success"] == [{"back": True}]
    assert submit["failure"] == [] and submit["response"] is None
    assert submit["after"] == [{"set": "saving", "value": {"lit": False}}]
    button = create["tree"]["children"][1]
    assert button["events"]["onPress"] == [{"run": "submit"}]
    assert button["props"]["disabled"] == {"ref": ["saving"]}
    settings = screen(captured, SETTINGS)
    assert settings["tree"]["children"][2]["events"]["onPress"] == [
        {"store": "token", "value": {"ref": ["token"]}},
        {"set": "saved", "value": {"lit": True}},
    ]


@needs_node
def test_loader_variants(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    chained(root)
    captured = capture(root, configuration({}))
    items = screen(captured, ITEMS)
    assert items["disposition"] == "native-screen", items["adaptation_reasons"]
    assert items["on_appear"] == ["load"]
    assert items["effects"][0]["checks_ok"] is False
    assert items["effects"][0]["before"] == [] and items["effects"][0]["after"] == []
    assert items["effects"][0]["success"] == [{"set": "items", "value": {"data": []}}]
    assert items["effects"][0]["failure"] == [{"set": "error", "value": {"lit": True}}]
    assert items["effects"][0]["response"] == {"array": {"model": "Item"}}
    root = fixture(tmp_path / "declaration")
    rewrite(root, ITEMS, "  const load = async () => {", "  async function load() {")
    rewrite(root, ITEMS, "    }\n  };\n\n  useEffect", "    }\n  }\n\n  useEffect")
    captured = capture(root, configuration({}))
    assert screen(captured, ITEMS)["disposition"] == "native-screen"
    assert screen(captured, ITEMS)["effects"][0]["id"] == "load"


@needs_node
@pytest.mark.parametrize(
    ("module", "old", "new", "code"),
    [
        (
            ITEMS,
            "fetch(`${API_BASE}/api/items`)",
            "fetch(`${API_BASE}/api/${error}`)",
            "SANKA_RN_NETWORK",
        ),
        (ITEMS, "    load();\n  }, []);", "    load();\n  }, [items]);", "SANKA_RN_NETWORK"),
        (
            ITEMS,
            "      const data: Item[] = await response.json();\n      setItems(data);",
            "      setItems([]);",
            "SANKA_RN_NETWORK",
        ),
        (
            CREATE,
            'AsyncStorage.getItem("token")',
            "AsyncStorage.getItem(title)",
            "SANKA_RN_STORAGE",
        ),
        (
            CREATE,
            "    } catch {\n      // The form keeps its values; the user can retry.\n    }",
            '    } catch {\n      setTitle("");\n    }',
            "SANKA_RN_NETWORK",
        ),
        (CREATE, 'method: "POST",', 'method: "PUT",', "SANKA_RN_NETWORK"),
        (
            CREATE,
            'method: "POST",',
            'method: "POST",\n        credentials: "include",',
            "SANKA_RN_NETWORK",
        ),
        (
            CREATE,
            "setSaving(true);\n    try {",
            "setSaving(true);\n    console.log(title);\n    try {",
            "SANKA_RN_NETWORK",
        ),
        (
            SETTINGS,
            'AsyncStorage.setItem("token", token)',
            "AsyncStorage.setItem(token, token)",
            "SANKA_RN_STORAGE",
        ),
    ],
)
def test_near_miss_shapes_become_dispositions(
    tmp_path: Path, module: str, old: str, new: str, code: str
) -> None:
    root = fixture(tmp_path)
    rewrite(root, module, old, new)
    captured = capture(root, configuration({}))
    entry = screen(captured, module)
    assert entry["disposition"] == "needs-manual-adaptation"
    assert code in {reason["code"] for reason in entry["adaptation_reasons"]}, entry[
        "adaptation_reasons"
    ]
    assert captured["gaps"] == []


@needs_node
def test_model_shapes_are_bounded(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    rewrite(root, "src/models.ts", "  done?: boolean;", "  done?: boolean;\n  tags: string[];")
    captured = capture(root, configuration({}))
    for module in (ITEMS, DETAIL):
        entry = screen(captured, module)
        assert entry["disposition"] == "needs-manual-adaptation"
        assert {reason["code"] for reason in entry["adaptation_reasons"]} == {"SANKA_RN_MODEL"}
    assert captured["models"] == []
    assert screen(captured, CREATE)["disposition"] == "native-screen"


@needs_node
def test_plan_is_deterministic_for_slice2(tmp_path: Path) -> None:
    documents = []
    for name, seed in (("first", "7"), ("second", "99")):
        root = fixture(tmp_path / name)
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
        assert response["data"]["readiness"] == 1.0
        assert "Sources/AppUI/APIClient.swift" in response["data"]["files"]
        models = response["data"]["files"]["Sources/AppUI/Models.swift"]
        assert "public struct Item: Codable, Equatable" in models
        assert "public var done: Bool?" in models
        items = response["data"]["files"]["Sources/AppUI/Screens/ItemsScreen.swift"]
        assert ".task { await appear() }" in items
        assert '.refreshable { await perform(effect: "load") }' in items
        assert 'public static let appearEffects: [String] = ["load"]' in items
        create = response["data"]["files"]["Sources/AppUI/Screens/CreateItemScreen.swift"]
        assert 'public static let submitEffects: [String] = ["submit"]' in create
        assert '"Authorization": ("Bearer " + state.token)' in create
        assert "case run(String)" in create
        assert (
            "public static let submitEffects: [String] = []"
            in response["data"]["files"]["Sources/AppUI/Screens/SettingsScreen.swift"]
        )
        assert (
            "case store(String, String)"
            in response["data"]["files"]["Sources/AppUI/Screens/SettingsScreen.swift"]
        )
        documents.append((root / ".sanka" / "native" / "plan.json").read_bytes())
    assert documents[0] == documents[1]


@needs_node
def test_verify_cases_validation(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    captured = capture(root, configuration({}))
    screens = native(captured)
    validate_verify_cases(CASES, captured, screens)

    def rejects(document: dict, fragment: str) -> None:
        with pytest.raises(ValueError) as error:
            validate_verify_cases(document, captured, screens)
        assert fragment in str(error.value), str(error.value)

    def variant(path: str, value: object) -> dict:
        """Copy CASES with the value at the ``|``-separated path replaced or removed."""
        document = json.loads(json.dumps(CASES))
        target = document
        *parents, last = path.split("|")
        for key in parents:
            target = target[key]
        if value is None:
            del target[last]
        else:
            target[last] = value
        return document

    rejects(variant("schema", "other"), "schema must be")
    rejects(variant("storage", {"token": 1}), "storage must map keys to strings")
    rejects(variant(f"screens|{CREATE}|effects|submit", None), "needs a response and a failure")
    rejects(variant("screens|src/screens/Other.tsx", {"effects": {}}), "not a native screen")
    rejects(variant(f"screens|{ITEMS}|effects|reload", {}), "no fetch effect 'reload'")
    rejects(
        variant(f"screens|{ITEMS}|effects|load|response", {"status": 200}), "needs status and body"
    )
    rejects(variant(f"screens|{ITEMS}|effects|load|response|status", 500), "status must be 2xx")
    rejects(
        variant(f"screens|{ITEMS}|effects|load|response|body", [{"id": 1}]),
        "missing required field 'title'",
    )
    rejects(
        variant(
            f"screens|{ITEMS}|effects|load|response|body", [{"id": 1, "title": "x", "done": None}]
        ),
        "optional fields are absent, never null",
    )
    rejects(
        variant(
            f"screens|{ITEMS}|effects|load|response|body", [{"id": 1, "title": "x", "extra": 1}]
        ),
        "has no fields ['extra']",
    )
    rejects(
        variant(f"screens|{DETAIL}|effects|load|response|body", {"id": "1", "title": "x"}),
        "expected number",
    )
    rejects(
        variant(f"screens|{CREATE}|effects|submit|failure", {"status": 200, "body": {}}),
        "must not be 2xx",
    )
    rejects(variant(f"screens|{CREATE}|effects|submit|failure", {"oops": 1}), "either")
    chained(root)
    captured = capture(root, configuration({}))
    with pytest.raises(ValueError) as error:
        validate_verify_cases(
            variant(f"screens|{ITEMS}|effects|load|failure", {"status": 500, "body": {}}),
            captured,
            native(captured),
        )
    assert "does not check response.ok" in str(error.value)
    with pytest.raises(ValueError) as error:
        load_verify_cases(root / ".sanka" / "native", captured, native(captured))
    assert "verify-cases.json is required" in str(error.value)
    assert ITEMS in str(error.value)


@needs_node
def test_replay_refuses_missing_verify_cases(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    apply(root)
    for command in ("test", "verify"):
        response = handle(request(root, command))
        assert response.outcome == "error"
        assert response.error is not None
        assert "verify-cases.json is required" in response.error.message
        assert not (root / ".sanka" / "native" / f"{command}.json").exists()
    (root / ".sanka" / "native" / "verify-cases.json").write_text("{not json")
    response = handle(request(root, "test"))
    assert response.error is not None and "invalid JSON" in response.error.message


def write_cases(root: Path) -> None:
    (root / ".sanka" / "native" / "verify-cases.json").write_text(json.dumps(CASES, indent=1))


def indexed(report: dict, side: str) -> dict:
    return {(document["screen"], document["scenario"]): document for document in report[side]}


@needs_node
@needs_swift
def test_source_swiftui_parity_for_slice2(tmp_path: Path, node_tools: Path) -> None:
    root = fixture(tmp_path)
    output = apply(root)
    write_cases(root)
    tested = handle(request(root, "test"))
    assert tested.outcome == "success", tested.error
    assert tested.data["ok"] is True and tested.data["failures"] == []
    assert tested.data["verify_cases"]["sha256"].startswith("sha256:")
    verified = handle(request(root, "verify"))
    assert verified.outcome == "success", verified.error
    report = verified.data
    assert report["ok"] is True and report["failures"] == []
    assert canonical(sorted(report["source"], key=canonical)) == canonical(
        sorted(report["candidate"], key=canonical)
    )
    observed = indexed(report, "candidate")
    scenarios = {
        screen_name: {scenario for (name, scenario) in observed if name == screen_name}
        for screen_name in ("ItemsScreen", "ItemDetailScreen", "CreateItemScreen", "SettingsScreen")
    }
    assert scenarios == {
        "ItemsScreen": {"initial", "loaded", "load-failed"},
        "ItemDetailScreen": {"initial", "loaded", "load-failed"},
        "CreateItemScreen": {
            "initial",
            "loaded",
            "load-failed",
            "type:0",
            "submit:1",
            "submit-failed:1",
        },
        "SettingsScreen": {"initial", "loaded", "load-failed", "type:1", "press:2"},
    }
    initial = observed[("ItemsScreen", "initial")]
    assert [child["role"] for child in initial["tree"]["children"]] == [
        "container",
        "indicator",
        "list",
    ]
    assert initial["raised"] == [
        {"set": "loading", "value": True},
        {"set": "error", "value": False},
    ]
    assert initial["requests"] == [
        {
            "effect": "load",
            "method": "GET",
            "url": "https://catalog.example.com/api/items",
            "headers": {},
            "body": None,
        }
    ]
    loaded = observed[("ItemsScreen", "loaded")]["tree"]
    rows = loaded["children"][1]["children"]
    assert [row["text"] for row in rows] == ["1", "2"]
    assert [child["text"] for child in rows[0]["children"][0]["children"]] == ["Milk", "done"]
    assert rows[1]["children"][0]["action"] == [{"navigate": "Detail", "params": {"id": 2}}]
    failed = observed[("ItemsScreen", "load-failed")]["tree"]
    assert [child["role"] for child in failed["children"]] == ["container", "text", "list"]
    assert failed["children"][1]["text"] == "Could not load items"
    detail = observed[("ItemDetailScreen", "loaded")]
    assert detail["params"] == {"id": 1}
    assert [child["text"] for child in detail["tree"]["children"][:3]] == [
        "Item #1",
        "Milk",
        "Done",
    ]
    assert detail["requests"][0]["url"] == "https://catalog.example.com/api/items/1"
    assert observed[("ItemDetailScreen", "load-failed")]["tree"]["children"][1]["text"] == (
        "Could not load the item"
    )
    submitted = observed[("CreateItemScreen", "submit:1")]
    assert submitted["raised"] == [
        {"set": "saving", "value": True},
        {"back": True},
        {"set": "saving", "value": False},
    ]
    assert submitted["requests"] == [
        {
            "effect": "submit",
            "method": "POST",
            "url": "https://catalog.example.com/api/items",
            "headers": {
                "Authorization": "Bearer fixture-token",
                "Content-Type": "application/json",
            },
            "body": {"title": "", "done": False},
        }
    ]
    refused = observed[("CreateItemScreen", "submit-failed:1")]
    assert refused["raised"] == [
        {"set": "saving", "value": True},
        {"set": "saving", "value": False},
    ]
    assert refused["tree"] == observed[("CreateItemScreen", "loaded")]["tree"]
    saved = observed[("SettingsScreen", "press:2")]
    assert saved["raised"] == [
        {"store": "token", "value": "fixture-token"},
        {"set": "saved", "value": True},
    ]
    assert saved["tree"]["children"][3]["text"] == "Saved"
    assert observed[("SettingsScreen", "loaded")]["tree"]["children"][1]["text"] == "fixture-token"
    assert observed[("SettingsScreen", "initial")]["tree"]["children"][1]["text"] == ""
    assert {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    } == {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }


@needs_node
@needs_swift
def test_tampered_candidates_fail_slice2(tmp_path: Path, node_tools: Path) -> None:
    root = fixture(tmp_path)
    output = apply(root)
    write_cases(root)
    create = output / "Sources" / "AppUI" / "Screens" / "CreateItemScreen.swift"
    original = create.read_text()
    # A submission that navigates on failure violates the zero-side-effect rule; test
    # alone catches it because the dump replays both outcomes.
    tampered = original.replace(
        "        } catch {\n        }\n", "        } catch {\n            emit(.back)\n        }\n"
    )
    assert tampered != original
    create.write_text(tampered)
    response = handle(request(root, "test"))
    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_PARITY_FAILED"
    report = json.loads((root / ".sanka" / "native" / "test.json").read_text())
    assert {(item["screen"], item["scenario"], item["reason"]) for item in report["failures"]} == {
        ("CreateItemScreen", "submit-failed:1", "failed submission navigated")
    }
    create.write_text(original.replace('"Bearer " + state.token', '"Token " + state.token'))
    response = handle(request(root, "verify"))
    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_PARITY_FAILED"
    report = json.loads((root / ".sanka" / "native" / "verify.json").read_text())
    assert report["ok"] is False
    assert {(item["screen"], item["scenario"]) for item in report["failures"]} == {
        ("CreateItemScreen", "submit:1"),
        ("CreateItemScreen", "submit-failed:1"),
    }
