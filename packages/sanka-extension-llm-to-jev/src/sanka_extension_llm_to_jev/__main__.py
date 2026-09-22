# SPDX-License-Identifier: Apache-2.0
"""Strict one-document sanka-extension/v1 subprocess transport."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sanka_extensions.code import decode_request, encode_response, failure_response

from .adapter import handle


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if isinstance(payload, dict):
            for key in ("project_root", "artifact_root"):
                raw = payload.get(key)
                if (
                    isinstance(raw, str)
                    and Path(raw).is_absolute()
                    and Path(raw).resolve() != Path(raw)
                ):
                    raise ValueError("root path contains symlink or traversal")
        request = decode_request(payload)
    except (ValueError, TypeError):
        # No request identity exists with which to construct a valid SDK response.
        sys.stderr.write("invalid sanka-extension/v1 request\n")
        return 2
    try:
        response = handle(request)
    except (ValueError, OSError, ImportError) as error:
        response = failure_response(request, code="JEV_MIGRATION_REJECTED", message=str(error))
    sys.stdout.write(
        json.dumps(encode_response(response), sort_keys=True, ensure_ascii=False) + "\n"
    )
    return 0 if response.outcome == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
