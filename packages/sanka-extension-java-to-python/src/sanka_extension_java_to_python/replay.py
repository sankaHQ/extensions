# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from sanka_java_migration.capture import digest, inventory
from sanka_java_migration.replay import environment, run, source_copy, source_run

from sanka_http_replay import compare, validate_observations

from .render import LOCK

RUNNERS = Path(__file__).parent / "runners"


def replay(
    root: Path,
    output: Path,
    captured: dict[str, Any],
    scenarios: list[dict[str, Any]],
    command: str,
) -> dict[str, Any]:
    if os.environ.get("SANKA_JAVA_REPLAY") != "1":
        raise ValueError("set SANKA_JAVA_REPLAY=1 only for source and candidate code you trust")
    python = Path(os.environ.get("SANKA_JAVA_PYTHON", ""))
    if not python.is_absolute() or not python.is_file():
        raise ValueError(
            "SANKA_JAVA_PYTHON must name the prepared target environment's absolute Python path"
        )
    before = inventory(output)
    expected_files = {"app.py", "requirements.lock", "scenarios.json", "test_app.py", "README.md"}
    if set(before) != expected_files:
        raise ValueError("candidate files differ from the first-profile output layout")
    if (output / "requirements.lock").read_text() != LOCK.read_text():
        raise ValueError("candidate dependency lock differs from the reviewed target profile")
    with tempfile.TemporaryDirectory(prefix="sanka-java-replay-") as directory:
        work = Path(directory).resolve()
        candidate = work / "candidate"
        candidate.mkdir()
        for name in before:
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output / name, destination)
        (candidate / "scenarios.json").write_text(json.dumps(scenarios))
        (candidate / "test_app.py").write_text((RUNNERS / "test_candidate.py.txt").read_text())
        (candidate / "observe.py").write_text((RUNNERS / "observe.py.txt").read_text())
        env = environment(work / "home")
        pins = dict(re.findall(r"^([a-zA-Z0-9-]+)==([^\s]+)", LOCK.read_text(), re.M))
        installed = json.loads(
            run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import json,importlib.metadata as m; "
                    "print(json.dumps({d.metadata['Name'].lower().replace('_','-'): "
                    "d.version for d in m.distributions()}))",
                ],
                candidate,
                env,
            )
        )
        if any(installed.get(name) != version for name, version in pins.items()):
            raise ValueError(
                "target interpreter differs from requirements.lock; install using --require-hashes"
            )
        run([str(python), "-m", "compileall", "-q", "app.py"], candidate, env)
        native = run(
            [
                str(python),
                "-m",
                "pytest",
                "-q",
                "test_app.py",
                "-o",
                "addopts=",
                "--junitxml=" + str(work / "pytest.xml"),
            ],
            candidate,
            env,
        )
        tests = ET.parse(work / "pytest.xml").findall(".//testcase")
        if len(tests) != len(scenarios) or any(list(test) for test in tests):
            raise ValueError(
                "native test count differs from scenarios or includes non-passing cases"
            )
        run(
            [
                str(python),
                "observe.py",
                str(candidate / "scenarios.json"),
                str(work / "candidate.json"),
            ],
            candidate,
            env,
        )
        observed = validate_observations(
            json.loads((work / "candidate.json").read_text()), scenarios
        )
        source = None
        runtime: dict[str, Any] = {}
        if command == "verify":
            source_work = source_copy(root, work / "source", captured, scenarios)
            runtime = source_run(source_work)
            source = validate_observations(
                json.loads((source_work / "observations.json").read_text()), scenarios
            )
        report = compare(scenarios, observed, source)
        report.update(
            schema="sanka.java-replay/v1",
            source_digest=captured["source_digest"],
            candidate_digest=digest(before),
            scenarios_digest=digest(scenarios),
            native_tests=native.strip(),
            candidate_observations=observed,
            source_observations=source,
            source_runtime=runtime,
            target_runtime={name: installed[name] for name in pins},
            transport="Spring MockMvc / FastAPI TestClient" if source else "FastAPI TestClient",
        )
    if inventory(output) != before or inventory(root) != captured["source_inventory"]:
        raise ValueError("source or candidate changed while replay was running")
    return report
