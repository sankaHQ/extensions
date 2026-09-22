# SPDX-License-Identifier: Apache-2.0
"""Run the pinned React Native example against release wheels or the published catalog.

The fixture supplies assertions, the source app and its toolchain checks. Only its
installer is replaced: no sibling checkout, editable extension or workspace SDK is
installed. Both targets need Node 22 on PATH; SwiftUI additionally requires macOS
and forwards DEVELOPER_DIR when it is set.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.qualify_api_release import EXAMPLES_REVISION, installer, load  # noqa: E402

TAG = "mobile-converters-v0.1.0a1"
EXAMPLE = "react-native/task-list"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--target", choices=["swiftui", "compose"], required=True)
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
    consumer = installer(examples, release, args.published_revision, tag=TAG)
    module = load(examples / EXAMPLE / "check.py", "mobile_acceptance")
    module.Candidate = consumer
    # The fixture's own entry point writes its report skeleton, runs accept() and
    # records a failure before re-raising; only its status decides the outcome.
    sys.argv = [
        str(module.__file__),
        "--target",
        args.target,
        "--report",
        str(args.report.resolve()),
    ]
    module.main()
    report = json.loads(args.report.read_text())
    if report["status"] != "passed_within_scope":
        raise RuntimeError(f"React Native {args.target} acceptance did not pass")
    print(f"Public consumer {args.target} acceptance passed: {args.report}")


if __name__ == "__main__":
    main()
