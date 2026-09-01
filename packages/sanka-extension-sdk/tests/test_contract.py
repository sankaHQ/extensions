# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import math

import pytest

from sanka_extension_sdk import (
    ExtensionRequest,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    failure_response,
    success_response,
)


def _request_payload() -> dict[str, object]:
    return {
        "schema_version": "sanka-extension/v1",
        "request_id": "req-1",
        "command": "scan",
        "project_root": "/work/source",
        "artifact_root": "/work/source/.sanka/extensions/sanka/drf-to-fastapi",
        "extension": {
            "id": "sanka/drf-to-fastapi",
            "version": "0.1.0a1",
            "manifest_digest": "a" * 64,
        },
        "fingerprint": {"schema_version": "sanka-fingerprint/v1"},
        "configuration": {},
        "prior_artifacts": [],
        "reviewed_plan_hash": None,
    }


def test_request_requires_matching_schema_command_and_absolute_roots() -> None:
    request = decode_request(_request_payload())

    assert request.command == "scan"

    with pytest.raises(ValueError, match="project_root"):
        decode_request({**encode_request(request), "project_root": "relative"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fingerprint", {"enabled": object()}),
        ("configuration", {"nested": [math.nan]}),
        ("prior_artifacts", ["/work/report.json", 1]),
    ],
)
def test_request_rejects_non_json_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        decode_request({**_request_payload(), field: value})


def test_request_rejects_boolean_manifest_digest() -> None:
    payload = _request_payload()
    payload["extension"] = {
        "id": "sanka/drf-to-fastapi",
        "version": "0.1.0a1",
        "manifest_digest": True,
    }

    with pytest.raises(ValueError, match="manifest_digest"):
        decode_request(payload)


def test_responses_echo_identity_and_require_structured_errors() -> None:
    request = decode_request(_request_payload())
    success = success_response(
        request,
        data={"count": 1},
        artifacts=["/work/source/.sanka/extensions/sanka/drf-to-fastapi/scan.json"],
        limitations=["static only"],
        next_actions=["plan"],
    )
    failure = failure_response(
        request,
        code="SANKA_EXTENSION_INPUT_REQUIRED",
        message="input required",
        details={"inputs": ["output"]},
    )

    assert decode_response(encode_response(success)) == success
    assert decode_response(encode_response(failure)) == failure

    invalid = encode_response(success)
    invalid["data"] = {"count": object()}
    with pytest.raises(ValueError, match=r"data\.count"):
        decode_response(invalid)

    invalid = encode_response(failure)
    invalid["error"] = {"code": "SANKA_EXTENSION_INPUT_REQUIRED", "message": "input required"}
    with pytest.raises(ValueError, match=r"error\.details"):
        decode_response(invalid)


def test_response_rejects_non_success_without_error() -> None:
    request = ExtensionRequest(
        request_id="req-1",
        command="scan",
        project_root="/work/source",
        artifact_root="/work/source/.sanka/extensions/sanka/drf-to-fastapi",
        extension_id="sanka/drf-to-fastapi",
        extension_version="0.1.0a1",
        manifest_digest="a" * 64,
        fingerprint={"schema_version": "sanka-fingerprint/v1"},
        configuration={},
        prior_artifacts=(),
        reviewed_plan_hash=None,
    )
    invalid = encode_response(success_response(request, data={}))
    invalid["outcome"] = "error"

    with pytest.raises(ValueError, match="error"):
        decode_response(invalid)
