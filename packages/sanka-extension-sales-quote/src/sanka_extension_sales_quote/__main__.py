# SPDX-License-Identifier: Apache-2.0
"""One bounded UTF-8 JSON request and response per isolated process."""

from __future__ import annotations

import sys

from sanka_extension_sales_quote.generator import generate_blueprint
from sanka_extensions.flow import (
    MAX_MESSAGE_BYTES,
    BlueprintRequest,
    BlueprintResponse,
    decode_message,
    encode_message,
)


def main() -> int:
    if len(sys.argv) != 1:
        sys.stderr.write("This executable accepts one Flow request on standard input.\n")
        return 2
    try:
        request = BlueprintRequest.from_dict(
            decode_message(sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1))
        )
    except (ValueError, RecursionError):
        sys.stderr.write("Invalid Flow protocol request.\n")
        return 2
    try:
        response = BlueprintResponse.success(request, generate_blueprint(request))
    except ValueError as error:
        response = BlueprintResponse.failure(request, code="FLOW_INPUT_INVALID", message=str(error))
    except Exception:
        response = BlueprintResponse.failure(
            request,
            code="FLOW_GENERATION_FAILED",
            message="Failed to generate the requested Blueprint.",
        )
    try:
        sys.stdout.buffer.write(encode_message(response))
    except ValueError:
        sys.stderr.write("Flow response exceeds the protocol limit.\n")
        return 2
    return 0 if response.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
