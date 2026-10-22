# SPDX-License-Identifier: Apache-2.0
"""Install a built trust-audit release into the public CLI and run its whole lifecycle.

The release directory is served over loopback HTTPS with an ephemeral certificate that
only the child CLI trusts. Wheel hashing, metadata checks and isolated installation
stay enabled. The check proves apply writes the patch directory and leaves every
project file byte-identical.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

EXTENSION_ID = "sanka/trust-audit"
MANIFEST_NAME = "sanka-extension-trust-audit.json"
PROJECT = {
    "requirements.txt": "requests==2.32.3\ncertifi==2024.2.2\n",
    ".nvmrc": "v22.20.0\n",
    "go.mod": "module example.com/app\n\ngo 1.26.3\n",
    "Dockerfile": "FROM golang:1.26.4-alpine AS build\nFROM eclipse-temurin:21-jre\n",
    "app/client.py": "import requests\nrequests.get(URL, verify=False)\n",
}
PATCHED = {".nvmrc", "Dockerfile", "go.mod", "requirements.txt"}


def run(*args: str, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"{args} failed ({result.returncode})\n{result.stdout}\n{result.stderr}")
    return result.stdout


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def check(release: Path, cli_version: str, work: Path) -> dict[str, object]:
    uv = shutil.which("uv")
    openssl = shutil.which("openssl")
    git = shutil.which("git")
    if not uv or not openssl or not git:
        raise RuntimeError("uv, OpenSSL and git are required")
    # No user configuration, credentials or installed Sanka state is inherited.
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
    env |= {"UV_NO_CONFIG": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "SANKA_HOME"):
        (work / name.lower()).mkdir()
        env[name] = str(work / name.lower())
    cli_env = work / "cli"
    run(uv, "venv", "--python", "3.12", str(cli_env), cwd=work, env=env)
    run(
        uv,
        "pip",
        "install",
        "--python",
        str(cli_env / "bin/python"),
        f"sanka-cli=={cli_version}",
        cwd=work,
        env=env,
    )

    cert, key, config = work / "localhost.pem", work / "localhost.key", work / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
        "basicConstraints=critical,CA:TRUE\n"
    )
    run(
        openssl,
        *("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"),
        *("-config", str(config), "-keyout", str(key), "-out", str(cert)),
        cwd=work,
        env=env,
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(release))
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env["SSL_CERT_FILE"] = str(cert)
        manifest = json.loads((release / MANIFEST_NAME).read_text())
        for wheel in manifest["wheels"]:
            if sha(release / wheel["name"]) != wheel["sha256"]:
                raise AssertionError(f"Release wheel differs from its manifest: {wheel['name']}")
            wheel["url"] = f"https://localhost:{server.server_port}/{wheel['name']}"
        catalog = work / "marketplace"
        catalog.mkdir()
        (catalog / "extension.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (catalog / "marketplace.json").write_text(
            json.dumps(
                {
                    "schema_version": "sanka-marketplace/v1",
                    "extensions": [{"id": EXTENSION_ID, "manifest": "extension.json"}],
                }
            )
        )
        project = work / "project"
        for name, content in PROJECT.items():
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            (project / name).write_text(content)
        before = {name: sha(project / name) for name in PROJECT}
        cli = str(cli_env / "bin/sanka")

        def sanka(*args: str) -> dict[str, object]:
            output = run(cli, *args, "--json", cwd=project, env=env)
            result: dict[str, object] = json.loads(output)
            return result

        run(cli, "extension", "marketplace", "add", str(catalog), "--name", "release", "--trust",
            cwd=project, env=env)  # fmt: skip
        run(cli, "extension", "add", EXTENSION_ID, "--marketplace", "release", cwd=project, env=env)
        sanka("scan", ".")
        sanka("plan", ".", "--to", "trust-patches")
        plan_hash = json.loads((project / ".sanka/plan.json").read_text())["plan_hash"]
        sanka("apply", "--plan-hash", plan_hash)
    finally:
        server.shutdown()

    output = project / "sanka-trust-patches"
    if {name: sha(project / name) for name in PROJECT} != before:
        raise AssertionError("apply modified a project file")
    for name in ("MANIFEST.json", "README.md", "trust-audit.patch"):
        if not (output / name).is_file():
            raise AssertionError(f"patch directory is missing {name}")
    copies = {p.relative_to(output / "files").as_posix() for p in (output / "files").rglob("*")}
    if {name for name in copies if (output / "files" / name).is_file()} != PATCHED:
        raise AssertionError(f"unexpected patched files: {sorted(copies)}")
    # The combined patch must reproduce the full copies on a clean checkout.
    clean = work / "clean"
    clean.mkdir()
    for name, content in PROJECT.items():
        (clean / name).parent.mkdir(parents=True, exist_ok=True)
        (clean / name).write_text(content)
    run(git, "init", "-q", cwd=clean, env=env)
    run(git, "apply", str(output / "trust-audit.patch"), cwd=clean, env=env)
    for name in PATCHED:
        if (clean / name).read_bytes() != (output / "files" / name).read_bytes():
            raise AssertionError(f"patch and full copy differ for {name}")
    return {
        "extension": EXTENSION_ID,
        "version": manifest["version"],
        "cli": cli_version,
        "plan_hash": plan_hash,
        "wheels": {wheel["name"]: wheel["sha256"] for wheel in manifest["wheels"]},
        "patched": sorted(PATCHED),
        "result": "passed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--cli-version", default="0.3.11")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="trust-audit-acceptance-") as directory:
        report = check(args.release_dir.resolve(), args.cli_version, Path(directory))
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Trust audit acceptance passed with sanka-cli {args.cli_version}")


if __name__ == "__main__":
    main()
