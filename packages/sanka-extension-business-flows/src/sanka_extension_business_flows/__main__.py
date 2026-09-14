# SPDX-License-Identifier: Apache-2.0
"""One bounded Flow request on stdin, one correlated response on stdout."""

from __future__ import annotations

import sys

from sanka_extension_business_flows.generator import generate
from sanka_extensions.flow import (
    MAX_MESSAGE_BYTES,
    BlueprintRequest,
    BlueprintResponse,
    decode_message,
    encode_message,
)


def main() -> int:
    if len(sys.argv) != 1:
        print("This extension accepts only a Flow blueprint request on stdin", file=sys.stderr)
        return 2
    try:
        request = BlueprintRequest.from_dict(
            decode_message(sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1))
        )
    except (ValueError, TypeError, KeyError):
        # Malformed requests have no trustworthy identity for a correlated reply.
        print("Invalid Flow blueprint request", file=sys.stderr)
        return 2
    try:
        response = generate(request)
    except (ValueError, TypeError, KeyError):
        response = BlueprintResponse.failure(
            request,
            code="BUSINESS_RECIPE_UNSUPPORTED",
            message="Recipe identity, settings or target capabilities are unsupported",
        )
    sys.stdout.buffer.write(encode_message(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
