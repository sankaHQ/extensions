# SPDX-License-Identifier: Apache-2.0
"""Warm Maven's dependencies for the trusted synthetic fixture, then replay offline."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sanka_java_migration.capture import capture
from sanka_java_migration.replay import source_copy, source_run

from sanka_http_replay import load_scenarios

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "packages/sanka-extension-java-to-python/tests/fixtures/greetings"


def main() -> None:
    captured = capture(FIXTURE)
    if not captured["ready"]:
        raise ValueError("synthetic fixture is outside the qualified source profile")
    scenarios = load_scenarios(FIXTURE / "sanka-verify.json")
    with tempfile.TemporaryDirectory(prefix="sanka-java-prepare-") as directory:
        work = source_copy(FIXTURE, Path(directory).resolve() / "source", captured, scenarios)
        source_run(work, offline=False)
        print(json.dumps(source_run(work), indent=2))


if __name__ == "__main__":
    main()
