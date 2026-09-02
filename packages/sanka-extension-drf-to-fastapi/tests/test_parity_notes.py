# SPDX-License-Identifier: Apache-2.0
"""Parity notes: source-derived facts attached to every scanned route.

Each fixture below is a synthetic DRF application copied from the public migration
bench (Apache-2.0). The scans run in a clean subprocess because Django settings are
process-global.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from adapter_cli import run_cli

from sanka_extension_drf_to_fastapi.model import FrameworkScan, PlannedRoute
from sanka_extension_drf_to_fastapi.parity import FAMILIES

FIXTURES = Path(__file__).parent / "fixtures"


def _scan(tmp_path: Path, fixture: str) -> tuple[Path, dict[str, object]]:
    project = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, project)
    completed = run_cli(["scan", str(project)], project)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((project / ".sanka" / "scan.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return project, payload


def _route(scan: dict[str, object], method: str, path: str) -> dict[str, object]:
    routes = scan["routes"]
    assert isinstance(routes, list)
    for route in routes:
        if route["method"] == method and route["path"] == path:
            assert isinstance(route, dict)
            return route
    raise AssertionError(f"route not scanned: {method} {path}")


def _notes(route: dict[str, object]) -> list[dict[str, object]]:
    notes = route["parity_notes"]
    assert isinstance(notes, list)
    return notes


def _codes(route: dict[str, object]) -> set[str]:
    return {str(note["code"]) for note in _notes(route)}


def _message(route: dict[str, object], code: str) -> str:
    matches = [str(note["message"]) for note in _notes(route) if note["code"] == code]
    assert matches, (code, sorted(_codes(route)))
    return matches[0]


def _assert_every_route_carries_derivable_notes(scan: dict[str, object]) -> None:
    routes = scan["routes"]
    assert isinstance(routes, list)
    assert scan["schema_version"] == 7
    for route in routes:
        notes = _notes(route)
        assert notes, route["path"]
        assert all(note["family"] in FAMILIES for note in notes)
        assert "SANKA_DRF_PARITY_UNAVAILABLE" not in {note["code"] for note in notes}, route


def test_auth_notes_state_ordering_headers_and_exact_details(tmp_path: Path) -> None:
    _project, scan = _scan(tmp_path, "drf_documents_project")
    _assert_every_route_carries_derivable_notes(scan)
    detail = _route(scan, "GET", "/api/documents/{pk}/")
    assert {
        "SANKA_DRF_PARITY_DYNAMIC_PERMISSIONS",
        "SANKA_DRF_PARITY_AUTH_ORDER",
        "SANKA_DRF_PARITY_UNAUTHENTICATED",
        "SANKA_DRF_PARITY_FORBIDDEN",
        "SANKA_DRF_PARITY_PERMISSION_CUSTOM",
        "SANKA_DRF_PARITY_TOKEN_HEADER",
        "SANKA_DRF_PARITY_SESSION_CSRF",
    } <= _codes(detail)
    # per-action permissions were evaluated for this operation, with the source location
    dynamic = next(n for n in _notes(detail) if n["code"] == "SANKA_DRF_PARITY_DYNAMIC_PERMISSIONS")
    assert dynamic["source"] == "documents/views.py:18"
    assert "IsDocumentOwner" in str(dynamic["message"])
    # the first authenticator decides 401-vs-403, and a missing object is not consulted first
    unauthenticated = _message(detail, "SANKA_DRF_PARITY_UNAUTHENTICATED")
    assert "401 with WWW-Authenticate: Token" in unauthenticated
    assert '"Authentication credentials were not provided."' in unauthenticated
    assert "not 404" in unauthenticated
    # exact token failure strings come from the live classes, subclass branch included
    token = _message(detail, "SANKA_DRF_PARITY_TOKEN_HEADER")
    assert '"Token has expired."' in token
    assert '"Invalid token."' in token
    assert "documents_accesstoken" in token
    # object-level permissions are recognised as never blocking list/create
    custom = _message(detail, "SANKA_DRF_PARITY_PERMISSION_CUSTOM")
    assert "object-level only" in custom
    session = _message(detail, "SANKA_DRF_PARITY_SESSION_CSRF")
    assert '"CSRF token missing."' in session
    assert "%s" not in session
    # the anonymous list route keeps the ordering fact but has no unauthenticated branch
    listing = _route(scan, "GET", "/api/documents/")
    assert "SANKA_DRF_PARITY_AUTH_ORDER" in _codes(listing)
    assert "SANKA_DRF_PARITY_UNAUTHENTICATED" not in _codes(listing)
    review = _route(scan, "POST", "/api/documents/{pk}/review/")
    assert "SANKA_DRF_PARITY_CUSTOM_ACTION" in _codes(review)
    assert "IsAdminUser" in _message(review, "SANKA_DRF_PARITY_DYNAMIC_PERMISSIONS")


def test_pagination_ordering_filtering_and_conditional_notes(tmp_path: Path) -> None:
    _project, scan = _scan(tmp_path, "drf_records_project")
    _assert_every_route_carries_derivable_notes(scan)
    listing = _route(scan, "GET", "/api/records/")
    cursor = _message(listing, "SANKA_DRF_PARITY_CURSOR_PAGINATION")
    assert '{"next", "previous", "results"}' in cursor
    assert "page_size 2" in cursor
    assert "(-posted_at, -id)" in cursor
    assert '"Invalid cursor"' in cursor
    ordering = _message(listing, "SANKA_DRF_PARITY_DEFAULT_ORDERING")
    assert "(-posted_at, -id)" in ordering
    assert "the order is total" in ordering
    ordering_filter = _message(listing, "SANKA_DRF_PARITY_ORDERING_FILTER")
    assert "amount, posted_at, label" in ordering_filter
    assert "StableOrderingFilter overrides get_ordering" in ordering_filter
    search = _message(listing, "SANKA_DRF_PARITY_SEARCH_FILTER")
    assert "(label, category)" in search
    assert "`^` istartswith" in search
    # conditional responses attach to the operations that implement them, not the others
    for method, path in (
        ("GET", "/api/records/{pk}/"),
        ("PUT", "/api/records/{pk}/"),
        ("PATCH", "/api/records/{pk}/"),
    ):
        conditional = _message(_route(scan, method, path), "SANKA_DRF_PARITY_CONDITIONAL")
        assert "If-None-Match" in conditional
        assert "headers set: Cache-Control, ETag, Vary" in conditional
    for method, path in (
        ("GET", "/api/records/"),
        ("POST", "/api/records/"),
        ("DELETE", "/api/records/{pk}/"),
    ):
        assert "SANKA_DRF_PARITY_CONDITIONAL" not in _codes(_route(scan, method, path))


def test_multipart_uniqueness_and_message_notes(tmp_path: Path) -> None:
    _project, scan = _scan(tmp_path, "drf_artifacts_project")
    _assert_every_route_carries_derivable_notes(scan)
    create = _route(scan, "POST", "/api/files/")
    parsers = _message(create, "SANKA_DRF_PARITY_PARSERS")
    assert "multipart/form-data" in parsers
    assert "application/json) answers 415" in parsers
    file_field = next(n for n in _notes(create) if n["code"] == "SANKA_DRF_PARITY_FILE_FIELD")
    assert file_field["source"] == "artifacts/serializers.py:41"
    message = str(file_field["message"])
    assert '"No file was submitted."' in message
    assert '"The submitted file is empty."' in message
    assert '"Only files with .csv, .json, or .txt extensions are allowed."' in message
    assert "byte-exact" in message
    unique = _message(create, "SANKA_DRF_PARITY_UNIQUE_FIELD")
    assert '{"key": ["artifact with this key already exists."]}' in unique
    nullability = _message(create, "SANKA_DRF_PARITY_NULLABILITY")
    assert "`key`, `label`, `file`" in nullability
    messages = _message(create, "SANKA_DRF_PARITY_FIELD_MESSAGES")
    assert '"Ensure this field has no more than 80 characters."' in messages
    assert "{min_length}" not in messages  # unconfigured constraints are not rendered
    assert "JSON parse error" not in messages  # the view does not parse JSON at all
    overrides = [n for n in _notes(create) if n["code"] == "SANKA_DRF_PARITY_SERIALIZER_OVERRIDE"]
    assert {str(n["source"]) for n in overrides} == {
        "artifacts/serializers.py:41",
        "artifacts/serializers.py:51",
    }
    # write-only facts stay off read routes
    listing = _route(scan, "GET", "/api/files/")
    assert not {"multipart", "uniqueness", "nullability", "messages"} & {
        str(note["family"]) for note in _notes(listing)
    }


def test_routing_notes_and_native_routes_carry_facts_too(tmp_path: Path) -> None:
    project, scan = _scan(tmp_path, "drf_crud_project")
    _assert_every_route_carries_derivable_notes(scan)
    detail = _route(scan, "GET", "/api/gadgets/{pk}/")
    assert detail["native"] is True
    allowed = _message(detail, "SANKA_DRF_PARITY_ALLOWED_METHODS")
    assert "GET, PUT, PATCH, DELETE, HEAD, OPTIONS" in allowed
    assert 'Method \\"X\\" not allowed.' in allowed
    missing = _message(detail, "SANKA_DRF_PARITY_MISSING_OBJECT")
    assert '"Not found."' in missing
    assert "SANKA_DRF_PARITY_NO_APPEND_SLASH" in _codes(detail)
    options = _message(detail, "SANKA_DRF_PARITY_OPTIONS_METADATA")
    assert '"name": "Gadget Instance"' in options
    create = _route(scan, "POST", "/api/gadgets/")
    assert "JSON parse error" in _message(create, "SANKA_DRF_PARITY_FIELD_MESSAGES")
    # the plan carries the notes for every route and the manifest inventory repeats them
    plan = run_cli(["plan", str(project), "--to", "fastapi", "--json"], project)
    assert plan.returncode == 0, plan.stderr
    planned = json.loads(plan.stdout)
    assert planned["schema_version"] == 4
    routes = {(r["method"], r["path"]): r for r in planned["routes"]}
    assert routes[("GET", "/api/gadgets/{pk}/")]["parity_notes"]
    restored = PlannedRoute.from_dict(routes[("GET", "/api/gadgets/{pk}/")])
    assert restored.parity_notes[0].family == "routing"
    assert FrameworkScan.from_dict(scan).routes[0].parity_notes


def test_gap_report_renders_notes_for_routes_needing_adaptation(tmp_path: Path) -> None:
    project, _scan_payload = _scan(tmp_path, "drf_documents_project")
    plan = run_cli(["plan", str(project), "--to", "fastapi", "--json"], project)
    assert plan.returncode == 0, plan.stderr
    plan_hash = json.loads(plan.stdout)["plan_hash"]
    # readiness is below the default gate, so apply refuses but writes the gap report
    applied = run_cli(["apply", "--root", str(project), "--plan-hash", plan_hash], project)
    report = project / ".sanka" / "gap-report" / "GAP-REPORT.md"
    assert report.is_file(), (applied.returncode, applied.stdout, applied.stderr)
    text = report.read_text(encoding="utf-8")
    assert "parity/auth `SANKA_DRF_PARITY_TOKEN_HEADER`" in text
    assert "(documents/authentication.py:8)" in text
    inventory = json.loads(
        (project / ".sanka" / "gap-report" / "gap-report.json").read_text(encoding="utf-8")
    )
    assert all("parity_notes" in route for route in inventory["unsupported_routes"])


def test_legacy_artifacts_without_notes_still_load() -> None:
    route = PlannedRoute.from_dict(
        {
            "method": "GET",
            "path": "/api/x/",
            "operation": "list",
            "source_view": "app.views.X",
            "strategy": "needs-manual-adaptation",
            "automatic": False,
        }
    )
    assert route.parity_notes == ()
