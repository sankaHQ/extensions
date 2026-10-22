# SPDX-License-Identifier: Apache-2.0
"""Opt-in, offline source execution in a disposable copy (not a sandbox)."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .capture import JAVA, canonical, digest, inventory


def run(command: list[str], cwd: Path, env: dict[str, str], timeout: int = 180) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"execution failed: {error}") from error
    if result.returncode:
        raise ValueError(
            f"{Path(command[0]).name} failed ({result.returncode}):\n"
            f"{result.stdout[-8000:]}\n{result.stderr[-4000:]}"
        )
    return result.stdout + result.stderr


def environment(home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    env = {
        k: v for k, v in os.environ.items() if k in {"PATH", "JAVA_HOME", "SYSTEMROOT", "TMPDIR"}
    }
    env.update(
        HOME=str(home),
        LANG="C.UTF-8",
        PYTHONHASHSEED="0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        MAVEN_OPTS=f"-Xmx512m -Duser.home={home}",
    )
    return env


def source_copy(
    root: Path, work: Path, captured: dict[str, Any], scenarios: list[dict[str, Any]]
) -> Path:
    if inventory(root) != captured["source_inventory"]:
        raise ValueError("source changed before replay")
    work.mkdir()
    shutil.copyfile(root / "pom.xml", work / "pom.xml")
    for name in captured["source_inventory"]:
        if name.startswith("src/main/java/"):
            destination = work / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, destination)
    harness = work / "src/test/java/sanka/probe/ReplayTest.java"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        (JAVA / "ReplayTest.java.txt")
        .read_text()
        .replace("APPLICATION_CLASS", captured["application"])
    )
    (work / "scenarios.json").write_text(canonical(scenarios))
    (work / "settings.xml").write_text(
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"/>\n'
    )
    return work


def source_run(work: Path, *, offline: bool = True) -> dict[str, Any]:
    if os.environ.get("SANKA_JAVA_REPLAY") != "1":
        raise ValueError("set SANKA_JAVA_REPLAY=1 only for source and candidate code you trust")
    maven = Path(os.environ.get("SANKA_JAVA_MAVEN", ""))
    repository = Path(os.environ.get("SANKA_JAVA_MAVEN_REPO", ""))
    if (
        not maven.is_absolute()
        or not maven.is_file()
        or not repository.is_absolute()
        or not repository.is_dir()
    ):
        raise ValueError(
            "explicit absolute SANKA_JAVA_MAVEN and prepared SANKA_JAVA_MAVEN_REPO required"
        )
    env = environment(work / "home")
    versions = run([str(maven), "--version"], work, env)
    if "Apache Maven 3.9.16" not in versions or "Java version: 21." not in versions:
        raise ValueError("replay requires Maven 3.9.16 and a Java 21 JDK")
    env.update(
        SANKA_PROBE_CASES=str(work / "scenarios.json"),
        SANKA_PROBE_OUTPUT=str(work / "observations.json"),
    )
    command = [
        str(maven),
        "-B",
        "-ntp",
        "-s",
        str(work / "settings.xml"),
        "-gs",
        str(work / "settings.xml"),
        f"-Dmaven.repo.local={repository}",
        "-Dtest=sanka.probe.ReplayTest",
        "-DfailIfNoTests=true",
        "-Dsurefire.failIfNoSpecifiedTests=true",
    ]
    if offline:
        command.append("--offline")
    run([*command, "test"], work, env)
    # Hash the executable dependency closure actually available to offline Maven.
    dependencies = {
        p.relative_to(repository).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(repository.rglob("*"))
        if p.is_file() and p.suffix in {".jar", ".pom"}
    }
    return {
        "toolchain": versions.strip(),
        "dependency_digest": digest(dependencies),
        "dependency_count": len(dependencies),
        "transport": "Spring MockMvc",
    }
