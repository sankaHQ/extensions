# SPDX-License-Identifier: Apache-2.0
"""Real Express source and native axum candidates; no TCP test servers."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sanka_extension_typescript_to_rust.adapter import handle
from sanka_extension_typescript_to_rust.capture import capture, configuration
from sanka_extensions.code import ExtensionRequest
from sanka_ts_capture import MIN_NODE_MAJOR, TypeScriptDriverError, node_executable, node_version

# The integer above 2^53 pins JavaScript double rounding, exactly as Express serves it.
PAYLOAD_SOURCE = (
    "{ message: 'hello \"world\" 日本語', items: [1, true, null, 9223372036854775809], "
    "nested: { ok: false } }"
)
PAYLOAD = {
    "message": 'hello "world" 日本語',
    "items": [1, True, None, 9223372036854776000],
    "nested": {"ok": False},
}
PACKAGE_JSON = json.dumps(
    {"name": "fixture", "private": True, "dependencies": {"express": "^5.2.1"}}
)


def source(extra: str = "") -> str:
    return (
        'import express from "express";\n'
        "const app = express();\n"
        'app.get("/health", (_req, res) => {\n'
        f"  res.json({PAYLOAD_SOURCE});\n"
        "});\n"
        f"{extra}"
        "export default app;\n"
    )


def project(root: Path, text: str | None = None) -> Path:
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.ts").write_text(source() if text is None else text)
    (root / "package.json").write_text(PACKAGE_JSON)
    return root


def request(root: Path, target: str = "axum") -> ExtensionRequest:
    return ExtensionRequest(
        "test",
        "plan",
        str(root),
        str(root / ".sanka" / "rust"),
        "sanka/typescript-to-rust",
        "0.1.0a1",
        "0" * 64,
        {},
        {"source_framework": "express", "target_framework": target},
        (),
        None,
    )


def apply(root: Path) -> Path:
    req = request(root)
    planned = handle(req)
    assert planned.outcome == "success", planned.error
    assert handle(req).data == planned.data
    result = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-reviewed-plan",
            configuration=req.configuration | {"extension_plan_hash": planned.data["plan_hash"]},
        )
    )
    assert result.outcome == "success", result.error
    return Path(str(result.data["output"]))


def _node_ready() -> bool:
    try:
        return node_version(node_executable())[0] >= MIN_NODE_MAJOR
    except TypeScriptDriverError:
        return False


needs_node = pytest.mark.skipif(not _node_ready(), reason="requires Node.js 20 or later")
needs_rust = pytest.mark.skipif(
    os.getenv("SANKA_RUST_TESTS") != "1", reason="set SANKA_RUST_TESTS=1 for cargo replay"
)


@needs_node
def test_review_and_fail_closed(tmp_path: Path) -> None:
    project(tmp_path)
    req = request(tmp_path)
    plan = handle(req)
    assert plan.outcome == "success", plan.error
    assert plan.data["files"]
    assert plan.data["capture"]["routes"] == [
        {
            "path": "/health",
            "method": "GET",
            "status": 200,
            "body": PAYLOAD,
            "body_json": json.dumps(PAYLOAD, ensure_ascii=False, separators=(",", ":")),
        }
    ]
    assert handle(dataclasses.replace(req, command="apply")).outcome == "error"
    old_hash = plan.data["plan_hash"]
    (tmp_path / "src" / "app.ts").write_text(source() + "\nconst secret = 'changed';\n")
    blocked = handle(
        dataclasses.replace(
            req,
            command="apply",
            reviewed_plan_hash="runtime-reviewed-plan",
            configuration=req.configuration | {"extension_plan_hash": old_hash},
        )
    )
    assert blocked.outcome == "error"
    assert not (tmp_path / ".sanka/rust/rust").exists()
    project(tmp_path)
    output = apply(tmp_path)
    assert (output / "Cargo.lock").is_file()
    assert (output / "src" / "lib.rs").is_file()
    assert (
        handle(
            dataclasses.replace(
                req,
                command="apply",
                reviewed_plan_hash="runtime-reviewed-plan",
                configuration=req.configuration | {"extension_plan_hash": old_hash},
            )
        ).outcome
        == "error"
    )


@needs_node
@pytest.mark.parametrize(
    "addition",
    [
        'import os from "node:os";\n',
        "function helper() { return 1; }\n",
        'app.post("/x", (_req, res) => { res.json({}); });\n',
        'app.get("/items/:id", (_req, res) => { res.json({}); });\n',
        'app.get("/dynamic", (req, res) => { res.json({ q: req.query.q }); });\n',
        "app.use(express.urlencoded());\n",
        'app.get("/two", (_req, res) => { const x = 1; res.json({ x }); });\n',
        'app.get("/health", (_req, res) => { res.json({ again: true }); });\n',
    ],
)
def test_unknown_behavior_blocks(tmp_path: Path, addition: str) -> None:
    project(tmp_path, source(addition))
    plan = handle(request(tmp_path))
    assert plan.outcome == "success", plan.error
    assert plan.data["files"] == {}
    assert plan.data["capture"]["gaps"]


@needs_node
def test_variants_inside_the_envelope(tmp_path: Path) -> None:
    text = (
        'import express, { type Request, type Response } from "express";\n'
        "const app = express();\n"
        "app.use(express.json());\n"
        'app.get("/a", async (req: Request, res: Response) => res.status(201).json({ a: 1 }));\n'
        'app.get("/b", function (req, res) { return res.json({ b: [2, "x"] }); });\n'
        "export { app };\n"
    )
    project(tmp_path, text)
    (tmp_path / "src" / "server.ts").write_text(
        'import app from "./app";\napp.listen(Number(process.env.PORT ?? 3000));\n'
    )
    captured = capture(tmp_path, configuration({}))
    assert captured["gaps"] == []
    assert captured["launcher"] == "src/server.ts"
    assert [(route["path"], route["status"]) for route in captured["routes"]] == [
        ("/a", 201),
        ("/b", 200),
    ]
    (tmp_path / "src" / "server.ts").write_text(
        'import app from "./app";\nconsole.log("boot");\napp.listen(3000);\n'
    )
    assert any("launcher" in gap for gap in capture(tmp_path, configuration({}))["gaps"])


@needs_node
def test_profiles_and_source_boundaries(tmp_path: Path) -> None:
    assert configuration({})["target_framework"] == "axum"
    assert configuration({"target": "axum"})["target_framework"] == "axum"
    for config in (
        {"source_framework": "fastify"},
        {"target_framework": "actix"},
        {"target": "actix"},
        {"target": "axum", "target_framework": "gin"},
        {"source_file": "../app.ts"},
        {"source_file": "src/app.js"},
        {"unknown": "x"},
    ):
        with pytest.raises(ValueError):
            configuration(config)
    project(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "^3.0.0"}}))
    assert any("major version" in gap for gap in capture(tmp_path, configuration({}))["gaps"])
    (tmp_path / "package.json").unlink()
    assert any("package.json" in gap for gap in capture(tmp_path, configuration({}))["gaps"])
    project(tmp_path)
    (tmp_path / "src" / "broken.ts").write_text("const = ;")
    assert capture(tmp_path, configuration({}))["gaps"]
    (tmp_path / "src" / "broken.ts").unlink()
    (tmp_path / "src" / "link.ts").symlink_to(tmp_path / "src" / "app.ts")
    assert handle(request(tmp_path)).outcome == "error"


@needs_node
def test_plan_tampering_and_review_attestation(tmp_path: Path) -> None:
    project(tmp_path)
    req = request(tmp_path)
    plan = handle(req)
    approved = dataclasses.replace(
        req,
        command="apply",
        reviewed_plan_hash="runtime-review",
        configuration=req.configuration | {"extension_plan_hash": plan.data["plan_hash"]},
    )
    assert handle(dataclasses.replace(approved, reviewed_plan_hash=None)).outcome == "error"
    stored = tmp_path / ".sanka/rust/plan.json"
    stored.write_text("{}")
    assert handle(approved).outcome == "error"
    assert not (tmp_path / ".sanka/rust/rust").exists()
    handle(req)
    assert (
        handle(
            dataclasses.replace(
                approved, configuration=approved.configuration | {"source_file": "src/other.ts"}
            )
        ).outcome
        == "error"
    )


@needs_node
def test_subprocess_protocol(tmp_path: Path) -> None:
    from sanka_extensions.code import encode_request

    project(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_typescript_to_rust"],
        input=json.dumps(encode_request(request(tmp_path))),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["outcome"] == "success"
    assert response["data"]["files"]["Cargo.lock"]
    approved = dataclasses.replace(
        request(tmp_path),
        command="apply",
        reviewed_plan_hash="runtime-review",
        configuration={
            "source_framework": "express",
            "extension_plan_hash": response["data"]["plan_hash"],
        },
    )
    result = subprocess.run(
        [sys.executable, "-m", "sanka_extension_typescript_to_rust"],
        input=json.dumps(encode_request(approved)),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = Path(json.loads(result.stdout)["data"]["output"])
    assert (output / "src" / "lib.rs").is_file()


@needs_node
def test_plan_is_independent_of_checkout_location(tmp_path: Path) -> None:
    plans = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        project(root)
        plans.append(handle(request(root)).data)
    assert plans[0] == plans[1]


@needs_node
@needs_rust
def test_source_rust_parity(tmp_path: Path, node_tools: Path) -> None:
    project(tmp_path)
    output = apply(tmp_path)
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    tested = handle(dataclasses.replace(request(tmp_path), command="test"))
    assert tested.outcome == "success", tested.error
    verified = handle(dataclasses.replace(request(tmp_path), command="verify"))
    assert verified.outcome == "success", verified.error
    assert verified.data["source"] == [
        {
            "id": "get.health",
            "method": "GET",
            "path": "/health",
            "status": 200,
            "body": PAYLOAD,
            "media_type": "application/json",
        }
    ]
    assert verified.data["candidate"] == verified.data["source"]
    assert verified.data["scenario_origin"] == "default"
    assert verified.data["comparison"]["ok"] is True
    assert verified.data["rust_version"] == "rustc 1.93.1"
    assert verified.data["node_version"].startswith("v22.")
    assert {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    } == before
    fmt = subprocess.run(
        ["cargo", "fmt", "--check"], cwd=output, capture_output=True, text=True, check=False
    )
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr


@needs_node
@needs_rust
def test_replay_detects_candidate_changes(tmp_path: Path, node_tools: Path) -> None:
    project(tmp_path)
    output = apply(tmp_path)
    library = output / "src" / "lib.rs"
    # Python equality treats True == 1; JSON contracts must preserve the type.
    library.write_text(library.read_text().replace("[1,true,null", "[1,1,null"))
    req = dataclasses.replace(request(tmp_path), command="verify")
    response = handle(req)
    assert response.outcome == "error"
    assert response.error is not None
    assert response.error.code == "SANKA_EXTENSION_PARITY_FAILED"
    report = json.loads((tmp_path / ".sanka/rust/verify.json").read_text())
    assert report["ok"] is False
    assert report["candidate_digest"]
    assert json.dumps(report["source"]) != json.dumps(report["candidate"])
    lock = output / "Cargo.lock"
    lock.write_text(lock.read_text() + "\n# changed\n")
    response = handle(req)
    assert response.outcome == "error"
    assert response.error is not None
    assert "Cargo.lock differs" in response.error.message
    assert not (tmp_path / ".sanka/rust/verify.json").exists()


@needs_node
def test_replay_requires_current_applied_plan(tmp_path: Path) -> None:
    project(tmp_path)
    req = dataclasses.replace(request(tmp_path), command="verify")
    assert handle(req).outcome == "error"
    output = apply(tmp_path)
    extra = output / "extra.rs"
    extra.symlink_to(tmp_path / "src" / "app.ts")
    response = handle(req)
    assert response.error is not None and "regular files" in response.error.message
    extra.unlink()
    (tmp_path / "src" / "app.ts").write_text(source() + "\n// changed\n")
    response = handle(req)
    assert response.error is not None
    assert "differs from the applied plan" in response.error.message
