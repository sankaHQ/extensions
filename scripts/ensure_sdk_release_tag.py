# SPDX-License-Identifier: Apache-2.0
"""Create an immutable SDK tag or verify its exact peeled source before upload."""

from __future__ import annotations

import re
import subprocess
import sys

TAG = "sdk-v0.1.0a4"


def remote_revision() -> str | None:
    ref = f"refs/tags/{TAG}"
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", ref, ref + "^{}"],
        check=True,
        capture_output=True,
        text=True,
    )
    refs = {}
    for line in result.stdout.splitlines():
        digest, name = line.split()
        if name not in {ref, ref + "^{}"} or not re.fullmatch(r"[0-9a-f]{40}", digest):
            raise RuntimeError("SDK remote tag returned an invalid identity")
        if name in refs:
            raise RuntimeError("SDK remote tag returned duplicate identities")
        refs[name] = digest
    return refs.get(ref + "^{}", refs.get(ref))


def ensure_tag(expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ValueError("SDK release requires the full reviewed source commit")
    current = remote_revision()
    if current is None:
        # Never force: a racing tag creation must fail, not replace provenance.
        subprocess.run(["git", "push", "origin", f"{expected}:refs/tags/{TAG}"], check=True)
        current = remote_revision()
    if current != expected:
        raise RuntimeError("SDK tag does not match the reviewed source; refusing publication")


if __name__ == "__main__":
    ensure_tag(sys.argv[1])
