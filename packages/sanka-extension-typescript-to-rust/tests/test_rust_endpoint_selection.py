# SPDX-License-Identifier: Apache-2.0
"""Selected Express endpoints remain cumulative through the public lifecycle."""

import dataclasses
import os
import subprocess
from pathlib import Path

from test_typescript_to_rust import needs_node, project, request, source

from sanka_extension_typescript_to_rust.adapter import handle


@needs_node
def test_partial_then_cumulative_rust(tmp_path: Path):
    project(tmp_path, source('app.get("/two", (_req, res) => res.json({value: 2}));\n'))
    base = request(tmp_path)
    for selected in (["GET /health"], ["GET /health", "GET /two"]):
        req = dataclasses.replace(
            base, configuration=base.configuration | {"selected_endpoints": selected}
        )
        planned = handle(req)
        assert planned.outcome == "success", planned.error
        assert [
            row["method"] + " " + row["path"] for row in planned.data["capture"]["routes"]
        ] == selected
        applied = handle(
            dataclasses.replace(
                req,
                command="apply",
                reviewed_plan_hash="reviewed",
                configuration=req.configuration
                | {"extension_plan_hash": planned.data["plan_hash"]},
            )
        )
        assert applied.outcome == "success", applied.error
        output = Path(str(applied.data["output"]))
        note = output / "notes.txt"
        if len(selected) == 1:
            note.write_text("keep")
        else:
            assert note.read_text() == "keep"
        if os.getenv("SANKA_RUST_TESTS") == "1":
            result = subprocess.run(
                ["cargo", "test", "--locked", "-j", "2"],
                cwd=output,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
