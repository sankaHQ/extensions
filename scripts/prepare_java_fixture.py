# SPDX-License-Identifier: Apache-2.0
"""Warm Maven's dependencies for the trusted synthetic fixture, then replay offline."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from sanka_java_migration.capture import capture
from sanka_java_migration.postgres import fixture_databases
from sanka_java_migration.replay import source_copy, source_run

from sanka_http_replay import load_scenarios

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "packages/sanka-extension-java-to-python/tests/fixtures/greetings"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    args = parser.parse_args()
    fixture = args.fixture.resolve()
    captured = capture(fixture)
    if not captured["ready"]:
        raise ValueError("synthetic fixture is outside the qualified source profile")
    scenarios = load_scenarios(fixture / "sanka-verify.json")
    with tempfile.TemporaryDirectory(prefix="sanka-java-prepare-") as directory:
        parent = Path(directory).resolve()
        work = source_copy(fixture, parent / "source", captured, scenarios)
        model = captured.get("persistence")
        if model:
            with fixture_databases(parent, model) as databases:
                print(
                    json.dumps(
                        source_run(work, offline=False, database=databases["source"], model=model),
                        indent=2,
                    )
                )
        else:
            source_run(work, offline=False)
            print(json.dumps(source_run(work), indent=2))


if __name__ == "__main__":
    main()
