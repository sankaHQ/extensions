# SPDX-License-Identifier: Apache-2.0
"""Local deterministic hashing for extension artifacts."""

from __future__ import annotations

import hashlib
import json


def content_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
