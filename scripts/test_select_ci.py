# SPDX-License-Identifier: Apache-2.0
from scripts.select_ci import affected


def test_changes_include_transitive_consumers_and_fail_closed() -> None:
    packages = {"sdk": set(), "capture": {"sdk"}, "go": {"capture"}, "flask": {"sdk"}}
    assert affected(packages, ["packages/capture/src/parser.py"]) == {"capture", "go"}
    assert affected(packages, ["packages/sdk/src/protocol.py"]) == set(packages)
    assert affected(packages, ["packages/go/tests/test_http.py"]) == {"go"}
    for path in [
        "uv.lock",
        "scripts/build_release.py",
        "packages/new/src/main.py",
        "packages/go/tests/fixtures/app.py",
        "packages/go/tests/conftest.py",
    ]:
        assert affected(packages, [path]) == set(packages)
