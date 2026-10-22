# SPDX-License-Identifier: Apache-2.0
"""Start/stop a task-owned PostgreSQL 17.11 fixture server, never an existing cluster."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "resume", "stop"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.absolute()
    if root != root.resolve() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("fixture server root must be a canonical non-symlink directory")
    executable = shutil.which("pg_ctl")
    initdb = shutil.which("initdb")
    if not executable or not initdb:
        raise ValueError("PostgreSQL 17.11 pg_ctl and initdb must be on PATH")
    version = subprocess.check_output([executable, "--version"], text=True)
    if "17.11" not in version:
        raise ValueError("PostgreSQL 17.11 tools required")
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT"}}
    env.update(LC_ALL="C", LANG="C")
    if args.command in {"stop", "resume"}:
        config = json.loads((root / "owner.json").read_text())
        if config != {"root": str(root), "purpose": "sanka-java-fixture"}:
            raise ValueError("not a task-owned fixture server")
        if args.command == "stop":
            subprocess.run(
                [executable, "-D", str(root / "data"), "stop", "-m", "fast"], env=env, check=True
            )
        else:
            connection = json.loads((root / "connection.json").read_text())
            start(executable, root, connection["port"], env)
        return
    if root.exists():
        raise ValueError("fixture root already exists; inspect its owner before reuse")
    root.mkdir(mode=0o700, parents=True)
    password = secrets.token_urlsafe(32)
    password_file = root / "password"
    password_file.write_text(password)
    password_file.chmod(0o600)
    try:
        subprocess.run(
            [
                initdb,
                "-D",
                str(root / "data"),
                "-U",
                "sanka_java_fixture",
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
                "--pwfile=" + str(password_file),
            ],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    finally:
        password_file.unlink(missing_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config_file = root / "connection.json"
    config_file.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": port,
                "user": "sanka_java_fixture",
                "password": password,
                "database": "postgres",
            }
        )
    )
    config_file.chmod(0o600)
    (root / "owner.json").write_text(
        json.dumps({"root": str(root), "purpose": "sanka-java-fixture"})
    )
    start(executable, root, port, env)
    print(f"Fixture connection configuration saved privately to {config_file}")


def start(executable: str, root: Path, port: int, env: dict[str, str]) -> None:
    subprocess.run(
        [
            executable,
            "-D",
            str(root / "data"),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -k '' -c max_connections=30 -c shared_buffers=32MB",
            "start",
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
