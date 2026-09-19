# SPDX-License-Identifier: Apache-2.0
"""Bounded replay of the qualified GET contract with cargo and the real Express source."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sanka_ts_capture import node_executable, node_version, transpile_sources

from .capture import canonical, capture, digest
from .render import RUST_VERSION, render, rust_string

NODE_MAJOR = 22
PINNED_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "contract.json")
DATABASE_PINNED = ("migrations/0001_initial.up.sql", "migrations/0001_initial.down.sql")
TARGET_DATABASE = "SANKA_RUST_TARGET_TEST_DATABASE_URL"
SOURCE_DATABASE = "SANKA_RUST_SOURCE_TEST_DATABASE_URL"
RESERVED = ("tests/sanka_contract_probe.rs", "sanka-observed.json")
EXPRESS_RUNNER = Path(__file__).resolve().parent / "node" / "express-run.js"
CARGO_TIMEOUT = 1200

PROBE = """// SPDX-License-Identifier: Apache-2.0
use axum::body::{Body, to_bytes};
use axum::http::Request;
use tower::ServiceExt;

#[tokio::test]
async fn sanka_contract_replay() {
    let paths = [@PATHS@];
@SETUP@    let mut observed = Vec::new();
    for path in paths {
        let request = Request::builder()
            .method("GET")
            .uri(path)
            .body(Body::empty())
            .expect("request");
        let response = @APP@
            .oneshot(request)
            .await
            .expect("response");
        let status = response.status().as_u16();
        let media_type = response
            .headers()
            .get("content-type")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .to_string();
        let body = to_bytes(response.into_body(), 1_048_576)
            .await
            .expect("body");
        let value: serde_json::Value = serde_json::from_slice(&body).expect("JSON body");
        observed.push(serde_json::json!({
            "path": path,
            "status": status,
            "media_type": media_type,
            "body": value,
        }));
    }
    let encoded = serde_json::to_vec(&observed).expect("encode");
    std::fs::write("sanka-observed.json", encoded).expect("write");
}
"""

PROBE_SETUP = """    let url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&url)
        .await
        .expect("connect to DATABASE_URL");
"""


def _database_url(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.startswith(("postgres://", "postgresql://")):
        raise ValueError(
            f"replay with database_layer sqlx requires {name} to name a dedicated "
            "PostgreSQL test database (postgres://...)"
        )
    return value


def _cargo(
    command: list[str],
    cwd: Path,
    target_dir: Path | None = None,
    database_url: str | None = None,
) -> str:
    environment = os.environ | {
        "CARGO_TERM_COLOR": "never",
        "CARGO_NET_RETRY": "2",
        "CARGO_INCREMENTAL": "0",
    }
    environment.pop("DATABASE_URL", None)
    if database_url is not None:
        environment["DATABASE_URL"] = database_url
    if target_dir is not None:
        # The candidate is compiled from a temporary copy; keep build artifacts beside the
        # output so repeated test/verify runs do not rebuild every dependency. An explicit
        # CARGO_TARGET_DIR in the environment (CI caches, local development) wins.
        environment.setdefault("CARGO_TARGET_DIR", str(target_dir))
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=CARGO_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"replay process failed: {error}") from error
    if result.returncode:
        raise ValueError("replay process failed: " + (result.stdout + result.stderr)[-4000:])
    return result.stdout


def _node(command: list[str], cwd: Path, modules: Path, database_url: str | None = None) -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "NODE_PATH": str(modules),
        "NODE_OPTIONS": "",
    }
    if database_url is not None:
        environment["DATABASE_URL"] = database_url
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"source replay failed: {error}") from error
    if result.returncode:
        raise ValueError("source replay failed: " + (result.stdout + result.stderr)[-4000:])


def _toolchain(candidate: Path) -> str:
    version = _cargo(["rustc", "--version"], candidate).split()
    if len(version) < 2 or version[1] != RUST_VERSION:
        raise ValueError(f"replay requires the qualified Rust {RUST_VERSION} toolchain")
    cargo = _cargo(["cargo", "--version"], candidate).split()
    if len(cargo) < 2 or cargo[1] != RUST_VERSION:
        raise ValueError(f"replay requires cargo from the Rust {RUST_VERSION} toolchain")
    return " ".join(version[:2])


def _node_modules(root: Path, packages: tuple[str, ...]) -> Path:
    explicit = os.environ.get("SANKA_NODE_TOOLS")
    modules = Path(explicit) if explicit else root / "node_modules"
    for package in packages:
        if not (modules / package / "package.json").is_file():
            raise ValueError(
                f"verify requires the source project's {package} installation under "
                "node_modules, or SANKA_NODE_TOOLS pointing at a directory that contains it"
            )
    return modules.resolve()


def _snapshot(output: Path) -> dict[str, bytes]:
    if output.is_symlink() or not output.is_dir():
        raise ValueError("candidate must be a regular directory")
    if (output / "target").exists():
        raise ValueError("remove the target build directory from the candidate before replay")
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
            if size > 10_000_000 or len(snapshot) >= 1000:
                raise ValueError("candidate exceeds replay limits")
            snapshot[path.relative_to(output).as_posix()] = path.read_bytes()
    return snapshot


def replay(root: Path, output: Path, captured: dict[str, Any], command: str) -> dict[str, Any]:
    if captured["gaps"]:
        raise ValueError("cannot replay unsupported source behavior")
    if any("write" in route or "lookup" in route for route in captured["routes"]):
        raise ValueError(
            "write replay requires the versioned shared HTTP scenario adapter; "
            "use the qualified PostgreSQL lifecycle test until it is adopted"
        )
    if not output.is_dir():
        raise ValueError("apply the reviewed plan before testing")
    snapshot = _snapshot(output)
    # Bind evidence to the exact bytes tested, including manually repaired handlers.
    candidate_hash = digest(
        {key: hashlib.sha256(value).hexdigest() for key, value in snapshot.items()}
    )
    config = captured["configuration"]
    database = config.get("database_layer") == "sqlx"
    target_url = _database_url(TARGET_DATABASE) if database else None
    source_url = _database_url(SOURCE_DATABASE) if database and command == "verify" else None
    if database and command == "verify" and source_url == target_url:
        raise ValueError("source and target test databases must be distinct")
    expected_files = render(captured)
    for name in PINNED_FILES + (DATABASE_PINNED if database else ()):
        if snapshot.get(name) != expected_files[name].encode():
            raise ValueError(f"candidate {name} differs from the applied plan")
    source_text = (root / config["source_file"]).read_text(encoding="utf-8")
    if capture(root, config) != captured:
        raise ValueError("source changed before replay")
    paths = [str(route["path"]) for route in captured["routes"]]
    with tempfile.TemporaryDirectory(prefix="sanka-rust-replay-") as temporary:
        workspace = Path(temporary)
        candidate = workspace / "candidate"
        candidate.mkdir()
        for name, content in snapshot.items():
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if any((candidate / name).exists() for name in RESERVED):
            raise ValueError("candidate uses reserved replay filenames")
        (candidate / "tests").mkdir(exist_ok=True)
        (candidate / "tests" / "sanka_contract_probe.rs").write_text(
            PROBE.replace("@PATHS@", ", ".join(rust_string(path) for path in paths))
            .replace("@SETUP@", PROBE_SETUP if database else "")
            .replace(
                "@APP@",
                "migrated_backend::app(pool.clone())" if database else "migrated_backend::app()",
            )
        )
        rust_version = _toolchain(candidate)
        offline = ["--offline"] if os.environ.get("SANKA_RUST_OFFLINE") == "1" else []
        target_dir = output.parent / "rust-target"
        target_dir.mkdir(exist_ok=True)
        _cargo(
            ["cargo", "test", "--locked", "--quiet", *offline, "--test", "sanka_contract_probe"],
            candidate,
            target_dir.resolve(),
            target_url,
        )
        actual = json.loads((candidate / "sanka-observed.json").read_text())
        result: dict[str, Any] = {
            "schema": "sanka.typescript-to-rust.replay/v1",
            "command": command,
            "rust_version": rust_version,
            "source_digest": captured["source_digest"],
            "candidate_digest": candidate_hash,
            "scope": (
                "GET status, JSON body and media type for captured routes"
                if command == "verify"
                else "axum handler execution and JSON response parsing"
            ),
            "complete_backend": False,
            "candidate": actual,
            "ok": len(actual) == len(paths)
            and all(
                item["status"] == route["status"] and item["media_type"] == "application/json"
                for item, route in zip(actual, captured["routes"], strict=True)
            ),
        }
        if command == "verify":
            node = node_executable()
            version = node_version(node)
            if version[0] != NODE_MAJOR:
                raise ValueError(
                    f"verify requires Node.js {NODE_MAJOR}.x to execute the source "
                    f"(found {version[0]}); set SANKA_NODE"
                )
            modules = _node_modules(root, ("express", "pg") if database else ("express",))
            transpiled = transpile_sources({config["source_file"]: source_text}, node=node)
            source_directory = workspace / "source"
            source_directory.mkdir()
            (source_directory / "app.js").write_text(transpiled[config["source_file"]])
            observed = source_directory / "source-observed.json"
            _node(
                [node, str(EXPRESS_RUNNER), "app.js", canonical(paths), str(observed)],
                source_directory,
                modules,
                source_url,
            )
            expected = json.loads(observed.read_text())
            result.update(
                source=expected,
                node_version="v" + ".".join(str(item) for item in version),
                ok=result["ok"] and canonical(actual) == canonical(expected),
            )
        if capture(root, config) != captured:
            raise ValueError("source changed during replay; discard observations")
        if _snapshot(output) != snapshot:
            raise ValueError("candidate changed during replay; discard observations")
        return result
