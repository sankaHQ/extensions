# SPDX-License-Identifier: Apache-2.0
"""Native list semantics on the records fixture: cursor pages, search, ordering, datetimes.

The fixture is bench task 007. Its viewset combines cursor pagination, SearchFilter, a
custom OrderingFilter with an id tie-break, and ETag logic in ``retrieve``/``update``; the
generator keeps list/create/destroy native and leaves the overridden actions manual.
Every scenario is served by DRF and by the generated app and compared byte for byte on
status, body (including ``next``/``previous`` cursor URLs) and the Allow header.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from adapter_cli import run_cli

FIXTURES = Path(__file__).parent / "fixtures"
PROBE = Path(__file__).parent / "records_parity_probe.py"


def _cursor(**tokens: str) -> str:
    """Encode a cursor exactly as DRF does (token order o, r, p)."""
    ordered = {key: tokens[key] for key in ("o", "r", "p") if key in tokens}
    return base64.b64encode(urlencode(ordered).encode("ascii")).decode("ascii")


PAGE_TWO = _cursor(p="2026-01-01 02:00:00+00:00")
PAGE_THREE = _cursor(o="1", p="2026-01-01 01:00:00+00:00")
BACK_TO_ONE = _cursor(r="1", p="2026-01-01 01:00:00+00:00")
BY_AMOUNT_AFTER_TEN = _cursor(p="10.00")
CONDITIONAL = ["ETag", "Cache-Control", "Vary", "Content-Length"]


def _etag(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return '"' + hashlib.sha256(canonical.encode("utf-8")).hexdigest() + '"'


RECORD_ONE_ETAG = _etag(
    {
        "id": 1,
        "label": "Alpha opening",
        "category": "retail",
        "amount": "10.00",
        "posted_at": "2026-01-01T09:00:00+09:00",
    }
)

SCENARIOS: list[dict[str, Any]] = [
    {"method": "GET", "path": "/api/records/"},
    {"method": "GET", "path": f"/api/records/?cursor={PAGE_TWO}"},
    {"method": "GET", "path": f"/api/records/?cursor={PAGE_THREE}"},
    {"method": "GET", "path": f"/api/records/?cursor={BACK_TO_ONE}"},
    {"method": "GET", "path": "/api/records/?cursor=not-base64!"},
    {"method": "GET", "path": "/api/records/?search=alpha"},
    {"method": "GET", "path": "/api/records/?search=Alpha,ops"},
    {"method": "GET", "path": "/api/records/?search=%22Alpha%20opening%22"},
    {"method": "GET", "path": "/api/records/?search=OPS"},
    {"method": "GET", "path": "/api/records/?search="},
    {"method": "GET", "path": "/api/records/?ordering=amount"},
    {"method": "GET", "path": f"/api/records/?ordering=amount&cursor={BY_AMOUNT_AFTER_TEN}"},
    {"method": "GET", "path": "/api/records/?ordering=-amount,label"},
    {"method": "GET", "path": "/api/records/?ordering=-label"},
    {"method": "GET", "path": "/api/records/?ordering=bogus"},
    {"method": "GET", "path": "/api/records/?ordering=amount&search=al"},
    {"method": "OPTIONS", "path": "/api/records/"},
    {"method": "PUT", "path": "/api/records/", "body": {"label": "x"}},
    {
        "method": "POST",
        "path": "/api/records/",
        "body": {
            "label": "Zulu",
            "category": "ops",
            "amount": "5.00",
            "posted_at": "2026-03-01T10:00:00Z",
        },
    },
    {
        "method": "POST",
        "path": "/api/records/",
        "body": {
            "label": "Naive",
            "category": "ops",
            "amount": "1",
            "posted_at": "2026-03-01T10:00",
        },
    },
    {
        "method": "POST",
        "path": "/api/records/",
        "body": {"label": "Date", "category": "ops", "amount": "1", "posted_at": "2026-03-02"},
    },
    {
        "method": "POST",
        "path": "/api/records/",
        "body": {"label": "Bad", "category": "ops", "amount": "1", "posted_at": "yesterday"},
    },
    {
        "method": "POST",
        "path": "/api/records/",
        "body": {"label": "Null", "category": "ops", "amount": "1", "posted_at": None},
    },
    {
        "method": "POST",
        "path": "/api/records/",
        "body": {
            "label": "Offset",
            "category": "ops",
            "amount": "1",
            "posted_at": "2026-03-01 10:00:00+05:30",
        },
    },
    {"method": "GET", "path": "/api/records/?ordering=-posted_at"},
    # carried ETag logic: retrieve/update/partial_update run the author's code verbatim
    {"method": "GET", "path": "/api/records/1/", "capture": CONDITIONAL},
    {"method": "GET", "path": "/api/records/5/", "capture": CONDITIONAL},
    {
        "method": "GET",
        "path": "/api/records/1/",
        "headers": {"If-None-Match": RECORD_ONE_ETAG},
        "capture": CONDITIONAL,
    },
    {
        "method": "GET",
        "path": "/api/records/1/",
        "headers": {"If-None-Match": '"stale"'},
        "capture": CONDITIONAL,
    },
    {
        "method": "GET",
        "path": "/api/records/1/",
        "headers": {"If-None-Match": "*"},
        "capture": CONDITIONAL,
    },
    {
        "method": "GET",
        "path": "/api/records/1/",
        "headers": {"If-None-Match": '"stale", ' + RECORD_ONE_ETAG},
        "capture": CONDITIONAL,
    },
    {"method": "GET", "path": "/api/records/999/", "capture": CONDITIONAL},
    {
        "method": "PUT",
        "path": "/api/records/3/",
        "body": {
            "label": "Updated fee",
            "category": "ops",
            "amount": 10,
            "posted_at": "2026-01-01T01:30:00-05:00",
        },
        "capture": CONDITIONAL,
    },
    {
        "method": "PATCH",
        "path": "/api/records/2/",
        "body": {"posted_at": "2026-02-03T23:45:00-08:00"},
        "capture": CONDITIONAL,
    },
    {
        "method": "PATCH",
        "path": "/api/records/2/",
        "body": {"amount": "abc"},
        "capture": CONDITIONAL,
    },
    {
        "method": "GET",
        "path": "/api/records/1/",
        "headers": {"If-None-Match": RECORD_ONE_ETAG},
        "capture": CONDITIONAL,
    },
    {"method": "OPTIONS", "path": "/api/records/1/"},
    {"method": "POST", "path": "/api/records/1/", "body": {}},
    {"method": "DELETE", "path": "/api/records/1/"},
    {"method": "GET", "path": "/api/records/1/", "capture": CONDITIONAL},
    {"method": "GET", "path": "/api/records/?search=alpha"},
]


def _clean_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("SANKA_TEST_DB", None)
    env.pop("BENCH_DB_PATH", None)
    return env


def _run_probe(
    mode: str, project: Path, database: Path, *, output: Path | None = None
) -> dict[str, Any]:
    argv = [
        sys.executable,
        str(PROBE),
        "--mode",
        mode,
        "--project",
        str(project),
        "--database",
        str(database),
        "--scenarios",
        json.dumps(SCENARIOS),
    ]
    if output is not None:
        argv.extend(["--output", str(output)])
    outcome = subprocess.run(
        argv,
        cwd=project,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert outcome.returncode == 0, outcome.stderr
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def records_project(tmp_path: Path) -> Path:
    project = tmp_path / "records"
    shutil.copytree(FIXTURES / "drf_records_project", project)
    return project


def test_plan_keeps_list_create_destroy_native_and_overridden_actions_manual(
    records_project: Path,
) -> None:
    scan = run_cli(["scan", str(records_project)], records_project)
    assert scan.returncode == 0, scan.stderr
    plan = run_cli(["plan", str(records_project), "--to", "fastapi", "--json"], records_project)
    assert plan.returncode == 0, plan.stderr
    planned = json.loads(plan.stdout)
    strategies = {
        (route["method"], route["path"]): route["strategy"]
        for route in planned["routes"]
        if route["strategy"] != "dropped-format-suffix-alias"
    }
    assert strategies[("GET", "/api/records/")] == "native-fastapi-crud"
    assert strategies[("POST", "/api/records/")] == "native-fastapi-crud"
    assert strategies[("DELETE", "/api/records/{pk}/")] == "native-fastapi-crud"
    assert strategies[("GET", "/api/")] == "native-fastapi-api-root"
    # the overridden retrieve/update are carried over verbatim, so they stay native
    for method in ("GET", "PUT", "PATCH"):
        assert strategies[(method, "/api/records/{pk}/")] == "native-fastapi-crud"
    assert planned["readiness"] == pytest.approx(1.0)
    scanned = json.loads((records_project / ".sanka" / "scan.json").read_text(encoding="utf-8"))
    view = next(v for v in scanned["view_details"] if v["name"].endswith("RecordViewSet"))
    assert view["carryover"]["class_name"] == "RecordViewSetCarryover"
    assert view["carryover"]["operations"] == ["retrieve", "update", "partial_update"]
    assert [m["name"] for m in view["carryover"]["methods"]] == [
        "retrieve",
        "update",
        "_conditional_response",
        "_etag",
        "_etag_matches",
    ]
    assert sorted(alias for alias, _module, _attr in view["carryover"]["imports"]) == [
        "Any",
        "Request",
        "Response",
        "hashlib",
        "json",
        "status",
    ]
    assert scanned["status_codes"]["HTTP_304_NOT_MODIFIED"] == 304
    assert view["listing"]["pagination"]["kind"] == "cursor"
    assert view["listing"]["pagination"]["page_size"] == 2
    assert view["listing"]["pagination"]["ordering"] == ["-posted_at", "-id"]
    assert view["listing"]["search"] == {
        "param": "search",
        "fields": [
            {"name": "label", "lookup": "icontains"},
            {"name": "category", "lookup": "icontains"},
        ],
    }
    assert view["listing"]["ordering"]["rule"] == "append-pk-follow-last"
    assert view["listing"]["ordering"]["fields"] == ["amount", "posted_at", "label"]
    posted_at = next(
        f for s in scanned["serializer_details"] for f in s["fields"] if f["name"] == "posted_at"
    )
    assert posted_at["kind"] == "datetime"
    assert posted_at["timezone"] == "Asia/Tokyo"
    assert dict(posted_at["messages"])["invalid"] == (
        "Datetime has wrong format. Use one of these formats instead: "
        "YYYY-MM-DDThh:mm[:ss[.uuuuuu]][+HH:MM|-HH:MM|Z]."
    )


def test_native_list_semantics_match_drf(records_project: Path, tmp_path: Path) -> None:
    scan = run_cli(["scan", str(records_project)], records_project)
    assert scan.returncode == 0, scan.stderr
    plan = run_cli(["plan", str(records_project), "--to", "fastapi"], records_project)
    assert plan.returncode == 0, plan.stderr
    assert "Native migration readiness: 100%" in plan.stdout
    plan_hash = json.loads(
        (records_project / ".sanka" / "plan-fastapi.json").read_text(encoding="utf-8")
    )["plan_hash"]
    applied = run_cli(
        ["apply", "--root", str(records_project), "--plan-hash", plan_hash, "--force"],
        records_project,
    )
    assert applied.returncode == 0, applied.stderr
    output = records_project / ".sanka" / "output" / "fastapi"
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    resource = manifest["resources"][0]
    assert resource["listing"]["pagination"]["cursor_param"] == "cursor"
    assert resource["view_carryover"] == {
        "class": "RecordViewSetCarryover",
        "operations": ["retrieve", "update", "partial_update"],
    }
    assert manifest["allow"]["/api/records/{pk}/"] == "GET, PUT, PATCH, DELETE, HEAD, OPTIONS"
    user_views = (output / "sanka_user_views.py").read_text(encoding="utf-8")
    assert "class RecordViewSetCarryover(CarryoverView):" in user_views
    assert "def _conditional_response(self, request: Request, response: Response)" in user_views
    assert "status.HTTP_304_NOT_MODIFIED" in user_views

    source = _run_probe("source", records_project, tmp_path / "source.sqlite3")
    native = _run_probe("native", records_project, tmp_path / "native.sqlite3", output=output)
    mismatches = [
        {"scenario": scenario, "source": expected, "native": actual}
        for scenario, expected, actual in zip(
            SCENARIOS, source["results"], native["results"], strict=True
        )
        if expected != actual
    ]
    assert not mismatches, json.dumps(mismatches, indent=1, ensure_ascii=False)[:6000]
    assert source["database"] == native["database"]
    # sanity: the interesting behaviours actually happened on the DRF side
    first = source["results"][0]["body"]
    assert [row["id"] for row in first["results"]] == [5, 4]
    assert first["next"].startswith("http://testserver/api/records/?cursor=")
    assert source["results"][4]["status"] == 404
    assert [row["id"] for row in source["results"][5]["body"]["results"]] == [4, 1]
    assert source["results"][17]["status"] == 405
    assert source["results"][18]["status"] == 201
    assert source["results"][18]["body"]["posted_at"] == "2026-03-01T19:00:00+09:00"
    assert source["results"][21]["status"] == 400
    by_path: dict[tuple[str, str, str], dict[str, Any]] = {}
    for scenario, result in zip(SCENARIOS, source["results"], strict=True):
        key = (
            scenario["method"],
            scenario["path"],
            json.dumps(scenario.get("headers", {}), sort_keys=True),
        )
        by_path.setdefault(key, result)  # first occurrence: before the DELETE at the end
    detail = by_path[("GET", "/api/records/1/", "{}")]
    assert detail["status"] == 200
    assert detail["captured"]["ETag"] == RECORD_ONE_ETAG
    assert detail["captured"]["Cache-Control"] == "private, max-age=0"
    matched = by_path[("GET", "/api/records/1/", json.dumps({"If-None-Match": RECORD_ONE_ETAG}))]
    assert matched["status"] == 304
    assert matched["body"] is None
    assert matched["captured"]["ETag"] == RECORD_ONE_ETAG
