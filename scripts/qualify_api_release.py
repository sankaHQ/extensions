# SPDX-License-Identifier: Apache-2.0
"""Run the pinned examples against release wheels or the published Git catalog.

The fixture supplies assertions, source apps and toolchains. Only its installer is
replaced: no sibling checkout, editable extension or workspace SDK is installed.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

EXAMPLES_REVISION = "e4b9990ccc21ec3d1e775b0955f021f2083fc524"
TAG = "api-converters-v0.1.0a8"


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installer(examples: Path, release: Path, revision: str | None, tag: str = TAG) -> Any:
    sys.path.insert(0, str(examples / "scripts"))
    candidate = load(examples / "scripts/candidate.py", "release_candidate_base")

    class ReleaseConsumer(candidate.Candidate):  # type: ignore[misc, name-defined]
        def _setup(self) -> None:
            self.env = {
                k: os.environ[k]
                for k in ("PATH", "TMPDIR", "LANG", "SYSTEMROOT", "DEVELOPER_DIR")
                if k in os.environ
            }
            self.env.update(
                UV_NO_CONFIG="1",
                GIT_CONFIG_GLOBAL=os.devnull,
                GIT_CONFIG_NOSYSTEM="1",
                GIT_TERMINAL_PROMPT="0",
                PYTHONHASHSEED="0",
            )
            for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "SANKA_HOME"):
                directory = self.root / name.lower()
                directory.mkdir()
                self.env[name] = str(directory)
            shutil.copytree(
                self.example,
                self.project,
                ignore=lambda _p, names: [
                    n for n in names if n in candidate.IGNORED or n.endswith(".pyc")
                ],
            )
            self.run("uv", "venv", "--python", "3.12", str(self.root / "cli"))
            self.run(
                "uv",
                "pip",
                "install",
                "--python",
                str(self.root / "cli/bin/python"),
                "sanka-cli==" + os.environ.get("SANKA_API_RELEASE_CLI_VERSION", "0.3.0"),
            )
            self.report["cli_dependencies"] = self.run(
                "uv", "pip", "freeze", "--python", str(self.root / "cli/bin/python")
            ).splitlines()
            data = json.loads((release / f"sanka-extension-{self.extension}.json").read_text())
            self.report.update(
                release_tag=tag,
                release_status="experimental-published" if revision else "release-candidate",
                extension_revision=revision,
                manifest_sha256=hashlib.sha256(
                    json.dumps(data, sort_keys=True).encode()
                ).hexdigest(),
                wheels={w["name"]: w["sha256"] for w in data["wheels"]},
            )
            if revision:
                checkout = self.root / "published-catalog"
                self.run("git", "init", str(checkout))
                self.run(
                    "git",
                    "fetch",
                    "--depth",
                    "1",
                    "https://github.com/sankaHQ/extensions.git",
                    revision,
                    cwd=checkout,
                )
                observed = self.run("git", "rev-parse", "FETCH_HEAD", cwd=checkout).strip()
                if observed != revision:
                    raise ValueError("Published catalog revision mismatch")
                public_manifest = json.loads(
                    self.run(
                        "git",
                        "show",
                        f"FETCH_HEAD:packages/sanka-extension-{self.extension}/extension.json",
                        cwd=checkout,
                    )
                )
                if public_manifest != data:
                    raise ValueError("Public Git manifest differs from released asset")
                self.cli(
                    "extension",
                    "marketplace",
                    "add",
                    "https://github.com/sankaHQ/extensions.git",
                    "--revision",
                    revision,
                    "--name",
                    "release",
                    "--trust",
                )
            else:
                # Preserve the checked-in contract and digests; only wheel URLs
                # point at a task-owned HTTPS server before publication.
                config = self.root / "openssl.cnf"
                config.write_text(
                    "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                    "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
                    "basicConstraints=critical,CA:TRUE\n"
                )
                cert, key = self.root / "localhost.pem", self.root / "localhost.key"
                self.run(
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-config",
                    str(config),
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                )
                self.server = http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    functools.partial(candidate.QuietHandler, directory=str(release)),
                )
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
                marketplace = self.root / "marketplace"
                marketplace.mkdir()
                for wheel in data["wheels"]:
                    wheel["url"] = f"https://localhost:{self.server.server_port}/{wheel['name']}"
                (marketplace / "extension.json").write_text(json.dumps(data))
                (marketplace / "marketplace.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "sanka-marketplace/v1",
                            "extensions": [{"id": data["id"], "manifest": "extension.json"}],
                        }
                    )
                )
                self.cli(
                    "extension",
                    "marketplace",
                    "add",
                    str(marketplace),
                    "--name",
                    "release",
                    "--trust",
                )
            self.cli("extension", "add", data["id"], "--marketplace", "release")

        def cli(self, *args: str) -> dict[str, Any]:
            command = [*args, "--json"]
            self.report["commands"].append(command)
            result: dict[str, Any] = json.loads(
                self.run(
                    str(self.root / "cli/bin/sanka"),
                    *command,
                    env=(
                        self.env | {"SSL_CERT_FILE": str(self.root / "localhost.pem")}
                        if not revision
                        else self.env
                    ),
                )
            )
            if result.get("outcome") == "error":
                raise RuntimeError(f"CLI reported failure: {result}")
            return result

    return ReleaseConsumer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--target", choices=["go", "rust"], required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--published-revision")
    args = parser.parse_args()
    if args.published_revision and not re.fullmatch(r"[0-9a-f]{40}", args.published_revision):
        parser.error("Published catalog must pin a full immutable commit")
    examples, release = args.examples.resolve(), args.release.resolve()
    observed = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=examples, text=True
    ).strip()
    if observed != EXAMPLES_REVISION:
        raise ValueError("Acceptance examples must be at the qualified immutable commit")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=examples, check=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.unlink(missing_ok=True)
    consumer = installer(examples, release, args.published_revision)
    if args.target == "go":
        module = load(examples / "flask/status-api/scripts/accept_migration.py", "go_acceptance")
        module.Candidate = consumer
        report = module.accept()
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        if report["outcome"] != "passed":
            raise RuntimeError("Go acceptance did not pass")
    else:
        sys.path.insert(0, str(examples / "express/status-api"))
        module = load(examples / "express/status-api/check.py", "rust_acceptance")
        module.Candidate = consumer
        sys.argv = [str(module.__file__), "--report", str(args.report.resolve())]
        module.main()
    print(f"Public consumer {args.target} acceptance passed: {args.report}")


if __name__ == "__main__":
    main()
