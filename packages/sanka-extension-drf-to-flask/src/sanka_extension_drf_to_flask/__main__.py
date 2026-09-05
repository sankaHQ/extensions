# SPDX-License-Identifier: Apache-2.0
"""One-document stdio entry point for the DRF-to-Flask extension."""

from __future__ import annotations

import json
import sys

from sanka_extension_drf_to_flask.adapter import handle
from sanka_extension_sdk import (
    SCHEMA_VERSION,
    ExtensionRequest,
    decode_request,
    encode_response,
    failure_response,
)


def main() -> int:
    request: ExtensionRequest | None = None
    try:
        request = decode_request(json.loads(sys.stdin.read()))
        response = handle(request)
        document = json.dumps(encode_response(response), sort_keys=True) + "\n"
    except Exception as error:
        if request is None:
            sys.stderr.write(f"invalid extension request: {error}\n")
            payload = {
                "schema_version": SCHEMA_VERSION,
                "outcome": "error",
                "error": {"code": "SANKA_EXTENSION_PROTOCOL", "message": str(error)},
            }
            sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
            return 1
        response = failure_response(
            request,
            code="SANKA_EXTENSION_EXECUTION_FAILED",
            message=str(error),
        )
        document = json.JSONEncoder(sort_keys=True).encode(encode_response(response)) + "\n"
    sys.stdout.write(document)
    return 0 if response.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
