# SPDX-License-Identifier: Apache-2.0
"""One-document stdio framing shared by the two small executable adapters."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

from sanka_extensions.code import (
    SCHEMA_VERSION,
    ExtensionRequest,
    ExtensionResponse,
    decode_request,
    encode_response,
    failure_response,
)


def stdio(handler: Callable[[ExtensionRequest], ExtensionResponse]) -> int:
    request: ExtensionRequest | None = None
    try:
        request = decode_request(json.load(sys.stdin))
        response = handler(request)
    except Exception as error:
        if request is None:
            json.dump(
                {
                    "schema_version": SCHEMA_VERSION,
                    "outcome": "error",
                    "error": {
                        "code": "SANKA_EXTENSION_PROTOCOL",
                        "message": "Invalid extension request",
                    },
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
            return 1
        response = failure_response(
            request, code="SANKA_EXTENSION_EXECUTION_FAILED", message=str(error)
        )
    json.dump(encode_response(response), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if response.outcome == "success" else 1
